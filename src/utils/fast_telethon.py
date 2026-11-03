import math
import os
import json
import asyncio
import time
from telethon.tl.functions.upload import GetFileRequest
from telethon.utils import get_input_location

from utils.app_logger import log_event, describe_download_error

CHUNK_SIZE = 512 * 1024  # 512 KB per chunk (Telegram MTProto max limit)

# Number of times a single chunk is retried before the whole file attempt fails.
CHUNK_MAX_ATTEMPTS = 5
# Upper bound for how long a chunk can go without an answer from Telegram.
CHUNK_REQUEST_TIMEOUT = 25.0
# Upper bound for borrowing an exported (per-datacentre) sender.
SENDER_BORROW_TIMEOUT = 15.0


def _load_meta(meta_path, file_size, total_parts):
    """Safely loads download progress metadata if it exists and is valid."""
    if not os.path.exists(meta_path):
        return set()
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if data.get("file_size") == file_size and data.get("total_parts") == total_parts:
                return set(data.get("completed_parts", []))
    except Exception:
        pass
    return set()


def _save_meta(meta_path, file_size, chunk_size, total_parts, completed_parts):
    """Safely writes download progress metadata using atomic rename."""
    try:
        temp_meta = meta_path + ".tmp"
        data = {
            "file_size": file_size,
            "chunk_size": chunk_size,
            "total_parts": total_parts,
            "completed_parts": list(completed_parts)
        }
        with open(temp_meta, "w", encoding="utf-8") as f:
            json.dump(data, f)
        if os.path.exists(temp_meta):
            os.replace(temp_meta, meta_path)
    except Exception:
        pass


def _atomic_finalize_sync(temp_path, target_path, meta_path=None):
    """Atomically renames temp_path to target_path with retries for Windows file locks."""
    for attempt in range(5):
        try:
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                except Exception:
                    pass
            os.replace(temp_path, target_path)
            # Remove meta_path only after temp_path is successfully renamed to target_path
            if meta_path and os.path.exists(meta_path):
                try:
                    os.remove(meta_path)
                except Exception:
                    pass
            return True
        except (PermissionError, OSError) as e:
            if attempt < 4:
                time.sleep(0.3)
            else:
                raise e
    return False


async def _atomic_finalize_async(temp_path, target_path, meta_path=None):
    """Async atomic rename with retries for Windows file locks."""
    for attempt in range(5):
        try:
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                except Exception:
                    pass
            os.replace(temp_path, target_path)
            # Remove meta_path only after temp_path is successfully renamed to target_path
            if meta_path and os.path.exists(meta_path):
                try:
                    os.remove(meta_path)
                except Exception:
                    pass
            return True
        except (PermissionError, OSError) as e:
            if attempt < 4:
                await asyncio.sleep(0.3)
            else:
                raise e
    return False


