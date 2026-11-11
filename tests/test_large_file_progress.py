"""Progress for files over 2 GB must reach the UI unchanged.

file_progress declared its byte counts as C++ ``int`` (32-bit). From the
first byte past 2 GiB every emit wrapped to a negative number and left an
OverflowError + SystemError behind: crash_report.log collected over 13,000 of
them while big K.30 videos downloaded.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIVE_GB = 5 * 1024 ** 3


class TestLargeFileProgress(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_byte_counts_over_2gb_are_delivered_intact(self):
        from workers.telegram_worker import WorkerSignals
        signals = WorkerSignals()
        received = []
        signals.file_progress.connect(lambda *args: received.append(args))
        hooked = []
        old_hook = sys.excepthook
        sys.excepthook = lambda exc_type, *_: hooked.append(exc_type.__name__)
        try:
            signals.file_progress.emit("1_6", 63867, FIVE_GB - 1, FIVE_GB, "5 MB/s")
        finally:
            sys.excepthook = old_hook

        self.assertEqual(received, [("1_6", 63867, FIVE_GB - 1, FIVE_GB, "5 MB/s")])
        self.assertEqual(hooked, [])

    def test_card_shows_half_done_for_half_of_a_5gb_file(self):
        from ui.components.download_card import DownloadCard
        from workers.telegram_worker import WorkerSignals
        meta = [{"id": 1, "name": "Big.mp4", "size": FIVE_GB}]
        card = DownloadCard("1_6", "K.30", 1, "x", 6, None, files_metadata=meta)
        signals = WorkerSignals()
        signals.file_progress.connect(
            lambda _task, msg_id, cur, total, speed: card.update_file_progress(msg_id, cur, total, speed))

        old_hook = sys.excepthook
        sys.excepthook = lambda *_: None
        try:
            signals.file_progress.emit("1_6", 1, FIVE_GB // 2, FIVE_GB, "5 MB/s")
        finally:
            sys.excepthook = old_hook

        self.assertEqual(card.file_rows[1].bar.value(), 50)


if __name__ == "__main__":
    unittest.main()
