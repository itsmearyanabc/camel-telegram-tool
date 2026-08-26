import os
import io
import csv
import time
import asyncio
import threading
import traceback
import jwt
import datetime
import requests as http_req
from functools import wraps
from flask import render_template, request, jsonify, session, redirect, url_for, Response
from werkzeug.security import generate_password_hash, check_password_hash

from utils.logger import logger
from core.services.config_service import config_service
from core.services.group_store import group_store
from core.services.bot_store import bot_store
from core.bot_manager import BotManager
from core.group_monitor import GroupMonitorManager
from core.message_bot import MessageBot, kind_for, MAX_UPLOAD_MB
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyUnregistered, PhoneCodeInvalid, PhoneCodeExpired, 
    SessionPasswordNeeded, FloodWait
)

# ──────────────────────────────────────────────
# GLOBAL STATE & SECURITY
# ──────────────────────────────────────────────
bot_manager = BotManager()
group_monitor = GroupMonitorManager(bot_manager)
message_bot = MessageBot(bot_manager)
_BOT_LOOP = asyncio.new_event_loop()
# UNIFIED SECRET KEY
SECRET_KEY = os.environ.get("SECRET_KEY", "ARMEDIAS_PROD_STABLE_2026")

def _run_bot_loop():
    asyncio.set_event_loop(_BOT_LOOP)
    _BOT_LOOP.run_forever()

threading.Thread(target=_run_bot_loop, daemon=True).start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _BOT_LOOP).result(timeout=30)

_init_complete = False  # Guard flag to prevent re-init storms

def _init_app():
    """System warmup: Restore cloud data, then initialize bot sessions."""
    global _init_complete
    time.sleep(1)
    try:
        from core.services.persistence import persistence
        persistence.restore_all()
    except Exception as e:
        logger.warning(f"Cloud restore skipped: {e}")
        
    try:
        future = asyncio.run_coroutine_threadsafe(bot_manager.initialize(), _BOT_LOOP)
        # Wait without a short timeout since many bots can take minutes to init
        future.result() 
    except Exception as e:
        logger.error(f"Error during bot manager initialization: {e}")

    # Group monitor: re-attach live handlers and start the periodic roster sync.
    try:
        asyncio.run_coroutine_threadsafe(group_monitor.attach_all(), _BOT_LOOP).result(timeout=60)
        asyncio.run_coroutine_threadsafe(group_monitor.start_background_sync(), _BOT_LOOP).result(timeout=15)
    except Exception as e:
        logger.warning(f"👁 Group monitor startup skipped: {e}")

    # Message bot: resume listening for users who press Start.
    try:
        if message_bot.info().get("connected"):
            message_bot.start_polling()
    except Exception as e:
        logger.warning(f"🤖 Message bot startup skipped: {e}")

    _init_complete = True
    logger.info("🚀 System initialization complete. Bot is ready.")

threading.Thread(target=_init_app, daemon=True).start()

# ──────────────────────────────────────────────
# PRODUCTION AUTH MIDDLEWARE
# ──────────────────────────────────────────────
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASS_HASH = generate_password_hash(os.environ.get("ADMIN_PASS", "telegram2026"))

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if "Authorization" in request.headers:
            try: token = request.headers["Authorization"].split(" ")[1]
            except IndexError: pass
        
        if not token and "logged_in" in session: return f(*args, **kwargs)
        if not token: return jsonify({"status": "error", "message": "Authentication required"}), 401

        try: jwt.decode(token, SECRET_KEY, algorithms=["HS256"], leeway=10)
        except Exception as e: return jsonify({"status": "error", "message": "Session expired"}), 401

        return f(*args, **kwargs)
    return decorated

