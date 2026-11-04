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
from utils.fast_telethon import DownloadFloodGate
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

    def test_each_record_appears_once(self):
        # The main window also connected LogBus to the view, so every line
        # showed twice. The view subscribes itself; nobody else may.
        log_event("ERROR", "once-marker")
        self.view._flush_pending()

        self.assertEqual(self.view.rendered_text().count("once-marker"), 1)

    def test_records_logged_before_the_view_opened_keep_their_level(self):
        from ui.views.logs_view import LogsView

        log_event("ERROR", "preload-marker")
        view = LogsView()

        self.assertIn("[ERROR] preload-marker", view.rendered_text())
        view.combo_filter.setCurrentIndex(1)  # errors only
        self.assertIn("preload-marker", view.rendered_text())

    def test_log_file_history_is_shown_verbatim(self):
        from ui.views.logs_view import LogsView

        line = "2026-11-03 12:00:00,000 [WARNING] file-history-marker"
        with open(al.get_log_path(), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        saved = list(al._RING)
        al._RING.clear()
        try:
            view = LogsView()
        finally:
            al._RING.extend(saved)

        self.assertIn(line, view.rendered_text().splitlines())


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


class TestFloodGate(unittest.TestCase):

    def test_remaining_tracks_an_active_flood_wait(self):
        gate = DownloadFloodGate(min_request_interval=0, cooldown_period=0)
        self.assertEqual(gate.remaining(), 0.0)

        gate.trip(120)

        self.assertGreater(gate.remaining(), 100)
        gate.reset()
        self.assertEqual(gate.remaining(), 0.0)


class TestRateLimitIsNotAStall(unittest.TestCase):
    """A FloodWait is a deliberate pause and must never abort the download."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_watchdog_waits_out_a_flood_wait_instead_of_aborting(self):
        class OpenGate:
            def remaining(self):
                return 600.0

        original = cd.GLOBAL_FLOODGATE
        cd.GLOBAL_FLOODGATE = OpenGate()
        try:
            async def scenario():
                stop = asyncio.Event()
                phase = ["waiting for the first response from Telegram"]
                events = []
                download = asyncio.create_task(asyncio.sleep(3600))
                await asyncio.sleep(0.02)

                watchdog = asyncio.create_task(
                    cd._stall_watchdog(
                        9, [time.time()], phase, [False], [None], stop, download,
                        warn_seconds=0.01, abort_seconds=0.02,
                        stall_cb=lambda mid, idle, ph: events.append((mid, idle, ph)),
                        poll_seconds=0.01,
                    )
                )
                await asyncio.sleep(0.12)
                aborted = download.cancelled()

                stop.set()
                await watchdog
                download.cancel()
                await asyncio.gather(download, return_exceptions=True)
                return events, aborted, phase[0]

            events, aborted, phase = self._run(scenario())
        finally:
            cd.GLOBAL_FLOODGATE = original

        self.assertFalse(aborted, "a FloodWait pause must not be treated as a stall")
        self.assertTrue(events, "the wait should be surfaced to the UI")
        self.assertEqual(events[0][0], 9)
        self.assertEqual(events[0][1], 0)
        self.assertIn("rate limit", events[0][2].lower())
        self.assertIn("rate limit", phase.lower())


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

    def _download_returning_nothing(self, media, size=100):
        class NoDataMessage(MockMessage):
            async def download_media(self, file=None, progress_callback=None):
                return None

        message = NoDataMessage(9, size=size)
        message.media = media
        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"error": error, "reason": reason})

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
        return results

    def test_no_file_reason_names_the_media_type(self):
        # Some files fail on every run because Telethon cannot download their
        # kind of media; the reason has to say which kind so the user can
        # decide whether to remove them.
        class MessageMediaWebPage:
            pass

        results = self._download_returning_nothing(MessageMediaWebPage())

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["error"])
        self.assertIn("Download produced no file", results[0]["reason"])
        self.assertIn("media: MessageMediaWebPage", results[0]["reason"])

    def test_no_file_reason_includes_the_document_type_and_size(self):
        class Document:
            mime_type = "video/mp4"
            size = 3 * 1024 * 1024

        class MessageMediaDocument:
            document = Document()

        results = self._download_returning_nothing(MessageMediaDocument(), size=0)

        self.assertIn("media: MessageMediaDocument, video/mp4, 3.0 MB", results[0]["reason"])

    def test_no_file_reason_says_when_there_is_no_media(self):
        results = self._download_returning_nothing(None)

        self.assertIn("media: none", results[0]["reason"])

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


class TestMediaGoneFromTelegram(unittest.TestCase):
    """A deleted message (or one whose media was removed) can never download.

    Refreshing the expired file reference is how this shows up: Telegram
    returns None (deleted) or a message without media. It must fail at once
    with that reason, not retry and end in "'NoneType' object has no attribute
    'media'".
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_filename = cd.get_media_filename
        self.orig_peer = sys.modules["telethon.utils"].get_peer_id
        cd.get_media_filename = lambda *a, **k: "file.mp4"
        sys.modules["telethon.utils"].get_peer_id = lambda channel: -100123

    def tearDown(self):
        cd.get_media_filename = self.orig_filename
        sys.modules["telethon.utils"].get_peer_id = self.orig_peer
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _download(self, refetched, size=100):
        from telethon.errors import FileReferenceExpiredError

        calls = []

        class ExpiredMessage(MockMessage):
            async def download_media(self, file=None, progress_callback=None):
                calls.append(self.id)
                raise FileReferenceExpiredError(request=None)

        class Client:
            async def get_messages(self, channel, ids=None):
                return refetched

        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"error": error, "reason": reason})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cd.download_single_file(
                client=Client(),
                channel=object(),
                message=ExpiredMessage(7, size=size),
                folder_name=self.temp_dir,
                complete_cb=complete_cb,
            ))
        finally:
            loop.close()
        return calls, results

    def test_deleted_message_fails_once_with_a_clear_reason(self):
        calls, results = self._download(refetched=None)

        self.assertEqual(calls, [7], "a deleted message must not be retried")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["error"])
        self.assertTrue(al.is_deleted_on_telegram(results[0]["reason"]))
        self.assertIn("message was deleted", results[0]["reason"])

    def test_parallel_engine_expiry_does_not_fall_back_for_a_deleted_message(self):
        # Large files hit the expired reference in the parallel engine first;
        # Telethon's fallback must not be tried on a message that is gone.
        from telethon.errors import FileReferenceExpiredError

        async def expired_fast_download(**kwargs):
            raise FileReferenceExpiredError(request=None)

        orig_fast = cd.fast_download_file
        cd.fast_download_file = expired_fast_download
        try:
            calls, results = self._download(refetched=None, size=5 * 1024 * 1024)
        finally:
            cd.fast_download_file = orig_fast

        self.assertEqual(calls, [], "Telethon fallback must not run for a deleted message")
        self.assertEqual(len(results), 1)
        self.assertTrue(al.is_deleted_on_telegram(results[0]["reason"]))

    def test_message_without_media_fails_once_with_a_clear_reason(self):
        calls, results = self._download(refetched=MockMessage(7))

        self.assertEqual(calls, [7])
        self.assertEqual(len(results), 1)
        self.assertTrue(al.is_deleted_on_telegram(results[0]["reason"]))
        self.assertIn("media was removed", results[0]["reason"])

    def test_ordinary_failures_are_not_labelled_deleted(self):
        self.assertFalse(al.is_deleted_on_telegram("Network timeout - Telegram did not answer in time"))
        self.assertFalse(al.is_deleted_on_telegram(None))


