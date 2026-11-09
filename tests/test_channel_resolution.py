"""
Tests for channel/entity resolution in ``core_downloader.fetch_channel``.

The failure that motivated these tests: fetching the numeric ID
``-3889572674`` reported only "Telegram completely declined access", because

* a negative ID was only ever tried as a *basic group*, never as the channel
  ``-1003889572674`` that the same digits may refer to,
* the dialog fallback compared raw strings, so a missing ``-100`` marker could
  never match the real chat in the account's dialog list, and
* the underlying Telegram error was thrown away, leaving no clue about *why*
  the fetch failed.

Every behaviour guarded here is something a user sees directly.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import core_downloader as cd
from telethon.tl import types
from telethon.utils import get_peer_id


class FakeDialog:
    """Minimal stand-in for ``telethon.tl.custom.dialog.Dialog``."""

    def __init__(self, entity):
        self.entity = entity
        self.id = get_peer_id(entity)
        self.title = getattr(entity, "title", str(self.id))


class FakeClient:
    """Records ``get_entity`` calls and serves a fixed dialog list."""

    def __init__(self, resolver, dialogs=(), archived_dialogs=(), me=None, connected=True):
        self._resolver = resolver
        self.dialogs = list(dialogs)
        self.archived_dialogs = list(archived_dialogs)
        self.me = me
        self.calls = []
        self.connected = connected
        self.connects = 0
        self.dialog_scans = 0

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connects += 1
        self.connected = True

    async def get_me(self):
        return self.me

    async def get_entity(self, value):
        self.calls.append(value)
        outcome = self._resolver(value)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def iter_dialogs(self, archived=False):
        self.dialog_scans += 1
        source = self.archived_dialogs if archived else self.dialogs

        async def gen():
            for dialog in source:
                yield dialog

        return gen()


class ChatIdInvalid(Exception):
    """Stands in for ``telethon.errors.rpcerrorlist.ChatIdInvalidError``."""


class TestNumericIdCandidates(unittest.TestCase):

    def test_properly_marked_channel_is_left_alone(self):
        self.assertEqual(cd._numeric_id_candidates("-1003889572674"), [-1003889572674])

    def test_bare_negative_id_tries_group_then_channel(self):
        self.assertEqual(
            cd._numeric_id_candidates("-3889572674"),
            [-3889572674, -1003889572674],
        )

    def test_raw_positive_channel_id_is_marked(self):
        self.assertEqual(
            cd._numeric_id_candidates("3889572674"),
            [-1003889572674, 3889572674],
        )

    def test_non_numeric_input_has_no_numeric_candidates(self):
        self.assertEqual(cd._numeric_id_candidates("@somechannel"), [])
        self.assertEqual(cd._numeric_id_candidates("https://t.me/+abcdef"), [])


class TestRpcDetailFormatting(unittest.TestCase):

    def test_long_rpc_text_is_flattened_and_bounded(self):
        exc = Exception("line one\\nline two " + "x" * 500)

        detail = cd._clean_rpc_detail(exc)

        self.assertNotIn("\\n", detail)
        self.assertTrue(detail.startswith("Exception: line one line two"))
        self.assertLessEqual(len(detail), 260)

    def test_chat_id_invalid_gets_a_plain_description(self):
        class ChatIdInvalidError(Exception):
            pass

        reason = cd.describe_download_error(ChatIdInvalidError("Invalid object ID for a chat"))

        self.assertIn("not a basic group", reason)


class TestFetchChannelResolution(unittest.TestCase):

    def setUp(self):
        self.logged = []
        self._orig_log_event = cd.log_event
        cd.log_event = lambda level, message: self.logged.append((level, message))

    def tearDown(self):
        cd.log_event = self._orig_log_event

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_group_id_resolves_directly(self):
        group = SimpleNamespace(id=3889572674, title="My Group", migrated_to=None)
        client = FakeClient(lambda value: group)

        resolved = self._run(cd.fetch_channel(client, "-3889572674"))

        self.assertIs(resolved, group)
        self.assertEqual(client.calls, [-3889572674])

    def test_channel_missing_the_marker_resolves_from_dialogs(self):
        """The screenshot scenario: -3889572674 is really the channel -1003889572674."""
        channel_peer = types.PeerChannel(3889572674)
        client = FakeClient(
            lambda value: ChatIdInvalid("The chat ID used is invalid")
            if value == -3889572674
            else ValueError("Could not find the input entity for PeerChannel"),
            dialogs=[FakeDialog(types.PeerChat(111)), FakeDialog(channel_peer)],
        )

        resolved = self._run(cd.fetch_channel(client, "-3889572674"))

        self.assertEqual(get_peer_id(resolved), -1003889572674)
        self.assertEqual(client.calls, [-3889572674, -1003889572674])

    def test_archived_dialogs_are_searched_too(self):
        channel_peer = types.PeerChannel(3889572674)
        client = FakeClient(
            lambda value: ValueError(f"no entity {value}"),
            dialogs=[FakeDialog(types.PeerChat(111))],
            archived_dialogs=[FakeDialog(channel_peer)],
        )

        resolved = self._run(cd.fetch_channel(client, "-3889572674"))

        self.assertEqual(get_peer_id(resolved), -1003889572674)

    def test_candidate_priority_beats_dialog_order(self):
        """A real basic group must win over a channel that happens to share the number."""
        group_peer = types.PeerChat(3889572674)
        channel_peer = types.PeerChannel(3889572674)
        client = FakeClient(
            lambda value: ValueError(f"no entity {value}"),
            dialogs=[FakeDialog(channel_peer), FakeDialog(group_peer)],
        )

        resolved = self._run(cd.fetch_channel(client, "-3889572674"))

        self.assertIs(resolved, group_peer)

    def test_username_is_passed_through_without_dialog_scan(self):
        target = SimpleNamespace(id=42, title="Channel", migrated_to=None)
        client = FakeClient(lambda value: target, dialogs=[FakeDialog(types.PeerChat(1))])

        resolved = self._run(cd.fetch_channel(client, "somechannel"))

        self.assertIs(resolved, target)
        self.assertEqual(client.calls, ["somechannel"])

    def test_migrated_group_follows_the_new_supergroup(self):
        migrated_to = types.InputChannel(3889572674, 999)
        old_group = SimpleNamespace(id=111, title="Old Group", migrated_to=migrated_to)
        upgraded = SimpleNamespace(id=3889572674, title="Upgraded Group", migrated_to=None)

        client = FakeClient(
            lambda value: upgraded if value is migrated_to else old_group
        )

        resolved = self._run(cd.fetch_channel(client, "-111"))

        self.assertIs(resolved, upgraded)
        self.assertEqual(client.calls, [-111, migrated_to])
        self.assertTrue(any("upgraded" in message for _level, message in self.logged))

    def test_forbidden_entity_is_reported_as_missing_membership(self):
        forbidden = type("ChannelForbidden", (), {"id": 3889572674})()
        target = SimpleNamespace(id=3889572674, title="Channel", migrated_to=None)
        client = FakeClient(
            lambda value: forbidden if value == -3889572674 else target
        )

        resolved = self._run(cd.fetch_channel(client, "-3889572674"))

        self.assertIs(resolved, target)
        self.assertEqual(client.calls, [-3889572674, -1003889572674])

    def test_error_keeps_the_underlying_telegram_reason(self):
        client = FakeClient(
            lambda value: ChatIdInvalid("The chat ID used is invalid")
            if value == -3889572674
            else ValueError("Could not find the input entity for PeerChannel")
        )

        with self.assertRaises(Exception) as ctx:
            self._run(cd.fetch_channel(client, "-3889572674"))

        message = str(ctx.exception)
        self.assertIn("Telegram refused access to -3889572674", message)
        self.assertIn("ChatIdInvalid", message)
        self.assertIn("The chat ID used is invalid", message)
        self.assertIn("-1003889572674 (channel)", message)
        self.assertIn("Scanned 0 chat(s)", message)
        # The old message contained literal "\n" text; it must be real newlines now.
        self.assertNotIn("\\n", message)

        self.assertEqual(len(self.logged), 1)
        level, logged_message = self.logged[0]
        self.assertEqual(level, "ERROR")
        self.assertIn("fetch_channel failed for -3889572674", logged_message)
        self.assertIn("ChatIdInvalid", logged_message)

    def test_error_names_the_signed_in_account(self):
        client = FakeClient(
            lambda value: ValueError(f"Could not find the input entity for {value}"),
            me=SimpleNamespace(first_name="Test", last_name="User", username="tester", id=42),
        )

        with self.assertRaises(Exception) as ctx:
            self._run(cd.fetch_channel(client, "-3889572674"))

        self.assertIn("Signed in as: Test User (@tester), id 42", str(ctx.exception))

    def test_missing_entity_after_the_scan_still_explains_itself(self):
        client = FakeClient(
            lambda value: ValueError(f"Could not find the input entity for {value}"),
            dialogs=[FakeDialog(types.PeerChat(7))],
        )

        with self.assertRaises(Exception) as ctx:
            self._run(cd.fetch_channel(client, "-3889572674"))

        message = str(ctx.exception)
        self.assertIn("Scanned 1 chat(s)", message)
        self.assertIn("invite link", message)

    def test_a_definite_no_access_answer_skips_the_dialog_scan(self):
        # Telegram itself said the account cannot see this channel, so scanning
        # every chat cannot find it; done every 5 minutes by a subscription to a
        # channel the account left, the scans add up to rate limits (HTTP 429).
        from telethon.errors import ChannelPrivateError
        client = FakeClient(
            lambda value: ChannelPrivateError(request=None),
            dialogs=[FakeDialog(types.PeerChat(7))],
        )

        with self.assertRaises(Exception) as ctx:
            self._run(cd.fetch_channel(client, "-1003889572674"))

        message = str(ctx.exception)
        self.assertIsInstance(ctx.exception, cd.ChannelUnavailableError)
        self.assertEqual(client.dialog_scans, 0)
        self.assertNotIn("Scanned", message)
        self.assertIn("No access to this channel", message)

    def test_a_dropped_connection_is_reopened_before_the_lookup(self):
        target = SimpleNamespace(id=4479181295, title="Channel", migrated_to=None)
        client = FakeClient(lambda value: target, connected=False)

        resolved = self._run(cd.fetch_channel(client, "-1004479181295"))

        self.assertIs(resolved, target)
        self.assertEqual(client.connects, 1)

    def test_a_connection_failure_is_not_reported_as_refused_access(self):
        client = FakeClient(
            lambda value: ConnectionError("Cannot send requests while disconnected"),
            dialogs=[FakeDialog(types.PeerChat(7))],
        )

        with self.assertRaises(Exception) as ctx:
            self._run(cd.fetch_channel(client, "-4479181295"))

        message = str(ctx.exception)
        self.assertNotIsInstance(ctx.exception, cd.ChannelUnavailableError,
                                 "a network failure says nothing about access")
        self.assertNotIn("refused access", message)
        self.assertIn("Could not reach Telegram", message)
        self.assertNotIn("Scanned", message)
        self.assertEqual(client.dialog_scans, 0)


if __name__ == "__main__":
    unittest.main()
