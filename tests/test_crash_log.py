"""A native crash must leave a Python stack trace behind.

v2.9.7 died with an access violation in pyside6.abi3.dll and nothing in
downloader.log. The windowed exe has no stderr, so faulthandler has to write
to a file of its own.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


class TestCrashLog(unittest.TestCase):

    def test_a_segfault_writes_the_stack_to_crash_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "crash.log")
            script = textwrap.dedent(f"""
                import sys, faulthandler
                sys.path.insert(0, {SRC!r})
                from utils.crash_log import enable_crash_log
                enable_crash_log({path!r}, "9.9.9")
                def crashing_function():
                    faulthandler._sigsegv()
                crashing_function()
            """)
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)

            self.assertNotEqual(proc.returncode, 0, "the child must actually crash")
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            self.assertIn("9.9.9", text)
            self.assertIn("Fatal Python error", text)
            self.assertIn("crashing_function", text)

    def _run_child(self, path, ending):
        script = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {SRC!r})
            from utils.crash_log import enable_crash_log
            enable_crash_log({path!r}, "9.9.9")
        """) + ending
        return subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)

    def test_a_normal_exit_is_recorded(self):
        # v2.9.11 vanished with no crash trace; without an exit line nobody could
        # tell a clean quit from the process being killed from outside.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "crash.log")
            proc = self._run_child(path, "sys.exit(0)\n")
            self.assertEqual(proc.returncode, 0)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("exited normally", text)

    def test_a_killed_process_leaves_no_exit_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "crash.log")
            self._run_child(path, "os._exit(1)\n")  # like TerminateProcess: no cleanup runs
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("9.9.9", text)
            self.assertNotIn("exited normally", text)

    def test_the_log_is_appended_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "crash.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write("earlier crash\n")
            sys.path.insert(0, SRC)
            from utils import crash_log
            handle = crash_log.enable_crash_log(path, "1.0")
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            finally:
                crash_log.disable_crash_log()
            self.assertIn("earlier crash", text)
            self.assertIn("1.0", text)
            self.assertIsNotNone(handle)


if __name__ == "__main__":
    unittest.main()