class TestParallelFailureKeepsPartialDownload(unittest.TestCase):
    """When the parallel engine gives up after saving chunks, the next attempt
    must resume from .part. Telethon's single-stream fallback starts from byte
    0 and writes straight to the final path, throwing those chunks away.
    """

    SIZE = 5 * 1024 * 1024

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_filename = cd.get_media_filename
        self.orig_peer = sys.modules["telethon.utils"].get_peer_id
        self.orig_fast = cd.fast_download_file
        self.orig_sleep = cd.asyncio.sleep
        cd.get_media_filename = lambda *a, **k: "file.mp4"
        sys.modules["telethon.utils"].get_peer_id = lambda channel: -100123

        async def no_sleep(_seconds):
            return None

        cd.asyncio.sleep = no_sleep

    def tearDown(self):
        cd.get_media_filename = self.orig_filename
        sys.modules["telethon.utils"].get_peer_id = self.orig_peer
        cd.fast_download_file = self.orig_fast
        cd.asyncio.sleep = self.orig_sleep
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _download(self, saved_chunks):
        from utils.fast_telethon import CHUNK_SIZE, _save_meta
        import math

        fast_calls = []
        fallback_calls = []

        async def timing_out_fast_download(**kwargs):
            fast_calls.append(kwargs["target_path"])
            if saved_chunks:
                total = math.ceil(self.SIZE / CHUNK_SIZE)
                _save_meta(kwargs["target_path"] + ".part.meta", self.SIZE, CHUNK_SIZE, total, saved_chunks)
            raise asyncio.TimeoutError()

        cd.fast_download_file = timing_out_fast_download

        class Message(MockMessage):
            async def download_media(self, file=None, progress_callback=None):
                fallback_calls.append(file)
                return None

        class Client:
            async def get_messages(self, channel, ids=None):
                return None

        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"error": error, "reason": reason})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cd.download_single_file(
                client=Client(),
                channel=object(),
                message=Message(7, size=self.SIZE),
                folder_name=self.temp_dir,
                complete_cb=complete_cb,
            ))
        finally:
            loop.close()
        return fast_calls, fallback_calls, results

    def _download_with_expiring_reference(self, expiries):
        # The task fetched its messages hours ago, so a file that only now
        # reaches the front of the queue starts with an expired reference.
        from telethon.errors import FileReferenceExpiredError

        fast_calls = []
        fallback_calls = []

        async def expiring_fast_download(**kwargs):
            fast_calls.append(kwargs["location"])
            if len(fast_calls) <= expiries:
                raise FileReferenceExpiredError(request=None)
            with open(kwargs["target_path"], "wb") as f:
                f.write(b"x")
            return True

        cd.fast_download_file = expiring_fast_download

        class Message(MockMessage):
            async def download_media(self, file=None, progress_callback=None):
                fallback_calls.append(file)
                return None

        stale = Message(7, size=self.SIZE)
        stale.media = "stale-media"
        fresh = Message(7, size=self.SIZE)
        fresh.media = "fresh-media"

        class Client:
            async def get_messages(self, channel, ids=None):
                return fresh

        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"error": error, "filepath": filepath})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cd.download_single_file(
                client=Client(),
                channel=object(),
                message=stale,
                folder_name=self.temp_dir,
                complete_cb=complete_cb,
            ))
        finally:
            loop.close()
        return fast_calls, fallback_calls, results

    def test_expired_reference_is_refreshed_and_retried_in_parallel(self):
        # The single-stream fallback is several times slower and cannot resume
        fast_calls, fallback_calls, results = self._download_with_expiring_reference(expiries=1)

        self.assertEqual(fast_calls, ["stale-media", "fresh-media"])
        self.assertEqual(fallback_calls, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["error"])

    def test_reference_that_expires_again_still_falls_back(self):
        fast_calls, fallback_calls, results = self._download_with_expiring_reference(expiries=99)

        self.assertEqual(fast_calls[:2], ["stale-media", "fresh-media"])
        self.assertTrue(fallback_calls, "a second expiry must not loop forever")

    def test_timeout_with_saved_chunks_retries_the_parallel_engine(self):
        fast_calls, fallback_calls, results = self._download(saved_chunks={0, 1, 2})

        self.assertEqual(fallback_calls, [], "fallback would restart the file from byte 0")
        self.assertEqual(len(fast_calls), 3, "each retry must resume through the parallel engine")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["error"])
        self.assertIn("Network timeout", results[0]["reason"])
        self.assertTrue(os.path.exists(os.path.join(self.temp_dir, "file.mp4.part.meta")))

    def test_timeout_before_any_chunk_still_uses_the_fallback(self):
        fast_calls, fallback_calls, results = self._download(saved_chunks=set())

        self.assertEqual(len(fast_calls), 1)
        self.assertEqual(len(fallback_calls), 1)


