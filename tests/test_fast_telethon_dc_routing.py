import unittest
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import utils.fast_telethon as ft
from utils.fast_telethon import _download_part


class MockInputLocation:
    SUBCLASS_OF_ID = 0x1523d462


def _result(data):
    return type("MockResult", (), {"bytes": data})()


class DisconnectedSender:
    """A borrowed sender whose own reconnect gave up: every send fails."""

    def __init__(self):
        self.disconnected = False

    async def send(self, req):
        raise ConnectionError("Cannot send requests while disconnected")

    async def disconnect(self):
        self.disconnected = True


class HealthySender:
    async def send(self, req):
        return _result(b"Y" * req.limit)


class RoutingClient:
    """Mimics the parts of TelegramClient that _download_part touches."""

    def __init__(self, home_dc, senders):
        self.session = type("Session", (), {"dc_id": home_dc})()
        self._borrow_sender_lock = asyncio.Lock()
        self._borrowed_senders = {}
        self._senders = list(senders)
        self.borrowed_dcs = []
        self.main_calls = 0

    async def _borrow_exported_sender(self, dc_id):
        self.borrowed_dcs.append(dc_id)
        state, sender = self._borrowed_senders.get(dc_id, (None, None))
        if sender is None:
            sender = self._senders.pop(0)
            self._borrowed_senders[dc_id] = (object(), sender)
        return sender

    async def _return_exported_sender(self, sender):
        pass

    async def __call__(self, req):
        self.main_calls += 1
        return _result(b"M" * req.limit)


class TestDcRouting(unittest.TestCase):

    def setUp(self):
        self._orig_sleep = asyncio.sleep

        async def no_sleep(_seconds):
            return None

        ft.asyncio.sleep = no_sleep

    def tearDown(self):
        ft.asyncio.sleep = self._orig_sleep

    def run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_same_dc_uses_main_connection_not_exported_sender(self):
        # Exporting auth to the DC the client already lives on fails with
        # DcIdInvalidError and leaks a connected socket per attempt.
        client = RoutingClient(home_dc=5, senders=[HealthySender()])

        data = self.run_async(_download_part(client, MockInputLocation(), 0, 1024, dc_id=5))

        self.assertEqual(data, b"M" * 1024)
        self.assertEqual(client.borrowed_dcs, [])
        self.assertEqual(client.main_calls, 1)

    def test_other_dc_still_uses_exported_sender(self):
        client = RoutingClient(home_dc=5, senders=[HealthySender()])

        data = self.run_async(_download_part(client, MockInputLocation(), 0, 1024, dc_id=4))

        self.assertEqual(data, b"Y" * 1024)
        self.assertEqual(client.borrowed_dcs, [4])
        self.assertEqual(client.main_calls, 0)

    def test_dead_exported_sender_is_evicted_and_replaced(self):
        # Telethon keeps handing out a cached sender whose reconnect gave up
        # while other workers keep borrowing it; it must be dropped so the next
        # attempt builds a fresh connection.
        dead = DisconnectedSender()
        client = RoutingClient(home_dc=5, senders=[dead, HealthySender()])

        data = self.run_async(_download_part(client, MockInputLocation(), 0, 1024, dc_id=4))

        self.assertEqual(data, b"Y" * 1024)
        self.assertTrue(dead.disconnected)
        self.assertEqual(client.borrowed_dcs, [4, 4])

    def test_expired_file_reference_is_not_retried_with_the_same_location(self):
        # Retrying cannot help: the stale reference is baked into the location,
        # so the caller has to re-fetch the message instead.
        from telethon.errors import FileReferenceExpiredError

        class ExpiredClient(RoutingClient):
            async def __call__(self, req):
                self.main_calls += 1
                raise FileReferenceExpiredError(request=None)

        client = ExpiredClient(home_dc=5, senders=[])

        with self.assertRaises(FileReferenceExpiredError):
            self.run_async(_download_part(client, MockInputLocation(), 0, 1024, dc_id=5))
        self.assertEqual(client.main_calls, 1)

    def test_task_cancellation_propagates_and_keeps_progress(self):
        # The stall watchdog cancels the download task. Swallowing that made the
        # caller fall back to a single-stream download from byte 0 that wrote
        # straight to the final file, ignoring the chunks already in .part.
        import tempfile
        import shutil
        from utils.fast_telethon import fast_download_file, CHUNK_SIZE, _load_meta

        ft.asyncio.sleep = self._orig_sleep
        temp_dir = tempfile.mkdtemp()
        target = os.path.join(temp_dir, "stalled.mp4")
        first_chunk_done = asyncio.Event()

        class HangsAfterFirstChunk(RoutingClient):
            async def __call__(self, req):
                if req.offset == 0:
                    first_chunk_done.set()
                    return _result(b"A" * req.limit)
                await asyncio.Event().wait()  # Telegram never answers

        async def scenario():
            client = HangsAfterFirstChunk(home_dc=5, senders=[])
            task = asyncio.create_task(fast_download_file(
                client, MockInputLocation(), target, CHUNK_SIZE * 3, dc_id=5, workers=2,
                gate=ft.DownloadFloodGate(),
            ))
            await first_chunk_done.wait()
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        try:
            self.run_async(scenario())
            self.assertFalse(os.path.exists(target), "nothing may be written to the final path")
            saved = _load_meta(target + ".part.meta", CHUNK_SIZE * 3, 3)
            self.assertEqual(saved, {0})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_eviction_leaves_a_newer_cached_sender_alone(self):
        # Another worker may already have replaced the dead sender.
        dead = DisconnectedSender()
        fresh = HealthySender()
        client = RoutingClient(home_dc=5, senders=[])
        client._borrowed_senders[4] = (object(), fresh)

        self.run_async(ft._evict_exported_sender(client, 4, dead))

        self.assertIs(client._borrowed_senders[4][1], fresh)
        self.assertTrue(dead.disconnected)


if __name__ == "__main__":
    unittest.main()