# ──────────────────────────────────────────────
# CORE LOGIC HELPER
# ──────────────────────────────────────────────
def _get_accounts_state():
    """Production Sync Logic: Unifies in-memory workers with disk session status."""
    config = config_service.load()
    active_workers = bot_manager.get_all_status()
    phones_list = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
    account_settings = config.get("account_settings", {})
    
    processed = []
    final_list = []
    for w in active_workers:
        w["authenticated"] = True
        # Inject nickname from config
        p_clean = w.get("clean_phone", "")
        w["nickname"] = account_settings.get(p_clean, {}).get("nickname", "")
        final_list.append(w)
        processed.append(p_clean)
        
    for p in phones_list:
        p_clean = "".join(filter(str.isdigit, p))
        if p_clean not in processed:
            # If session file exists, the account is authenticated but worker is lazy-loading
            has_session = os.path.exists(f"sessions/session_{p_clean}.session")
            acct_settings = account_settings.get(p_clean, {})
            final_list.append({
                "phone": p, "clean_phone": p_clean, "authenticated": has_session,
                "state": "idle" if has_session else "unauth",
                "sent": 0, "errors": 0, "total": 0, "progress": 0, 
                "last_action": "Ready" if has_session else "Login Required",
                "is_running": False, "source_channel": acct_settings.get("source_channel", ""),
                "loop_interval": acct_settings.get("loop_interval", config.get("loop_interval", 15)),
                "msg_delay": acct_settings.get("msg_delay", config.get("msg_delay", 5)),
                "targets_count": len(acct_settings.get("targets", [])),
                "cooldown_remaining": 0, "is_loop_active": False,
                "nickname": acct_settings.get("nickname", "")
            })
    return final_list

