import asyncio
import os
import sys
import json
import traceback
from telethon import TelegramClient
from telethon.utils import get_peer_id
from telethon.tl.types import (
    InputMessagesFilterPhotos,
    InputMessagesFilterVideo,
    InputMessagesFilterDocument,
    InputMessagesFilterMusic,
    InputMessagesFilterUrl,
    InputMessagesFilterGif,
    InputMessagesFilterVoice,
    InputMessagesFilterRoundVideo,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaPaidMedia,
    MessageExtendedMedia,
    MessageExtendedMediaPreview
)
from database import save_task_db, load_active_tasks_db, remove_task_db, cache_media_list, mark_media_completed, get_completed_state_db
from utils.file_utils import get_media_filename
from utils.fast_telethon import (
    fast_download_file, _atomic_finalize_sync, GLOBAL_FLOODGATE,
    CONNECTION_DROPPED, cancelled_on_purpose, reconnect_client,
)
from utils.app_logger import log_event, describe_download_error, DELETED_ON_TELEGRAM, is_deleted_on_telegram

# Watchdog defaults (overridable in Settings). A download that produces no
# progress for STALL_WARN seconds is reported as stalled; one that stays idle
# for STALL_ABORT seconds is cancelled so it stops holding a concurrency slot.
DEFAULT_STALL_WARN_SECONDS = 45
DEFAULT_STALL_ABORT_SECONDS = 180
STALL_POLL_SECONDS = 5.0

# The single-stream fallback writes here; only a complete file is renamed into
# place, so a failed or cut-off download never sits under the real name.
INCOMPLETE_SUFFIX = ".dl"


class IncompleteDownloadError(Exception):
    """Telegram stopped sending data before the whole file arrived."""


