import sqlite3
import os
import json
from resource_utils import get_project_root
from utils.file_utils import get_media_filename

DB_PATH = os.path.join(get_project_root(), "downloader.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Media Cache: Persistent storage for every message metadata we've seen
    # This replaces the need to re-fetch from Telegram for every tab switch
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS media_cache (
        msg_id INTEGER,
        channel_id TEXT,
        media_type TEXT, 
        title TEXT,
        size INTEGER,
        date TEXT,
        completed INTEGER DEFAULT 0,
        raw_json TEXT,
        downloaded_path TEXT,
        channel_title TEXT,
        PRIMARY KEY (msg_id, channel_id)
    )
    """)
    
    try:
        cursor.execute("ALTER TABLE media_cache ADD COLUMN downloaded_path TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE media_cache ADD COLUMN channel_title TEXT")
    except sqlite3.OperationalError:
        pass
    
    # 2. Tasks: The active download queue
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY, -- 'channelId_mediaId'
        channel_input TEXT,
        media_id INTEGER,
        paused INTEGER DEFAULT 0,
        download_path TEXT,
        download_limit INTEGER,
        max_speed_kb INTEGER,
        selected_ids TEXT, -- JSON list of message IDs
        title TEXT,
        total_items INTEGER,
        folder_name TEXT,
        skipped_ids TEXT -- JSON list of message IDs the user removed from this task
    )
    """)

    # Migration: tasks removed individually by the user (e.g. items that stalled)
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN skipped_ids TEXT")
    except sqlite3.OperationalError:
        pass
    
    conn.commit()
    conn.close()

# Auto-initialize DB on import
init_db()

# --- TASK MANAGEMENT ---

def normalize_channel_key(chan_str):
    if not chan_str:
        return ""
    s = str(chan_str).strip()
    if "t.me/" in s:
        s = s.rstrip('/').split('/')[-1]
    if s.startswith("@"):
        s = s[1:]
    if s.startswith("-100"):
        s = s[4:]
    elif s.startswith("-"):
        s = s[1:]
    return s

