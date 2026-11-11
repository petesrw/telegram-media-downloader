"""Write a Python stack trace to a file when the process crashes natively.

An access violation inside Qt kills the windowed exe without a word in
downloader.log. faulthandler can still dump every thread's stack, but the exe
has no stderr, so it needs a file that stays open for the process's lifetime.

A clean shutdown appends an "exited normally" line. A session with neither a
stack trace nor that line was killed from outside (Task Manager, the OS, a
driver reset), which no in-process handler can observe.
"""

import atexit
import faulthandler
from datetime import datetime

_crash_file = None  # kept open on purpose: faulthandler writes to its descriptor


def enable_crash_log(path, version):
    """Append crashes to `path`; returns the open file (or None on failure)."""
    global _crash_file
    try:
        f = open(path, "a", encoding="utf-8")
        f.write(f"\n=== TG Media Downloader {version} started "
                f"{datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
    except Exception:
        return None
    _crash_file = f
    atexit.register(_record_normal_exit)
    return f


def _record_normal_exit():
    if _crash_file is None:
        return
    try:
        _crash_file.write(f"=== exited normally {datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
        _crash_file.flush()
    except Exception:
        pass


def disable_crash_log():
    global _crash_file
    atexit.unregister(_record_normal_exit)
    faulthandler.disable()
    if _crash_file is not None:
        _crash_file.close()
        _crash_file = None
