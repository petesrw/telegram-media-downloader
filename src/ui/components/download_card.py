from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QProgressBar, QSizePolicy, QFrame, QScrollArea
)
from PySide6.QtCore import Qt, Signal, QSize, QLocale
import os

from utils.app_logger import DELETED_ON_TELEGRAM, is_deleted_on_telegram

# Styles are now moved to dark_style.qss and style.qss


class DownloadCard(QWidget):
    def __init__(self, task_id, title, total_items, folder_name, media_type, parent_worker,
                 completed=0, is_paused=False, download_path="downloads", download_limit=5,
                 max_speed_kb=0, files_metadata=None):
        super().__init__()
        self.task_id       = task_id
        self.title         = title
        self.total_items   = total_items
        self.folder_name   = folder_name
        self.parent_worker = parent_worker
        self.completed     = completed
        self.download_path = download_path
        self.download_limit= download_limit
        self.max_speed_kb  = max_speed_kb
        self.files_metadata= files_metadata or []
        self.file_rows     = {}
        self.is_expanded   = False
        self.is_paused     = is_paused
        self.last_speed_val= 0 # KB/s
        
        self.setup_ui(media_type)
        self.populate_files()
        self.connect_buttons()

    def connect_buttons(self):
        self.btn_trash.clicked.connect(lambda checked=False: self.removeRequested.emit(self.task_id))
        self.btn_up.clicked.connect(lambda checked=False: self.moveUpRequested.emit(self.task_id))
        self.btn_down.clicked.connect(lambda checked=False: self.moveDownRequested.emit(self.task_id))

    removeRequested = Signal(str)
    moveUpRequested = Signal(str)
    moveDownRequested = Signal(str)
    reselectRequested = Signal(str)
    removeFileRequested = Signal(str, int)  # task_id, msg_id
    removeFailedRequested = Signal(str, list)  # task_id, msg_ids

    @property
    def failed_count(self):
        return len(self.failed_ids)

    @property
    def deleted_count(self):
        return len(self.deleted_ids)

    # ──────────────────────────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────────────────────────
    def setup_ui(self, media_type):
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 8)
        outer.setSpacing(0)

        # ── main card frame ─────────────────────────────────────────────────
        self.card_main = QFrame()
        self.card_main.setObjectName("DownloadCardFrame")

        ml = QHBoxLayout(self.card_main)
        ml.setContentsMargins(18, 16, 18, 16)
        ml.setSpacing(16)

        # badge
        badge_text = {1:"IMG",2:"VID",3:"PDF",4:"ZIP",5:"AUD",6:"ALL"}.get(media_type,"ALL")
        badge = QLabel(badge_text)
        badge.setObjectName("MediaTypeBadge")
        badge.setAlignment(Qt.AlignCenter)
        ml.addWidget(badge, alignment=Qt.AlignTop)

        # right column
        rc = QVBoxLayout()
        rc.setSpacing(8)

        # row 1 – title + status badge
        tr = QHBoxLayout()
        self.lbl_title = QLabel(self.title)
        self.lbl_title.setObjectName("CardTitle")
        self.lbl_title.setWordWrap(True)

        self.lbl_status_text = QLabel("Downloading…")
        self.lbl_status_text.setObjectName("StatusBadge")
        self.lbl_status_text.setProperty("state", "active")

        tr.addWidget(self.lbl_title, stretch=1)
        tr.addWidget(self.lbl_status_text)
        rc.addLayout(tr)

        # row 2 – media pill
        pr = QHBoxLayout()
        pill_label = {1:"◈ Images",2:"◈ Videos",3:"◈ PDFs",
                      4:"◈ ZIPs",5:"◈ Audio",6:"◈ All Media"}.get(media_type,"◈ All Media")
        lbl_pill = QLabel(pill_label)
        lbl_pill.setObjectName("TypePill")
        pr.addWidget(lbl_pill)
        pr.addStretch()
        rc.addLayout(pr)

        # row 3 – batch progress bar + count label
        self.batch_progress_bar = QProgressBar()
        self.batch_progress_bar.setFixedHeight(20)
        self.batch_progress_bar.setAlignment(Qt.AlignCenter)
        self.batch_progress_bar.setFormat("%p%")
        # Match the status bar's Western digits even when Windows uses native ones
        self.batch_progress_bar.setLocale(QLocale.c())
        self.batch_progress_bar.setMaximum(max(self.total_items, 1))
        self.batch_progress_bar.setValue(self.completed)
        self.batch_progress_bar.setProperty("state", "active")
        rc.addWidget(self.batch_progress_bar)

        self.lbl_status = QLabel(f"Downloaded {self.completed} out of {self.total_items} items")
        self.lbl_status.setObjectName("MutedText")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        rc.addWidget(self.lbl_status)

        # row 3.5 - verify status (hidden by default)
        self.lbl_verify_status = QLabel("")
        self.lbl_verify_status.setObjectName("MutedText")
        self.lbl_verify_status.setAlignment(Qt.AlignCenter)
        self.lbl_verify_status.setStyleSheet("color: #FBBF24; font-weight: bold;")
        self.lbl_verify_status.setVisible(False)
        rc.addWidget(self.lbl_verify_status)

        # row 3.6 - last failure / stall reason (hidden until there is one)
        self.lbl_reason = QLabel("")
        self.lbl_reason.setObjectName("CardReason")
        self.lbl_reason.setWordWrap(True)
        self.lbl_reason.setVisible(False)
        rc.addWidget(self.lbl_reason)

        # Sets, not counters: a file may fail more than once, and the
        # bulk remove has to know which files these are
        self.failed_ids = set()
        self.deleted_ids = set()

        # row 4 – action buttons
        ar = QHBoxLayout()
        ar.setSpacing(6)

        self.btn_pause = QPushButton("▶ Resume" if self.is_paused else "⏸ Pause")
        self.btn_pause.setObjectName("CardButton")
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_folder = QPushButton("📂 Folder")
        self.btn_folder.setObjectName("CardButton")
        self.btn_folder.clicked.connect(self.open_folder)

        self.btn_verify = QPushButton("🛡️ Verify")
        self.btn_verify.setObjectName("CardButton")
        self.btn_verify.clicked.connect(self.run_health_check)

        self.btn_trash = QPushButton("🗑 Remove")
        self.btn_trash.setObjectName("CardButton")

        self.btn_remove_failed = QPushButton()
        self.btn_remove_failed.setObjectName("CardButton")
        self.btn_remove_failed.setToolTip("Drop every failed or deleted file from this task")
        self.btn_remove_failed.clicked.connect(
            lambda checked=False: self.removeFailedRequested.emit(
                self.task_id, sorted(self.failed_ids | self.deleted_ids)
            )
        )
        self.btn_remove_failed.setVisible(False)

        self.btn_reselect = QPushButton("🔄 Re-select")
        self.btn_reselect.setObjectName("CardButton")
        self.btn_reselect.setToolTip("Add or remove files for this specific task")
        self.btn_reselect.clicked.connect(lambda checked=False: self.reselectRequested.emit(self.task_id))

        ar.addWidget(self.btn_pause)
        ar.addWidget(self.btn_folder)
        ar.addWidget(self.btn_verify)
        ar.addWidget(self.btn_reselect)
        ar.addWidget(self.btn_trash)
        ar.addWidget(self.btn_remove_failed)
        ar.addStretch()

        self.btn_up = QPushButton("⬆")
        self.btn_up.setFixedWidth(30)
        self.btn_up.setObjectName("CardButtonCompact")
        self.btn_up.setToolTip("Move queue item UP")

        self.btn_down = QPushButton("⬇")
        self.btn_down.setFixedWidth(30)
        self.btn_down.setObjectName("CardButtonCompact")
        self.btn_down.setToolTip("Move queue item DOWN")

        self.btn_expand = QPushButton("▼ Files")
        self.btn_expand.setObjectName("ExpandButton")
        self.btn_expand.setCursor(Qt.PointingHandCursor)
        self.btn_expand.clicked.connect(self.toggle_expand)

        ar.addWidget(self.btn_up)
        ar.addWidget(self.btn_down)
        ar.addWidget(self.btn_expand)

        rc.addLayout(ar)
        ml.addLayout(rc, stretch=1)
        outer.addWidget(self.card_main)

        # ── expand area ─────────────────────────────────────────────────────
        self.expand_area = QFrame()
        self.expand_area.setVisible(False)
        self.expand_area.setObjectName("expandArea")

        ea_layout = QVBoxLayout(self.expand_area)
        ea_layout.setContentsMargins(12, 4, 12, 4)
        ea_layout.setSpacing(8)

        hdr = QLabel("FILES IN THIS QUEUE")
        hdr.setObjectName("queueHeader")
        ea_layout.addWidget(hdr)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(260)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setObjectName("queueScroll")

        self.files_container = QWidget()
        self.files_container.setObjectName("filesContainer")

        self.files_layout = QVBoxLayout(self.files_container)
        self.files_layout.setContentsMargins(0, 0, 0, 0)
        self.files_layout.setSpacing(6)
        self.files_layout.addStretch()

        self.scroll.setWidget(self.files_container)
        ea_layout.addWidget(self.scroll)

        # Styles are now controlled by object names in QSS

        outer.addWidget(self.expand_area)

        # apply initial state visually
        if self.is_paused:
            self.btn_pause.setText("▶ Resume")
            self.lbl_status_text.setText("Paused")
            self.lbl_status_text.setProperty("state", "paused")
            self.batch_progress_bar.setProperty("state", "paused")
        else:
            self.btn_pause.setText("⏸ Pause")
            self.lbl_status_text.setText("Downloading…")
            self.lbl_status_text.setProperty("state", "active")
            self.batch_progress_bar.setProperty("state", "active")

        # Initial style application
        self.lbl_status_text.style().unpolish(self.lbl_status_text)
        self.lbl_status_text.style().polish(self.lbl_status_text)
        self.batch_progress_bar.style().unpolish(self.batch_progress_bar)
        self.batch_progress_bar.style().polish(self.batch_progress_bar)

    # ──────────────────────────────────────────────────────────────────────────
    # Slots
    # ──────────────────────────────────────────────────────────────────────────
    def toggle_expand(self):
        self.is_expanded = not self.is_expanded
        self.expand_area.setVisible(self.is_expanded)
        self.btn_expand.setText("▲ Files" if self.is_expanded else "▼ Files")

    def mouseDoubleClickEvent(self, event):
        self.open_folder()
        super().mouseDoubleClickEvent(event)

    def refresh_from_metadata(self, title, total_items, completed, files_metadata, is_paused=None):
        if total_items is None:
            total_items = 0
            
        self.title         = title
        self.total_items   = total_items
        self.completed     = completed
        self.files_metadata= files_metadata or []
        self.lbl_title.setText(title)
        self.batch_progress_bar.setMaximum(max(total_items, 1))
        self.batch_progress_bar.setValue(completed)
        self.lbl_status.setText(f"Downloaded {completed} out of {total_items} items")
        
        # Sync pause/completed status if we now have pending items
        if total_items > completed:
            state_val = "active"
            if is_paused is not None:
                self.is_paused = is_paused
                self.btn_pause.setText("▶ Resume" if is_paused else "⏸ Pause")
                self.lbl_status_text.setText("Paused" if is_paused else "Downloading…")
                state_val = "paused" if is_paused else "active"
            
            self.btn_pause.setEnabled(True)
            self.lbl_status_text.setProperty("state", state_val)
            self.batch_progress_bar.setProperty("state", state_val)
        elif total_items > 0 and completed >= total_items:
            self._set_completed_style()
            self.btn_pause.setEnabled(False)
        
        # Force style refresh for properties
        self.lbl_status_text.style().unpolish(self.lbl_status_text)
        self.lbl_status_text.style().polish(self.lbl_status_text)
        self.batch_progress_bar.style().unpolish(self.batch_progress_bar)
        self.batch_progress_bar.style().polish(self.batch_progress_bar)

        for i in reversed(range(self.files_layout.count())):
            w = self.files_layout.itemAt(i).widget()
            if w:
                w.setParent(None); w.deleteLater()
        self.file_rows.clear()
        self.populate_files()

    def update_progress(self, current, total):
        if total != self.total_items:
            self.total_items = total
            self.batch_progress_bar.setMaximum(max(total, 1))
        self.batch_progress_bar.setValue(current)
        self.completed = current
        self.lbl_status.setText(f"Downloaded {current} out of {total} items")
        
        # Only mark as completed if total > 0 (it may be 0 while still loading metadata)
        if total > 0 and current >= total:
            self._set_completed_style()
            self.btn_pause.setEnabled(False)

    def _set_completed_style(self):
        self.lbl_status_text.setText("Completed ✓")
        self.lbl_status_text.setProperty("state", "completed")
        self.batch_progress_bar.setProperty("state", "completed")
        self.lbl_status_text.style().unpolish(self.lbl_status_text)
        self.lbl_status_text.style().polish(self.lbl_status_text)
        self.batch_progress_bar.style().unpolish(self.batch_progress_bar)
        self.batch_progress_bar.style().polish(self.batch_progress_bar)

    def populate_files(self):
        def fmt(size):
            if size == 0: return "—"
            for unit in ["B","KB","MB","GB"]:
                if size < 1024: return f"{size:.1f} {unit}"
                size /= 1024
            return f"{size:.1f} GB"

        # Items the user removed are not part of the queue any more
        visible_meta = [m for m in self.files_metadata if not m.get("removed")]

        max_visible = 100
        for idx, meta in enumerate(visible_meta[:max_visible]):
            row = FileRow(meta["id"], meta["name"], fmt(meta["size"]), idx)
            if meta.get("completed"):
                row.set_completed()
            row.removeRequested.connect(self._on_row_remove_requested)
            self.files_layout.addWidget(row)
            self.file_rows[meta["id"]] = row

        if len(visible_meta) > max_visible:
            from PySide6.QtWidgets import QLabel
            more_count = len(visible_meta) - max_visible
            lbl_more = QLabel(f"◈ ... and {more_count} more files in queue ...")
            lbl_more.setObjectName("MutedText")
            lbl_more.setAlignment(Qt.AlignCenter)
            lbl_more.setStyleSheet("padding: 8px; font-style: italic; color: #888;")
            self.files_layout.addWidget(lbl_more)

        self._restore_saved_failures()

    def _restore_saved_failures(self):
        """Show failures recorded before a restart, so they can be removed."""
        for meta in self.files_metadata:
            reason = meta.get("failed_reason")
            if reason and not meta.get("completed") and not meta.get("removed"):
                self.mark_file_failed(meta["id"], reason)

    # ── per-file failure / stall reporting ──────────────────────────────
    def _on_row_remove_requested(self, msg_id):
        self.removeFileRequested.emit(self.task_id, msg_id)

    def _show_reason(self, text, color):
        self.lbl_reason.setText(text)
        self.lbl_reason.setStyleSheet(f"color: {color};")
        self.lbl_reason.setVisible(bool(text))

    def _refresh_status_badge(self, state, text):
        self.lbl_status_text.setText(text)
        self.lbl_status_text.setProperty("state", state)
        self.lbl_status_text.style().unpolish(self.lbl_status_text)
        self.lbl_status_text.style().polish(self.lbl_status_text)

    def mark_file_failed(self, msg_id, reason):
        """Show that one file could not be downloaded, and why."""
        if is_deleted_on_telegram(reason):
            self.mark_file_deleted(msg_id, reason)
            return
        self.failed_ids.add(msg_id)
        self._refresh_remove_failed()
        self._show_reason(f"❌ File {msg_id} failed: {reason}", "#EF4444")
        self._refresh_status_badge("failed", f"Failed: {self.failed_count}")
        row = self.file_rows.get(msg_id)
        if row:
            row.set_failed(reason)

    def mark_file_deleted(self, msg_id, reason):
        """Label a file Telegram no longer has; a retry cannot bring it back."""
        self.failed_ids.discard(msg_id)
        self.deleted_ids.add(msg_id)
        self._refresh_remove_failed()
        self._show_reason(f"🗑️ File {msg_id}: {reason}", "#9CA3AF")
        if self.failed_count == 0:
            # Real failures need attention more, so they keep the badge
            self._refresh_status_badge("deleted", f"{DELETED_ON_TELEGRAM}: {self.deleted_count}")
        row = self.file_rows.get(msg_id)
        if row:
            row.set_deleted(reason)

    def mark_file_stalled(self, msg_id, reason):
        """Show that a file stopped making progress, with the last known state."""
        if str(reason).lower().startswith("telegram rate limit"):
            # FloodWait is a temporary, expected pause - the file is not broken.
            self._show_reason(f"⏳ {reason}", "#F59E0B")
            self._refresh_status_badge("stalled", "Rate Limited")
        else:
            self._show_reason(f"⚠️ File {msg_id} is stalled: {reason}", "#F59E0B")
            self._refresh_status_badge("stalled", "Stalled")
        row = self.file_rows.get(msg_id)
        if row:
            row.set_stalled(reason)

    def _refresh_remove_failed(self):
        count = len(self.failed_ids | self.deleted_ids)
        self.btn_remove_failed.setText(f"🧹 Remove failed ({count})")
        self.btn_remove_failed.setVisible(count > 0)

    def clear_reason(self):
        self._show_reason("", "")

    def remove_file(self, msg_id):
        """Drop one file from this card and adjust the counters accordingly."""
        row = self.file_rows.pop(msg_id, None)
        if row:
            row.setParent(None)
            row.deleteLater()

        self.files_metadata = [m for m in self.files_metadata if m.get("id") != msg_id]
        self.lbl_reason.setVisible(False)
        self.failed_ids.discard(msg_id)
        self.deleted_ids.discard(msg_id)
        self._refresh_remove_failed()

        # A removed item is no longer pending work
        self.total_items = max(0, self.total_items - 1)
        if self.completed > self.total_items:
            self.completed = self.total_items
        self.batch_progress_bar.setMaximum(max(self.total_items, 1))
        self.batch_progress_bar.setValue(self.completed)
        self.lbl_status.setText(f"Downloaded {self.completed} out of {self.total_items} items")

        if self.completed >= self.total_items:
            self._set_completed_style()
            self.btn_pause.setEnabled(False)
        else:
            self._refresh_status_badge("active", "Downloading…")

    def update_file_progress(self, msg_id, current_bytes, total_bytes, speed_str):
        # A file that reports progress is no longer stalled
        self.lbl_reason.setVisible(False)
        self.lbl_status_text.setText(f"⬇ {speed_str}")
        
        # Extract numeric speed for global stats (e.g. "450 KB/s" -> 450)
        import re
        match = re.search(r'([\d.]+)\s*([KMG]?B/s)', speed_str)
        if match:
            val = float(match.group(1))
            unit = match.group(2)
            if 'MB/s' in unit: val *= 1024
            elif 'GB/s' in unit: val *= 1024 * 1024
            self.last_speed_val = val
        else:
            self.last_speed_val = 0

        if self.lbl_status_text.property("state") != "active":
            self.lbl_status_text.setProperty("state", "active")
            self.lbl_status_text.style().unpolish(self.lbl_status_text)
            self.lbl_status_text.style().polish(self.lbl_status_text)
            
        if msg_id in self.file_rows:
            self.file_rows[msg_id].set_progress(current_bytes, total_bytes)

    def mark_file_completed(self, msg_id):
        self.failed_ids.discard(msg_id)
        self.deleted_ids.discard(msg_id)
        self._refresh_remove_failed()
        if msg_id in self.file_rows:
            self.file_rows[msg_id].set_completed()

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.parent_worker.pause_download(self.task_id)
            try:
                from database import update_task_paused_db
                update_task_paused_db(self.task_id, is_paused=True)
            except Exception:
                pass
            self.btn_pause.setText("▶ Resume")
            self.lbl_status_text.setText("Paused")
            self.lbl_status_text.setProperty("state", "paused")
            self.batch_progress_bar.setProperty("state", "paused")
        else:
            parts = str(self.task_id).rsplit('_', 2)
            media_id = 6
            if len(parts) >= 2 and parts[-1].isdigit():
                media_id = int(parts[-1])
                if len(parts) == 3 and parts[-2].isdigit():
                    channel_input = f"{parts[0]}_{parts[-2]}"
                else:
                    channel_input = str(self.task_id).rsplit('_', 1)[0]
            else:
                channel_input = self.task_id
                
            self.parent_worker.start_download(
                channel_input=channel_input, media_id=media_id,
                download_path=self.download_path, download_limit=self.download_limit,
                max_speed_kb=self.max_speed_kb, task_id=self.task_id)
            self.btn_pause.setText("⏸ Pause")
            self.lbl_status_text.setText("Downloading…")
            self.lbl_status_text.setProperty("state", "active")
            self.batch_progress_bar.setProperty("state", "active")
        
        # Force style refresh for properties
        self.lbl_status_text.style().unpolish(self.lbl_status_text)
        self.lbl_status_text.style().polish(self.lbl_status_text)
        self.batch_progress_bar.style().unpolish(self.batch_progress_bar)
        self.batch_progress_bar.style().polish(self.batch_progress_bar)

    def run_health_check(self):
        self.btn_verify.setEnabled(False)
        self.btn_verify.setText("🛡️ Verifying...")
        self.lbl_verify_status.setVisible(True)
        self.lbl_verify_status.setText("Checking file integrity against disk...")
        
        from database import unmark_media_completed, mark_media_completed, get_media_downloaded_path
        c_id = self.task_id.split('_')[0].replace("-100", "", 1)
        
        corrupt = 0
        missing = 0
        valid = 0
        
        for msg_id, row in self.file_rows.items():
            meta = next((m for m in self.files_metadata if m["id"] == msg_id), None)
            if not meta:
                continue
            
            fpath = os.path.join(self.folder_name, meta["name"])
            if not os.path.exists(fpath):
                db_filename = get_media_downloaded_path(c_id, msg_id)
                if db_filename:
                    candidate = os.path.join(self.folder_name, db_filename) if not os.path.isabs(db_filename) else db_filename
                    if os.path.exists(candidate):
                        fpath = candidate

            if not os.path.exists(fpath):
                missing += 1
                row.icon.setText("❓")
                row.bar.setProperty("state", "idle")
                try:
                    unmark_media_completed(c_id, msg_id)
                except Exception:
                    pass
            else:
                actual_size = os.path.getsize(fpath)
                if meta["size"] > 0 and actual_size != meta["size"]:
                    corrupt += 1
                    row.icon.setText("⚠️")
                    row.bar.setProperty("state", "paused")
                    try:
                        unmark_media_completed(c_id, msg_id)
                    except Exception:
                        pass
                else:
                    valid += 1
                    row.icon.setText("✅")
                    row.bar.setProperty("state", "completed")
                    try:
                        mark_media_completed(c_id, msg_id)
                    except Exception:
                        pass
            row.bar.style().unpolish(row.bar)
            row.bar.style().polish(row.bar)
            
        self.completed = valid
        self.batch_progress_bar.setValue(valid)
        self.lbl_status.setText(f"Downloaded {valid} out of {self.total_items} items")

        if corrupt > 0 or missing > 0:
            total_bad = missing + corrupt
            self.lbl_verify_status.setText(f"Found {total_bad} missing/incomplete files. Click Resume to download.")
            self.lbl_verify_status.setStyleSheet("color: #F59E0B;")
            self.btn_pause.setEnabled(True)
            self.btn_pause.setText("▶ Resume")
            self.is_paused = True
            self.lbl_status_text.setText("Paused")
            self.lbl_status_text.setProperty("state", "paused")
            self.batch_progress_bar.setProperty("state", "paused")
        else:
            self.lbl_verify_status.setText(f"All {valid} files verified successfully!")
            self.lbl_verify_status.setStyleSheet("color: #10B981;")
            if self.total_items > 0 and valid >= self.total_items:
                self._set_completed_style()
                self.btn_pause.setEnabled(False)

        self.lbl_status_text.style().unpolish(self.lbl_status_text)
        self.lbl_status_text.style().polish(self.lbl_status_text)
        self.batch_progress_bar.style().unpolish(self.batch_progress_bar)
        self.batch_progress_bar.style().polish(self.batch_progress_bar)

        self.btn_verify.setEnabled(True)
        self.btn_verify.setText("🛡️ Verify")

    def set_reselect_loading(self, loading):
        if loading:
            self.btn_reselect.setText("⏳ Loading...")
            self.btn_reselect.setEnabled(False)
        else:
            self.btn_reselect.setText("🔄 Re-select")
            self.btn_reselect.setEnabled(True)

    def open_folder(self):
        if os.path.exists(self.folder_name):
            if os.name == 'nt':
                os.startfile(self.folder_name)
            else:
                os.system(f"xdg-open '{self.folder_name}'")


