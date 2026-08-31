# ARMEDIAS AI — Telegram Message Automation Hub

Flask + SocketIO dashboard that drives **multiple Telegram *user* accounts** (Pyrogram MTProto,
not the Bot API) to auto-forward every new message from one source channel into a list of
target groups, on a repeating interval.

Repo: `github.com/itsmearyanabc/camel-telegram-tool` — the only remote. Started fresh on
2026-08-27 with a single initial commit; the codebase was carried over from an earlier project
but **none of that history is here and that repo is not a remote.**

---

## 1. What it actually does

1. Admin logs into the web dashboard (`/login` → JWT in `localStorage`).
2. Admin registers phone numbers and completes a Telegram OTP (+2FA) login per number.
   Each produces `sessions/session_<digits>.session` (Pyrogram SQLite session).
3. Per account, admin sets: **source channel**, **target list**, **loop interval** (minutes),
   **msg delay** (seconds between sends), and an optional **nickname**.
4. A `BotWorker` per account installs a Pyrogram `MessageHandler` on the source channel.
   New message → queue every target → forward one-by-one with delay + jitter.
5. If no new source message arrives within `loop_interval` minutes, the *last* message is
   re-forwarded (the "loop"). Progress streams to the browser over SocketIO every 2s.

Semantics worth remembering: **only the most recent source message is ever live.** A new
message flushes the pending queue and replaces the campaign.

---

## 2. Architecture

```
app.py                         Flask + SocketIO bootstrap, atexit graceful shutdown
api/routes.py                  ALL routes + auth + the dedicated asyncio loop thread
core/bot_manager.py            Orchestrator; owns Dict[clean_phone -> BotWorker]
core/bot_worker.py             Per-account engine: monitor, queue, dispatch, retry
core/group_monitor.py          Group Monitor engine: live handlers + roster diff sync
core/message_bot.py            Message Bot engine: Bot API sends + /start capture
core/services/
  config_service.py            config.json load/atomic-save (+ Supabase sync on save)
  loop_manager.py              start/stop a single asyncio.Task, cancels the previous
  progress_tracker.py          lock-guarded sent/failed/total counters
  group_store.py               SQLite store for monitored groups / members / events
  bot_store.py                 SQLite store for bot token / user pool / send log
  persistence.py               Supabase Storage REST backup of config + sessions + group DB
utils/logger.py                rotating logs/bot.log, UTF-8 forced (Windows-safe)
utils/config_loader.py         LEGACY duplicate of config_service — see section 6
templates/ static/             vanilla-JS dashboard, DOM-diffed cards
static/group_monitor.js        Group Monitor tab controller (loads after app.js)
static/message_bot.js          Message Bot tab controller (loads after app.js)
```

### The threading model — the single most important thing here

Flask/gunicorn runs threaded; Pyrogram is asyncio. `api/routes.py` creates **one dedicated
event loop on a background thread** (`_BOT_LOOP`) at import time. Every async call from a
route goes through:

```python
run_async(coro)   # asyncio.run_coroutine_threadsafe(coro, _BOT_LOOP).result(timeout=30)
```

All Pyrogram clients, workers and tasks live on `_BOT_LOOP`. **Never await worker code
directly from a Flask handler, and never create a second event loop.**

Background threads started at import: `_run_bot_loop`, `_init_app` (Supabase restore →
`bot_manager.initialize()`), `_status_worker` (SocketIO push, 2s), `_cleanup_stale_auth`
(60s), `_keep_alive` (5-min self-ping).

### Identity convention

A phone is canonicalised to **digits only** (`"".join(filter(str.isdigit, phone))`) — called
`clean_phone` / `p_clean` — and that is the key for `bot_manager.workers`, `account_settings`,
and session filenames. This is re-implemented inline in ~10 places; keep it consistent.

---

## 3. State & persistence

