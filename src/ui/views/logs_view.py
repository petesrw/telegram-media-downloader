"""
Diagnostics view.

Shows everything the download engine reports about failures and stalls, with a
filter and the option to open the on-disk ``downloader.log`` for support.

Records arrive from the worker thread through ``LogBus.record``, which Qt turns
into a queued call, and are flushed to the widget on a short timer so a burst of
messages (for example a big retry storm) cannot freeze the UI.
"""

import os
from collections import deque

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QComboBox, QCheckBox, QFrame
)

from utils.app_logger import (
    LogBus, clear_log_file, get_log_path, get_recent_logs, tail_log_file
)

LEVEL_COLORS = {
    "DEBUG": "#94A3B8",
    "INFO": "#2BA5E4",
    "WARNING": "#F59E0B",
    "ERROR": "#EF4444",
}

FLUSH_INTERVAL_MS = 150
MAX_VISIBLE_LINES = 2000


class LogsView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._lines = deque(maxlen=MAX_VISIBLE_LINES)
        self._pending = deque()
        self._min_level = None
        self.setup_ui()

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()

        LogBus.instance().record.connect(self.append_log)
        self._preload()

    # ──────────────────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────────────────
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 20, 40, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        lbl_title = QLabel("🗒️ Download Log")
        lbl_title.setObjectName("MainHeader")
        header.addWidget(lbl_title)
        header.addStretch()

        self.combo_filter = QComboBox()
        self.combo_filter.addItems(["All levels", "Errors only", "Warnings + errors"])
        self.combo_filter.currentIndexChanged.connect(self._on_filter_changed)

        self.chk_autoscroll = QCheckBox("Auto-scroll")
        self.chk_autoscroll.setChecked(True)

        self.btn_open = QPushButton("📂 Open Log File")
        self.btn_open.setObjectName("SecondaryButton")
        self.btn_open.clicked.connect(self.open_log_file)

        self.btn_clear = QPushButton("🗑 Clear")
        self.btn_clear.setObjectName("SecondaryButton")
        self.btn_clear.clicked.connect(self.clear_logs)

        header.addWidget(self.combo_filter)
        header.addWidget(self.chk_autoscroll)
        header.addWidget(self.btn_open)
        header.addWidget(self.btn_clear)
        layout.addLayout(header)

        self.lbl_path = QLabel(f"Log file: {get_log_path()}")
        self.lbl_path.setObjectName("MutedText")
        self.lbl_path.setWordWrap(True)
        layout.addWidget(self.lbl_path)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        layout.addWidget(divider)

        # QTextEdit (not QPlainTextEdit) because only the rich-text widget
        # actually renders the per-level colours.
        self.view = QTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setAcceptRichText(True)
        self.view.document().setMaximumBlockCount(MAX_VISIBLE_LINES)
        self.view.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.view, stretch=1)

        self.lbl_empty = QLabel(
            "No log entries yet. Failures, stalls and retries will appear here."
        )
        self.lbl_empty.setObjectName("MutedText")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_empty)

    # ──────────────────────────────────────────────────────────────────
    # Log intake
    # ──────────────────────────────────────────────────────────────────
    def append_log(self, level, timestamp, message):
        """Slot for LogBus.record. Safe to call from the worker thread."""
        self._pending.append((level, timestamp, message))
        if not self._lines:
            self.lbl_empty.setVisible(False)

    def _preload(self):
        """Show what is already in the ring buffer and the log file."""
        for timestamp, level, message in get_recent_logs(MAX_VISIBLE_LINES):
            self._lines.append((timestamp, level, message))

        # If the app just started, the file holds history from previous runs
        if not self._lines:
            for raw in tail_log_file(500):
                self._lines.append((raw, "", ""))

        if self._lines:
            self.lbl_empty.setVisible(False)
            self._render_lines()
            self._scroll_to_bottom()

    def _on_filter_changed(self, index):
        self._min_level = {0: None, 1: "ERROR", 2: "WARNING"}.get(index)
        self._render_lines()
        self._scroll_to_bottom()

    def _flush_pending(self):
        if not self._pending:
            return

        appended = []
        while self._pending:
            appended.append(self._pending.popleft())

        for entry in appended:
            self._lines.append(entry)

        # Only the new entries need rendering when no filter is active
        if self._min_level is None:
            for level, timestamp, message in appended:
                self._append_html(level, timestamp, message)
        else:
            self._render_lines()

        self._scroll_to_bottom()

    # ──────────────────────────────────────────────────────────────────
    # Rendering
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _format(level, timestamp, message):
        if not level and not timestamp:
            return message  # raw line preloaded from the log file
        return f"{timestamp} [{level}] {message}"

    def _append_html(self, level, timestamp, message):
        color = LEVEL_COLORS.get((level or "INFO").upper(), "#94A3B8")
        html = (
            f'<span style="color:{color};">'
            f"{self._escape(self._format(level, timestamp, message))}"
            f"</span>"
        )
        # QTextEdit has no appendHtml(); insert at the end of the document
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(html)
        cursor.insertBlock()
        self.view.setTextCursor(cursor)

    @staticmethod
    def _escape(text):
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _render_lines(self):
        self.view.clear()
        for level, timestamp, message in self._lines:
            if not self._passes_filter(level):
                continue
            self._append_html(level, timestamp, message)

    def _passes_filter(self, level):
        if self._min_level is None:
            return True
        order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
        # Raw file lines have no level - keep them unless only errors are wanted
        if not level:
            return True
        return order.get(level.upper(), 0) >= order.get(self._min_level, 0)

    def _scroll_to_bottom(self):
        if self.chk_autoscroll.isChecked():
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    # ──────────────────────────────────────────────────────────────────
    # Actions
    # ──────────────────────────────────────────────────────────────────
    def clear_logs(self):
        clear_log_file()
        self._lines.clear()
        self._pending.clear()
        self.view.clear()
        self.lbl_empty.setVisible(True)

    # ──────────────────────────────────────────────────────────────────
    # Test/debug helper
    # ──────────────────────────────────────────────────────────────────
    def rendered_text(self):
        return self.view.toPlainText()

    def open_log_file(self):
        path = get_log_path()
        if os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))
        else:
            # Nothing logged yet - open the folder so the user sees where it goes
            folder = os.path.dirname(os.path.abspath(path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
