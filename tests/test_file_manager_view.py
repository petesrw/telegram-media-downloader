"""The Files tab must stay responsive with thousands of downloaded files.

On 2026-11-05 a history of 1,848 files took ~1.3s to build on every open and
every search keystroke, and "Select All" froze the app for ~18s: each row had
its own checkbox and button widgets, and the selection was recounted after
every single checkbox.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QPushButton
    _QT = True
except ImportError:  # pragma: no cover - PySide6 is a runtime dependency
    _QT = False


def _items(count):
    return [
        {
            "channel_id": "123",
            "msg_id": i,
            "title": f"file_{i}.mp4",
            "media_type": "videos",
            "size": 1024 * (i + 1),
            "date": "2026-11-05 10:00:00",
            "downloaded_path": f"G:/x/file_{i}.mp4",
            "channel_title": "K.27.1",
        }
        for i in range(count)
    ]


@unittest.skipUnless(_QT, "PySide6 is not installed")
class TestFileManagerViewScales(unittest.TestCase):

    COUNT = 2000

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        import ui.views.file_manager_view as fm

        self.fm = fm
        self.items = _items(self.COUNT)
        self._orig = (fm.get_all_completed_media, fm.get_cached_channels_summary,
                      fm.resolve_item_disk_path)
        fm.get_all_completed_media = lambda: list(self.items)
        fm.get_cached_channels_summary = lambda: [
            {"channel_id": "123", "channel_title": "K.27.1", "count": self.COUNT}
        ]
        # Even rows are on disk, odd rows are missing
        fm.resolve_item_disk_path = lambda item: (
            item["downloaded_path"], item["msg_id"] % 2 == 0
        )
        self.view = fm.FileManagerView()
        self.view.resize(1100, 700)
        self.view.show()
        self._app.processEvents()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        (self.fm.get_all_completed_media, self.fm.get_cached_channels_summary,
         self.fm.resolve_item_disk_path) = self._orig

    def _action_texts(self, row):
        widget = self.view.table.cellWidget(row, 7)
        if widget is None:
            return []
        return [b.text() for b in widget.findChildren(QPushButton)]

    def test_all_rows_are_listed(self):
        self.assertEqual(self.view.table.rowCount(), self.COUNT)
        self.assertIn(f"{self.COUNT} items", self.view.lbl_stats.text())

    def test_only_rows_on_screen_get_button_widgets(self):
        with_widgets = sum(
            1 for row in range(self.view.table.rowCount())
            if self.view.table.cellWidget(row, 7) is not None
        )
        self.assertGreater(with_widgets, 0)
        self.assertLess(with_widgets, 100)

    def test_rows_scrolled_into_view_get_their_buttons(self):
        last = self.COUNT - 1  # odd -> missing on disk
        self.assertEqual(self._action_texts(last), [])

        self.view.table.scrollToBottom()
        self._app.processEvents()

        self.assertEqual(self._action_texts(last), ["🔄 Re-fetch", "🗑"])
        self.assertEqual(self._action_texts(last - 1), ["📂 Open", "📁 Folder", "🗑"])

    def test_select_all_is_fast_and_selects_every_row(self):
        start = time.perf_counter()
        self.view.chk_select_all.setChecked(True)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 1.0)
        selected = self.view.get_selected_rows_data()
        self.assertEqual(len(selected), self.COUNT)
        self.assertEqual(self.view.lbl_selected_count.text(), f"{self.COUNT} selected")
        self.assertTrue(self.view.btn_bulk_remove_db.isEnabled())

        self.view.chk_select_all.setChecked(False)
        self.assertEqual(self.view.get_selected_rows_data(), [])
        self.assertFalse(self.view.btn_bulk_remove_db.isEnabled())

    def test_ticking_one_row_selects_that_file(self):
        self.view.table.item(5, 0).setCheckState(Qt.Checked)

        selected = self.view.get_selected_rows_data()
        self.assertEqual([item["msg_id"] for item, _, _ in selected], [5])
        self.assertEqual(self.view.lbl_selected_count.text(), "1 selected")

    def test_rebuilding_the_list_is_fast(self):
        start = time.perf_counter()
        self.view.refresh_list()
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_search_waits_for_typing_to_pause(self):
        self.view.input_search.setText("file_1999")
        # Not filtered on the keystroke itself...
        self.assertEqual(self.view.table.rowCount(), self.COUNT)

        self.view._search_timer.timeout.emit()
        self.assertEqual(self.view.table.rowCount(), 1)


if __name__ == "__main__":
    unittest.main()