| What | Where | Notes |
|---|---|---|
| Global + per-account settings | `config.json` | atomic write via `mkstemp` + `os.replace` |
| Telegram auth | `sessions/session_<digits>.session` | Pyrogram SQLite |
| Logs | `logs/bot.log` | rotating, 10 MB x 5 |
| Cloud backup | Supabase Storage bucket `telegram-sessions` | optional; no-op if env unset |

`config.json` shape:

```json
{
  "api_id": "", "api_hash": "", "phones": "+91...\n+91...",
  "source_channel": "-100...", "loop_interval": 15, "msg_delay": 5,
  "targets": ["@grp"],
  "account_settings": {
    "919999999999": {
      "nickname": "", "source_channel": "", "loop_interval": 15, "msg_delay": 5,
      "targets": [], "is_loop_active": true,
      "last_msg_id": 123, "last_from_chat": -100,
      "target_results": {"@grp": {"status": "failed", "error": "...", "ts": 0}}
    }
  }
}
```

`phones` is a **newline-delimited string**, not a list. Per-account values override globals.

### The target pool

`targets` is edited through a selectable table in Session Settings, not a textarea.
`BotWorker.target_results` holds a per-target verdict for the current campaign —
`idle` → `queued` → `sending` → `sent`/`failed`, with the failure reason — which
rides out on the existing 2s `status_update` so the pool animates live while a
run is in flight. That is what makes a dead channel findable and removable.

Two things worth knowing:

- **Verdicts are written to config once per campaign**, when the queue drains,
  never per target — each save is a config rewrite plus a cloud sync.
  `bot_manager._start_worker` restores them, so the pool still shows last run's
  failures after a restart.
- **The pool saves on every edit** via `POST /api/session/targets`, while the
  rest of the form saves on submit. They are separate endpoints on purpose:
  sharing one would let an unsaved form field overwrite itself. Pool edits call
  `worker.update_targets()`, which swaps the list *without* touching the source
  handler or restarting the interval countdown — `update_settings()` does both
  and is wrong for a checkbox tick.

**Crash recovery:** `is_loop_active`, `last_msg_id`, `last_from_chat` are persisted so that on
restart `_start_worker` restores the campaign and auto-resumes. On resume the worker dispatches
*immediately* rather than waiting out the interval — deliberate, to beat Render's idle spin-down.

---

## 4. Environment

| Var | Purpose |
|---|---|
| `ADMIN_USER` / `ADMIN_PASS` | dashboard login (defaults `admin` / `telegram2026`) |
| `SECRET_KEY` | Flask session + JWT HS256 signing |
| `API_ID` / `API_HASH` | override the Telegram creds in `config.json` |
| `SUPABASE_URL` / `SUPABASE_KEY` | enable cloud persistence |
| `RENDER_EXTERNAL_URL` / `RENDER_EXTERNAL_HOSTNAME` | enable keep-alive ping |
| `PORT` | 5001 local, 5000 in Docker |

Run locally: `python app.py` → http://localhost:5001
Prod: gunicorn + `GeventWebSocketWorker`, **`--workers 1`** (mandatory — worker state is
in-process; a second worker would duplicate every forward).

Deps: `pyrogram==2.0.106` (official, pinned), TgCrypto, Flask 3, flask-socketio,
PyJWT, gevent.

---

## 5. API surface (all `@token_required` except login)

```
GET  /                       dashboard        GET /login   GET /logout
POST /api/login              → JWT (7 days)
GET  /api/dashboard/sync     merged worker + on-disk account state
POST /api/session/start | stop | dispatch | settings | rename
POST /api/session/targets    replace the target pool (saves immediately)
POST /save-global            api creds + global defaults
POST /api/add-account | logout-account | delete-account
GET  /api/account-targets?phone=
POST /api/auth/send_code → /api/auth/sign_in → /api/auth/check_password   (OTP → 2FA)
GET  /logs                   last 100 lines
WS   status_update (server→client, 2s) / request_sync (client→server)
```

`_get_accounts_state()` is the merge point: live workers first, then registered-but-not-loaded
phones synthesised from `config.json` + session-file existence.

---

## 6. Known rough edges (verified in this tree)