# ──────────────────────────────────────────────────────────────────────────────
# FileRow – shown in the expand area
# ──────────────────────────────────────────────────────────────────────────────
class FileRow(QWidget):
    removeRequested = Signal(int)  # msg_id

    def __init__(self, msg_id, filename, size_str, row_index=0):
        super().__init__()
        self.msg_id = msg_id
        self.setObjectName("FileRow")
        self.setProperty("alt", row_index % 2 == 1)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(34) # Slightly taller for better readability

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 10, 4)
        layout.setSpacing(8)

        self.icon = QLabel("⏳")
        self.icon.setFixedWidth(18)
        self.icon.setObjectName("FileRowIcon")

        # Name + optional reason line (stacked so long reasons stay readable)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)

        self.lbl_name = QLabel(filename)
        self.lbl_name.setObjectName("FileRowName")

        self.lbl_reason = QLabel("")
        self.lbl_reason.setObjectName("FileRowReason")
        self.lbl_reason.setVisible(False)

        text_col.addWidget(self.lbl_name)
        text_col.addWidget(self.lbl_reason)

        self.lbl_size = QLabel(size_str)
        self.lbl_size.setFixedWidth(58)
        self.lbl_size.setObjectName("FileRowSize")
        self.lbl_size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.bar = QProgressBar()
        self.bar.setFixedHeight(5)
        self.bar.setFixedWidth(100)
        self.bar.setTextVisible(False)
        self.bar.setMaximum(100)
        self.bar.setValue(0)
        self.bar.setObjectName("FileProgressBar")
        self.bar.setProperty("state", "idle")

        self.btn_remove = QPushButton("✕")
        self.btn_remove.setObjectName("FileRowRemove")
        self.btn_remove.setToolTip(
            "Remove this file from the queue so the task can continue"
        )
        self.btn_remove.setFixedSize(20, 20)
        self.btn_remove.setCursor(Qt.PointingHandCursor)
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self.msg_id))

        layout.addWidget(self.icon)
        layout.addLayout(text_col, stretch=3)
        layout.addWidget(self.lbl_size)
        layout.addWidget(self.bar)
        layout.addWidget(self.btn_remove)

    def _set_reason(self, text, color="#EF4444"):
        self.lbl_reason.setText(text)
        if color:
            self.lbl_reason.setStyleSheet(f"color: {color};")
        self.lbl_reason.setVisible(bool(text))
        self.lbl_name.setToolTip(text or self.lbl_name.text())
        self.setToolTip(text or "")
        # Give the row extra room for the second line only when it is shown
        self.setFixedHeight(48 if text else 34)

    def set_progress(self, current, total):
        if total:
            pct = int(current * 100 / total)
            self.bar.setValue(pct)
        if self.bar.property("state") != "active":
            self.bar.setProperty("state", "active")
            self.bar.style().unpolish(self.bar)
            self.bar.style().polish(self.bar)
            self.icon.setText("⬇️")

    def set_completed(self):
        self.bar.setValue(100)
        self.bar.setProperty("state", "completed")
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.icon.setText("✅")
        self._set_reason("")

    def set_failed(self, reason):
        """Icon + tooltip explaining why this file could not be downloaded."""
        self.bar.setProperty("state", "error")
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.icon.setText("❌")
        self._set_reason(reason, "#EF4444")

    def set_deleted(self, reason):
        """Icon + label for a file Telegram no longer has."""
        self.bar.setProperty("state", "deleted")
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.icon.setText("🗑️")
        self._set_reason(reason, "#9CA3AF")

    def set_stalled(self, reason):
        """Icon + tooltip for a file that stopped making progress."""
        self.bar.setProperty("state", "stalled")
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.icon.setText("⚠️")
        self._set_reason(reason, "#F59E0B")