class TestDroppedConnection(unittest.TestCase):
    """At 15:55 on 2026-11-03 Telethon gave up on its connection: every queued
    file logged "cancelled/paused by user" (nobody paused) and every later one
    failed with "Cannot send requests while disconnected" until a restart.
    """

    SIZE = 5 * 1024 * 1024

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_filename = cd.get_media_filename
        self.orig_peer = sys.modules["telethon.utils"].get_peer_id
        self.orig_fast = cd.fast_download_file
        self.orig_sleep = cd.asyncio.sleep
        cd.get_media_filename = lambda *a, **k: "file.mp4"
        sys.modules["telethon.utils"].get_peer_id = lambda channel: -100123
        real_sleep = self.orig_sleep

        async def no_sleep(_seconds):
            await real_sleep(0)

        cd.asyncio.sleep = no_sleep

    def tearDown(self):
        cd.get_media_filename = self.orig_filename
        sys.modules["telethon.utils"].get_peer_id = self.orig_peer
        cd.fast_download_file = self.orig_fast
        cd.asyncio.sleep = self.orig_sleep
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _download(self, first_failure, client):
        calls = []

        async def fast_download(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise first_failure
            with open(kwargs["target_path"], "wb") as handle:
                handle.write(b"x" * self.SIZE)
            return True

        cd.fast_download_file = fast_download
        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"paused": paused, "filepath": filepath, "error": error})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(cd.download_single_file(
                client=client,
                channel=object(),
                message=MockMessage(7, size=self.SIZE),
                folder_name=self.temp_dir,
                complete_cb=complete_cb,
            ))
        finally:
            loop.close()
        return calls, results

    class Client:
        def __init__(self, connected=True):
            self.connected = connected
            self.connect_calls = 0

        def is_connected(self):
            return self.connected

        async def connect(self):
            self.connect_calls += 1
            self.connected = True

        async def get_messages(self, channel, ids=None):
            return None

    def test_request_cancelled_by_telethon_is_retried_not_reported_as_pause(self):
        calls, results = self._download(asyncio.CancelledError(), self.Client())

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["paused"], "nobody paused this download")
        self.assertTrue(results[0]["filepath"])

    def test_dropped_client_is_reconnected_before_retrying(self):
        client = self.Client(connected=False)
        calls, results = self._download(ConnectionError("Cannot send requests while disconnected"), client)

        self.assertEqual(client.connect_calls, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(results[0]["filepath"])


class TestDeletedOnTelegramLabel(unittest.TestCase):
    """Files Telegram no longer has get their own label, not a red failure."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        if not al._QT_AVAILABLE:
            raise unittest.SkipTest("PySide6 is not installed")

        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def _card(self):
        from ui.components.download_card import DownloadCard

        files = [
            {"id": 1, "name": "a.mp4", "size": 10},
            {"id": 2, "name": "b.mp4", "size": 10},
        ]
        return DownloadCard("t1", "K.27.1", 2, "folder", "all", None, files_metadata=files)

    def _deleted_reason(self):
        return f"{al.DELETED_ON_TELEGRAM} - the message was deleted from the chat"

    def test_batch_progress_percent_uses_western_digits(self):
        # Windows set to Thai digits made the bar read "๖๙%" while the status
        # bar showed "69.1%". The system locale reproduces it on such machines.
        from PySide6.QtCore import QLocale

        previous = QLocale()
        QLocale.setDefault(QLocale.system())
        try:
            card = self._card()
            card.batch_progress_bar.setRange(0, 100)
            card.batch_progress_bar.setValue(69)
            self.assertEqual(card.batch_progress_bar.text(), "69%")
        finally:
            QLocale.setDefault(previous)

    def test_deleted_file_row_is_labelled_not_failed(self):
        card = self._card()
        card.mark_file_failed(1, self._deleted_reason())

        row = card.file_rows[1]
        self.assertEqual(row.icon.text(), "🗑️")
        self.assertEqual(row.bar.property("state"), "deleted")
        self.assertTrue(row.lbl_reason.text().startswith(al.DELETED_ON_TELEGRAM))
        self.assertEqual(card.failed_count, 0)
        self.assertEqual(card.deleted_count, 1)
        self.assertEqual(card.lbl_status_text.text(), f"{al.DELETED_ON_TELEGRAM}: 1")
        self.assertEqual(card.lbl_status_text.property("state"), "deleted")

    def test_remove_failed_button_is_hidden_until_a_file_fails(self):
        card = self._card()

        self.assertTrue(card.btn_remove_failed.isHidden())

    def test_remove_failed_button_asks_to_drop_failed_and_deleted_files(self):
        card = self._card()
        card.mark_file_failed(1, "Network timeout - Telegram did not answer in time")
        card.mark_file_failed(2, self._deleted_reason())
        # A large queue only shows its first rows; hidden ones count too
        card.mark_file_failed(500, "Download produced no file")
        requested = []
        card.removeFailedRequested.connect(lambda task_id, ids: requested.append((task_id, ids)))

        self.assertFalse(card.btn_remove_failed.isHidden())
        self.assertIn("3", card.btn_remove_failed.text())
        card.btn_remove_failed.click()

        self.assertEqual(requested, [("t1", [1, 2, 500])])

    def test_removing_files_updates_the_failed_count(self):
        card = self._card()
        card.mark_file_failed(1, "Network timeout - Telegram did not answer in time")
        card.mark_file_failed(2, "Network timeout - Telegram did not answer in time")

        card.remove_file(1)
        self.assertEqual(card.failed_count, 1)
        self.assertIn("1", card.btn_remove_failed.text())

        card.remove_file(2)
        self.assertEqual(card.failed_count, 0)
        self.assertTrue(card.btn_remove_failed.isHidden())

    def test_a_failed_file_that_later_completes_is_not_removed(self):
        card = self._card()
        card.mark_file_failed(1, "Network timeout - Telegram did not answer in time")

        card.mark_file_completed(1)

        self.assertTrue(card.btn_remove_failed.isHidden())

    def test_real_failures_keep_the_failed_badge(self):
        card = self._card()
        card.mark_file_failed(1, "Network timeout - Telegram did not answer in time")
        card.mark_file_failed(2, self._deleted_reason())

        self.assertEqual(card.failed_count, 1)
        self.assertEqual(card.deleted_count, 1)
        self.assertEqual(card.lbl_status_text.property("state"), "failed")
        self.assertEqual(card.file_rows[1].icon.text(), "❌")
        self.assertEqual(card.file_rows[2].icon.text(), "🗑️")


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

    def test_whole_category_task_reloads_without_a_selection(self):
        # A whole-category task has no selection (None). It used to come back
        # as [], so the worker fetched zero messages after a restart and
        # declared the unfinished task complete.
        database.save_task_db({
            "channel_input": "-100123",
            "media_id": 2,
            "title": "Channel: test",
            "selected_message_ids": None,
        })

        tasks = database.load_active_tasks_db()
        self.assertIsNone(tasks[0]["selected_message_ids"])
        self.assertIsNone(database.get_task_db("-100123", 2)["selected_message_ids"])

        # Re-saving the loaded task must keep it a whole-category task
        database.save_task_db(tasks[0])
        self.assertIsNone(database.load_active_tasks_db()[0]["selected_message_ids"])

    def test_rows_saved_with_an_empty_selection_reload_as_whole_category(self):
        # Rows written by earlier versions store "[]" for whole-category tasks
        database.save_task_db({"channel_input": "-100123", "media_id": 2, "title": "t"})

        self.assertIsNone(database.load_active_tasks_db()[0]["selected_message_ids"])

    def test_selected_files_survive_a_save_and_load(self):
        database.save_task_db({
            "channel_input": "-100123",
            "media_id": 6,
            "title": "t",
            "selected_message_ids": [7, 8],
        })

        self.assertEqual(database.load_active_tasks_db()[0]["selected_message_ids"], [7, 8])

    def test_skip_reports_when_no_task_row_matches(self):
        # The caller logs a warning in this case instead of silently losing it
        self.assertEqual(database.add_task_skipped_id("999_6", 5), 0)
        self.assertEqual(database.load_task_skipped_ids("999_6"), set())


if __name__ == "__main__":
    unittest.main()