- **`utils/config_loader.py` is dead weight** — a near-duplicate of `config_service` without
  the Supabase sync. Only `diagnostic.py` still references it. Everything real uses
  `config_service`. Deleting it is safe; using it would silently skip cloud sync.
- **Tests are stale and will not run.** `tests/*` and `qa_test.py` import `WorkerState` and
  `account_worker`, neither of which exists any more; `BotWorker.__init__` also gained a
  required `global_semaphore` arg. Treat `tests/` as historical, not a safety net.
- **Root-level scripts are one-off manual tools**, not part of the app: `brutal_test.py`,
  `stress_test.py`, `final_audit.py`, `diagnostic.py`, `test_live_forward.py` (the last has a
  hard-coded phone number and target).
- **`config.json` is local-only and gitignored** — it holds live credentials, the source
  channel and the target list. `config.example.json` is the committed template. Keep it that
  way; never `git add -f` the real one.
- `_ADMIN_PASS_HASH` is computed once at import from the env var, so changing `ADMIN_PASS`
  requires a restart.
- Concurrency ceiling is `BotManager.global_semaphore = asyncio.Semaphore(3)` — at most 3
  accounts sending at any instant, across all workers.
- ~~`last_dispatch_time` never set~~ — fixed 2026-08-27; the worker now stamps it on each
  successful forward, so "Last Sent" shows a real time.
- ~~Dispatch button unreachable~~ — fixed 2026-08-27; it was created then hidden for
  authenticated accounts, leaving `/api/session/dispatch` with no UI path. Now "Send Now".
- **`requirements.txt` pins official `pyrogram==2.0.106`** (commit 8a5fb1e). The
  `pyroratnagram` fork this file used to name is gone — an unofficial two-release
  package is not where a library holding Telegram session keys belongs.
- ~~A timed-out forward was retried~~ — fixed. `wait_for(..., timeout=15)` cancels
  locally but Telegram may already have accepted the forward, so the retry posted
  the same message to the group twice. `asyncio.TimeoutError` is now terminal and
  says so in the reason.
- ~~A failed handler detach stacked a duplicate~~ — fixed. `_remove_monitor` only
  cleared `self._handler` inside the `try`, so a failure stranded a live handler
  and the next `_setup_monitor` added a second one — doubling every forward.
  The reference is now dropped only on confirmed removal, and setup refuses to
  stack.
- ~~`config_service.save()` uploaded to Supabase inline~~ — fixed. It was a
  synchronous 30s-timeout POST, and several callers run on `_BOT_LOOP`, so it
  stalled every Telegram client. Now uses the same `schedule_backup` debounce as
  the two SQLite stores; `app.py` flushes pending backups on shutdown.
- ~~Merging a half-known recipient crashed~~ — fixed. Adding someone known by ID
  when a username-only row already existed (or vice versa) violated the unique
  index and 500'd the import. `add_recipient` now absorbs the duplicate: sums
  `sent_count`, re-points `send_log`, deletes the stray row.
- ~~Supabase-stored attachments were never pruned~~ — fixed. The disk fallback
  pruned at 24h but the Storage path grew forever. `persistence.prune_uploads()`
  runs on each upload.
- ~~`preflight.sh` inspected `/opt/armedias`~~ — fixed. `setup.sh` defaults to
  `telegram-tool`, so preflight checked a path the installer never uses and always
  reported "free". It now takes the same `app-name` argument. `deploy/DEPLOY.md`
  was likewise split between both names and is now consistent.
- ~~`.hidden` CSS class was never defined~~ — fixed 2026-08-27. The OTP modal's step logic
  toggled a class with no rule behind it, so all three login steps rendered at once.

---

## 7. Group Monitor

A second, independent feature on its own dashboard tab. It watches groups through an
already-authenticated account and records **who is in the group, who joined, who left** —
each row carrying the Telegram username *and* the numeric user ID, so users who hide their
username are still identifiable.

