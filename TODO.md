# 🚀 TG Media Downloader - v3.0 Roadmap Plan

> [!IMPORTANT]
> **We are currently focusing entirely on stabilizing and improving the robustness of existing features.** 
> If you have ideas or suggestions for new features, please join our [GitHub Discussions (Ideas)](https://github.com/vinodkr494/telegram-media-downloader/discussions/categories/ideas) instead of opening an issue. 

This document tracks the vision and high-level goals for the major **v3.0.0** release.

---

## ⚡ Core Engine & Performance
- [ ] **Segmented Multi-Connection**: Implement 8-16 parallel chunks per file for maximum throughput.
- [ ] **Auto-Cloud Synchronization**: Direct upload integration for **Google Drive, Dropbox, and OneDrive**.
- [ ] **External Engine Support**: Built-in support for of-loading tasks to **Aria2 or JDownloader 2**.
- [x] **Anti-Flood Smart Pacing**: Intelligent traffic management to avoid Telegram's "Flood Wait" limits.

## 🎨 Visual & UI/UX Excellence
- [ ] **Media Preview Gallery View**: High-resolution grid-based thumbnail browser for easier selection.
- [ ] **Built-in Media Player**: Native player for previewing videos, music, and voice notes within the app.
- [ ] **Global Floating Mini-Widget**: Minimalist draggable overlay for tracking progress while the app is hidden.
- [ ] **Theme Personalization Engine**: Full custom `.qss` theme support and a primitive "Theme Creator" UI.

## 🔍 Advanced Content Discovery
- [ ] **Universal Global Search**: SQLite FTS5-powered keyword search across **ALL** previously fetched channel history.
- [ ] **AI Smart Renaming**: Lightweight local NLP to auto-rename files based on message context and captions.
- [ ] **Regex-Based Auto-Filters**: Create "Watcher" rules for automated file categorization and filtering.
- [ ] **Visual Duplicate Detection**: AI-powered similarity check to prune near-identical image downloads.

## 🤖 Automation & Scheduling
- [ ] **Download Scheduler**: Set custom "Active Windows" (e.g., 2 AM – 6 AM) for automated night downloads.
- [ ] **Auto-Join/Fetch/Leave Mode**: Provide a join link; app handles the rest and leaves the channel automatically.
- [ ] **Post-Download FFmpeg Pipeline**: Auto-convert `.ogg` to `.mp3` or `.mkv` to `.mp4` after completion.
- [ ] **Remote Web Dashboard**: Secure local web access to manage tasks from a mobile device or another PC.

## 🛡️ Management & Security
- [ ] **Dynamic Path Templating v2**: Advanced pathing, e.g., `/{category}/{year}/{month}/{msg_id}_{filename}.{ext}`.
- [ ] **Auto-Cleanup & Storage Rules**: Policies to maintain storage limits (e.g., "Keep only the last 100GB").
- [ ] **Mass Forwarding Engine**: Forward selected media batches to "Saved Messages" instead of downloading.
- [ ] **Python Plugin API**: Hook system for community-made filters and post-processing scripts.

---

## ✅ Roadmap

- **Current Version**: 2.9.12
- **Completed in v2.9.12**:
  - [x] **64-bit Byte Progress**: `file_progress` uses `qlonglong` for current/total bytes; files over 2 GiB no longer wrap negative or flood crash_report.log with OverflowError.
  - [x] **Clean-Exit Marker**: crash.log gets "exited normally" via atexit, so a silent disappearance can be told apart from a quit.
- **Completed in v2.9.11**:
  - [x] **Card Row Teardown**: rows are removed with removeWidget + hide + deleteLater, never setParent(None). Stress repro of the pyside6.abi3.dll crash: 2/4 before, 0/6 after.
  - [x] **Subscriptions Skip Settled Posts**: merges no longer re-queue completed or deleted-on-Telegram posts, which restarted the task and rebuilt the card every 5 minutes.
- **Completed in v2.9.10**:
  - [x] **Park Unreachable Tasks**: `ChannelUnavailableError` from fetch_channel pauses the saved task and shows it as a card, instead of auto-resuming and failing on every launch.
- **Completed in v2.9.9**:
  - [x] **Reconnect**: `ensure_connected` reopens the client before lookups and subscription checks after Telethon gives up (seen after an HTTP 429).
  - [x] **No Pointless Dialog Scan**: ChannelPrivateError or a connection error skips the full chat scan; connection failures say "Could not reach Telegram" instead of "refused access".
- **Completed in v2.9.8**:
  - [x] **No Restart On Merge**: Subscription checks defer while the channel's task is running instead of restarting it.
  - [x] **crash.log**: faulthandler dumps all thread stacks on native crashes (v2.9.7 crashed in pyside6.abi3.dll; root cause unconfirmed).
- **Completed in v2.9.7**:
  - [x] **Subscribe Mode**: Watch channels for new posts (polling with `min_id` checkpoint), queue them as normal tasks, defer while a whole-category task of the same channel runs.
- **Completed in v2.9.6**:
  - [x] **Atomic Single-Stream Downloads**: Fallback writes `<name>.dl`, size-checked before rename; short downloads fail instead of completing.
  - [x] **Verify All Files**: Health check covers every file in the task, not only the 100 drawn rows.
- **Completed in v2.9.5**:
  - [x] **cryptg Bundled**: Native AES instead of pure Python (~1000x faster per chunk); startup warning when missing; a test fails on a build machine without it.
- **Completed in v2.9.4**:
  - [x] **Fast Files Tab**: Checkable items instead of per-row widgets, buttons only for visible rows, debounced search, single-pass Select All.
- **Completed in v2.9.3**:
  - [x] **Saved Failures**: Failed files and their reasons are stored with the task, so the Remove failed button shows them after a restart.
  - [x] **No-Media Posts**: Messages without media fail immediately as "Deleted on Telegram" instead of "Download produced no file".
- **Completed in v2.9.2**:
  - [x] **Resume After Restart**: Whole-category tasks reload without a selection instead of an empty one, so they no longer show "Completed" after reopening the app.
  - [x] **Fresh References Use The Parallel Engine**: An expired file reference is refreshed and retried in parallel instead of falling back to single-stream.
  - [x] **Remove Failed (N)**: One button drops every failed or deleted file from a task.
- **Completed in v2.9.1**:
  - [x] **Resume After Stalls/Timeouts**: Watchdog cancels and chunk timeouts keep `.part` progress instead of restarting through the single-stream fallback.
  - [x] **Automatic Reconnect**: The main client reconnects after Telethon gives up, and requests it cancels are retried rather than reported as a user pause.
  - [x] **DC Routing**: Same-DC files use the main connection (no `DC_ID_INVALID` socket leak); dead exported senders are evicted.
  - [x] **Deleted on Telegram**: Deleted messages and removed media fail once with their own label.
  - [x] **UI**: Western digits on the progress bar; Logs tab shows each line once with its level.
- **Completed in v2.9.0**:
  - [x] **Download Diagnostics**: Failures now carry a reason (rate limit, timeout, access lost, disk full, ...) instead of being swallowed by a `print()` that a windowed build never shows.
  - [x] **Stall Watchdog**: Files with no progress are flagged as Stalled (45s default) and stalled attempts are cancelled after 180s so they release their concurrency slot instead of hanging the queue.
  - [x] **Logs Tab & downloader.log**: New sidebar view with level filtering, auto-scroll and a rotating log file for support.
  - [x] **Per-File Removal**: Remove a single broken item from a task (partial data cleaned, choice persisted in `skipped_ids`) so the remaining files can finish.
- **Completed in v2.8.6**:
  - [x] **Anti-Flood Smart Pacing & Shared Circuit Breaker**: Proactive inter-worker burst spacing (20ms) and shared reactive FloodWait circuit breaker that halts all FastTelethon workers and downloads on `FloodWaitError` with half-open cooldown recovery.
  - [x] **Windows File Lock Protection**: Added retry handling for atomic config file replacement on Windows to prevent file-lock collisions.
- **Completed in v2.8.5**:
  - [x] **End-to-End Proxy Support**: Plumbed SOCKS5, SOCKS4, and HTTP proxy support across all TelegramClient connections.
  - [x] **Pre-Login Proxy Configuration**: Added proxy setup dialog accessible prior to authentication.
  - [x] **Settings & Dark Mode Retention**: Merged settings cleanly into config to preserve theme preferences.
- **Completed in v2.8.4**:
  - [x] **Telegram Stars Paid Media**: Detect, unpack, and download purchased Star photos, videos, and multi-media albums (`MessageMediaPaidMedia`).
  - [x] **Channel Handle Filename Prefixing**: Prepend `@{username}_` or `channel_title_` to downloaded media filenames via GUI setting toggle.
  - [x] **Paid Media Category & Cache Integration**: Integrated paid media scanning into Media and All views with accurate file size metadata.
- **Completed in v2.8.3**:
  - [x] **Premature Queue Completion Fix**: Scoped initial completion counters strictly to the current task's messages.
  - [x] **Channel Isolation**: Isolated channel download states to prevent cross-channel ID collisions.
  - [x] **Guarded Preallocated `.part` Finalization**: Require verified `.meta` chunk progress before finalizing full-sized `.part` files.
  - [x] **Orphaned `.part` & `.meta` File Cleanup**: Cleaned up leftover temporary and meta files on completion, fallback, or existing target file.
  - [x] **FloodWait & Worker Lifecycle**: Honored full FloodWait durations and safely cancelled background tasks prior to closing file handles.
- **Completed in v2.8.2**:
  - [x] **Resuming Stall Fix**: Added 25s MTProto request timeouts and retry recovery to prevent worker coroutines and concurrency slots from hanging indefinitely.
  - [x] **Complete `.part` Finalization**: Instant detection and atomic replacement of full-sized `.part` files with Windows file-lock retry handling.
  - [x] **Byte-Range Resumable Chunk Tracking**: Lightweight `.part.meta` chunk-state sidecars for clean interrupted download resumption.
  - [x] **Deduplication Stability**: Preserved in-progress `.part` candidate names without runaway `(2)`, `(3)` duplicate suffix increments.
  - [x] **Verify & Persistence Reconciliation**: Reconciled database completion states against filesystem with one-click Resume re-downloading.
- **Completed in v2.8.1**:
  - [x] **Concurrent Duplicate Renaming Fix**: In-flight file reservation & `.part` collision prevention.
  - [x] **Configurable Moved/Deleted Files Re-download**: User setting to preserve completion state when moving files.
  - [x] **FastTelethon Error Cleanup**: Clean up broken/orphaned `.part` files on failure or cancellation.
- **Completed in v2.8.0**:
  - [x] **File Manager**: Dedicated Files tab to manage the download list, search, filter, open files/folders, and delete items from history or disk.
  - [x] **FastTelethon Parallel Downloader**: 4-worker chunk streaming (512KB chunks) for turbo download speeds.
  - [x] **Re-downloading Deleted Files**: Automatic physical disk presence verification.
  - [x] **Publication Date Filename Prefixing**: Date-based media naming and clean untitled video/photo naming.
  - [x] **Custom Folder Naming**: Support `{username}` and `{channel_id}` placeholders in the download path template.
  - [x] **Forum Auto-separation**: Option to automatically download all topics from a forum into separate subfolders named after the topics when the main channel ID is provided.
- **Next Milestone**: 3.0.0 (The Media Operating System)

