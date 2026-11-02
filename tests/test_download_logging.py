"""
Tests for the download diagnostics added on top of the FastTelethon engine:

* failures must carry a human readable reason instead of vanishing,
* a download that stops making progress must be reported and eventually
  aborted so it no longer occupies a concurrency slot,
* files the user removes must never be queued again,
* log records must reach both the file and the UI bus.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import core_downloader as cd
import database
import utils.app_logger as al
from utils.app_logger import (
    LogBus,
    describe_download_error,
    get_recent_logs,
    log_event,
)


class MockMessage:
    def __init__(self, msg_id, size=100):
        self.id = msg_id
        self.size = size
        self.media = None
        self.photo = None
        self.video = None
        self.document = None
        self.message = ""
        self.date = None


class FakeSecondsError(Exception):
    """Stand-in for a Telethon error carrying a ``seconds`` retry hint."""

    seconds = 0


class TestDescribeDownloadError(unittest.TestCase):

    def test_floodwait_mentions_the_wait(self):
        class FloodWaitError(Exception):
            seconds = 420

        reason = describe_download_error(FloodWaitError("420"))
        self.assertIn("rate limit", reason)
        self.assertIn("420", reason)

    def test_timeout_is_reported_as_a_network_problem(self):
        self.assertIn("Network timeout", describe_download_error(asyncio.TimeoutError()))

    def test_disk_full_is_reported(self):
        err = OSError(28, "No space left on device")
        err.errno = 28
        self.assertIn("Disk full", describe_download_error(err))

    def test_permission_error_is_reported(self):
        self.assertIn("permission", describe_download_error(PermissionError()).lower())

    def test_expired_file_reference_is_recognised(self):
        class FileReferenceExpiredError(Exception):
            pass

        self.assertIn("reference expired", describe_download_error(FileReferenceExpiredError("x")))

    def test_unknown_error_falls_back_to_class_and_message(self):
        reason = describe_download_error(ValueError("weird payload"))
        self.assertIn("ValueError", reason)
        self.assertIn("weird payload", reason)

    def test_cancellation_is_labelled(self):
        self.assertEqual(describe_download_error(asyncio.CancelledError()), "Cancelled")


class TestLogBusAndFile(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self._orig_root = al.get_project_root
        self._orig_enabled = al._log_to_file_enabled
        al.get_project_root = lambda: self.temp_dir
        al._log_to_file_enabled = lambda: True
        al.reload_logger()

    def tearDown(self):
        al.get_project_root = self._orig_root
        al._log_to_file_enabled = self._orig_enabled
        al.reload_logger()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_log_event_writes_to_file_and_ring(self):
        log_event("ERROR", "unit-test-marker")
        with open(al.get_log_path(), "r", encoding="utf-8") as handle:
            contents = handle.read()
        self.assertIn("unit-test-marker", contents)
        self.assertIn("[ERROR]", contents)
        self.assertTrue(
            any("unit-test-marker" in record[2] for record in get_recent_logs())
        )

    def test_log_event_publishes_on_the_bus(self):
        if not al._QT_AVAILABLE:
            self.skipTest("PySide6 is not installed")

        received = []

        def slot(level, timestamp, message):
            received.append((level, message))

        bus = LogBus.instance()
        bus.record.connect(slot)
        try:
            log_event("WARNING", "bus-marker")
        finally:
            bus.record.disconnect(slot)

        self.assertIn(("WARNING", "bus-marker"), received)

    def test_logger_never_raises_when_the_file_is_unwritable(self):
        al.get_project_root = lambda: os.path.join(self.temp_dir, "does-not-exist")
        al.reload_logger()
        # Must not raise even though the directory is missing
        log_event("ERROR", "cannot be written")


class TestLogsViewRendering(unittest.TestCase):
    """The Logs tab must colour-code levels and honour its level filter.

    These are real widgets, so the class runs offscreen. Two bugs this guards
    against: a plain-text widget silently drops colours, and QTextEdit has no
    ``appendHtml``.
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        if not al._QT_AVAILABLE:
            raise unittest.SkipTest("PySide6 is not installed")

        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        cls._temp_dir = tempfile.mkdtemp()
        cls._orig_root = al.get_project_root
        cls._orig_enabled = al._log_to_file_enabled
        al.get_project_root = lambda: cls._temp_dir
        al._log_to_file_enabled = lambda: True
        al.reload_logger()

        from ui.views.logs_view import LogsView

        cls.view = LogsView()

    @classmethod
    def tearDownClass(cls):
        al.get_project_root = cls._orig_root
        al._log_to_file_enabled = cls._orig_enabled
        al.reload_logger()
        shutil.rmtree(cls._temp_dir, ignore_errors=True)

    def test_levels_are_colour_coded(self):
        log_event("ERROR", "view-red-marker")
        log_event("WARNING", "view-amber-marker")
        log_event("INFO", "view-blue-marker")
        self.view._flush_pending()

        html = self.view.view.document().toHtml().upper()
        self.assertIn("#EF4444", html)
        self.assertIn("#F59E0B", html)
        self.assertIn("#2BA5E4", html)

    def test_level_filter_hides_lower_levels(self):
        log_event("ERROR", "filter-red-marker")
        log_event("INFO", "filter-blue-marker")
        self.view._flush_pending()

        self.view.combo_filter.setCurrentIndex(1)  # errors only
        text = self.view.rendered_text()
        self.assertIn("filter-red-marker", text)
        self.assertNotIn("filter-blue-marker", text)

        self.view.combo_filter.setCurrentIndex(0)  # all levels
        self.assertIn("filter-blue-marker", self.view.rendered_text())