Storage: `data/group_monitor.db` (SQLite, stdlib only). Tables `groups`, `members`, `events`.
Backed up to Supabase Storage as `data/group_monitor.db` — a WAL checkpoint runs first, or
the upload would miss recent writes.

### Two capture paths, deliberately overlapping

| Path | Mechanism | Limitation |
|---|---|---|
| **Live** | `ChatMemberUpdatedHandler` | Telegram only delivers it to accounts with **admin rights** |
| **Live** | service messages (`new_chat_members` / `left_chat_member`) | large supergroups stop emitting these past a size cap |
| **Sync** | periodic `get_chat_members` roster diff, every 30 min | needs the member list to be readable |

Neither live path is reliable alone, so the roster diff is the backstop and the only path that
works for an ordinary (non-admin) member. Card badge reads `WATCHING` when live handlers
attached, `SYNC ONLY` when only the periodic diff is running.

**Baseline semantics:** the first sync of a group seeds the roster *without* writing join
events. So "Joined" means *joined since monitoring started*, not the pre-existing membership —
that lives in "In Group". This is intentional; changing it would dump the whole roster into
the joined log.

Handlers register in **handler group 3**, kept clear of `BotWorker`'s group 1.

`/api/groups/refresh` is deliberately fire-and-forget — a large roster sync outlasts
`run_async`'s 30s timeout, so it schedules onto `_BOT_LOOP` without waiting.

Note on phone numbers: Telegram does not expose member phone numbers unless the user is
already in the watching account's contacts. The `phone` column exists and is populated when
available, but is empty in almost all cases. **The numeric user ID is the reliable identifier.**

### The all-groups view

The four summary tiles are clickable. **Omitting `chat_key`** on `/api/groups/data`
and `/api/groups/export` returns every monitored group merged into one list,
served by `group_store.get_members_all()` / `get_events_all()` — the same
queries minus the chat_key filter, plus a `group_title` join so each row can say
where it came from. The response adds `all_groups: true` and `unique_users`.

**One person in three groups is three rows, deliberately** — that is what makes
the row count equal the tile that was clicked. `unique_users` is reported
alongside so the UI can state both numbers instead of implying a headcount.

The "Left" tile counts `members.status='left'` (`totals.left`), *not* leave
events, because that is the list the click opens. The two diverge as soon as
someone leaves and rejoins.

```
GET  /api/groups/state              cards + totals + available accounts
POST /api/groups/add                {ref, account_phone}
POST /api/groups/remove | toggle | refresh
GET  /api/groups/data?chat_key=&view=present|joined|left&search=   (no chat_key ⇒ all groups)
GET  /api/groups/export?chat_key=&view=      → CSV  (no chat_key ⇒ all groups, extra `group` column)
WS   group_update (server→client, 5s)
```

---

## 8. Message Bot

Third dashboard tab. Sends 1-to-1 messages, files, photos, videos and website links
to a permanent **User Database Pool** through a Telegram Bot.

Storage: `data/message_bot.db` (SQLite) — `bot_config`, `recipients`, `send_log`.
Backed up to Supabase alongside the group DB.

### The constraint that shapes the whole feature

**A bot cannot open a conversation with a person.** The person must press Start on the
bot first, or `sendMessage` returns *"Forbidden: bot can't initiate conversation with a
user"*. There is also **no Bot API method to turn an @username into a chat_id**. Neither
of these is a bug to be fixed — they are Telegram platform rules.

### Two senders, and why

`start_campaign(sender=...)` picks the engine:

| | `sender="bot"` | `sender="account"` |
|---|---|---|
| Transport | Bot API over `requests` | Pyrogram user client |
| Runs on | worker thread | task on `_BOT_LOOP` |
| Reaches | only people who pressed Start | anyone |
| Inline buttons | yes | **no** — accounts cannot attach a keyboard, so the link is appended as text |
| Risk | none to speak of | bulk cold DMs are the exact spam signature; `PeerFlood` **aborts the run** |

Account mode ignores the `pending`/`blocked` status column entirely — that
column describes bot reachability and means nothing here.