def _discard(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            log_event("WARNING", f"could not remove incomplete file {os.path.basename(path)}: {e}")


def _accept_download(file_path, expected_filepath, incomplete_path, file_size):
    """Return the final path of a finished download, or raise when it is short."""
    got = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    if file_size and got < file_size:
        if file_path == incomplete_path:
            _discard(incomplete_path)
        raise IncompleteDownloadError(
            f"Download incomplete - received {got} of {file_size} bytes"
        )
    if incomplete_path and file_path == incomplete_path:
        os.replace(incomplete_path, expected_filepath)
        return expected_filepath
    return file_path


def crypto_backend_warning():
    """Return a warning when Telethon must decrypt downloads in pure Python.

    Without cryptg every byte is decrypted by Python code, which caps a
    download at ~500 KB/s and holds the GIL so long that the UI freezes.
    """
    try:
        import telethon.crypto.aes as telethon_aes
    except Exception:
        return None
    if getattr(telethon_aes, "cryptg", None) is not None:
        return None
    return (
        "cryptg is not installed - Telethon decrypts downloads in pure Python, "
        "so downloads are slow (~500 KB/s) and the window may stop responding. "
        "Install the pinned version from requirements.txt and rebuild."
    )


def _describe_media(message):
    """Short "media: <type>[, mime, size]" note for failures nobody can retry away."""
    media = getattr(message, 'media', None)
    if media is None:
        return "media: none"
    parts = [type(media).__name__]
    document = getattr(media, 'document', None)
    if document is not None:
        mime = getattr(document, 'mime_type', None)
        if mime:
            parts.append(mime)
        size = getattr(document, 'size', None)
        if size:
            parts.append(_fmt_bytes(size))
    return "media: " + ", ".join(parts)

def _fmt_bytes(size):
    try:
        size = float(size or 0)
    except (TypeError, ValueError):
        size = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def _swallow_task_result(task):
    """Consume a background task's result so Python does not warn on GC."""
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass

async def _stall_watchdog(message_id, last_activity, stall_phase, warned, abort_reason,
                          stop_event, download_task, warn_seconds, abort_seconds, stall_cb,
                          poll_seconds=STALL_POLL_SECONDS):
    """Report (and eventually cancel) a download that stopped making progress."""
    last_rate_limit_note = [None]
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            return
        except asyncio.TimeoutError:
            pass

        # A FloodWait is a deliberate pause, not a stall: while the shared gate
        # is open the download is healthy, so never abort it - report the wait
        # instead so the card explains the 0% rather than looking hung.
        remaining = GLOBAL_FLOODGATE.remaining()
        if remaining > 0:
            last_activity[0] = time.time()
            minutes = max(1, int((remaining + 59) // 60))
            if last_rate_limit_note[0] != minutes:
                last_rate_limit_note[0] = minutes
                phase = f"Telegram rate limit - resuming in ~{minutes}m"
                stall_phase[0] = phase
                if stall_cb:
                    try:
                        stall_cb(message_id, 0, phase)
                    except Exception:
                        pass
            continue

        idle = time.time() - last_activity[0]
        if warn_seconds and idle >= warn_seconds and not warned[0]:
            warned[0] = True
            phase = stall_phase[0]
            log_event(
                "WARNING",
                f"message {message_id}: no progress for {int(idle)}s - {phase}",
            )
            if stall_cb:
                try:
                    stall_cb(message_id, int(idle), phase)
                except Exception:
                    pass

        if abort_seconds and idle >= abort_seconds:
            reason = f"Stalled - no progress for {int(idle)}s while {stall_phase[0]}"
            abort_reason[0] = reason
            log_event("ERROR", f"message {message_id}: {reason}")
            if download_task is not None and not download_task.done():
                download_task.cancel()
            return

def load_active_tasks():
    return load_active_tasks_db()

def save_active_tasks(tasks):
    # Sync with SQLite by first deleting all tasks in the DB
    try:
        import sqlite3
        from database import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error syncing active tasks to DB: {e}")

    # For backward compatibility, keep the loop but save each to DB
    for t in tasks:
        save_task_db(t)

def load_download_state(channel_id=None):
    # We return a set of msg_ids for a specific channel to ensure ID isolation
    if not channel_id:
        return set()
    res = get_completed_state_db()
    c_id = str(channel_id).replace("-100", "", 1)
    return {msg_id for ch_id, msg_id in res if ch_id == c_id}

def save_download_state(state):
    # This is usually called file-by-file in the worker via complete_cb
    # but if called globally, we can't easily map to channels here.
    # We recommend using mark_media_completed instead.
    pass

def parse_channel_input(channel_input):
    """
    Parses channel input which might contain a topic ID in 'channelID_topicID' format
    or a Telegram URL with a topic ID (e.g., t.me/c/123456789/1).
    Returns (clean_channel_input, topic_id).
    """
    s = str(channel_input).strip()
    
    # 0. Handle web.telegram.org format (Web A, Web K, Web Z formats)
    if "web.telegram.org" in s:
        try:
            if "#" in s:
                fragment = s.split("#")[-1]
                # Extract value from query param p= if present (common in Web K/Z)
                if "p=" in fragment:
                    p_val = fragment.split("p=")[-1].split("&")[0]
                    fragment = p_val
                
                # Strip leading 'c' if it's followed by digits (e.g., c123456789 -> 123456789)
                if fragment.startswith("c"):
                    cleaned_part = fragment[1:].replace("-", "").split("_")[0]
                    if cleaned_part.isdigit():
                        fragment = fragment[1:]
                
                # If there's a topic/message ID separator '_'
                if "_" in fragment:
                    parts = fragment.split("_")
                    if len(parts) >= 2 and parts[1].isdigit():
                        return parts[0], int(parts[1])
                s = fragment
        except: pass

    # 1. Handle URL format: https://t.me/c/123456789/1
    if "t.me/c/" in s:
        try:
            # Extract the part after /c/
            parts = s.split("t.me/c/")[-1].split("/")
            if len(parts) >= 2:
                chan_id = parts[0]
                topic_id = parts[1]
                if topic_id.isdigit():
                    # Prefix with -100 if it's a numeric ID
                    if chan_id.isdigit() and not chan_id.startswith("-"):
                        chan_id = f"-100{chan_id}"
                    return chan_id, int(topic_id)
        except: pass

    # 2. Handle ID_topicID format: -100123456789_123
    if "_" in s:
        parts = s.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            # Basic validation that parts[0] looks like a channel ID or username
            return parts[0], int(parts[1])
            
    return s, None

def _numeric_id_candidates(raw):
    """Return the peer IDs worth trying for a purely numeric channel input.

    Telegram keeps users (positive), basic groups (``-chat_id``) and
    channels/supergroups (``-100channel_id``) in separate numeric ranges.
    A pasted ID regularly loses its ``-100`` marker, and Telethon resolves a
    bare negative number only as a *basic group*, so the other interpretation
    has to be tried as well instead of giving up on the first miss.
    """
    raw = str(raw).strip()
    negative = raw.startswith("-")
    digits = raw[1:] if negative else raw
    if not digits.isdigit():
        return []

    if negative:
        candidates = [int(f"-{digits}")]
        if not (digits.startswith("100") and len(digits) >= 12):
            # A channel with the same raw ID is addressed as -100<id>
            candidates.append(int(f"-100{digits}"))
    else:
        candidates = []
        if len(digits) >= 8:
            # A raw channel ID is addressed as -100<id>
            candidates.append(int(f"-100{digits}"))
        candidates.append(int(digits))

    ordered = []
    for value in candidates:
        if value not in ordered:
            ordered.append(value)
    return ordered


def _describe_peer_id(value):
    """Human-readable label for a numeric peer ID, used in logs and errors."""
    text = str(value)
    if text.startswith("-100"):
        return f"{text} (channel)"
    if text.startswith("-"):
        return f"{text} (basic group)"
    return f"{text} (user or unmarked chat)"


def _clean_rpc_detail(exc, limit=240):
    """One-line, bounded rendering of a Telegram exception for the UI.

    Telethon's server messages contain documentation snippets (and sometimes
    literal ``\\n`` sequences), which are unreadable inside a QMessageBox.
    """
    text = " ".join(str(exc).replace("\\n", " ").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return f"{type(exc).__name__}: {text}"


def _inaccessible_reason(entity):
    """Explain a placeholder entity Telegram returns instead of raising."""
    kind = type(entity).__name__
    if kind == "ChatEmpty":
        return "Telegram returned an empty chat - the group no longer exists"
    if kind == "ChatForbidden":
        return "the account is not a participant of this group"
    if kind == "ChannelForbidden":
        return "the account is not a participant of this channel"
    return None


async def _follow_migration(client, entity):
    """Follow ``Chat.migrated_to`` after a basic group became a supergroup.

    The old chat ID keeps existing but no longer carries the messages, so
    returning it unchanged makes every later call fail on a dead peer.
    """
    target = getattr(entity, "migrated_to", None)
    if not target:
        return entity
    try:
        upgraded = await client.get_entity(target)
    except Exception as e:
        log_event(
            "WARNING",
            f"group {getattr(entity, 'id', '?')} was upgraded, but the new "
            f"supergroup could not be resolved: {describe_download_error(e)} ({e})",
        )
        return entity
    log_event(
        "INFO",
        f"group {getattr(entity, 'id', '?')} was upgraded to supergroup "
        f"{getattr(upgraded, 'id', '?')}; using the supergroup",
    )
    return upgraded


async def _find_in_dialogs(client, candidate_ids):
    """Scan active then archived dialogs for any of ``candidate_ids``.

    Returns ``(entity, scanned)``. The entity is chosen in candidate priority
    order, not in the order the scan happened to see the dialogs.
    """
    if not candidate_ids:
        return None, 0

    wanted = {int(value) for value in candidate_ids}
    matches = {}
    scanned = 0

    for archived in (False, True):
        if len(matches) == len(wanted):
            break
        async for dialog in client.iter_dialogs(archived=archived):
            scanned += 1
            entity = dialog.entity
            try:
                marked = get_peer_id(entity)
                raw = get_peer_id(entity, add_mark=False)
            except Exception:
                continue
            for value in (marked, raw):
                if value in wanted and value not in matches:
                    matches[value] = entity

    for value in candidate_ids:
        if value in matches:
            print(f"DEBUG: Found {_describe_peer_id(value)} among {scanned} dialogs")
            return matches[value], scanned
    return None, scanned


async def fetch_channel(client, channel_input):
    """Fetch a channel by username or ID.

    Numeric IDs are ambiguous (users, basic groups and channels all use plain
    numbers), so every plausible interpretation is tried before failing.
    """
    original_input = str(channel_input).strip()
    candidate_ids = _numeric_id_candidates(original_input)
    attempts = candidate_ids or [original_input]

    errors = []
    for candidate in attempts:
        try:
            entity = await client.get_entity(candidate)
        except Exception as e:
            errors.append((candidate, e))
            print(f"DEBUG: get_entity({_describe_peer_id(candidate)}) failed: {type(e).__name__}: {e}")
            continue

        reason = _inaccessible_reason(entity)
        if reason:
            errors.append((candidate, RuntimeError(reason)))
            print(f"DEBUG: get_entity({_describe_peer_id(candidate)}) returned {type(entity).__name__}: {reason}")
            continue

        entity = await _follow_migration(client, entity)
        title = getattr(entity, 'title', getattr(entity, 'username', getattr(entity, 'first_name', 'Unknown')))
        print(f"DEBUG: Successfully resolved channel/entity: '{title}' (ID: {getattr(entity, 'id', '?')})")
        return entity

    # Fallback: the entity may still be in this account's dialog list even when
    # Telethon cannot resolve the bare ID from its session cache.
    entity = None
    scanned = 0
    if candidate_ids:
        print(f"Direct lookup for {original_input} failed. Searching through dialogs... this may take a moment.")
        try:
            entity, scanned = await _find_in_dialogs(client, candidate_ids)
        except Exception as dialog_err:
            print(f"Dialog search also failed: {dialog_err}")
            log_event(
                "WARNING",
                f"dialog scan failed while resolving {original_input}: "
                f"{type(dialog_err).__name__}: {dialog_err}",
            )

    if entity is not None:
        return await _follow_migration(client, entity)

    # Keep the real Telegram error in the message: a generic "access denied"
    # leaves the user with no idea whether the ID is wrong, private, or banned.
    primary = errors[0] if errors else (original_input, RuntimeError("Telegram returned no entity"))
    detail = describe_download_error(primary[1])
    raw_error = _clean_rpc_detail(primary[1])
    error_msg = f"Telegram refused access to {original_input}."

    # Name the account in use: a valid chat ID that resolves nowhere is usually
    # either the wrong account or a chat that account is not a member of.
    try:
        me = await client.get_me()
    except Exception:
        me = None
    if me is not None:
        name = " ".join(
            part for part in (getattr(me, "first_name", ""), getattr(me, "last_name", ""))
            if part
        ).strip()
        account = name or "unknown name"
        username = getattr(me, "username", None)
        if username:
            account += f" (@{username})"
        account += f", id {getattr(me, 'id', '?')}"
        error_msg += f"\nSigned in as: {account}"

    error_msg += f"\n\nReason: {detail}"
    if detail != raw_error:
        error_msg += f"\nTelegram said: {raw_error}"
    if len(errors) > 1:
        also = "; ".join(
            f"{_describe_peer_id(value)} -> {describe_download_error(err)}"
            for value, err in errors[1:]
        )
        error_msg += f"\n\nAlso tried: {also}"
    if candidate_ids:
        error_msg += f"\n\nScanned {scanned} chat(s) on this account; none of these IDs matched."
    error_msg += (
        "\n\nTo fix this:\n"
        "1. Open the chat on your phone so it moves to the top of your chat list, then retry.\n"
        "2. Check that you are logged into the Telegram account that is a member of this chat.\n"
        "3. Or paste the invite link (https://t.me/...) instead of the numeric ID."
    )

    log_event(
        "ERROR",
        f"fetch_channel failed for {original_input}: "
        + "; ".join(
            f"{value} -> {describe_download_error(err)} ({type(err).__name__}: {err})"
            for value, err in errors
        ),
    )
    raise Exception(error_msg) # Re-raise with the helpful tip
import time

def get_unique_filepath(folder, filename, reserved_paths=None):
    os.makedirs(folder, exist_ok=True)
    base, ext = os.path.splitext(filename)
    counter = 1
    new_filename = filename
    new_filepath = os.path.join(folder, new_filename)

    def is_occupied(path):
        # 1. Check in-memory reservation for concurrent downloads
        if reserved_paths is not None and (path in reserved_paths or (path + ".part") in reserved_paths):
            return True
        # 2. Check if file exists on disk with content > 0 bytes
        if os.path.exists(path):
            try:
                if os.path.getsize(path) > 0:
                    return True
            except Exception:
                return True
        # 3. Check if active/in-progress .part file exists on disk with content > 0 bytes
        if os.path.exists(path + ".part"):
            try:
                if os.path.getsize(path + ".part") > 0:
                    return True
            except Exception:
                return True
        return False

    while is_occupied(new_filepath):
        counter += 1
        new_filename = f"{base} ({counter}){ext}"
        new_filepath = os.path.join(folder, new_filename)

    if reserved_paths is not None:
        reserved_paths.add(new_filepath)

    return new_filepath

def _is_file_reference_error(exc):
    name = type(exc).__name__
    return "FileReference" in name or "FilerefUpgrade" in name


def _has_saved_chunks(filepath, file_size):
    """True when the parallel engine left resumable chunks for `filepath`."""
    from utils.fast_telethon import CHUNK_SIZE, _load_meta
    import math

    if not filepath or not file_size:
        return False
    total_parts = math.ceil(file_size / CHUNK_SIZE)
    return bool(_load_meta(filepath + ".part.meta", file_size, total_parts))


async def _refetch_message(client, channel, message):
    """Re-fetches `message` for a fresh file reference.

    Returns (fresh_message, gone_reason). gone_reason is set when Telegram no
    longer serves the media at all - the message was deleted, or its media was
    removed - since no retry can download it then. A lookup error returns
    (None, None) so the normal retry path decides.
    """
    try:
        fresh = await client.get_messages(channel, ids=message.id)
    except Exception as e:
        log_event("WARNING", f"message {message.id}: could not re-fetch - {describe_download_error(e)}")
        return None, None
    if fresh is None:
        return None, f"{DELETED_ON_TELEGRAM} - the message was deleted from the chat"
    if getattr(fresh, "media", None) is None:
        return None, f"{DELETED_ON_TELEGRAM} - the media was removed from the post"
    return fresh, None


async def download_single_file(client, channel, message, folder_name, progress_cb=None, complete_cb=None, cancel_event=None, max_speed_kb=None, reserved_paths=None, stall_cb=None):
    from ui.views.settings_view import load_config
    cfg = load_config()
    rename_duplicates = cfg.get("rename_duplicates", True)
    use_message_date = cfg.get("use_message_date", True)
    prefix_file_date = cfg.get("prefix_file_date", True)
    try:
        stall_warn = float(cfg.get("stall_warn_seconds", DEFAULT_STALL_WARN_SECONDS))
        stall_abort = float(cfg.get("stall_abort_seconds", DEFAULT_STALL_ABORT_SECONDS))
    except (TypeError, ValueError):
        stall_warn = DEFAULT_STALL_WARN_SECONDS
        stall_abort = DEFAULT_STALL_ABORT_SECONDS
    if stall_abort <= 0:
        stall_abort = None
    if stall_warn < 0:
        stall_warn = DEFAULT_STALL_WARN_SECONDS

    # Shared stall-tracking state (containers so the nested callback can mutate)
    last_activity = [time.time()]
    stall_phase = ["waiting for the first response from Telegram"]
    stall_warned = [False]
    abort_reason = [None]

    def fail(msg_id, reason):
        """Report a failure together with the reason it happened."""
        log_event("WARNING" if is_deleted_on_telegram(reason) else "ERROR", f"message {msg_id}: {reason}")
        if complete_cb:
            complete_cb(msg_id, paused=False, filepath=None, error=True, reason=reason)

    if message is not None and getattr(message, "media", None) is None:
        # The post no longer carries media (removed or edited away); no retry
        # or fallback can download it, so say so instead of "no file".
        fail(message.id, f"{DELETED_ON_TELEGRAM} - the media was removed from the post")
        return

    max_retries = 3
    incomplete_path = None
    for attempt in range(max_retries):
        _discard(incomplete_path)
        incomplete_path = None
        last_activity[0] = time.time()
        stall_phase[0] = "waiting for the first response from Telegram"
        stall_warned[0] = False
        abort_reason[0] = None

        watchdog_stop = asyncio.Event()
        watchdog_task = None
        if (stall_warn or stall_abort) and message is not None:
            watchdog_task = asyncio.create_task(
                _stall_watchdog(
                    message.id, last_activity, stall_phase, stall_warned, abort_reason,
                    watchdog_stop, asyncio.current_task(), stall_warn, stall_abort, stall_cb,
                )
            )
            watchdog_task.add_done_callback(_swallow_task_result)
        try:
            # Speed tracking variables & cancellation signal
            start_time = [time.time()]
            last_bytes = [0]
            is_first_cb = [True]
            smoothed_speed = [0.0]
            
            class PauseRequested(Exception): pass
            
            async def internal_progress(current, total):
                if cancel_event and cancel_event.is_set():
                    raise PauseRequested()

                # Any callback means data is flowing again - reset the watchdog
                last_activity[0] = time.time()
                
                tot = total or getattr(message, 'size', 0) or 0
                if is_first_cb[0]:
                    is_first_cb[0] = False
                    last_bytes[0] = current
                    start_time[0] = time.time()
                    if current > 0:
                        # Resume offset read from disk, not data from Telegram
                        stall_phase[0] = (
                            f"resumed at {_fmt_bytes(current)}, waiting for more data from Telegram"
                        )
                    if progress_cb:
                        initial_status = "Resuming..." if current > 0 else "Starting..."
                        progress_cb(message.id, current, tot, speed_str=initial_status)
                    return

                now = time.time()
                elapsed = now - start_time[0]
                if elapsed >= 0.2:
                    bytes_diff = current - last_bytes[0]
                    instant_speed_kb_s = (bytes_diff / elapsed) / 1024 if elapsed > 0 else 0.0
                    
                    if max_speed_kb and instant_speed_kb_s > max_speed_kb:
                        expected_time = (bytes_diff / 1024) / max_speed_kb
                        sleep_time = expected_time - elapsed
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                            now = time.time()
                            elapsed = now - start_time[0]
                            instant_speed_kb_s = (bytes_diff / elapsed) / 1024 if elapsed > 0 else 0.0

                    # Exponential Moving Average for silky smooth speed output
                    if smoothed_speed[0] <= 0.0:
                        smoothed_speed[0] = instant_speed_kb_s
                    else:
                        smoothed_speed[0] = 0.7 * smoothed_speed[0] + 0.3 * instant_speed_kb_s

                    speed_val = smoothed_speed[0]
                    speed_str = f"{(speed_val/1024):.1f} MB/s" if speed_val > 1024 else f"{int(speed_val)} KB/s"
                    start_time[0] = now
                    last_bytes[0] = current
                    stall_phase[0] = (
                        f"transferred {_fmt_bytes(current)} of {_fmt_bytes(tot)}"
                        if tot else f"transferred {_fmt_bytes(current)}"
                    )
                    if progress_cb:
                        progress_cb(message.id, current, tot, speed_str=speed_str)

            # 🌟 Special handling for Telegram Stars Paid Media (MessageMediaPaidMedia)
            msg_media = getattr(message, 'media', None)
            if isinstance(msg_media, MessageMediaPaidMedia):
                ext_list = getattr(msg_media, 'extended_media', [])
                unlocked_items = [item for item in ext_list if isinstance(item, MessageExtendedMedia)]
                if not unlocked_items:
                    stars = getattr(msg_media, 'stars_amount', 0)
                    log_event(
                        "INFO",
                        f"message {message.id}: skipped - Star paid media "
                        f"({stars} Stars) is not purchased on this account",
                    )
                    if progress_cb:
                        progress_cb(message.id, 0, 0, speed_str=f"Skipped ({stars}⭐ Unpaid)")
                    if complete_cb:
                        complete_cb(message.id, filepath=None)
                    return

                # Download unlocked items in paid media album
                last_filepath = None
                total_unlocked = len(unlocked_items)
                from telethon.utils import get_peer_id
                from database import get_media_downloaded_path, update_media_downloaded_path
                c_id = str(get_peer_id(channel)).replace("-100", "", 1)

                for sub_idx, ext_item in enumerate(unlocked_items, start=1):
                    if cancel_event and cancel_event.is_set():
                        if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                        return

                    actual_media = ext_item.media
                    sub_size = 0
                    if getattr(actual_media, 'document', None):
                        sub_size = getattr(actual_media.document, 'size', 0)
                    elif getattr(actual_media, 'photo', None) and getattr(actual_media.photo, 'sizes', None):
                        for s in reversed(actual_media.photo.sizes):
                            if hasattr(s, 'size'):
                                sub_size = s.size
                                break

                    sub_num = sub_idx if total_unlocked > 1 else None
                    item_fname = get_media_filename(
                        message,
                        prefix_date=prefix_file_date,
                        channel=channel,
                        target_media=actual_media,
                        sub_index=sub_num
                    )

                    item_path = os.path.join(folder_name, item_fname)
                    if rename_duplicates:
                        item_path = get_unique_filepath(folder_name, item_fname, reserved_paths=reserved_paths)
                    elif reserved_paths is not None:
                        reserved_paths.add(item_path)

                    if os.path.exists(item_path) and sub_size and os.path.getsize(item_path) >= sub_size:
                        last_filepath = item_path
                        continue

                    # Download with progress
                    downloaded_file = None
                    if sub_size > 1024 * 1024:
                        try:
                            ok = await fast_download_file(
                                client=client,
                                location=actual_media,
                                target_path=item_path,
                                file_size=sub_size,
                                progress_callback=internal_progress,
                                cancel_event=cancel_event,
                                workers=4
                            )
                            if ok and os.path.exists(item_path):
                                downloaded_file = item_path
                        except PauseRequested:
                            if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                            return
                        except Exception as fast_err:
                            print(f"Fast download fallback for Star media {message.id}: {fast_err}")

                    if not downloaded_file:
                        try:
                            await GLOBAL_FLOODGATE.wait_if_gated(cancel_event)
                            downloaded_file = await client.download_media(
                                actual_media,
                                file=item_path,
                                progress_callback=internal_progress
                            )
                        except PauseRequested:
                            if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                            return

                    if downloaded_file and os.path.exists(downloaded_file):
                        last_filepath = downloaded_file
                        if use_message_date and getattr(message, 'date', None):
                            try:
                                mtime = message.date.timestamp()
                                os.utime(downloaded_file, (mtime, mtime))
                            except Exception: pass

                if last_filepath:
                    try:
                        update_media_downloaded_path(c_id, message.id, os.path.basename(last_filepath))
                    except Exception: pass
                    if complete_cb:
                        complete_cb(message.id, filepath=last_filepath)
                    return

            file_size = (
                message.video.size if getattr(message, 'video', None)
                else message.document.size if getattr(message, 'document', None) 
                else message.audio.size if getattr(message, 'audio', None)
                else getattr(message, 'size', 0)
            )
            
            # Deduplication Check
            file_name = get_media_filename(message, prefix_date=prefix_file_date, channel=channel)
            
            expected_filepath = None
            if file_name:
                from telethon.utils import get_peer_id
                from database import get_media_downloaded_path, update_media_downloaded_path
                c_id = str(get_peer_id(channel)).replace("-100", "", 1)
                
                db_filename = get_media_downloaded_path(c_id, message.id) if rename_duplicates else None
                if db_filename:
                    candidate_filepath = os.path.join(folder_name, db_filename)
                    # Check if this exact file or its .part exists on disk
                    if os.path.exists(candidate_filepath) or os.path.exists(candidate_filepath + ".part"):
                        expected_filepath = candidate_filepath
                    else:
                        expected_filepath = get_unique_filepath(folder_name, file_name, reserved_paths=reserved_paths)
                        update_media_downloaded_path(c_id, message.id, os.path.basename(expected_filepath))
                else:
                    candidate_filepath = os.path.join(folder_name, file_name)
                    if rename_duplicates:
                        is_candidate_reserved = reserved_paths is not None and (candidate_filepath in reserved_paths or (candidate_filepath + ".part") in reserved_paths)
                        if (os.path.exists(candidate_filepath) or os.path.exists(candidate_filepath + ".part")) and not is_candidate_reserved:
                            expected_filepath = candidate_filepath
                        else:
                            expected_filepath = get_unique_filepath(folder_name, file_name, reserved_paths=reserved_paths)
                        update_media_downloaded_path(c_id, message.id, os.path.basename(expected_filepath))
                    else:
                        expected_filepath = candidate_filepath

                if reserved_paths is not None:
                    reserved_paths.add(expected_filepath)
            
            if expected_filepath:
                # 1. Target file already completely exists
                if os.path.exists(expected_filepath):
                    existing_size = os.path.getsize(expected_filepath)
                    if file_size and existing_size >= file_size:
                        # Clean up any leftover orphaned .part or .meta files
                        part_path = expected_filepath + ".part"
                        meta_path = expected_filepath + ".part.meta"
                        if os.path.exists(part_path):
                            try: os.remove(part_path)
                            except Exception: pass
                        if os.path.exists(meta_path):
                            try: os.remove(meta_path)
                            except Exception: pass
                        if progress_cb:
                            progress_cb(message.id, existing_size, existing_size, speed_str="Skipped (Exists)")
                        if complete_cb:
                            complete_cb(message.id, filepath=expected_filepath)
                        return

                # 2. Check if .part file is genuinely fully downloaded and can be finalized
                part_path = expected_filepath + ".part"
                meta_path = expected_filepath + ".part.meta"
                if os.path.exists(part_path) and file_size and os.path.getsize(part_path) == file_size:
                    can_finalize = False
                    if file_size > 1024 * 1024 or os.path.exists(meta_path):
                        # Chunked download: MUST verify with metadata that all chunks were completed
                        if os.path.exists(meta_path):
                            from utils.fast_telethon import CHUNK_SIZE, _load_meta
                            import math
                            total_parts = math.ceil(file_size / CHUNK_SIZE)
                            completed_parts = _load_meta(meta_path, file_size, total_parts)
                            if total_parts > 0 and len(completed_parts) == total_parts:
                                can_finalize = True
                    else:
                        # Non-chunked small file where size matches
                        can_finalize = True

                    if can_finalize and _atomic_finalize_sync(part_path, expected_filepath, meta_path):
                        if progress_cb:
                            progress_cb(message.id, file_size, file_size, speed_str="Complete")
                        if complete_cb:
                            complete_cb(message.id, filepath=expected_filepath)
                        return



            dir_path = os.path.join(folder_name, "")
            if file_name:
                incomplete_path = expected_filepath + INCOMPLETE_SUFFIX
                target_path = incomplete_path
            else:
                target_path = dir_path
            
            file_path = None

            # 🚀 Strategy 0: High-Speed FastTelethon Parallel Chunk Downloader for files > 1MB
            message_gone = False
            reference_refreshed = False
            while file_size and file_size > 1024 * 1024 and expected_filepath:
                try:
                    target_obj = getattr(message, 'media', None) or getattr(message, 'document', None) or getattr(message, 'video', None) or message
                    if target_obj:
                        success = await fast_download_file(
                            client=client,
                            location=target_obj,
                            target_path=expected_filepath,
                            file_size=file_size,
                            progress_callback=internal_progress,
                            cancel_event=cancel_event,
                            workers=4
                        )
                        if success and os.path.exists(expected_filepath):
                            file_path = expected_filepath
                except PauseRequested:
                    if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                    return
                except Exception as fast_err:
                    if isinstance(fast_err, ConnectionError) and client:
                        await reconnect_client(client)
                    if client and channel and _is_file_reference_error(fast_err):
                        # Telethon's fallback would re-fetch too, but crashes with
                        # "'NoneType' object has no attribute 'media'" when the
                        # message is gone; check first and hand it a fresh message.
                        refreshed, gone_reason = await _refetch_message(client, channel, message)
                        if gone_reason:
                            fail(message.id, gone_reason)
                            message_gone = True
                            break
                        if refreshed is not None:
                            message = refreshed
                            if not reference_refreshed:
                                # Messages are fetched when the task starts, so files
                                # reached hours later all expire at chunk 0. A fresh
                                # reference works with the parallel engine again.
                                reference_refreshed = True
                                log_event(
                                    "INFO",
                                    f"message {message.id}: file reference refreshed; "
                                    f"retrying the parallel download",
                                )
                                continue
                    if _has_saved_chunks(expected_filepath, file_size):
                        # Telethon's fallback restarts from byte 0 straight into the
                        # final file, discarding the chunks in .part. Let the retry
                        # loop run the parallel engine again so it resumes instead.
                        log_event(
                            "WARNING",
                            f"parallel download of {os.path.basename(expected_filepath)} failed with "
                            f"saved progress; retrying it instead of the single-stream fallback",
                        )
                        raise
                    log_event(
                        "WARNING",
                        f"message {message.id}: parallel engine failed, using Telethon "
                        f"fallback - {describe_download_error(fast_err)} ({fast_err})",
                    )
                break
            if message_gone:
                break

            if not file_path:
                try:
                    await GLOBAL_FLOODGATE.wait_if_gated(cancel_event)
                    file_path = await message.download_media(
                        file=target_path,
                        progress_callback=internal_progress,
                    )
                except PauseRequested:
                    _discard(incomplete_path)  # single-stream cannot resume
                    if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                    return
                except AttributeError as attr_err:
                    # Fallback for Telethon 1.38.x PhotoSize bug ('PhotoSize' object has no attribute 'location')
                    if "location" in str(attr_err) and getattr(message, 'photo', None):
                        try:
                            # Strategy 1: Download the photo object directly (higher level, often bypasses the bug)
                            await GLOBAL_FLOODGATE.wait_if_gated(cancel_event)
                            file_path = await client.download_media(
                                message.photo,
                                file=target_path,
                                progress_callback=internal_progress
                            )
                        except Exception as e2:
                            print(f"Fallback Strategy 1 failed: {e2}")
                            # Strategy 2: Manual construction of InputPhotoFileLocation (lowest level)
                            from telethon.tl.types import InputPhotoFileLocation
                            photo = message.photo
                            best_size = None
                            if photo.sizes:
                                for sz in reversed(photo.sizes):
                                    if hasattr(sz, 'type'):
                                        best_size = sz
                                        break
                            
                            if best_size:
                                loc = InputPhotoFileLocation(
                                    id=photo.id,
                                    access_hash=photo.access_hash,
                                    file_reference=photo.file_reference,
                                    thumb_size=best_size.type
                                )
                                # If target_path is a directory, specify a filename
                                final_target = target_path
                                if os.path.isdir(final_target):
                                    final_target = os.path.join(final_target, file_name or f"Photo_{message.id}.jpg")
                                
                                try:
                                    file_path = await client.download_file(
                                        loc,
                                        file=final_target,
                                        progress_callback=internal_progress,
                                    )
                                except PauseRequested:
                                    _discard(incomplete_path)  # single-stream cannot resume
                                    if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                                    return
                                except Exception as e3:
                                    print(f"Fallback Strategy 2 failed: {e3}")
                                    # If both fail, we re-raise the original error to allow retry logic to take over
                                    raise attr_err
                            else:
                                print("Fallback Strategy 2 failed: No best_size found")
                                raise attr_err
                    else:
                        raise attr_err

            if not file_path:
                _discard(incomplete_path)
                fail(
                    message.id,
                    "Download produced no file - Telegram returned no data and the "
                    f"single-stream fallback failed ({_describe_media(message)})",
                )
                break

            file_path = _accept_download(file_path, expected_filepath, incomplete_path, file_size)
            incomplete_path = None

            if complete_cb:
                complete_cb(message.id, filepath=file_path)
                
                # 📝 Message-Media Linker: Save sidecar .txt if message has text
                if file_path and os.path.exists(file_path):
                    # Clean up any leftover orphaned .part and .meta files for this completed file
                    part_f = file_path + ".part"
                    meta_f = part_f + ".meta"
                    if os.path.exists(part_f):
                        try: os.remove(part_f)
                        except Exception: pass
                    if os.path.exists(meta_f):
                        try: os.remove(meta_f)
                        except Exception: pass

                    if use_message_date and getattr(message, 'date', None):
                        try:
                            mtime = message.date.timestamp()
                            os.utime(file_path, (mtime, mtime))
                        except Exception as e:
                            print(f"Error setting file time for {file_path}: {e}")
                            
                    msg_text = (message.message or "").strip()
                    if msg_text:
                        base_path = os.path.splitext(file_path)[0]
                        txt_path = base_path + ".txt"
                        try:
                            with open(txt_path, "w", encoding="utf-8") as f:
                                f.write(msg_text)
                            if use_message_date and getattr(message, 'date', None):
                                os.utime(txt_path, (mtime, mtime))
                        except Exception as e:
                            print(f"Error saving sidecar text: {e}")
            break

        except asyncio.CancelledError:
            _discard(incomplete_path)
            incomplete_path = None
            if abort_reason[0]:
                # The stall watchdog cancelled us: this is a failure with a reason,
                # not a user pause, so report it as such.
                fail(message.id, abort_reason[0])
            elif cancelled_on_purpose(cancel_event):
                log_event("INFO", f"message {message.id}: cancelled/paused by user")
                if complete_cb: complete_cb(message.id, paused=True, filepath=None)
            elif attempt < max_retries - 1:
                # Telethon cancels in-flight requests when its connection gives up
                log_event(
                    "WARNING",
                    f"message {message.id}: {CONNECTION_DROPPED} - retrying "
                    f"(attempt {attempt+1}/{max_retries})",
                )
                if client:
                    await reconnect_client(client)
                continue
            else:
                fail(message.id, f"Failed after {max_retries} attempts - {CONNECTION_DROPPED}")
            break
        except Exception as e:
            _discard(incomplete_path)
            incomplete_path = None
            reason = describe_download_error(e)
            from telethon.errors import FloodWaitError
            if isinstance(e, FloodWaitError):
                GLOBAL_FLOODGATE.trip(getattr(e, 'seconds', 2))
            if client and channel and _is_file_reference_error(e):
                refreshed, gone_reason = await _refetch_message(client, channel, message)
                if gone_reason:
                    fail(message.id, gone_reason)
                    break
                if refreshed is not None:
                    message = refreshed
            if isinstance(e, ConnectionError) and client:
                await reconnect_client(client)
            if attempt < max_retries - 1:
                wait_time = getattr(e, 'seconds', 2)
                log_event(
                    "WARNING",
                    f"message {message.id}: {reason} - retrying in {wait_time}s "
                    f"(attempt {attempt+1}/{max_retries})",
                )
                await asyncio.sleep(wait_time)
                if client and channel:
                    try:
                        refreshed = await client.get_messages(channel, ids=message.id)
                        if refreshed: message = refreshed
                    except: pass
            else:
                fail(message.id, f"Failed after {max_retries} attempts - {reason}")
        finally:
            watchdog_stop.set()
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()

async def download_in_batches_headless(client, channel, messages, folder_name, batch_size, downloaded_state, progress_cb, complete_cb, task_cancel_event=None, max_speed_kb=None, msg_folder_resolver=None, stall_cb=None, skipped_ids=None):
    semaphore = asyncio.Semaphore(batch_size)
    reserved_paths = set()
    skipped_ids = set(skipped_ids or ())
    
    def internal_complete(msg_id, paused=False, filepath=None, error=False, reason=None):
        if not paused and not error and filepath:
            # Persistent state in SQLite - only mark completed if file was actually downloaded
            from telethon.utils import get_peer_id
            from database import mark_media_completed, update_media_downloaded_path
            try:
                ch_id = get_peer_id(channel)
                mark_media_completed(ch_id, msg_id)
                update_media_downloaded_path(ch_id, msg_id, filepath)
            except: pass
            downloaded_state.add(msg_id)
        if complete_cb:
            complete_cb(msg_id, paused=paused, filepath=filepath, error=error, reason=reason)

    def internal_stall(msg_id, idle_seconds, phase):
        if stall_cb:
            stall_cb(msg_id, idle_seconds, phase)

    async def download_message(message):
        async with semaphore:
            if task_cancel_event and task_cancel_event.is_set():
                if complete_cb: complete_cb(message.id, paused=True, filepath=None)
                return
            target_folder = msg_folder_resolver(message) if msg_folder_resolver else folder_name
            await download_single_file(client, channel, message, target_folder, progress_cb, internal_complete, task_cancel_event, max_speed_kb, reserved_paths=reserved_paths, stall_cb=internal_stall)

    # Files the user removed from a task must never be queued again
    tasks = [download_message(m) for m in messages if m.id not in downloaded_state and m.id not in skipped_ids]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def get_messages_by_type(client, channel, media_choice, min_id=None, max_id=None, limit=None, topic_id=None):
    """
    media_choice: 
    1 - Images
    2 - Videos
    3 - Files & Documents
    4 - ZIP files
    5 - Audio files
    6 - All Media
    """
    filter_type = None
    if media_choice == 1:
        filter_type = InputMessagesFilterPhotos()
    elif media_choice == 2:
        filter_type = InputMessagesFilterVideo()
    elif media_choice in [3, 4]:
        filter_type = InputMessagesFilterDocument()
    elif media_choice == 5:
        filter_type = InputMessagesFilterMusic()
    else:
        filter_type = None # All media
        
    kwargs = {"limit": limit}
    if min_id: kwargs["min_id"] = min_id
    if max_id: kwargs["max_id"] = max_id
    if topic_id: kwargs["reply_to"] = topic_id

    # Handle Audio / Voice combined fetch
    if media_choice == 5:
        try:
            music_msgs = await client.get_messages(channel, filter=InputMessagesFilterMusic(), **kwargs)
        except Exception:
            music_msgs = []
        try:
            voice_msgs = await client.get_messages(channel, filter=InputMessagesFilterVoice(), **kwargs)
        except Exception:
            voice_msgs = []
        try:
            doc_msgs = await client.get_messages(channel, filter=InputMessagesFilterDocument(), **kwargs)
            audio_docs = [m for m in doc_msgs if getattr(m, 'document', None) and getattr(m.document, 'mime_type', '').startswith("audio/")]
        except Exception:
            audio_docs = []
            
        combined_dict = {m.id: m for m in (list(music_msgs) + list(voice_msgs) + list(audio_docs))}
        return sorted(combined_dict.values(), key=lambda x: x.id, reverse=True)

    if filter_type:
        kwargs["filter"] = filter_type

    messages = await client.get_messages(channel, **kwargs)
    
    # Post-filtering
    if media_choice == 3:
        # Keep all valid documents
        messages = [m for m in messages if getattr(m, 'document', None) is not None]
    elif media_choice == 4:
        # ZIPs and compressed archives
        messages = [m for m in messages if getattr(m, 'document', None) and getattr(m.document, 'mime_type', '') in [
            "application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
            "application/x-tar", "application/gzip", "application/x-bzip2"
        ]]
    elif media_choice == 6:
        # Only messages that actually contain media (photos, videos, docs, audios) - exclude plain text
        messages = [m for m in messages if getattr(m, 'media', None) is not None]
        
    return messages

async def fetch_categorized_media(client, channel, limit=None, topic_id=None):
    """
    Fetches up to `limit` messages for each distinct media category IN PARALLEL.
    Now uses a semaphore to prevent "Server closed the connection" errors and includes retries.
    """
    if limit is None:
        try:
            limit = int(os.getenv('FETCH_LIMIT', 2000))
        except:
            limit = 2000
            
    if topic_id:
        print(f"DEBUG: fetch_categorized_media using topic_id={topic_id}")
    # 🛡️ Limit concurrency to 1 simultaneous request to prevent Telegram from forcefully dropping connections
    sem = asyncio.Semaphore(1)
    
    async def get_messages_with_sem(filter_type=None, limit_val=limit):
        async with sem:
            for attempt in range(3):
                try:
                    kwargs = {}
                    if filter_type: kwargs['filter'] = filter_type
                    if topic_id is not None: kwargs['reply_to'] = topic_id
                    return await client.get_messages(channel, limit=limit_val, **kwargs)
                except Exception as e:
                    if "closed the connection" in str(e).lower() and attempt < 2:
                        await asyncio.sleep(1) # Wait a bit before retry
                        continue
                    raise e

    try:
        # Fetch fresh data from Telegram
        (photos, videos, round_vids, docs, music, voice, links, gifs, all_msgs, topic_ref) = await asyncio.gather(
            get_messages_with_sem(InputMessagesFilterPhotos()),
            get_messages_with_sem(InputMessagesFilterVideo()),
            get_messages_with_sem(InputMessagesFilterRoundVideo()),
            get_messages_with_sem(InputMessagesFilterDocument()),
            get_messages_with_sem(InputMessagesFilterMusic()),
            get_messages_with_sem(InputMessagesFilterVoice()),
            get_messages_with_sem(InputMessagesFilterUrl()),
            get_messages_with_sem(InputMessagesFilterGif()),
            get_messages_with_sem(limit_val=limit), # Base feed
            get_messages_with_sem(limit_val=1) # Reference message for topic title if possible
        )
        
        # If we have a topic_id, try to get the topic title from the first message
        topic_title = None
        if topic_id and all_msgs:
            # In Telethon, forum topics are technically replies.
            # We can try to fetch the service message that created the topic or just use one message.
            pass

        paid_media = [m for m in all_msgs if isinstance(getattr(m, 'media', None), MessageMediaPaidMedia)]

        # 🗄️ CACHE RESULTS IN SQLite for faster tab switching
        from telethon.utils import get_peer_id
        try:
            ch_id = get_peer_id(channel)
            m_dict = {
                "Media": sorted(list(photos) + list(videos) + list(round_vids) + list(paid_media), key=lambda m: m.id, reverse=True),
                "Files": list(docs),
                "ZIPs": [m for m in docs if m.document and m.document.mime_type in ["application/zip", "application/x-rar-compressed", "application/x-7z-compressed"]],
                "Music": list(music),
                "Voice": list(voice),
                "Links": list(links),
                "GIFs": list(gifs),
                "Chat": [m for m in all_msgs if getattr(m, 'media', None) is None and (m.message or "").strip()]
            }
            cache_media_list(ch_id, m_dict)
        except Exception as cache_err:
            print(f"Failed to cache media: {cache_err}")
            
    except Exception as e:
        print(f"Fetch Categorized Media global failure: {e}")
        return {k.lower(): [] for k in ["Media", "Files", "ZIPs", "Music", "Voice", "Links", "GIFs", "Chat", "All"]}

    # ZIPs/Archives secondary filter from documents
    zips = [m for m in docs if m.document and m.document.mime_type in ["application/zip", "application/x-rar-compressed", "application/x-7z-compressed"]]
    
    # "Chat" = messages with NO media at all
    chats = [m for m in all_msgs if getattr(m, 'media', None) is None and (m.message or "").strip()]

    # Aggregate into "All" - Using a dict to deduplicate by message ID
    all_dict = {}
    all_raw = list(photos) + list(videos) + list(round_vids) + list(docs) + list(music) + list(voice) + list(links) + list(gifs) + list(chats) + list(paid_media)
    for m in all_raw:
        all_dict[m.id] = m
    all_sorted = sorted(all_dict.values(), key=lambda x: x.id, reverse=True)

    return {
        "All":   all_sorted[:limit], # Limit the "All" tab to the most recent items
        "Media": sorted(list(photos) + list(videos) + list(round_vids) + list(paid_media), key=lambda m: m.id, reverse=True)[:limit],
        "Files": list(docs),
        "ZIPs":  list(zips),
        "Music": list(music),
        "Voice": list(voice),
        "Links": list(links),
        "GIFs":  list(gifs),
        "Chat":  list(chats),
    }

def get_folder_name(media_choice):
    folders = {
        1: "images",
        2: "videos",
        3: "pdfs",
        4: "zips",
        5: "audio",
        6: "all_media"
    }
    return f"downloads/{folders.get(media_choice, 'all_media')}"