def save_task_db(task):
    """Saves or updates a single task object in SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Deduplication normalized key
    ch = str(task.get("channel_input", "")).strip()
    ch_key = normalize_channel_key(ch)
    media_id = task.get("media_id", 6)
    topic_id = task.get("topic_id")
    
    # Use existing task_id if provided and clean, otherwise compute canonical ID
    task_id = task.get("task_id")
    if not task_id or "t.me/" in task_id or " " in task_id:
        task_id = f"{ch_key}_{topic_id}_{media_id}" if topic_id else f"{ch_key}_{media_id}"
    
    # NULL means a whole-category task; [] would read back as "nothing selected"
    selected = task.get("selected_message_ids")
    selected_json = json.dumps(selected) if selected else None
    skipped_list = task.get("skipped_ids") or []
    skipped_json = json.dumps(sorted({int(m) for m in skipped_list}))
    
    cursor.execute("""
    INSERT INTO tasks (
        task_id, channel_input, media_id, paused, download_path, 
        download_limit, max_speed_kb, selected_ids, title, total_items, folder_name,
        skipped_ids
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(task_id) DO UPDATE SET
        channel_input=excluded.channel_input,
        paused=excluded.paused,
        download_path=excluded.download_path,
        download_limit=excluded.download_limit,
        max_speed_kb=excluded.max_speed_kb,
        selected_ids=excluded.selected_ids,
        title=excluded.title,
        total_items=excluded.total_items,
        folder_name=excluded.folder_name,
        skipped_ids=excluded.skipped_ids
    """, (
        task_id, ch, media_id, 1 if task.get("paused") else 0,
        task.get("download_path"), task.get("download_limit"),
        task.get("max_speed_kb"), selected_json, task.get("title"),
        task.get("total_items"), task.get("folder_name"), skipped_json
    ))
    
    conn.commit()
    conn.close()

def update_task_paused_db(task_id_or_chan, is_paused=True):
    """Directly updates the paused status of any matching task in SQLite."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    paused_val = 1 if is_paused else 0
    clean = normalize_channel_key(task_id_or_chan)
    cursor.execute("""
        UPDATE tasks SET paused=? 
        WHERE task_id=? 
           OR task_id LIKE ? 
           OR channel_input=? 
           OR channel_input LIKE ?
           OR title=?
    """, (paused_val, str(task_id_or_chan), f"%{clean}%", str(task_id_or_chan), f"%{clean}%", str(task_id_or_chan)))
    conn.commit()
    conn.close()

def _parse_id_list(raw):
    """Parse a JSON list column, tolerating NULL and malformed values."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []

def _parse_selection(raw):
    """None for a whole-category task, else the selected message IDs.

    Older rows store "[]" for whole-category tasks; an empty selection is
    never saved on purpose, so it also means "download everything".
    """
    return _parse_id_list(raw) or None

def load_active_tasks_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks")
    rows = cursor.fetchall()
    
    tasks = []
    for r in rows:
        t = dict(r)
        t["paused"] = bool(t["paused"])
        t["selected_message_ids"] = _parse_selection(t.get("selected_ids"))
        t["skipped_ids"] = _parse_id_list(t.get("skipped_ids"))
        tasks.append(t)
    
    conn.close()
    return tasks

def get_task_db(ch_input, media_id=6):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    ch_key = str(ch_input).replace("-100", "", 1)
    task_id = f"{ch_key}_{media_id}"
    cursor.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        t = dict(row)
        t["selected_message_ids"] = _parse_selection(t.get("selected_ids"))
        t["skipped_ids"] = _parse_id_list(t.get("skipped_ids"))
        return t
    return None

def _task_match_query(task_id_or_chan):
    """Build a WHERE clause that finds a task row from a card's task_id.

    Cards are usually keyed by the resolved numeric ID, but a card can still
    hold a username-based key, so both the full key and the channel portion are
    matched (the same forgiving approach used by ``update_task_paused_db``).
    """
    key = str(task_id_or_chan)
    clean = normalize_channel_key(key)
    candidates = [clean]
    chan_only = normalize_channel_key(key.split("_")[0])
    if chan_only and chan_only != clean:
        candidates.append(chan_only)

    clauses = ["task_id=?", "channel_input=?", "title=?"]
    params = [key, key, key]
    for candidate in dict.fromkeys(c for c in candidates if c):
        clauses.append("task_id LIKE ?")
        params.append(f"%{candidate}%")
        clauses.append("channel_input LIKE ?")
        params.append(f"%{candidate}%")

    return "SELECT task_id, skipped_ids FROM tasks WHERE " + " OR ".join(clauses), params

def add_task_skipped_id(task_id_or_chan, msg_id):
    """Persistently remember that the user removed one file from a task.

    Without this, resuming the task would queue the removed message again.
    Returns how many task rows were updated (0 means the removal was not
    persisted because no matching task row exists).
    """
    try:
        msg_id = int(msg_id)
    except (TypeError, ValueError):
        return 0

    query, params = _task_match_query(task_id_or_chan)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(query, params)

    updated = 0
    for row in cursor.fetchall():
        ids = set(_parse_id_list(row["skipped_ids"]))
        ids.add(msg_id)
        cursor.execute(
            "UPDATE tasks SET skipped_ids=? WHERE task_id=?",
            (json.dumps(sorted(ids)), row["task_id"]),
        )
        updated += 1
    conn.commit()
    conn.close()
    return updated

def load_task_skipped_ids(task_id_or_chan):
    """Return the set of message IDs the user removed from a task."""
    query, params = _task_match_query(task_id_or_chan)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(query, params)
    ids = set()
    for row in cursor.fetchall():
        ids.update(_parse_id_list(row["skipped_ids"]))
    conn.close()
    return ids

def remove_task_db(ch_input, media_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    ch_key = str(ch_input).replace("-100", "", 1)
    task_id = f"{ch_key}_{media_id}"
    cursor.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()

# --- MEDIA CACHE ---

def cache_media_list(channel_id, messages_dict):
    """
    Saves a full categorized dictionary from fetch_categorized_media into DB.
    messages_dict: { 'Media': [msg_objs], 'Files': [msg_objs], ... }
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Normalize channel ID
    c_id = str(channel_id).replace("-100", "", 1)
    
    try:
        from ui.views.settings_view import load_config
        cfg = load_config()
        prefix_date = cfg.get("prefix_file_date", True)
    except Exception:
        prefix_date = True
        
    # We use a batch transaction for speed
    for category, msgs in messages_dict.items():
        for m in msgs:
            # We don't save raw telethon objects, just the essentials
            m_title = get_media_filename(m, prefix_date=prefix_date)
            m_size = 0
            if hasattr(m, 'media') and m.media and m.media.__class__.__name__ == 'MessageMediaPaidMedia':
                for item in getattr(m.media, 'extended_media', []):
                    if hasattr(item, 'media'):
                        sub_m = item.media
                        if hasattr(sub_m, 'document') and sub_m.document:
                            m_size += getattr(sub_m.document, 'size', 0)
                        elif hasattr(sub_m, 'photo') and sub_m.photo and hasattr(sub_m.photo, 'sizes') and sub_m.photo.sizes:
                            m_size += sub_m.photo.sizes[-1].size if hasattr(sub_m.photo.sizes[-1], 'size') else 0
            elif hasattr(m, 'file') and m.file:
                m_size = getattr(m.file, 'size', 0)
            elif hasattr(m, 'document') and m.document:
                m_size = getattr(m.document, 'size', 0)
            elif hasattr(m, 'photo') and m.photo:
                # Find size from photo.sizes
                if hasattr(m.photo, 'sizes') and m.photo.sizes:
                    m_size = m.photo.sizes[-1].size if hasattr(m.photo.sizes[-1], 'size') else 0
            elif hasattr(m, 'size'):
                m_size = m.size

            cursor.execute("""
            INSERT INTO media_cache (msg_id, channel_id, media_type, title, size, date)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(msg_id, channel_id) DO UPDATE SET
                media_type=excluded.media_type,
                title=excluded.title,
                size=excluded.size
            """, (m.id, c_id, category, m_title, m_size, str(m.date) if hasattr(m, 'date') else ""))
            
    conn.commit()
    conn.close()

def get_cached_media(channel_id, category=None):
    """Retrieves cached media for a channel from SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    c_id = str(channel_id).replace("-100", "", 1)
    if category:
        cursor.execute("SELECT * FROM media_cache WHERE channel_id=? AND media_type=? ORDER BY msg_id DESC", (c_id, category))
    else:
        cursor.execute("SELECT * FROM media_cache WHERE channel_id=? ORDER BY msg_id DESC", (c_id,))
        
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def mark_media_completed(channel_id, msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    c_id = str(channel_id).replace("-100", "", 1)
    cursor.execute("""
    INSERT INTO media_cache (msg_id, channel_id, completed)
    VALUES (?, ?, 1)
    ON CONFLICT(msg_id, channel_id) DO UPDATE SET completed=1
    """, (msg_id, c_id))
    conn.commit()
    conn.close()

def unmark_media_completed(channel_id, msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    c_id = str(channel_id).replace("-100", "", 1)
    cursor.execute("UPDATE media_cache SET completed=0 WHERE msg_id=? AND channel_id=?", (msg_id, c_id))
    conn.commit()
    conn.close()

def get_media_downloaded_path(channel_id, msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    c_id = str(channel_id).replace("-100", "", 1)
    cursor.execute("SELECT downloaded_path FROM media_cache WHERE msg_id=? AND channel_id=?", (msg_id, c_id))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def update_media_downloaded_path(channel_id, msg_id, downloaded_path):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    c_id = str(channel_id).replace("-100", "", 1)
    cursor.execute("""
    INSERT INTO media_cache (msg_id, channel_id, downloaded_path)
    VALUES (?, ?, ?)
    ON CONFLICT(msg_id, channel_id) DO UPDATE SET downloaded_path=excluded.downloaded_path
    """, (msg_id, c_id, downloaded_path))
    conn.commit()
    conn.close()

def get_completed_state_db():
    """Returns a set of (channel_id, msg_id) for fast lookup of finished files."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, msg_id FROM media_cache WHERE completed=1")
    res = set(cursor.fetchall())
    conn.close()
    return res