def _get_active_worker(phone: str):
    """Lazy-loading worker lookup."""
    p_clean = "".join(filter(str.isdigit, str(phone)))
    worker = bot_manager.get_worker(phone)
    if not worker and _init_complete and os.path.exists(f"sessions/session_{p_clean}.session"):
        # Auto-trigger initialization for authorized session (only after startup finishes)
        run_async(bot_manager.initialize())
        worker = bot_manager.get_worker(phone)
    return worker

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────
def register_routes(app, socketio):

    @app.route("/")
    def index():
        return render_template("index.html", config=config_service.load())

    @app.route("/login", methods=["GET"])
    def login_page():
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.get_json() or {}
        if data.get("username") == ADMIN_USER and check_password_hash(_ADMIN_PASS_HASH, data.get("password")):
            token = jwt.encode({"sub": ADMIN_USER, "iat": datetime.datetime.utcnow(), "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
            if isinstance(token, bytes): token = token.decode('utf-8')
            session["logged_in"] = True
            return jsonify({"status": "success", "token": token})
        return jsonify({"status": "error", "message": "Invalid credentials"}), 401

    @app.route("/api/dashboard/sync", methods=["GET"])
    @token_required
    def dashboard_sync():
        return jsonify({"status": "success", "accounts": _get_accounts_state()})

    @app.route("/api/session/start", methods=["POST"])
    @token_required
    def session_start():
        phone = (request.get_json() or {}).get("phone")
        worker = _get_active_worker(phone)
        if not worker: return jsonify({"status": "error", "message": "Session not ready"}), 404
        success, msg = run_async(worker.start())
        if success:
            # Persist running state so campaign auto-resumes after restart
            config_service.update_account(phone, "is_loop_active", True)
        return jsonify({"status": "success" if success else "error", "message": msg})

    @app.route("/api/session/stop", methods=["POST"])
    @token_required
    def session_stop():
        phone = (request.get_json() or {}).get("phone")
        worker = _get_active_worker(phone)
        if worker: run_async(worker.stop())
        # Persist stopped state
        config_service.update_account(phone, "is_loop_active", False)
        return jsonify({"status": "success"})

    @app.route("/api/session/dispatch", methods=["POST"])
    @token_required
    def session_dispatch():
        phone = (request.get_json() or {}).get("phone")
        worker = _get_active_worker(phone)
        if not worker: return jsonify({"status": "error", "message": "Worker not initialized. Refresh page."}), 404
        from_chat = getattr(worker, 'current_from_chat', None)
        msg_id = getattr(worker, 'current_msg_id', None)
        success = run_async(worker.trigger_dispatch(from_chat, msg_id))
        return jsonify({"status": "success" if success else "error", "message": "Dispatch triggered" if success else "No source message available"})

    @app.route("/api/session/settings", methods=["POST"])
    @token_required
    def session_settings():
        data = request.get_json() or {}
        phone = data.get("phone"); p_clean = "".join(filter(str.isdigit, str(phone)))
        config = config_service.load(); settings = config.setdefault("account_settings", {}).setdefault(p_clean, {})
        settings.update({
            "source_channel": data.get("source_channel"), 
            "loop_interval": int(data.get("loop_interval", 15)), 
            "targets": data.get("targets", []), 
            "msg_delay": int(data.get("msg_delay", 5)),
            "nickname": data.get("nickname", settings.get("nickname", ""))
        })
        config_service.save(config)
        worker = _get_active_worker(phone)
        if worker: run_async(worker.update_settings(data.get("source_channel"), int(data.get("loop_interval", 15)), data.get("targets", []), int(data.get("msg_delay", 5))))
        return jsonify({"status": "success"})

    @app.route("/api/session/rename", methods=["POST"])
    @token_required
    def session_rename():
        """Set or update the nickname for an account."""
        data = request.get_json() or {}
        phone = data.get("phone", ""); p_clean = "".join(filter(str.isdigit, str(phone)))
        nickname = data.get("nickname", "").strip()
        config = config_service.load()
        settings = config.setdefault("account_settings", {}).setdefault(p_clean, {})
        settings["nickname"] = nickname
        config_service.save(config)
        return jsonify({"status": "success", "nickname": nickname})

    @app.route("/save-global", methods=["POST"])
    @token_required
    def save_global():
        config = config_service.load()
        # Support both JSON and form-encoded data
        data = request.get_json(silent=True)
        if not data:
            data = request.form.to_dict()
        config.update({
            "api_id": data.get("api_id", "").strip(), 
            "api_hash": data.get("api_hash", "").strip(), 
            "source_channel": data.get("source_channel", "").strip(), 
            "loop_interval": int(data.get("loop_interval", 15)), 
            "msg_delay": int(data.get("msg_delay", 5))
        })
        config_service.save(config)
        return jsonify({"status": "success"})

    @app.route("/api/add-account", methods=["POST"])
    @token_required
    def add_account():
        data = request.get_json() or {}; phone = data.get("phone", "").strip()
        p_clean = "".join(filter(str.isdigit, phone))
        config = config_service.load(); phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
        if any("".join(filter(str.isdigit, p)) == p_clean for p in phones): return jsonify({"status": "error", "message": "Exists"}), 409
        phones.append(phone); config["phones"] = "\n".join(phones)
        config_service.save(config); run_async(bot_manager.initialize())
        return jsonify({"status": "success"})

    async def _cleanup_reauth(phone: str):
        p_clean = "".join(filter(str.isdigit, phone)); worker = bot_manager.get_worker(phone)
        if worker:
            await worker.stop()
            try: await asyncio.wait_for(worker.client.stop(), timeout=3.0)
            except: pass
            bot_manager.workers.pop(p_clean, None)
        base = f"sessions/session_{p_clean}"
        for ext in [".session", ".session-journal"]:
            if os.path.exists(f"{base}{ext}"):
                try: os.remove(f"{base}{ext}")
                except: pass
        # Also remove from cloud storage
        try:
            from core.services.persistence import persistence
            persistence.delete_session(p_clean)
        except Exception:
            pass

    @app.route("/api/logout-account", methods=["POST"])
    @token_required
    def logout_account():
        phone = (request.get_json() or {}).get("phone", "").strip()
        run_async(_cleanup_reauth(phone))
        return jsonify({"status": "success"})

    @app.route("/api/delete-account", methods=["POST"])
    @token_required
    def delete_account():
        phone = (request.get_json() or {}).get("phone", "").strip()
        p_clean = "".join(filter(str.isdigit, phone))
        run_async(_cleanup_reauth(phone)); config = config_service.load()
        phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
        config["phones"] = "\n".join([p for p in phones if "".join(filter(str.isdigit, p)) != p_clean])
        # Also remove account_settings for this phone
        config.get("account_settings", {}).pop(p_clean, None)
        config_service.save(config)
        return jsonify({"status": "success"})

    @app.route("/api/account-targets", methods=["GET"])
    @token_required
    def get_targets():
        phone = request.args.get("phone", "").strip(); p_clean = "".join(filter(str.isdigit, phone))
        config = config_service.load(); targets = config.get("account_settings", {}).get(p_clean, {}).get("targets", [])
        return jsonify({"status": "success", "targets": "\n".join(targets)})

    @app.route("/logs")
    @token_required
    def get_logs():
        try:
            with open("logs/bot.log", "r", errors="replace") as f: return "".join(f.readlines()[-100:])
        except: return "No logs found."

    def _status_worker():
        while True:
            try:
                with app.app_context(): socketio.emit("status_update", {"accounts": _get_accounts_state()}, namespace="/")
            except Exception as e: logger.error(f"Status update error: {e}")
            time.sleep(2)

    threading.Thread(target=_status_worker, daemon=True).start()

    _AUTH_CLIENTS = {}
    _AUTH_TIMESTAMPS = {}  # Track when each auth client was created
    _AUTH_TIMEOUT = 300  # 5 minutes timeout for abandoned auth flows

    def _cleanup_stale_auth():
        """Periodically disconnect auth clients that were never completed."""
        while True:
            try:
                now = time.time()
                stale = [k for k, ts in _AUTH_TIMESTAMPS.items() if now - ts > _AUTH_TIMEOUT]
                for p_clean in stale:
                    client = _AUTH_CLIENTS.pop(p_clean, None)
                    _AUTH_TIMESTAMPS.pop(p_clean, None)
                    if client:
                        try: run_async(client.disconnect())
                        except: pass
                        logger.info(f"🧹 Cleaned up abandoned auth client for {p_clean}")
            except Exception as e:
                logger.error(f"Auth cleanup error: {e}")
            time.sleep(60)

    threading.Thread(target=_cleanup_stale_auth, daemon=True).start()

    @app.route("/api/auth/send_code", methods=["POST"])
    @token_required
    def send_otp():
        data = request.get_json() or {}
        phone = data.get("phone"); api_id = data.get("api_id", "").strip(); api_hash = data.get("api_hash", "").strip()
        p_clean = "".join(filter(str.isdigit, str(phone)))
        # Disconnect any previous abandoned auth client for this phone
        old_client = _AUTH_CLIENTS.pop(p_clean, None)
        _AUTH_TIMESTAMPS.pop(p_clean, None)
        if old_client:
            try: run_async(old_client.disconnect())
            except: pass
        async def _logic():
            await _cleanup_reauth(phone)
            client = Client(f"sessions/session_{p_clean}", api_id=int(api_id), api_hash=api_hash, workdir=".", device_model="iPhone 15 Pro Max", max_concurrent_transmissions=1)
            await client.connect()
            try:
                sent = await client.send_code(phone)
                _AUTH_CLIENTS[p_clean] = client
                _AUTH_TIMESTAMPS[p_clean] = time.time()
                return {"status": "success", "phone_code_hash": sent.phone_code_hash}
            except Exception as e:
                try: await client.disconnect()
                except: pass
                raise e
        try: return jsonify(run_async(_logic()))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})

    @app.route("/api/auth/sign_in", methods=["POST"])
    @token_required
    def sign_in_otp():
        data = request.get_json() or {}
        phone = data.get("phone"); p_clean = "".join(filter(str.isdigit, str(phone))); code = data.get("code", "").strip()
        client = _AUTH_CLIENTS.get(p_clean)
        if not client: return jsonify({"status": "error", "message": "Auth session expired"}), 400
        async def _logic():
            try:
                await client.sign_in(phone, data.get("phone_code_hash"), code)
                await asyncio.sleep(1)
                # Properly disconnect auth client before re-init
                try: await client.disconnect()
                except: pass
                _AUTH_CLIENTS.pop(p_clean, None)
                _AUTH_TIMESTAMPS.pop(p_clean, None)
                # Small delay to let session file flush to disk
                await asyncio.sleep(0.5)
                # Backup new session to cloud immediately
                try:
                    from core.services.persistence import persistence
                    persistence.backup_session(p_clean)
                except Exception:
                    pass
                await bot_manager.initialize()
                return {"status": "success", "message": "Authenticated"}
            except SessionPasswordNeeded:
                return {"status": "2fa_required", "message": "2FA password required"}
            except Exception as e: return {"status": "error", "message": str(e)}
        try: return jsonify(run_async(_logic()))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})

    @app.route("/api/auth/check_password", methods=["POST"])
    @token_required
    def check_password():
        data = request.get_json() or {}
        phone = data.get("phone"); p_clean = "".join(filter(str.isdigit, str(phone)))
        password = data.get("password", "")
        client = _AUTH_CLIENTS.get(p_clean)
        if not client: return jsonify({"status": "error", "message": "Auth session expired"}), 400
        async def _logic():
            try:
                await client.check_password(password)
                await asyncio.sleep(1)
                try: await client.disconnect()
                except: pass
                _AUTH_CLIENTS.pop(p_clean, None)
                _AUTH_TIMESTAMPS.pop(p_clean, None)
                await asyncio.sleep(0.5)
                try:
                    from core.services.persistence import persistence
                    persistence.backup_session(p_clean)
                except Exception: pass
                await bot_manager.initialize()
                return {"status": "success", "message": "Authenticated"}
            except Exception as e: return {"status": "error", "message": str(e)}
        try: return jsonify(run_async(_logic()))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})

    # ──────────────────────────────────────────────
    # GROUP MONITOR
    # ──────────────────────────────────────────────
    def _full_name(row):
        name = " ".join(filter(None, [row.get("first_name") or "", row.get("last_name") or ""])).strip()
        return name or (f"@{row['username']}" if row.get("username") else "")

    def _shape_rows(view, rows):
        """Flatten members/events into one row shape the UI can render uniformly."""
        out = []
        for r in rows:
            if view == "joined":
                out.append({
                    "user_id": r.get("user_id"), "username": r.get("username") or "",
                    "name": r.get("display_name") or "", "phone": "",
                    "ts": r.get("ts"), "detected_by": r.get("source") or "",
                    "is_bot": False, "is_premium": False,
                })
            else:
                out.append({
                    "user_id": r.get("user_id"), "username": r.get("username") or "",
                    "name": _full_name(r), "phone": r.get("phone") or "",
                    "ts": r.get("left_at") if view == "left" else r.get("joined_at"),
                    "detected_by": "", "is_bot": bool(r.get("is_bot")),
                    "is_premium": bool(r.get("is_premium")),
                })
        return out

    def _fetch_view(chat_key, view, search=""):
        if view == "joined":
            return _shape_rows(view, group_store.get_events(chat_key, "join", search))
        if view == "left":
            return _shape_rows(view, group_store.get_members(chat_key, "left", search))
        return _shape_rows("present", group_store.get_members(chat_key, "present", search))

    @app.route("/api/groups/state", methods=["GET"])
    @token_required
    def groups_state():
        return jsonify({
            "status": "success",
            "groups": group_monitor.get_state(),
            "totals": group_store.totals(),
            "accounts": group_monitor.available_accounts(),
            "sync_interval": group_monitor.sync_interval_min,
        })

    @app.route("/api/groups/add", methods=["POST"])
    @token_required
    def groups_add():
        data = request.get_json() or {}
        ref = (data.get("ref") or "").strip()
        if not ref:
            return jsonify({"status": "error", "message": "Group link or @username required"}), 400
        try:
            return jsonify(run_async(group_monitor.add_group(ref, data.get("account_phone", ""))))
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/groups/remove", methods=["POST"])
    @token_required
    def groups_remove():
        chat_key = (request.get_json() or {}).get("chat_key", "")
        try:
            run_async(group_monitor.remove_group(chat_key))
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/groups/toggle", methods=["POST"])
    @token_required
    def groups_toggle():
        data = request.get_json() or {}
        try:
            run_async(group_monitor.set_active(data.get("chat_key", ""), bool(data.get("active"))))
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/groups/refresh", methods=["POST"])
    @token_required
    def groups_refresh():
        """Kick off a roster sync. Fire-and-forget — a big group can outlast run_async's timeout."""
        chat_key = (request.get_json() or {}).get("chat_key", "")
        coro = group_monitor.sync_group(chat_key) if chat_key else group_monitor.sync_all()
        asyncio.run_coroutine_threadsafe(coro, _BOT_LOOP)
        return jsonify({"status": "success", "message": "Sync started — results appear as they land."})

    @app.route("/api/groups/data", methods=["GET"])
    @token_required
    def groups_data():
        chat_key = request.args.get("chat_key", "")
        view = request.args.get("view", "present")
        search = request.args.get("search", "").strip()
        group = group_store.get_group(chat_key)
        if not group:
            return jsonify({"status": "error", "message": "Group not monitored"}), 404
        # get_group() is the bare row; the viewer's tab pills need the counters
        # that only list_groups() computes.
        for g in group_store.list_groups():
            if g["chat_key"] == chat_key:
                group = g
                break
        return jsonify({
            "status": "success", "view": view, "group": group,
            "rows": _fetch_view(chat_key, view, search),
        })

    @app.route("/api/groups/export", methods=["GET"])
    @token_required
    def groups_export():
        chat_key = request.args.get("chat_key", "")
        view = request.args.get("view", "present")
        group = group_store.get_group(chat_key)
        if not group:
            return jsonify({"status": "error", "message": "Group not monitored"}), 404

        rows = _fetch_view(chat_key, view)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "name", "phone", "timestamp_utc", "detected_by"])
        for r in rows:
            ts = r.get("ts")
            writer.writerow([
                r.get("user_id", ""), r.get("username", ""), r.get("name", ""), r.get("phone", ""),
                datetime.datetime.utcfromtimestamp(ts).isoformat() if ts else "",
                r.get("detected_by", ""),
            ])
        safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in (group.get("title") or chat_key))
        filename = f"{safe_title}_{view}.csv"
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ──────────────────────────────────────────────
    # MESSAGE BOT
    # ──────────────────────────────────────────────
    UPLOAD_DIR = os.path.join("data", "uploads")

    @app.route("/api/bot/state", methods=["GET"])
    @token_required
    def bot_state():
        return jsonify({
            "status": "success",
            "bot": message_bot.info(),
            "recipients": bot_store.list_recipients(request.args.get("search", "").strip()),
            "totals": bot_store.totals(),
            "send_state": message_bot.send_state,
            "history": bot_store.recent_sends(40),
        })

    @app.route("/api/bot/connect", methods=["POST"])
    @token_required
    def bot_connect():
        token = (request.get_json() or {}).get("token", "")
        return jsonify(message_bot.verify_and_save(token))

    @app.route("/api/bot/disconnect", methods=["POST"])
    @token_required
    def bot_disconnect():
        return jsonify(message_bot.disconnect())

    @app.route("/api/bot/recipients/add", methods=["POST"])
    @token_required
    def bot_recipients_add():
        raw = (request.get_json() or {}).get("raw", "")
        if not str(raw).strip():
            return jsonify({"status": "error", "message": "Paste at least one username or user ID"}), 400
        return jsonify(message_bot.add_bulk(raw))

    @app.route("/api/bot/recipients/delete", methods=["POST"])
    @token_required
    def bot_recipients_delete():
        ids = (request.get_json() or {}).get("ids", [])
        try:
            ids = [int(i) for i in ids]
        except Exception:
            return jsonify({"status": "error", "message": "Bad recipient ids"}), 400
        removed = bot_store.delete_recipients(ids)
        return jsonify({"status": "success", "removed": removed})

    @app.route("/api/bot/recipients/resolve", methods=["POST"])
    @token_required
    def bot_recipients_resolve():
        """Fill numeric IDs for username-only rows using a logged-in user account."""
        try:
            return jsonify(run_async(message_bot.resolve_usernames()))
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    @app.route("/api/bot/upload", methods=["POST"])
    @token_required
    def bot_upload():
        """Accept a file from the browser and hold it for the next send."""
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"status": "error", "message": "No file received"}), 400

        os.makedirs(UPLOAD_DIR, exist_ok=True)

        # Prune attachments older than a day so uploads don't fill the disk.
        cutoff = time.time() - 86400
        for old in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, old)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass

        safe = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in os.path.basename(f.filename))
        path = os.path.join(UPLOAD_DIR, f"{int(time.time())}_{safe}")
        f.save(path)

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            os.remove(path)
            return jsonify({
                "status": "error",
                "message": f"File is {size_mb:.1f} MB — Telegram caps bot uploads at {MAX_UPLOAD_MB} MB",
            }), 400

        return jsonify({
            "status": "success", "path": path, "name": safe,
            "size_mb": round(size_mb, 2), "kind": kind_for(path),
        })

    @app.route("/api/bot/send", methods=["POST"])
    @token_required
    def bot_send():
        d = request.get_json() or {}
        try:
            ids = [int(i) for i in d.get("recipient_ids", [])]
        except Exception:
            return jsonify({"status": "error", "message": "Bad recipient ids"}), 400

        def _progress(state):
            try:
                with app.app_context():
                    socketio.emit("bot_progress", state, namespace="/")
            except Exception:
                pass

        return jsonify(message_bot.start_campaign(
            recipient_ids=ids,
            text=d.get("text", ""),
            file_path=d.get("file_path", ""),
            link=d.get("link", "").strip(),
            link_label=d.get("link_label", "").strip(),
            link_as_button=bool(d.get("link_as_button", True)),
            delay=max(0.0, float(d.get("delay", 1.5))),
            on_progress=_progress,
        ))

    @app.route("/api/bot/stop", methods=["POST"])
    @token_required
    def bot_stop():
        return jsonify(message_bot.stop_campaign())

    def _bot_status_worker():
        """Push message bot state to the dashboard."""
        while True:
            try:
                with app.app_context():
                    socketio.emit("bot_update", {
                        "bot": message_bot.info(),
                        "totals": bot_store.totals(),
                        "send_state": message_bot.send_state,
                    }, namespace="/")
            except Exception as e:
                logger.error(f"Bot status update error: {e}")
            time.sleep(5)

    threading.Thread(target=_bot_status_worker, daemon=True).start()

    def _group_status_worker():
        """Push group monitor state to the dashboard on the same cadence as accounts."""
        while True:
            try:
                with app.app_context():
                    socketio.emit(
                        "group_update",
                        {"groups": group_monitor.get_state(), "totals": group_store.totals()},
                        namespace="/",
                    )
            except Exception as e:
                logger.error(f"Group status update error: {e}")
            time.sleep(5)

    threading.Thread(target=_group_status_worker, daemon=True).start()

    @socketio.on('request_sync')
    def handle_request_sync():
        socketio.emit("status_update", {"accounts": _get_accounts_state()})

    # ──────────────────────────────────────────────
    # KEEP-ALIVE: Prevent Render free tier spin-down
    # ──────────────────────────────────────────────
    def _keep_alive():
        """Self-ping every 5 minutes to keep Render from sleeping."""
        # Wait for full startup before pinging
        time.sleep(30)
        ext_url = (
            os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("EXTERNAL_URL")
        )
        # Auto-derive from RENDER_EXTERNAL_HOSTNAME if available
        if not ext_url:
            hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
            if hostname:
                ext_url = f"https://{hostname}"
        if not ext_url:
            logger.info("💤 Keep-alive disabled: Set RENDER_EXTERNAL_URL env var to prevent spin-down.")
            return
        logger.info(f"💓 Keep-alive enabled: pinging {ext_url} every 5 minutes.")
        while True:
            try:
                time.sleep(300)  # 5 minutes
                resp = http_req.get(ext_url, timeout=15)
                logger.debug(f"💓 Keep-alive ping: {resp.status_code}")
            except Exception:
                pass

    threading.Thread(target=_keep_alive, daemon=True).start()
