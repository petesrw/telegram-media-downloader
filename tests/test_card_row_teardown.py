"""Rows removed from a download card must stay owned by Qt until deleted.

v2.9.7-v2.9.10 crashed with an access violation in pyside6.abi3.dll while a
card rebuilt its file rows. Rows were removed with ``setParent(None)`` before
``deleteLater()``, turning each into an orphan top-level widget. A stress run
(rebuild rows, fail files, collect garbage while another thread allocates)
reproduced the exact crash stack in 2 of 4 runs with that teardown and in
0 of 6 with removed rows kept under their parent until Qt deletes them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TestCardRowTeardown(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _card(self, count=5):
        from ui.components.download_card import DownloadCard
        meta = [{"id": i, "name": f"Video_{i}.mp4", "size": 100} for i in range(count)]
        return DownloadCard("1_6", "K.30", count, "x", 6, None, files_metadata=meta), meta

    def _assert_qt_owned(self, rows):
        from shiboken6 import Shiboken
        for row in rows:
            if Shiboken.isValid(row):
                self.assertIsNotNone(row.parent(), "a removed row must keep its parent until deleted")
                self.assertFalse(Shiboken.ownedByPython(row))
                self.assertTrue(row.isHidden())

    def test_rebuilding_the_rows_keeps_old_rows_qt_owned(self):
        card, meta = self._card()
        old_rows = list(card.file_rows.values())

        card.refresh_from_metadata("K.30", len(meta), 0, [dict(m) for m in meta])

        self.assertEqual(len(card.file_rows), len(meta))
        self._assert_qt_owned(old_rows)

    def test_removing_one_file_keeps_its_row_qt_owned(self):
        card, _meta = self._card()
        row = card.file_rows[2]

        card.remove_file(2)

        self.assertNotIn(2, card.file_rows)
        self._assert_qt_owned([row])

    def test_replacing_subscription_rows_keeps_old_rows_qt_owned(self):
        from ui.views.downloads_view import DownloadsView
        view = DownloadsView()
        view.set_subscriptions([{"sub_id": "a", "title": "K.30", "media_id": 2}])
        old = list(view.subscription_rows.values())

        view.set_subscriptions([{"sub_id": "b", "title": "News", "media_id": 6}])

        self._assert_qt_owned(old)


if __name__ == "__main__":
    unittest.main()
