"""
Central logging for TG Media Downloader.

Two problems motivated this module:

1. The packaged app is built with ``console=False`` (see the PyInstaller spec),
   so every ``print()`` used for diagnostics vanished in the .exe. A failed
   download therefore looked like a silent hang.
2. Nothing ever told the user *why* an item did not download, so the only
   recovery was guessing.

Everything here is defensive: logging must never be the reason a download
fails, so every I/O or Qt interaction is wrapped.

Usage::

    from utils.app_logger import log_event, describe_download_error
    log_event("ERROR", f"message {msg_id}: {describe_download_error(exc)}")
"""

import asyncio
import logging
import os
import threading
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler

from resource_utils import get_project_root

# Qt is a hard dependency of the GUI, but the download engine must stay
# importable in headless environments (tests, CLI tools), so the signal bus
# degrades to a no-op when PySide6 is not available.
try:
    from PySide6.QtCore import QObject, Signal

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only without Qt installed
    QObject = object
    Signal = None
    _QT_AVAILABLE = False

LOG_FILE_NAME = "downloader.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_RING_SIZE = 2000

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# In-memory ring buffer so the Logs view can show events recorded before it was
# opened (and so tests can assert without touching the file system).
_RING = deque(maxlen=LOG_RING_SIZE)
_RING_LOCK = threading.Lock()

_LOGGER = None
_LOGGER_LOCK = threading.Lock()


if _QT_AVAILABLE:

    class LogBus(QObject):
        """Qt signal bus that mirrors every log record into the UI thread."""

        record = Signal(str, str, str)  # level, timestamp, message

        _instance = None

        @classmethod
        def instance(cls) -> "LogBus":
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

        def publish(self, level: str, timestamp: str, message: str) -> None:
            # Emitting across threads is safe: Qt uses a queued connection
            # whenever the receiver lives in a different thread, which is
            # exactly the case for worker-thread download failures reported to
            # the Logs view.
            self.record.emit(level, timestamp, message)

else:  # pragma: no cover - headless fallback

    class _NullSignal:
        def connect(self, *args, **kwargs):
            return None

        def emit(self, *args, **kwargs):
            return None

    class LogBus:
        """Drop-in replacement for :class:`LogBus` without Qt."""

        record = _NullSignal()
        _instance = None

        @classmethod
        def instance(cls) -> "LogBus":
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

        def publish(self, level: str, timestamp: str, message: str) -> None:
            return None


def get_log_path() -> str:
    """Absolute path of the rotating log file (next to .env / config.json)."""
    return os.path.join(get_project_root(), LOG_FILE_NAME)


def _log_to_file_enabled() -> bool:
    try:
        from ui.views.settings_view import load_config

        return bool(load_config().get("log_to_file", True))
    except Exception:
        return True


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("tg_downloader")
    logger.setLevel(logging.DEBUG)
    # Never let these records bubble into the root logger (and double-print).
    logger.propagate = False

    if logger.handlers:
        return logger

    if _log_to_file_enabled():
        try:
            handler = RotatingFileHandler(
                get_log_path(),
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            )
            logger.addHandler(handler)
            return logger
        except Exception:
            # Read-only install directory, missing permission, disk full, ...
            pass

    # No file handler could be opened (read-only folder, logs disabled, ...).
    # Attach a NullHandler so the logging module does not fall back to writing
    # to a console that the packaged app does not have.
    logger.addHandler(logging.NullHandler())
    return logger


def _get_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is None:
        with _LOGGER_LOCK:
            if _LOGGER is None:
                _LOGGER = _build_logger()
    return _LOGGER


def reload_logger() -> None:
    """Re-apply ``log_to_file`` after the user changed it in Settings."""
    global _LOGGER
    with _LOGGER_LOCK:
        if _LOGGER is not None:
            for handler in list(_LOGGER.handlers):
                try:
                    handler.close()
                except Exception:
                    pass
                _LOGGER.removeHandler(handler)
        _LOGGER = _build_logger()


