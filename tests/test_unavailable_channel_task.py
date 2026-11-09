"""A saved task whose channel the account can no longer open.

The account left K.27.1, but its saved Videos task stayed "running", so every
launch resumed it, failed, and popped a Fetch Error - with no card in the
Queue to remove it from. Such a task must be parked as paused, so it shows as
a card the user can Remove and is not retried on every launch.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core_downloader as cd
import database
from workers import telegram_worker as tw


class TestUnavailableChannelTask(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir, "test.db")
        database.init_db()
        self.orig_fetch = tw.fetch_channel
        self.orig_log = tw.log_event
        tw.log_event = lambda level, message: None
        cd.save_active_tasks([{
            "channel_input": "-1003889572674", "media_id": 2, "topic_id": None,
            "paused": False, "download_path": self.temp_dir, "title": "K.27.1",
            "selected_message_ids": [1, 2], "total_items": 2,
        }])

    def tearDown(self):
        tw.fetch_channel = self.orig_fetch
        tw.log_event = self.orig_log
        database.DB_PATH = self.orig_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, error):
        async def fetch(client, channel_input):
            raise error

        tw.fetch_channel = fetch
        worker = tw.TelegramWorker("session", 1, "hash")
        unavailable, errors = [], []
        worker.signals.task_unavailable.connect(lambda task_id, msg: unavailable.append(task_id))
        worker.signals.error_occurred.connect(lambda chan, msg: errors.append(msg))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._download_coro(
                "-1003889572674", 2, self.temp_dir, 5, 0, False, [1, 2], "3889572674_2"))
        finally:
            loop.close()
        paused = [t.get("paused") for t in cd.load_active_tasks()]
        return unavailable, errors, paused

    def test_no_access_parks_the_task_as_paused(self):
        unavailable, errors, paused = self._run(cd.ChannelUnavailableError("No access"))

        self.assertEqual(unavailable, ["3889572674_2"])
        self.assertEqual(paused, [True], "must not auto-resume on the next launch")
        self.assertEqual(len(errors), 1, "the user still sees why once")

    def test_a_network_failure_keeps_the_task_running(self):
        unavailable, errors, paused = self._run(ConnectionError("disconnected"))

        self.assertEqual(unavailable, [])
        self.assertEqual(paused, [False])
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
