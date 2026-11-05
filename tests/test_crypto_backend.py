"""Without cryptg, Telethon decrypts every byte in pure Python.

That capped downloads at ~500 KB/s and held the GIL so long that the UI froze
("Not Responding") whenever a download ran. The build Python had silently
lacked cryptg although requirements.txt pins it, so the app must say so.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import core_downloader as cd
import telethon.crypto.aes as telethon_aes


class TestCryptoBackendWarning(unittest.TestCase):

    def setUp(self):
        self.orig = telethon_aes.cryptg

    def tearDown(self):
        telethon_aes.cryptg = self.orig

    def test_warns_when_telethon_has_no_fast_aes(self):
        telethon_aes.cryptg = None
        warning = cd.crypto_backend_warning()
        self.assertIsNotNone(warning)
        self.assertIn("cryptg", warning)

    def test_silent_when_cryptg_is_used(self):
        telethon_aes.cryptg = object()
        self.assertIsNone(cd.crypto_backend_warning())

    def test_build_python_has_cryptg(self):
        # Fails the suite on a machine that would build a slow exe
        import cryptg  # noqa: F401
        self.assertIsNotNone(self.orig)


if __name__ == "__main__":
    unittest.main()
