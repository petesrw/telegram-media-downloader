from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QCheckBox, QComboBox, 
    QFrame, QFileDialog, QSpinBox, QMessageBox,
    QScrollArea, QGridLayout
)
from PySide6.QtCore import Qt, Signal
import json
import os
from resource_utils import get_project_root

CONFIG_FILE = os.path.join(get_project_root(), "config.json")

def load_config():
    default_config = {
        "download_path": "downloads",
        "download_limit": 5,
        "initial_fetch_limit": 2000,
        "max_speed_kb": 0,
        "forum_auto_separation": False,
        "rename_duplicates": True,
        "use_message_date": True,
        "prefix_file_date": True,
        "prefix_channel_handle": False,
        "redownload_deleted": False,
        "stall_warn_seconds": 45,   # report "no progress" after this many seconds
        "stall_abort_seconds": 180, # cancel a stalled attempt so it frees its slot
        "log_to_file": True,        # write downloader.log next to config.json
        "dark_mode": None,  # None = follow Windows system setting
        "proxy": {
            "enabled": False,
            "type": "SOCKS5",
            "host": "",
            "port": "",
            "user": "",
            "pass": ""
        }
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "proxy" in data and isinstance(data["proxy"], dict):
                        default_config["proxy"].update(data["proxy"])
                        data_copy = dict(data)
                        del data_copy["proxy"]
                        default_config.update(data_copy)
                    else:
                        default_config.update(data)
        except Exception as e:
            print(f"Error loading config: {e}")
    return default_config

def save_config(config_data):
    try:
        temp_file = CONFIG_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
        os.replace(temp_file, CONFIG_FILE)
    except Exception as e:
        print(f"Error saving config: {e}")

def get_telegram_proxy():
    """
    Returns a proxy dict suitable for Telethon if proxy is enabled and configured,
    or None if proxy is disabled or invalid.
    """
    cfg = load_config()
    proxy = cfg.get("proxy", {})
    if not proxy.get("enabled", False):
        return None

    host = str(proxy.get("host", "")).strip()
    port_val = proxy.get("port", "")
    if not host or not port_val:
        return None

    try:
        port = int(str(port_val).strip())
        if not (1 <= port <= 65535):
            return None
    except (ValueError, TypeError):
        return None

    p_type = str(proxy.get("type", "SOCKS5")).strip().lower()
    if p_type not in ("socks5", "socks4", "http"):
        p_type = "socks5"

    proxy_dict = {
        "proxy_type": p_type,
        "addr": host,
        "port": port,
        "rdns": True
    }
    user = proxy.get("user")
    if user:
        user_str = str(user).strip()
        if user_str:
            proxy_dict["username"] = user_str
    pwd = proxy.get("pass")
    if pwd:
        pwd_str = str(pwd)
        if pwd_str:
            proxy_dict["password"] = pwd_str

    return proxy_dict

class SettingsView(QWidget):
    logoutRequested = Signal()
    settingsSaved = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        # Main Layout
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Main Scroll Area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setStyleSheet("QScrollArea { background-color: transparent; }")

        # Container for scrolling content
        self.container = QWidget()
        self.scroll_layout = QVBoxLayout(self.container)
        self.scroll_layout.setContentsMargins(40, 40, 40, 40)
        self.scroll_layout.setSpacing(32)
        self.scroll_layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        # Header
        lbl_header = QLabel("⚙️ Settings")
        lbl_header.setObjectName("MainHeaderLarge")
        self.scroll_layout.addWidget(lbl_header)

        # Max width constraint to keep it centered and elegant
        self.settings_card = QFrame()
        self.settings_card.setObjectName("WhiteCard")
        self.settings_card.setMaximumWidth(800)
        self.scroll_layout.addWidget(self.settings_card)

        self.clayout = QVBoxLayout(self.settings_card)
        self.clayout.setContentsMargins(32, 32, 32, 32)
        self.clayout.setSpacing(24)

        # --- Section 1: Downloads ---
        dl_header = QHBoxLayout()
        lbl_dl_icon = QLabel("📂")
        lbl_dl_icon.setStyleSheet("font-size: 18px;")
        lbl_dl_title = QLabel("Download Settings")
        lbl_dl_title.setObjectName("SectionHeader")
        dl_header.addWidget(lbl_dl_icon)
        dl_header.addWidget(lbl_dl_title)
        dl_header.addStretch()
        self.clayout.addLayout(dl_header)

        lbl_path_desc = QLabel("Primary Folder Path")
        lbl_path_desc.setObjectName("ControlLabel")
        self.clayout.addWidget(lbl_path_desc)

        path_row = QHBoxLayout()
        self.input_path = QLineEdit("downloads")
        self.input_path.setReadOnly(False)
        self.input_path.setFixedHeight(36)
        
        self.btn_browse = QPushButton("Browse Folder")
        self.btn_browse.setObjectName("SecondaryButton")
        self.btn_browse.clicked.connect(self.browse_path)
        
        self.btn_open = QPushButton("📂 Open")
        self.btn_open.setObjectName("PrimaryButton")
        self.btn_open.clicked.connect(self.open_folder)
        
        path_row.addWidget(self.input_path, stretch=1)
        path_row.addWidget(self.btn_browse)
        path_row.addWidget(self.btn_open)
        self.clayout.addLayout(path_row)

        lbl_template_tip = QLabel("💡 <b>Dynamic variables supported</b>: {channel}, {username}, {channel_id}, {category}, {year}, {month}, {day}")
        lbl_template_tip.setObjectName("MutedText")
        self.clayout.addWidget(lbl_template_tip)

        self.chk_forum_sep = QCheckBox("Enable Forum Topic Auto-separation")
        self.chk_forum_sep.setToolTip("Automatically download all topics from a forum into separate subfolders named after the topics.")
        self.clayout.addWidget(self.chk_forum_sep)

        self.chk_rename_duplicates = QCheckBox("Rename Duplicate Filenames")
        self.chk_rename_duplicates.setToolTip("Automatically append suffix (2, 3, etc.) to duplicate filenames instead of overwriting existing files.")
        self.clayout.addWidget(self.chk_rename_duplicates)

        self.chk_use_msg_date = QCheckBox("Set File Date to Message Date")
        self.chk_use_msg_date.setToolTip("Set the file's modified and created times to match the date and time when the message was sent to the chat.")
        self.clayout.addWidget(self.chk_use_msg_date)

        self.chk_prefix_file_date = QCheckBox("Prefix Filenames with Publication Date (YYYY-MM-DD)")
        self.chk_prefix_file_date.setToolTip("Prepend the message publication date (e.g. 2026-01-01_filename.mp4) to all downloaded media for tidy chronological organization.")
        self.clayout.addWidget(self.chk_prefix_file_date)

        self.chk_prefix_channel_handle = QCheckBox("Prefix Filenames with Channel Handle / Name (@channel)")
        self.chk_prefix_channel_handle.setToolTip("Prepend the channel handle (e.g. @username_YYYY-MM-DD_file.mp4) or channel title to all filenames.")
        self.clayout.addWidget(self.chk_prefix_channel_handle)

        self.chk_redownload_deleted = QCheckBox("Re-download Files If Deleted/Moved from Folder")
        self.chk_redownload_deleted.setToolTip("When enabled, if a downloaded file is moved or deleted from the destination folder, it will be re-downloaded on the next run. When disabled (recommended if you organize files into other folders), downloaded items are remembered by the database and won't be re-downloaded.")
        self.clayout.addWidget(self.chk_redownload_deleted)

        self.clayout.addWidget(self._create_divider())

        # --- Section 2: Connection ---
        conn_header = QHBoxLayout()
        lbl_conn_icon = QLabel("🌐")
        lbl_conn_icon.setStyleSheet("font-size: 18px;")
        lbl_conn_title = QLabel("Network & Proxy")
        lbl_conn_title.setObjectName("SectionHeader")
        conn_header.addWidget(lbl_conn_icon)
        conn_header.addWidget(lbl_conn_title)
        conn_header.addStretch()
        self.clayout.addLayout(conn_header)

        self.chk_enable_proxy = QCheckBox("Enable Proxy Server Connection")
        self.clayout.addWidget(self.chk_enable_proxy)

        proxy_form = QVBoxLayout()
        proxy_row1 = QHBoxLayout()
        self.combo_proxy_type = QComboBox()
        self.combo_proxy_type.addItems(["SOCKS5", "SOCKS4", "HTTP"])
        self.combo_proxy_type.setFixedWidth(100)
        self.input_proxy_host = QLineEdit()
        self.input_proxy_host.setPlaceholderText("Hostname / IP Address")
        self.input_proxy_port = QLineEdit()
        self.input_proxy_port.setPlaceholderText("Port")
        self.input_proxy_port.setFixedWidth(80)
        
        proxy_row1.addWidget(self.combo_proxy_type)
        proxy_row1.addWidget(self.input_proxy_host, stretch=1)
        proxy_row1.addWidget(self.input_proxy_port)
        proxy_form.addLayout(proxy_row1)
        
        proxy_row2 = QHBoxLayout()
        self.input_proxy_user = QLineEdit()
        self.input_proxy_user.setPlaceholderText("Username (Optional)")
        self.input_proxy_pass = QLineEdit()
        self.input_proxy_pass.setPlaceholderText("Password (Optional)")
        self.input_proxy_pass.setEchoMode(QLineEdit.Password)
        
        proxy_row2.addWidget(self.input_proxy_user)
        proxy_row2.addWidget(self.input_proxy_pass)
        proxy_form.addLayout(proxy_row2)
        self.clayout.addLayout(proxy_form)

        self.clayout.addWidget(self._create_divider())

        # --- Section 3: Performance ---
        perf_header = QHBoxLayout()
        lbl_perf_icon = QLabel("🚀")
        lbl_perf_icon.setStyleSheet("font-size: 18px;")
        lbl_perf_title = QLabel("Performance & Limits")
        lbl_perf_title.setObjectName("SectionHeader")
        perf_header.addWidget(lbl_perf_icon)
        perf_header.addWidget(lbl_perf_title)
        perf_header.addStretch()
        self.clayout.addLayout(perf_header)

        limit_grid = QGridLayout()
        limit_grid.setColumnStretch(1, 1)

        limit_grid.addWidget(QLabel("Concurrent Connections:", objectName="ControlLabel"), 0, 0)
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(1, 100)
        self.spin_limit.setFixedHeight(30)
        limit_grid.addWidget(self.spin_limit, 0, 1)

        limit_grid.addWidget(QLabel("Global Download Speed Limit (KB/s):", objectName="ControlLabel"), 1, 0)
        self.spin_speed = QSpinBox()
        self.spin_speed.setRange(0, 999999)
        self.spin_speed.setFixedHeight(30)
        limit_grid.addWidget(self.spin_speed, 1, 1)

        limit_grid.addWidget(QLabel("Initial Media Fetch Limit (per type):", objectName="ControlLabel"), 2, 0)
        self.spin_fetch_limit = QSpinBox()
        self.spin_fetch_limit.setRange(10, 50000)
        self.spin_fetch_limit.setFixedHeight(30)
        limit_grid.addWidget(self.spin_fetch_limit, 2, 1)

        limit_grid.addWidget(QLabel("Report \"No Progress\" After (seconds):", objectName="ControlLabel"), 3, 0)
        self.spin_stall_warn = QSpinBox()
        self.spin_stall_warn.setRange(0, 3600)
        self.spin_stall_warn.setFixedHeight(30)
        self.spin_stall_warn.setToolTip(
            "A file that transfers nothing for this long is marked as stalled in the "
            "queue and recorded in the Logs tab. Raise it on very slow connections. "
            "0 disables the warning."
        )
        limit_grid.addWidget(self.spin_stall_warn, 3, 1)

        limit_grid.addWidget(QLabel("Abort Stalled Download After (seconds):", objectName="ControlLabel"), 4, 0)
        self.spin_stall_abort = QSpinBox()
        self.spin_stall_abort.setRange(0, 7200)
        self.spin_stall_abort.setFixedHeight(30)
        self.spin_stall_abort.setToolTip(
            "A stalled attempt is cancelled after this long so it stops holding a "
            "concurrency slot and the rest of the queue can continue. The file stays "
            "in the queue so you can resume or remove it. 0 disables the abort."
        )
        limit_grid.addWidget(self.spin_stall_abort, 4, 1)
        
        self.clayout.addLayout(limit_grid)
        self.clayout.addWidget(QLabel("Set 0 for unlimited speed. Using higher concurrency index may lead to better speeds but uses more resources.", objectName="MutedText"))
        self.clayout.addWidget(QLabel("The Initial Fetch Limit controls how many recent items are scanned when searching for a channel.", objectName="MutedText"))

        self.chk_log_to_file = QCheckBox("Write Diagnostics Log File (downloader.log)")
        self.chk_log_to_file.setToolTip(
            "Records download failures, stall warnings and retries to downloader.log "
            "next to config.json. Shown in the Logs tab either way."
        )
        self.clayout.addWidget(self.chk_log_to_file)

        self.clayout.addStretch()

        # Save Button at bottom of card
        self.btn_save = QPushButton("Save Settings")
        self.btn_save.setObjectName("SuccessButton")
        self.btn_save.setFixedHeight(40)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self.save_settings)
        self.clayout.addWidget(self.btn_save)

        # Logout SECTION separately (Danger Zone)
        self.scroll_layout.addWidget(QLabel("Danger Zone", objectName="SectionHeader", styleSheet="color: #EF4444; font-size: 11px; margin-top: 10px;"))
        
        logout_card = QFrame()
        logout_card.setObjectName("WhiteCard")
        logout_card.setStyleSheet("QFrame#WhiteCard { border: 1px solid #FEE2E2; }")
        logout_card.setMaximumWidth(800)
        
        log_l = QHBoxLayout(logout_card)
        log_l.setContentsMargins(20, 20, 20, 20)
        
        txt_l = QVBoxLayout()
        txt_l.addWidget(QLabel("Log out of Telegram", styleSheet="font-weight: bold; color: #EF4444;"))
        txt_l.addWidget(QLabel("This will clear your local session and close the connection.", objectName="MutedText"))
        log_l.addLayout(txt_l)
        
        log_l.addStretch()
        
        self.btn_logout = QPushButton("🚪 Logout")
        self.btn_logout.setObjectName("LogoutBtn")
        self.btn_logout.setFixedHeight(36)
        self.btn_logout.setMinimumWidth(120)
        self.btn_logout.setCursor(Qt.PointingHandCursor)
        self.btn_logout.clicked.connect(self.logout_clicked)
        log_l.addWidget(self.btn_logout)
        
        self.scroll_layout.addWidget(logout_card)
        
        # Assemble
        self.scroll_area.setWidget(self.container)
        root_layout.addWidget(self.scroll_area)
        
        self.load_settings()

    def _create_divider(self):
        d = QFrame()
        d.setObjectName("Divider")
        d.setFrameShape(QFrame.HLine)
        d.setFixedHeight(1)
        return d

    def load_settings(self):
        config = load_config()
        self.input_path.setText(config.get("download_path", "downloads"))
        self.spin_limit.setValue(config.get("download_limit", 5))
        self.spin_fetch_limit.setValue(config.get("initial_fetch_limit", 2000))
        self.spin_speed.setValue(config.get("max_speed_kb", 0))
        self.chk_forum_sep.setChecked(config.get("forum_auto_separation", False))
        self.chk_rename_duplicates.setChecked(config.get("rename_duplicates", True))
        self.chk_use_msg_date.setChecked(config.get("use_message_date", True))
        self.chk_prefix_file_date.setChecked(config.get("prefix_file_date", True))
        self.chk_prefix_channel_handle.setChecked(config.get("prefix_channel_handle", False))
        self.chk_redownload_deleted.setChecked(config.get("redownload_deleted", False))
        self.spin_stall_warn.setValue(int(config.get("stall_warn_seconds", 45)))
        self.spin_stall_abort.setValue(int(config.get("stall_abort_seconds", 180)))
        self.chk_log_to_file.setChecked(config.get("log_to_file", True))
        
        proxy = config.get("proxy", {})
        self.chk_enable_proxy.setChecked(proxy.get("enabled", False))
        self.combo_proxy_type.setCurrentText(proxy.get("type", "SOCKS5"))
        self.input_proxy_host.setText(proxy.get("host", ""))
        self.input_proxy_port.setText(str(proxy.get("port", "")))
        self.input_proxy_user.setText(proxy.get("user", ""))
        self.input_proxy_pass.setText(proxy.get("pass", ""))

    def save_settings(self):
        config = load_config()
        config.update({
            "download_path": self.input_path.text(),
            "download_limit": self.spin_limit.value(),
            "initial_fetch_limit": self.spin_fetch_limit.value(),
            "max_speed_kb": self.spin_speed.value(),
            "forum_auto_separation": self.chk_forum_sep.isChecked(),
            "rename_duplicates": self.chk_rename_duplicates.isChecked(),
            "use_message_date": self.chk_use_msg_date.isChecked(),
            "prefix_file_date": self.chk_prefix_file_date.isChecked(),
            "prefix_channel_handle": self.chk_prefix_channel_handle.isChecked(),
            "redownload_deleted": self.chk_redownload_deleted.isChecked(),
            "stall_warn_seconds": self.spin_stall_warn.value(),
            "stall_abort_seconds": self.spin_stall_abort.value(),
            "log_to_file": self.chk_log_to_file.isChecked(),
            "proxy": {
                "enabled": self.chk_enable_proxy.isChecked(),
                "type": self.combo_proxy_type.currentText(),
                "host": self.input_proxy_host.text().strip(),
                "port": self.input_proxy_port.text().strip(),
                "user": self.input_proxy_user.text().strip(),
                "pass": self.input_proxy_pass.text()
            }
        })
        save_config(config)

        # Apply the log_to_file toggle without a restart
        try:
            from utils.app_logger import reload_logger
            reload_logger()
        except Exception:
            pass
        
        # Sync existing database tasks with updated limits
        try:
            import sqlite3
            from database import DB_PATH
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE tasks SET max_speed_kb=?, download_limit=?", (self.spin_speed.value(), self.spin_limit.value()))
            conn.commit()
            conn.close()
        except Exception: pass

        self.settingsSaved.emit()

        # Notify user it was saved properly
        QMessageBox.information(self, "Settings Saved", "Configuration saved successfully!")

    def browse_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Download Directory")
        if folder:
            self.input_path.setText(folder)

    def open_folder(self):
        path = self.input_path.text()
        if os.path.exists(path):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))
        else:
            QMessageBox.warning(self, "Folder Not Found", f"The directory does not exist yet:\n{path}")

    def logout_clicked(self):
        self.logoutRequested.emit()
