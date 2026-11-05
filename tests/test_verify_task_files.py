"""🛡️ Verify must check every file of a task, not just the rows on screen.

A card draws at most 100 file rows, and Verify only walked those rows, so in a
1,310-file task it never saw the five cut-off videos marked completed.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import database

try:
    from PySide6.QtWidgets import QApplication
    _QT = True
except ImportError:  # pragma: no cover
    _QT = False

SIZE = 50
COUNT = 150


@unittest.skipUnless(_QT, "PySide6 is not installed")
class TestVerifyChecksEveryFile(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.unmarked, self.marked = [], []
        self._orig = (database.unmark_media_completed, database.mark_media_completed,
                      database.get_media_downloaded_path)
        database.unmark_media_completed = lambda c, m: self.unmarked.append(m)
        database.mark_media_completed = lambda c, m: self.marked.append(m)
        database.get_media_downloaded_path = lambda c, m: None

        files = []
        for i in range(1, COUNT + 1):
            name = f"v{i}.mp4"
            completed = i != 146
            files.append({"id": i, "name": name, "size": SIZE, "completed": completed})
            if i == 140:
                data = b"x" * 10        # cut off, but marked completed
            elif i in (145, 146):
                continue                # 145: completed but deleted; 146: not downloaded yet
            else:
                data = b"x" * SIZE
            with open(os.path.join(self.temp_dir, name), "wb") as f:
                f.write(data)

        from ui.components.download_card import DownloadCard
        self.card = DownloadCard("123_6", "K.27.1", COUNT, self.temp_dir, "all", None,
                                 files_metadata=files)

    def tearDown(self):
        (database.unmark_media_completed, database.mark_media_completed,
         database.get_media_downloaded_path) = self._orig
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_files_beyond_the_visible_rows_are_checked(self):
        self.assertLessEqual(len(self.card.file_rows), 100)

        self.card.run_health_check()

        self.assertIn(140, self.unmarked, "a cut-off file must be queued again")
        self.assertIn(145, self.unmarked, "a deleted file must be queued again")
        self.assertIn("2", self.card.lbl_verify_status.text())
        self.assertEqual(self.card.completed, COUNT - 3)

    def test_a_file_not_downloaded_yet_is_not_reported_missing(self):
        self.card.run_health_check()

        self.assertNotIn(146, self.unmarked)
        self.assertNotIn(146, self.marked)


if __name__ == "__main__":
    unittest.main()
