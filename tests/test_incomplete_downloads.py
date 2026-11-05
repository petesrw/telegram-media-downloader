"""A download that did not finish must never sit under its real file name.

On 2026-11-05 the download folder held 896 zero-byte videos and 7 cut-off
ones; VLC refused them ("Your input can't be opened"). Telethon's single-stream
fallback opens the final path with open(file, 'wb') before receiving any data
and leaves it there when the download fails. And whatever file came back was
marked completed without comparing its size to what Telegram reported.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import core_downloader as cd
import database

SIZE = 100


class WritingMessage:
    """A message whose single-stream download writes `write` bytes, then
    raises `error` (if any), like Telethon writing into the target path."""

    def __init__(self, write=SIZE, error=None):
        self.id = 7
        self.size = SIZE
        self.media = "media"
        self.photo = None
        self.video = None
        self.document = None
        self.message = ""
        self.date = None
        self.write = write
        self.error = error
        self.targets = []

    async def download_media(self, file=None, progress_callback=None):
        self.targets.append(file)
        with open(file, "wb") as f:
            f.write(b"x" * self.write)
        if self.error:
            raise self.error
        return file


class TestIncompleteDownloads(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir, "test.db")
        database.init_db()
        self.orig_filename = cd.get_media_filename
        self.orig_peer = sys.modules["telethon.utils"].get_peer_id
        self.orig_sleep = cd.asyncio.sleep
        cd.get_media_filename = lambda *a, **k: "video.mp4"
        sys.modules["telethon.utils"].get_peer_id = lambda channel: -100123

        async def no_sleep(_seconds):
            return None

        cd.asyncio.sleep = no_sleep
        self.final = os.path.join(self.temp_dir, "video.mp4")

    def tearDown(self):
        cd.get_media_filename = self.orig_filename
        sys.modules["telethon.utils"].get_peer_id = self.orig_peer
        cd.asyncio.sleep = self.orig_sleep
        database.DB_PATH = self.orig_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _download(self, message):
        results = []

        def complete_cb(msg_id, paused=False, filepath=None, error=False, reason=None):
            results.append({"error": error, "filepath": filepath, "reason": reason})

        asyncio.run(cd.download_single_file(
            client=None, channel=object(), message=message,
            folder_name=self.temp_dir, complete_cb=complete_cb,
        ))
        return results

    def _leftovers(self):
        return sorted(os.listdir(self.temp_dir))

    def test_a_failed_download_leaves_no_file_under_the_real_name(self):
        message = WritingMessage(write=0, error=ConnectionError("dropped"))

        results = self._download(message)

        self.assertTrue(results[-1]["error"])
        self.assertFalse(os.path.exists(self.final), "a 0-byte video must not be left behind")
        self.assertEqual(self._leftovers(), ["test.db"])
        # The fallback never wrote into the final path directly
        self.assertTrue(all(t != self.final for t in message.targets))

    def test_a_short_download_is_a_failure_not_a_completed_file(self):
        message = WritingMessage(write=40)

        results = self._download(message)

        self.assertTrue(results[-1]["error"])
        self.assertIn("40", results[-1]["reason"])
        self.assertIn(str(SIZE), results[-1]["reason"])
        self.assertFalse(any(not r["error"] for r in results), "must not be reported completed")
        self.assertFalse(os.path.exists(self.final))
        self.assertEqual(self._leftovers(), ["test.db"])

    def test_a_complete_download_lands_under_the_real_name(self):
        results = self._download(WritingMessage(write=SIZE))

        self.assertEqual(results, [{"error": False, "filepath": self.final, "reason": None}])
        self.assertEqual(os.path.getsize(self.final), SIZE)
        self.assertEqual(self._leftovers(), ["test.db", "video.mp4"])


if __name__ == "__main__":
    unittest.main()