def get_all_completed_media(channel_id=None, category=None, search=None):
    """Retrieves completed downloaded media from SQLite with optional filtering."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = """
    SELECT m.*, 
           COALESCE(m.channel_title, t.title, m.channel_id) as resolved_channel_title 
    FROM media_cache m
    LEFT JOIN tasks t ON t.channel_input LIKE '%' || m.channel_id || '%'
    WHERE m.completed=1
    """
    params = []
    
    if channel_id and channel_id != "all":
        c_id = str(channel_id).replace("-100", "", 1)
        query += " AND m.channel_id=?"
        params.append(c_id)
        
    if category and category != "all":
        query += " AND m.media_type=?"
        params.append(category)
        
    if search:
        query += " AND (m.title LIKE ? OR m.downloaded_path LIKE ? OR CAST(m.msg_id AS TEXT) LIKE ? OR m.channel_title LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term, term])
        
    query += " GROUP BY m.msg_id, m.channel_id ORDER BY m.date DESC, m.msg_id DESC"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def delete_media_cache_item(channel_id, msg_id):
    """Deletes an item from the media cache database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    c_id = str(channel_id).replace("-100", "", 1)
    cursor.execute("DELETE FROM media_cache WHERE msg_id=? AND channel_id=?", (msg_id, c_id))
    conn.commit()
    conn.close()

