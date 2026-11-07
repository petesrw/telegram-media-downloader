"""Subscribe mode: watch a channel and download each new post.

A subscription stores the newest post ID handled so far. Each check asks
Telegram only for posts after it (``min_id``) in the subscribed category and
hands them to an ordinary download task, so retries, resume and the card UI
all work as for any other task.
"""

from datetime import datetime

from telethon.utils import get_peer_id

import database
from core_downloader import fetch_channel, get_messages_by_type, parse_channel_input
from utils.app_logger import log_event

MEDIA_LABELS = {
    1: "Images",
    2: "Videos",
    3: "Files",
    4: "ZIPs",
    5: "Audio",
    6: "All Media",
}


def plan_batch(existing_task, new_ids):
    """Decide how new posts reach a download task.

    Returns (action, message_ids):
      "none"  - nothing to queue
      "start" - start a task with these posts
      "merge" - the channel already has a task with a file selection; queue
                the union so earlier posts keep downloading
      "defer" - a whole-category task is running; starting a task now would
                replace its "everything" with just the new posts, so wait
    """
    new_ids = sorted(set(new_ids))
    if not new_ids:
        return "none", []
    if existing_task is None:
        return "start", new_ids
    selection = existing_task.get("selected_message_ids")
    if not selection:
        return "defer", []
    queued = set(selection)
    if queued.issuperset(new_ids):
        return "none", []
    return "merge", sorted(queued | set(new_ids))


def find_task(active_tasks, channel_input, media_id, topic_id):
    """The saved task for this channel, topic and category, if any."""
    key = database.normalize_channel_key(channel_input)
    for task in active_tasks or []:
        if not isinstance(task, dict):
            continue
        if (database.normalize_channel_key(task.get("channel_input", "")) == key
                and task.get("media_id") == media_id
                and task.get("topic_id") == topic_id):
            return task
    return None


async def find_new_message_ids(client, channel, media_id, after_id, topic_id=None):
    """IDs of posts in the category newer than `after_id`, oldest first."""
    messages = await get_messages_by_type(
        client, channel, media_id, min_id=after_id or None, limit=None, topic_id=topic_id
    )
    return sorted(m.id for m in messages if m.id > (after_id or 0))


def _channel_title(entity, fallback):
    return str(getattr(entity, "title", None) or getattr(entity, "username", None) or fallback)


async def create_subscription(client, channel_input, media_id, download_path):
    """Subscribe from now on: only posts after the current newest one download."""
    clean, topic_id = parse_channel_input(channel_input)
    entity = await fetch_channel(client, clean)
    latest = await client.get_messages(entity, limit=1)
    last_msg_id = latest[0].id if latest else 0

    resolved = str(get_peer_id(entity))
    if topic_id:
        resolved = f"{resolved}_{topic_id}"
    sub = {
        "channel_input": resolved,
        "media_id": int(media_id),
        "title": _channel_title(entity, clean),
        "download_path": download_path,
        "last_msg_id": last_msg_id,
    }
    sub["sub_id"] = database.save_subscription(sub)
    log_event(
        "INFO",
        f"subscribed to {sub['title']} ({MEDIA_LABELS.get(int(media_id), media_id)}); "
        f"posts after #{last_msg_id} will download",
    )
    return sub


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


async def check_subscription(client, sub, active_tasks, start_download, is_running=None):
    """Queue the new posts of one subscription and record the result.

    `start_download(channel_input, media_id, download_path, selected_message_ids)`
    starts or updates the download task. `is_running(channel, media_id, topic_id)`
    says whether that channel's task is downloading right now: start_download
    restarts a running task, cancelling its in-flight files, so new posts wait
    for it to finish instead. Errors are recorded on the subscription instead
    of raised, so one broken channel does not stop the rest.
    """
    sub_id = sub["sub_id"]
    last_msg_id = int(sub.get("last_msg_id") or 0)
    media_id = int(sub["media_id"])
    title = sub.get("title") or sub["channel_input"]
    try:
        clean, topic_id = parse_channel_input(sub["channel_input"])
        entity = await fetch_channel(client, clean)
        new_ids = await find_new_message_ids(client, entity, media_id, last_msg_id, topic_id)
        action, ids = plan_batch(find_task(active_tasks, clean, media_id, topic_id), new_ids)
        if action == "merge" and is_running and is_running(clean, media_id, topic_id):
            action, ids = "defer", []
    except Exception as e:
        message = f"Check failed: {e}"
        database.update_subscription_check(sub_id, last_msg_id, _now(), message)
        log_event("WARNING", f"[subscription {title}] {message}")
        return {"action": "error", "new_ids": [], "last_msg_id": last_msg_id}

    if action in ("start", "merge"):
        start_download(sub["channel_input"], media_id, sub.get("download_path"), ids)
        last_msg_id = max(new_ids)
        message = f"{len(new_ids)} new post(s) queued"
        log_event("INFO", f"[subscription {title}] {message}")
    elif action == "defer":
        message = (f"{len(new_ids)} new post(s) wait until the current download "
                   f"of this channel finishes")
        log_event("INFO", f"[subscription {title}] {message}")
    elif new_ids:
        last_msg_id = max(new_ids)
        message = "New posts are already queued"
    else:
        message = "No new posts"

    database.update_subscription_check(sub_id, last_msg_id, _now(), message)
    return {"action": action, "new_ids": new_ids, "last_msg_id": last_msg_id}