class TestStallWatchdog(unittest.TestCase):

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_watchdog_reports_a_stall_and_aborts_the_attempt(self):
        async def scenario():
            stop = asyncio.Event()
            last_activity = [time.time()]
            phase = ["transferred 1.0 MB of 10.0 MB"]
            warned = [False]
            abort_reason = [None]
            events = []

            async def hanging_download():
                await asyncio.sleep(3600)

            download_task = asyncio.create_task(hanging_download())
            await asyncio.sleep(0.02)

            watchdog = asyncio.create_task(
                cd._stall_watchdog(
                    77, last_activity, phase, warned, abort_reason, stop,
                    download_task,
                    warn_seconds=0.01, abort_seconds=0.05,
                    stall_cb=lambda msg_id, idle, ph: events.append((msg_id, idle, ph)),
                    poll_seconds=0.01,
                )
            )

            try:
                await download_task
            except asyncio.CancelledError:
                pass
            await watchdog
            return events, abort_reason[0], warned[0], download_task.cancelled()

        events, reason, warned, was_cancelled = self._run(scenario())

        self.assertTrue(warned, "the stalled file should have been flagged")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 77)
        self.assertIn("1.0 MB of 10.0 MB", events[0][2])
        self.assertTrue(was_cancelled, "the stalled attempt must release its slot")
        self.assertIn("Stalled", reason or "")
        self.assertTrue(any("no progress" in r[2] for r in get_recent_logs()))

    def test_watchdog_stops_quietly_when_the_download_finishes(self):
        async def scenario():
            stop = asyncio.Event()
            events = []
            watchdog = asyncio.create_task(
                cd._stall_watchdog(
                    1, [time.time()], ["idle"], [False], [None], stop, None,
                    warn_seconds=0.01, abort_seconds=5,
                    stall_cb=lambda *a: events.append(a),
                    poll_seconds=0.01,
                )
            )
            await asyncio.sleep(0.02)
            stop.set()
            await watchdog
            return events

        # It is allowed to warn, but it must terminate once stop is set
        self._run(scenario())


class TestFailureReasons(unittest.TestCase):

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_single_file_reports_the_reason_after_retries(self):
        """A failing message must surface *why* instead of silently hanging."""
        message = MockMessage(5, size=100)

        class FailingMessage(MockMessage):
            async def download_media(self, file=None, progress_callback=None):
                raise FakeSecondsError("telegram said no")

        message = FailingMessage(5, size=100)

        from telethon.utils import get_input_location  # noqa: F401  (import parity)

        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"id": msg_id, "paused": paused, "error": error, "reason": reason})

        orig_filename = cd.get_media_filename
        orig_peer = sys.modules["telethon.utils"].get_peer_id
        cd.get_media_filename = lambda *a, **k: "file.mp4"
        sys.modules["telethon.utils"].get_peer_id = lambda channel: -100123
        try:
            self._run(cd.download_single_file(
                client=None,
                channel=object(),
                message=message,
                folder_name=self.temp_dir,
                complete_cb=complete_cb,
            ))
        finally:
            cd.get_media_filename = orig_filename
            sys.modules["telethon.utils"].get_peer_id = orig_peer

        self.assertTrue(results, "a failure must be reported, not swallowed")
        self.assertTrue(results[-1]["error"])
        self.assertFalse(results[-1]["paused"])
        self.assertIn("telegram said no", results[-1]["reason"])
        self.assertIn("Failed after 3 attempts", results[-1]["reason"])

    def test_batches_forward_the_skip_set_and_the_reason(self):
        attempted = []
        reported = []

        async def fake_single(client, channel, message, folder, progress_cb=None,
                              complete_cb=None, cancel_event=None, max_speed_kb=None,
                              reserved_paths=None, stall_cb=None):
            attempted.append(message.id)
            if complete_cb:
                complete_cb(message.id, paused=False, filepath=None, error=True,
                            reason="boom")

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            reported.append((msg_id, error, reason))

        original = cd.download_single_file
        cd.download_single_file = fake_single
        try:
            messages = [MockMessage(1), MockMessage(2), MockMessage(3)]
            self._run(cd.download_in_batches_headless(
                client=None,
                channel=None,
                messages=messages,
                folder_name=self.temp_dir,
                batch_size=2,
                downloaded_state=set(),
                progress_cb=None,
                complete_cb=complete_cb,
                skipped_ids={2},
            ))
        finally:
            cd.download_single_file = original

        self.assertEqual(sorted(attempted), [1, 3], "removed file must not be downloaded")
        self.assertEqual(len(reported), 2)
        self.assertTrue(all(item[1] for item in reported))
        self.assertTrue(all(item[2] == "boom" for item in reported))

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestSkippedFilesPersistence(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir, "test_downloader.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.orig_db_path
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_skipped_ids_survive_a_save_and_load(self):
        database.save_task_db({
            "channel_input": "-100123",
            "media_id": 6,
            "title": "Channel: test",
            "total_items": 3,
        })

        self.assertEqual(database.add_task_skipped_id("-100123_6", 42), 1)
        # Adding the same id twice must be idempotent
        database.add_task_skipped_id("-100123_6", 42)
        self.assertEqual(database.load_task_skipped_ids("-100123_6"), {42})

        tasks = database.load_active_tasks_db()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["skipped_ids"], [42])

        # Re-saving the loaded task must not lose the removed items
        database.save_task_db(tasks[0])
        self.assertEqual(database.load_active_tasks_db()[0]["skipped_ids"], [42])

    def test_skip_reports_when_no_task_row_matches(self):
        # The caller logs a warning in this case instead of silently losing it
        self.assertEqual(database.add_task_skipped_id("999_6", 5), 0)
        self.assertEqual(database.load_task_skipped_ids("999_6"), set())


if __name__ == "__main__":
    unittest.main()