def delete_multiple_media_cache_items(items):
    """Deletes multiple items from media_cache. items is list of (channel_id, msg_id)."""
    if not items:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for channel_id, msg_id in items:
        c_id = str(channel_id).replace("-100", "", 1)
        cursor.execute("DELETE FROM media_cache WHERE msg_id=? AND channel_id=?", (msg_id, c_id))
    conn.commit()
    conn.close()

def get_cached_channels_summary():
    """Returns a list of distinct channel summaries with titles and item counts from media cache."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT m.channel_id, 
           COALESCE(MAX(m.channel_title), MAX(t.title), m.channel_id) as channel_title,
           COUNT(*) as count, 
           SUM(m.size) as total_size 
    FROM media_cache m
    LEFT JOIN tasks t ON t.channel_input LIKE '%' || m.channel_id || '%'
    WHERE m.completed=1 
    GROUP BY m.channel_id
    ORDER BY count DESC
    """)
    rows = [dict(channel_id=r[0], channel_title=r[1], count=r[2], total_size=r[3]) for r in cursor.fetchall()]
    conn.close()
    return rows

def migrate_json_to_db():
    from resource_utils import get_project_root
    tasks_file = os.path.join(get_project_root(), "active_tasks.json")
    state_file = os.path.join(get_project_root(), "download_state.json")
    import json
    
    if not os.path.exists(tasks_file) and not os.path.exists(state_file):
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Migrate tasks
    if os.path.exists(tasks_file):
        try:
            with open(tasks_file, "r") as f:
                tasks = json.load(f)
                for t in tasks:
                    ch = str(t.get("channel_input", ""))
                    ch_key = ch.replace("-100", "", 1) if ch.startswith("-100") else ch
                    tid = f"{ch_key}_{t.get('media_id', 6)}"
                    cursor.execute("""
                    INSERT OR IGNORE INTO tasks (task_id, channel_input, media_id, paused, download_path, download_limit, max_speed_kb, selected_ids, title, total_items, folder_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (tid, ch, t.get('media_id', 6), 1 if t.get('paused') else 0, t.get('download_path'), t.get('download_limit'), t.get('max_speed_kb'), json.dumps(t.get('selected_message_ids', [])), t.get('title'), t.get('total_items'), t.get('folder_name')))
            os.rename(tasks_file, tasks_file + ".bak")
        except: pass
        
    # Migrate download state
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
                for mid in state:
                    cursor.execute("INSERT OR IGNORE INTO media_cache (msg_id, channel_id, completed) VALUES (?, ?, ?)", (mid, "legacy", 1))
            os.rename(state_file, state_file + ".bak")
        except: pass
        
    conn.commit()
    conn.close()
