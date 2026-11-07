"""Subscribe mode: watch a channel and download its new posts.

A subscription remembers the newest post when it was created and, on every
check, downloads only posts after it. Checking must never disturb a task that
is already downloading the same channel and category.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import database
import subscriptions as subs


class Msg:
    def __init__(self, msg_id, media="media"):
        self.id = msg_id
        self.media = media
        self.document = None


class FakeClient:
    """get_messages returns posts newer than min_id, newest first, like Telegram."""

    def __init__(self, ids):
        self.ids = ids
        self.calls = []

    async def get_messages(self, channel, limit=None, min_id=None, **kwargs):
        self.calls.append({"limit": limit, "min_id": min_id, **kwargs})
        found = sorted((i for i in self.ids if not min_id or i > min_id), reverse=True)
        if limit:
            found = found[:limit]
        return [Msg(i) for i in found]


class Entity:
    id = 3889572674
    title = "K.27.1"


class DbTestCase(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir, "test.db")
        database.init_db()

        self.orig_fetch = subs.fetch_channel
        self.orig_peer = subs.get_peer_id

        async def fake_fetch(client, channel_input):
            return Entity()

        subs.fetch_channel = fake_fetch
        subs.get_peer_id = lambda entity: -1003889572674

    def tearDown(self):
        subs.fetch_channel = self.orig_fetch
        subs.get_peer_id = self.orig_peer
        database.DB_PATH = self.orig_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestSubscriptionStore(DbTestCase):

    def test_add_list_and_remove(self):
        database.save_subscription({
            "channel_input": "-1003889572674", "media_id": 2, "title": "K.27.1",
            "download_path": "G:/youtube", "last_msg_id": 900,
        })
        # Subscribing again updates the same subscription
        database.save_subscription({
            "channel_input": "-1003889572674", "media_id": 2, "title": "K.27.1 (renamed)",
            "download_path": "G:/youtube", "last_msg_id": 950,
        })

        rows = database.list_subscriptions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "K.27.1 (renamed)")
        self.assertEqual(rows[0]["last_msg_id"], 950)

        database.update_subscription_check(rows[0]["sub_id"], 990, "2026-11-05 13:00", "3 new posts")
        row = database.list_subscriptions()[0]
        self.assertEqual((row["last_msg_id"], row["last_result"]), (990, "3 new posts"))

        database.remove_subscription(row["sub_id"])
        self.assertEqual(database.list_subscriptions(), [])

    def test_categories_of_one_channel_are_separate(self):
        for media_id in (1, 2):
            database.save_subscription({"channel_input": "-100123", "media_id": media_id,
                                        "last_msg_id": 1})
        self.assertEqual(len(database.list_subscriptions()), 2)


class TestPlanBatch(unittest.TestCase):

    def test_no_new_posts(self):
        self.assertEqual(subs.plan_batch(None, []), ("none", []))

    def test_new_posts_start_a_task(self):
        self.assertEqual(subs.plan_batch(None, [5, 3]), ("start", [3, 5]))

    def test_new_posts_join_a_task_with_a_selection(self):
        task = {"selected_message_ids": [1, 3]}
        self.assertEqual(subs.plan_batch(task, [3, 5]), ("merge", [1, 3, 5]))

    def test_posts_already_queued_need_nothing(self):
        task = {"selected_message_ids": [3, 5]}
        self.assertEqual(subs.plan_batch(task, [5]), ("none", []))

    def test_a_whole_category_task_is_never_replaced(self):
        # start_download would overwrite its "everything" with just the new posts
        for selection in (None, []):
            task = {"selected_message_ids": selection}
            self.assertEqual(subs.plan_batch(task, [5]), ("defer", []))


class TestSubscribe(DbTestCase):

    def test_subscribing_remembers_the_newest_post_only(self):
        client = FakeClient([10, 11, 12])

        sub = asyncio.run(subs.create_subscription(client, "@k27", 2, "G:/youtube"))

        self.assertEqual(sub["last_msg_id"], 12)
        self.assertEqual(sub["channel_input"], "-1003889572674")
        self.assertEqual(sub["title"], "K.27.1")
        self.assertEqual(client.calls[-1]["limit"], 1)
        self.assertEqual(len(database.list_subscriptions()), 1)

    def test_a_topic_is_kept(self):
        client = FakeClient([10])

        sub = asyncio.run(subs.create_subscription(client, "-1003889572674_55", 6, "G:/x"))

        self.assertEqual(sub["channel_input"], "-1003889572674_55")


class TestCheckSubscription(DbTestCase):

    def _sub(self, last_msg_id=12, media_id=6):
        database.save_subscription({
            "channel_input": "-1003889572674", "media_id": media_id, "title": "K.27.1",
            "download_path": "G:/youtube", "last_msg_id": last_msg_id,
        })
        return database.list_subscriptions()[0]

    def _check(self, client, active_tasks, running=False):
        started = []

        def start_download(channel_input, media_id, download_path, selected_message_ids):
            started.append((channel_input, media_id, download_path, selected_message_ids))

        result = asyncio.run(subs.check_subscription(
            client, self._current, active_tasks, start_download,
            is_running=lambda channel, media_id, topic_id: running))
        return result, started, database.list_subscriptions()[0]

    def test_new_posts_are_downloaded_and_the_checkpoint_moves(self):
        self._current = self._sub(last_msg_id=12)
        client = FakeClient([10, 11, 12, 13, 14])

        result, started, row = self._check(client, [])

        self.assertEqual(client.calls[-1]["min_id"], 12)
        self.assertEqual(started, [("-1003889572674", 6, "G:/youtube", [13, 14])])
        self.assertEqual(row["last_msg_id"], 14)
        self.assertIn("2 new", row["last_result"])
        self.assertEqual(result["action"], "start")

    def test_nothing_new_changes_nothing_but_the_check_time(self):
        self._current = self._sub(last_msg_id=12)

        result, started, row = self._check(FakeClient([10, 11, 12]), [])

        self.assertEqual(started, [])
        self.assertEqual(row["last_msg_id"], 12)
        self.assertTrue(row["last_checked"])

    def test_a_running_whole_category_task_defers_without_losing_posts(self):
        self._current = self._sub(last_msg_id=12)
        running = [{"channel_input": "3889572674", "media_id": 6, "topic_id": None,
                    "selected_message_ids": None}]

        result, started, row = self._check(FakeClient([13]), running)

        self.assertEqual(started, [])
        self.assertEqual(row["last_msg_id"], 12, "post 13 must be found again next time")
        self.assertEqual(result["action"], "defer")

    def test_new_posts_join_an_earlier_batch_still_downloading(self):
        self._current = self._sub(last_msg_id=13)
        running = [{"channel_input": "-1003889572674", "media_id": 6, "topic_id": None,
                    "selected_message_ids": [13]}]

        result, started, row = self._check(FakeClient([13, 14]), running)

        self.assertEqual(started, [("-1003889572674", 6, "G:/youtube", [13, 14])])
        self.assertEqual(row["last_msg_id"], 14)

    def test_a_downloading_task_is_never_restarted(self):
        # Merging restarted the live task, cancelling its in-flight files on
        # every check; each v2.9.7 crash followed such a restart.
        self._current = self._sub(last_msg_id=13)
        running = [{"channel_input": "-1003889572674", "media_id": 6, "topic_id": None,
                    "selected_message_ids": [13]}]

        result, started, row = self._check(FakeClient([13, 14]), running, running=True)

        self.assertEqual(started, [])
        self.assertEqual(result["action"], "defer")
        self.assertEqual(row["last_msg_id"], 13, "post 14 must be found again next time")

    def test_a_task_of_another_category_is_left_alone(self):
        self._current = self._sub(last_msg_id=12, media_id=2)
        other = [{"channel_input": "3889572674", "media_id": 6, "topic_id": None,
                  "selected_message_ids": None}]

        result, started, row = self._check(FakeClient([13]), other)

        self.assertEqual(result["action"], "start")
        self.assertEqual(len(started), 1)

    def test_an_error_is_recorded_not_raised(self):
        self._current = self._sub(last_msg_id=12)

        async def broken_fetch(client, channel_input):
            raise ValueError("channel is private")

        subs.fetch_channel = broken_fetch
        result, started, row = self._check(FakeClient([13]), [])

        self.assertEqual(result["action"], "error")
        self.assertIn("channel is private", row["last_result"])
        self.assertEqual(row["last_msg_id"], 12)


class TestSubscriptionsList(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_rows_show_each_subscription_and_unsubscribe(self):
        from ui.views.downloads_view import DownloadsView

        view = DownloadsView()
        view.set_subscriptions([
            {"sub_id": "a", "title": "K.27.1", "media_id": 2, "last_checked": "2026-11-05 13:00",
             "last_result": "2 new posts"},
            {"sub_id": "b", "title": "News", "media_id": 6, "last_checked": None,
             "last_result": None},
        ])
        removed = []
        view.unsubscribeRequested.connect(removed.append)

        self.assertFalse(view.subscriptions_container.isHidden())
        rows = view.subscription_rows
        self.assertEqual(len(rows), 2)
        self.assertIn("K.27.1", rows["a"].lbl_title.text())
        self.assertIn("Videos", rows["a"].lbl_title.text())
        self.assertIn("2 new posts", rows["a"].lbl_status.text())
        self.assertIn("not checked yet", rows["b"].lbl_status.text().lower())

        rows["a"].btn_unsubscribe.click()
        self.assertEqual(removed, ["a"])

        view.set_subscriptions([])
        self.assertTrue(view.subscriptions_container.isHidden())


if __name__ == "__main__":
    unittest.main()