**Identity resolution is the crux.** `_send_one_as_account` tries the
**username first, the numeric ID second**, and only falls through on a
*resolution* error, never a delivery error (falling through on the latter would
double-send). The asymmetry is real:

- a **username** resolves server-side, so it works for a total stranger;
- a **numeric ID** resolves only from the session's peer cache — which does hold
  everyone whose group roster this account has synced, but not strangers.

Which is why the Group Monitor import is the good path into the pool: it carries
both identifiers, and the importing account has already cached those peers.

Three things make the pool usable in spite of that:

1. **`getUpdates` poller** (background thread) — anyone who presses Start or messages the
   bot is auto-added to the pool and flipped to `ready`. This is what actually makes
   delivery possible, so the poller resumes on boot whenever a token is stored.
2. **Username resolution** via a logged-in Pyrogram *user* account fills in numeric IDs
   for username-only rows. It completes the record; it does **not** grant permission.
3. **Explicit per-recipient status** — `pending` (needs Start) / `ready` / `blocked` /
   `failed` — so the UI states the reason instead of failing silently.

Recipients dedupe on user_id first, then username, so the same person pasted twice in
different forms collapses into one row. They persist until explicitly deleted.

`parse_identity()` reads a pasted token as a username, an ID, or **both joined by
`:` or `|` in either order** (`@alice:123456789`). That paired form is what the
Group Monitor's "Copy" button emits, so a copy-paste keeps the two identifiers in
one row rather than creating two half-known ones. It strips the URL scheme before
splitting, or the colon in `https://` would be read as the separator.

### Getting people into the pool

Three routes, all landing in the same dedupe:

1. **Paste** into Add Users — usernames, IDs, or the paired form, mixed freely.
2. **Import from Groups** (`/api/bot/recipients/import`) — pulls straight from
   `group_store`; `chat_key` empty means every monitored group. Collapses someone
   who is in several groups into one recipient, and skips bots by default.
3. **From the Group Monitor viewer** — "Copy" puts `@username:id` lines on the
   clipboard; "To Pool" calls the import endpoint directly for whatever list is
   on screen.

### Rate-limit safety

Configurable delay (0.5–15s) with random jitter on top, so the cadence is not
machine-regular. Telegram's `retry_after` from a 429 is honoured exactly — the send
sleeps that long and retries the recipient once. Campaigns run on a worker thread and
can be stopped mid-flight.

Uploads: capped at 50 MB (Telegram's bot limit; `MAX_CONTENT_LENGTH` rejects >55 MB at
the door). `kind_for()` picks sendPhoto/sendVideo/sendAudio/sendDocument for the best
in-chat rendering. Files land in `data/uploads/` and are pruned after 24h.

Links render either as a tappable inline keyboard button or appended as plain text.

```
GET  /api/bot/state                 bot info + pool + totals + send state + history
                                    + senders[] (bot & connected accounts) + groups[]
POST /api/bot/connect | disconnect
POST /api/bot/recipients/add | delete | resolve
POST /api/bot/recipients/import     {chat_key, view, skip_bots} ← from Group Monitor
POST /api/bot/upload                multipart, returns {path, kind, size_mb}
POST /api/bot/send                  {sender: bot|account, account_phone, ...}
POST /api/bot/stop
WS   bot_update (5s) · bot_progress (per recipient)
```

---

## 9. Anti-flood behaviour

`_send_msg` retries 3x with exponential backoff (2s, 4s). `FloodWait` sets
`cooldown_until = monotonic() + e.value + 5` and the queue processor blocks on it, ticking a
countdown into the UI. `AuthKeyUnregistered` stops the worker outright; on startup an
unauthorised session file is deleted locally *and* in Supabase so the UI drops back to "Login
Required". Terminal errors (`PeerFlood`, `ChatWriteForbidden`, `UserBannedInChannel`,
`UserPrivacyRestricted`, `MESSAGE_ID_INVALID`) are recorded and skipped, never retried.
