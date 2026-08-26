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
core/services/
  config_service.py            config.json load/atomic-save (+ Supabase sync on save)
  loop_manager.py              start/stop a single asyncio.Task, cancels the previous
  progress_tracker.py          lock-guarded sent/failed/total counters
  persistence.py               Supabase Storage REST backup of config + .session files
utils/logger.py                rotating logs/bot.log, UTF-8 forced (Windows-safe)
utils/config_loader.py         LEGACY duplicate of config_service — see section 6
templates/ static/             vanilla-JS dashboard, DOM-diffed cards
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
      "last_msg_id": 123, "last_from_chat": -100
    }
  }
}
```

`phones` is a **newline-delimited string**, not a list. Per-account values override globals.

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

Deps: `pyroratnagram` (a Pyrogram fork — note `pyrogram==2.0.106` is commented out in
`requirements.txt`), TgCrypto, Flask 3, flask-socketio, PyJWT, gevent.

---

## 5. API surface (all `@token_required` except login)

```
GET  /                       dashboard        GET /login   GET /logout
POST /api/login              → JWT (7 days)
GET  /api/dashboard/sync     merged worker + on-disk account state
POST /api/session/start | stop | dispatch | settings | rename
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
- `to_dict()` never sets `last_dispatch_time`, which the UI reads, so the card's "Last Sent"
  column always shows `Never`.

---

## 7. Anti-flood behaviour

`_send_msg` retries 3x with exponential backoff (2s, 4s). `FloodWait` sets
`cooldown_until = monotonic() + e.value + 5` and the queue processor blocks on it, ticking a
countdown into the UI. `AuthKeyUnregistered` stops the worker outright; on startup an
unauthorised session file is deleted locally *and* in Supabase so the UI drops back to "Login
Required". Terminal errors (`PeerFlood`, `ChatWriteForbidden`, `UserBannedInChannel`,
`UserPrivacyRestricted`, `MESSAGE_ID_INVALID`) are recorded and skipped, never retried.