def log_event(level: str, message: str) -> str:
    """Record one event to memory, the rotating file, and the UI bus.

    Returns the formatted line (handy for tests).
    """
    level = str(level).upper()
    if level not in LEVELS:
        level = "INFO"

    message = str(message)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _RING_LOCK:
        _RING.append((timestamp, level, message))

    try:
        _get_logger().log(getattr(logging, level, logging.INFO), message)
    except Exception:
        pass

    try:
        LogBus.instance().publish(level, timestamp, message)
    except Exception:
        pass

    return f"{timestamp} [{level}] {message}"


def get_recent_logs(limit: int = 500):
    """Return the most recent in-memory records as (timestamp, level, message)."""
    with _RING_LOCK:
        rows = list(_RING)[-limit:]
    return rows


def tail_log_file(limit: int = 500):
    """Read the last ``limit`` lines of the log file (empty list if missing)."""
    path = get_log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=limit)]
    except Exception:
        return []


def clear_log_file() -> None:
    """Truncate the log file. Handler stays usable because we clear in place."""
    try:
        with open(get_log_path(), "w", encoding="utf-8"):
            pass
    except Exception:
        pass
    with _RING_LOCK:
        _RING.clear()


def describe_download_error(exc: BaseException) -> str:
    """Translate an exception into a short, user-facing reason for a failure."""
    name = type(exc).__name__
    text = str(exc).strip()
    low = text.lower()

    if isinstance(exc, asyncio.CancelledError) or name == "CancelledError":
        return "Cancelled"

    seconds = getattr(exc, "seconds", None)
    if "FloodWait" in name or "flood" in low:
        wait = f" ({seconds}s)" if seconds else ""
        return f"Telegram rate limit{wait} - wait for it to expire, then resume"

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Network timeout - Telegram did not answer in time"

    if "FileReferenceExpired" in name:
        return "File reference expired - it is refreshed automatically on retry"
    if "FileReferenceInvalid" in name:
        return "File reference invalid - the file must be re-fetched from Telegram"
    if "AuthKeyDuplicated" in name:
        return "Session used from two locations - log out and log in again"
    if any(k in name for k in ("AuthKeyUnregistered", "AuthKeyInvalid", "Unauthorized")):
        return "Session invalid or expired - log in again"
    if any(k in name for k in ("PhoneNumberBanned", "UserDeactivated", "UserDeactivatedBan")):
        return "Account restricted by Telegram"
    if "ChannelPrivate" in name or "ChatAdminRequired" in name or "ChannelInvalid" in name:
        return "No access to this channel - join it or check permissions"
    if "ChatIdInvalid" in name or "CHAT_ID_INVALID" in text:
        return "This ID is not a basic group - check the ID, or use the username/invite link"
    if "MsgIdInvalid" in name:
        return "Message no longer exists on Telegram"
    if "MessageNotModified" in name:
        return "Message cannot be downloaded in its current state"
    if "FilePart" in name or "FILE_PART" in text or "filepart" in low:
        return f"Telegram reported a missing file part - {text or name}"
    if "MEDIA_EMPTY" in text or "MediaEmpty" in name:
        return "Media is empty or unavailable on Telegram"
    if "DC_ID_INVALID" in text:
        return "Invalid Telegram data-centre - retry refreshes it"

    if isinstance(exc, PermissionError):
        return "Cannot write to the download folder (permission denied or file locked)"
    if isinstance(exc, FileNotFoundError):
        return f"Path not found - {text or name}"
    if isinstance(exc, MemoryError):
        return "Out of memory while buffering the download"
    if isinstance(exc, OSError):
        if getattr(exc, "errno", None) == 28:
            return "Disk full - no space left on device"
        return f"File or network I/O error - {text or name}"

    if "Could not find the input entity" in text:
        return "Channel/chat is not accessible from this account"
    if "closed the connection" in low:
        return "Telegram closed the connection (network or proxy)"
    if isinstance(exc, ConnectionError) or "connection" in low:
        return f"Connection error - {text or name}"
    if "database is locked" in low:
        return "Local database is locked - retry shortly"

    return f"{name}: {text}" if text else name
