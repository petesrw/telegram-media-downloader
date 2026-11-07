"""Write a Python stack trace to a file when the process crashes natively.

An access violation inside Qt kills the windowed exe without a word in
downloader.log. faulthandler can still dump every thread's stack, but the exe
has no stderr, so it needs a file that stays open for the process's lifetime.
"""

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
    return f


def disable_crash_log():
    global _crash_file
    faulthandler.disable()
    if _crash_file is not None:
        _crash_file.close()
        _crash_file = None