class DownloadFloodGate:
    """
    Proactive rate-limiter, shared flood-wait gate, and circuit breaker
    for FastTelethon parallel chunk workers.

    1. Proactive Spacing: Smooths out 0ms simultaneous chunk burst spikes across workers.
    2. Shared Reactive Gate: When ANY worker hits FloodWaitError, ALL workers
       pause immediately for the full duration (+ safety buffer) to prevent
       Telegram from escalating the ban.
    3. Half-Open Cooldown: After a flood-wait clears, requests are gently spaced
       under restricted concurrency so cooling endpoints aren't immediately re-banned.
    """

    def __init__(self, min_request_interval: float = 0.02, cooldown_period: float = 5.0):
        self._min_request_interval = min_request_interval  # 20ms burst pacing
        self._cooldown_period = cooldown_period            # 5s half-open cooldown
        self._wait_until = 0.0
        self._half_open_until = 0.0
        self._state = "CLOSED"  # "CLOSED", "OPEN", "HALF_OPEN"
        self._last_request_time = 0.0
        self._consecutive_successes = 0
        self._lock = None
        self._loop = None

    @property
    def lock(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._lock is None or self._loop != loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

    @property
    def state(self):
        return self._state

    def remaining(self) -> float:
        """Seconds left before an active FloodWait expires (0 when not gated)."""
        if self._state != "OPEN":
            return 0.0
        return max(0.0, self._wait_until - time.monotonic())

    def reset(self):
        """Resets the gate to initial CLOSED state."""
        self._wait_until = 0.0
        self._half_open_until = 0.0
        self._state = "CLOSED"
        self._last_request_time = 0.0
        self._consecutive_successes = 0

    def trip(self, seconds: float):
        """Trips the circuit breaker into OPEN state for the given duration."""
        now = time.monotonic()
        wait_duration = max(float(seconds), 1.0) + 1.5  # +1.5s safety buffer
        target_wait = now + wait_duration
        if target_wait > self._wait_until:
            self._wait_until = target_wait
        self._state = "OPEN"
        self._consecutive_successes = 0

    def report_success(self):
        """Called when a chunk request succeeds to heal the circuit breaker."""
        if self._state == "HALF_OPEN":
            self._consecutive_successes += 1
            if self._consecutive_successes >= 3:
                self._state = "CLOSED"
                self._half_open_until = 0.0

    async def wait_if_gated(self, cancel_event=None):
        """Waits if the gate is currently OPEN due to an active flood wait."""
        while self._state == "OPEN":
            if cancel_event and cancel_event.is_set():
                return False
            now = time.monotonic()
            if now < self._wait_until:
                sleep_time = min(self._wait_until - now, 0.5)
                await asyncio.sleep(sleep_time)
            else:
                break
        return True

    async def acquire(self, cancel_event=None):
        """
        Pauses if gate is OPEN (flood wait) or HALF_OPEN (cooling),
        and applies proactive pacing to avoid burst spikes.
        Returns True if acquired, or False if cancelled.
        """
        async with self.lock:
            while True:
                if cancel_event and cancel_event.is_set():
                    return False

                now = time.monotonic()

                # 1. Active flood wait (OPEN)
                if self._state == "OPEN":
                    if now < self._wait_until:
                        sleep_time = min(self._wait_until - now, 0.5)
                        await asyncio.sleep(sleep_time)
                        continue
                    else:
                        # Cooldown window has passed; transition to HALF_OPEN
                        self._state = "HALF_OPEN"
                        self._half_open_until = now + self._cooldown_period
                        self._consecutive_successes = 0

                # 2. Cooling down after flood wait (HALF_OPEN)
                if self._state == "HALF_OPEN":
                    if now >= self._half_open_until:
                        self._state = "CLOSED"
                    else:
                        # Gently space out requests during cooldown (200ms)
                        await asyncio.sleep(0.2)

                # 3. Proactive pacing: eliminate 0ms burst spikes
                now = time.monotonic()
                time_since_last = now - self._last_request_time
                if time_since_last < self._min_request_interval:
                    sleep_interval = self._min_request_interval - time_since_last
                    await asyncio.sleep(sleep_interval)

                self._last_request_time = time.monotonic()
                return True


GLOBAL_FLOODGATE = DownloadFloodGate()


def _needs_exported_sender(client, dc_id):
    """True when the file lives on a DC other than the one the client is on.

    Exporting authorization to the client's own DC fails with DcIdInvalidError,
    and Telethon connects the new sender before exporting, so every such attempt
    also leaks an open socket. Same-DC files go through the main connection,
    which is what Telethon's own download_file does.
    """
    if not dc_id:
        return False
    session = getattr(client, "session", None)
    home_dc = getattr(session, "dc_id", None)
    return home_dc is None or dc_id != home_dc


async def _evict_exported_sender(client, dc_id, sender):
    """Drops a cached exported sender that can no longer send.

    Telethon only marks a borrowed sender for reconnect after nobody has
    borrowed it for a while, so with parallel workers borrowing nonstop a sender
    whose reconnect gave up is handed out forever ("Cannot send requests while
    disconnected"). Removing it makes the next borrow build a fresh connection.
    """
    lock = getattr(client, "_borrow_sender_lock", None)
    cache = getattr(client, "_borrowed_senders", None)
    if lock is not None and cache is not None:
        async with lock:
            _state, cached = cache.get(dc_id, (None, None))
            if cached is sender:
                del cache[dc_id]
                log_event("WARNING", f"dropped dead connection to DC {dc_id}; reconnecting")
    try:
        await sender.disconnect()
    except Exception:
        pass


CONNECTION_DROPPED = "Telegram dropped the connection and cancelled the request"


def cancelled_on_purpose(cancel_event=None):
    """True when a CancelledError comes from a pause or a cancel of this task.

    Telethon also cancels every in-flight request when its own reconnect gives
    up; that one reaches us as CancelledError too, but nobody asked to stop.
    """
    if cancel_event is not None and cancel_event.is_set():
        return True
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def reconnect_client(client):
    """Reconnects the main client after Telethon gave up on its connection.

    Telethon retries a dropped connection a few times over a few seconds, then
    stops for good: every later request fails with "Cannot send requests while
    disconnected" until connect() is called. Concurrent callers share one
    attempt. Returns True when the client is connected afterwards.
    """
    is_connected = getattr(client, "is_connected", None)
    if is_connected is None or is_connected():
        return True

    lock = getattr(client, "_tgdl_reconnect_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        client._tgdl_reconnect_lock = lock

    async with lock:
        if client.is_connected():
            return True
        log_event("WARNING", "lost the connection to Telegram; reconnecting")
        try:
            await client.connect()
        except Exception as e:
            log_event("ERROR", f"reconnect to Telegram failed: {describe_download_error(e)} ({e})")
            return False
        connected = client.is_connected()
        if connected:
            log_event("INFO", "reconnected to Telegram")
        return connected


async def _download_part(client, location, offset, limit, dc_id=None, gate=None, cancel_event=None):
    """Fetches a single chunk from Telegram with timeout, proactive gating and retries.

    Every retry and the final failure are logged, because a chunk that keeps
    timing out is the usual reason a large download appears to hang.
    """
    for attempt in range(CHUNK_MAX_ATTEMPTS):
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError()

        if gate:
            allowed = await gate.acquire(cancel_event=cancel_event)
            if not allowed:
                raise asyncio.CancelledError()

        uses_exported_sender = _needs_exported_sender(client, dc_id)
        try:
            req = GetFileRequest(location=location, offset=offset, limit=limit)
            if uses_exported_sender:
                sender = await asyncio.wait_for(
                    client._borrow_exported_sender(dc_id), timeout=SENDER_BORROW_TIMEOUT
                )
                try:
                    result = await asyncio.wait_for(
                        sender.send(req), timeout=CHUNK_REQUEST_TIMEOUT
                    )
                except ConnectionError:
                    await _evict_exported_sender(client, dc_id, sender)
                    raise
                finally:
                    await client._return_exported_sender(sender)
            else:
                result = await asyncio.wait_for(client(req), timeout=CHUNK_REQUEST_TIMEOUT)

            if gate:
                gate.report_success()

            return result.bytes if result else b""
        except asyncio.CancelledError:
            if cancelled_on_purpose(cancel_event):
                raise
            error = ConnectionError(CONNECTION_DROPPED)
        except Exception as e:
            error = e

        e = error
        if isinstance(e, ConnectionError) and not uses_exported_sender:
            # Exported senders are evicted above; the main client must be reconnected
            await reconnect_client(client)

        last_attempt = attempt >= CHUNK_MAX_ATTEMPTS - 1
        reason = describe_download_error(e)
        if "FileReference" in type(e).__name__ or "FilerefUpgrade" in type(e).__name__:
            # The stale reference is part of `location`, so retrying here
            # can never succeed; the caller must re-fetch the message.
            log_event("WARNING", f"chunk @{offset}: {reason}")
            raise e
        if last_attempt:
            log_event(
                "ERROR",
                f"chunk @{offset} ({limit} bytes) failed after "
                f"{CHUNK_MAX_ATTEMPTS} attempts: {reason} ({e})",
            )
            raise e

        from telethon.errors import FloodWaitError

        if isinstance(e, FloodWaitError):
            wait = getattr(e, 'seconds', 1.0)
            if gate:
                gate.trip(wait)
            log_event(
                "WARNING",
                f"chunk @{offset}: FloodWait {wait}s, waiting "
                f"(attempt {attempt + 1}/{CHUNK_MAX_ATTEMPTS})",
            )
            await asyncio.sleep(wait + 1.0)
        else:
            wait = getattr(e, 'seconds', 1.0)
            backoff = min(wait, 3.0)
            log_event(
                "WARNING",
                f"chunk @{offset}: {reason} - retrying in {backoff}s "
                f"(attempt {attempt + 1}/{CHUNK_MAX_ATTEMPTS})",
            )
            await asyncio.sleep(backoff)


async def fast_download_file(client, location, target_path, file_size, dc_id=None, progress_callback=None, cancel_event=None, workers=4, gate=None):
    """
    Downloads media at maximum throughput using parallel chunk streams with resumable state
    and proactive FloodWait circuit breaker protection.
    Falls back gracefully if parallel streaming is not supported for the media.
    """
    if gate is None:
        gate = GLOBAL_FLOODGATE
    if file_size <= 0:
        return False

    # If target file is already fully downloaded on disk, skip and succeed
    if os.path.exists(target_path) and os.path.getsize(target_path) >= file_size:
        return True

    total_parts = math.ceil(file_size / CHUNK_SIZE)
    if total_parts <= 1 or workers <= 1:
        # Small file: single chunk is fast enough directly
        return False

    # Extract dc_id and InputFileLocation TLObject
    if isinstance(location, tuple) and len(location) == 2:
        dc_id, location = location
    elif not hasattr(location, 'SUBCLASS_OF_ID') or location.SUBCLASS_OF_ID != 0x1523d462:
        try:
            dc_id, location = get_input_location(location)
        except Exception:
            return False

    if not location or not hasattr(location, 'SUBCLASS_OF_ID'):
        return False

    temp_path = target_path + ".part"
    meta_path = temp_path + ".meta"
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    # Check existing .part and .meta files for resumption
    completed_parts = set()
    if os.path.exists(temp_path):
        existing_part_size = os.path.getsize(temp_path)
        if existing_part_size == file_size:
            completed_parts = _load_meta(meta_path, file_size, total_parts)
            # Only finalize if metadata confirms all parts are genuinely completed
            if total_parts > 0 and len(completed_parts) == total_parts:
                await _atomic_finalize_async(temp_path, target_path, meta_path)
                return True
        else:
            # File size mismatch, re-truncate
            with open(temp_path, "wb") as f:
                f.truncate(file_size)
            completed_parts = set()
            _save_meta(meta_path, file_size, CHUNK_SIZE, total_parts, completed_parts)
    else:
        with open(temp_path, "wb") as f:
            f.truncate(file_size)
        completed_parts = set()
        _save_meta(meta_path, file_size, CHUNK_SIZE, total_parts, completed_parts)

    missing_parts = [idx for idx in range(total_parts) if idx not in completed_parts]
    if not missing_parts and total_parts > 0:
        await _atomic_finalize_async(temp_path, target_path, meta_path)
        return True

    queue = asyncio.Queue()
    for idx in missing_parts:
        queue.put_nowait(idx)

    # Calculate initial downloaded bytes from already completed parts
    init_bytes = 0
    for idx in completed_parts:
        if idx == total_parts - 1:
            init_bytes += file_size - (idx * CHUNK_SIZE)
        else:
            init_bytes += CHUNK_SIZE

    downloaded_bytes = [init_bytes]
    lock = asyncio.Lock()
    file_handle = open(temp_path, "r+b")

    # Initial progress notification if resumed with existing parts
    if init_bytes > 0 and progress_callback:
        try:
            res = progress_callback(init_bytes, file_size)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

    class DownloadCancelled(Exception): pass

    save_meta_counter = [0]

    async def worker():
        while not queue.empty():
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled()

            try:
                part_idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            offset = part_idx * CHUNK_SIZE
            limit = CHUNK_SIZE

            chunk = await _download_part(client, location, offset, limit, dc_id=dc_id, gate=gate, cancel_event=cancel_event)
            if not chunk:
                queue.task_done()
                continue

            # If last chunk returned extra padding past file_size, trim it
            if offset + len(chunk) > file_size:
                chunk = chunk[:file_size - offset]

            async with lock:
                file_handle.seek(offset)
                file_handle.write(chunk)
                downloaded_bytes[0] += len(chunk)
                completed_parts.add(part_idx)
                current = downloaded_bytes[0]

                save_meta_counter[0] += 1
                if save_meta_counter[0] % 4 == 0 or len(completed_parts) == total_parts:
                    _save_meta(meta_path, file_size, CHUNK_SIZE, total_parts, completed_parts)

            if progress_callback:
                res = progress_callback(current, file_size)
                if asyncio.iscoroutine(res):
                    await res

            queue.task_done()

    tasks = []
    try:
        tasks = [asyncio.create_task(worker()) for _ in range(min(workers, len(missing_parts)))]
        await asyncio.gather(*tasks)
    except (DownloadCancelled, asyncio.CancelledError):
        # Persist current meta so the next attempt resumes from these chunks
        _save_meta(meta_path, file_size, CHUNK_SIZE, total_parts, completed_parts)
        if cancel_event and cancel_event.is_set():
            return False  # user pause
        # Anything else (the stall watchdog, shutdown) cancelled the task itself.
        # Swallowing it made the caller fall back to a single-stream download
        # from byte 0 straight into the final file, ignoring the saved chunks.
        raise
    except Exception as e:
        _save_meta(meta_path, file_size, CHUNK_SIZE, total_parts, completed_parts)
        done = len(completed_parts)
        log_event(
            "WARNING",
            f"parallel download of {os.path.basename(target_path)} stopped at "
            f"{done}/{total_parts} chunks: {describe_download_error(e)}",
        )
        raise e
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            file_handle.flush()
        except Exception:
            pass
        try:
            file_handle.close()
        except Exception:
            pass

    if cancel_event and cancel_event.is_set():
        _save_meta(meta_path, file_size, CHUNK_SIZE, total_parts, completed_parts)
        return False

    if len(completed_parts) == total_parts or downloaded_bytes[0] >= file_size:
        await _atomic_finalize_async(temp_path, target_path, meta_path)
        return True

    # Not a hard error, but the caller has to fall back to Telethon's own
    # downloader, so leave a breadcrumb explaining why.
    log_event(
        "WARNING",
        f"parallel download of {os.path.basename(target_path)} incomplete "
        f"({len(completed_parts)}/{total_parts} chunks) - falling back to Telethon",
    )
    return False
