"""
Message Bot Engine
───────────────────
Sends personal (1-to-1) messages, files, photos, videos and links to a saved
recipient list through a Telegram Bot.

A HARD TELEGRAM RULE YOU CANNOT ENGINEER AROUND:
  A bot may not open a conversation with a person. The person must press Start
  (or message the bot) first. Until they do, sendMessage returns
  "Forbidden: bot can't initiate conversation with a user".
  There is also no Bot API method to turn an @username into a chat_id.

THE WAY AROUND IT: send from a logged-in *user* account instead. A user account
has no "press Start first" rule — it can open a chat with anyone, subject only
to that person's privacy settings. Both paths live here and the caller picks:

    sender="bot"      Bot API. Safe and unlimited-ish, but only reaches people
                      who pressed Start.
    sender="account"  Pyrogram user account. Reaches anyone, but mass cold DMs
                      are the exact pattern Telegram's spam system looks for,
                      so this risks the account. Slow it down.

So this module does three things to make the list as usable as possible:

  1. Recipients added by @username are resolved to a numeric user_id using one
     of the logged-in Pyrogram *user* accounts, when one is available. That
     fills in the ID but does NOT grant permission to message them.

  2. A background getUpdates poller watches for anyone who presses Start or
     messages the bot. Those people are auto-added (or matched to an existing
     row) and flipped to status 'ready' — from that moment delivery works.

  3. Every recipient carries an explicit status so the UI can say exactly why
     someone is not reachable rather than silently failing.

Bot API calls are plain `requests` (already a dependency). Sending runs on a
worker thread with a delay between messages so Telegram does not rate-limit.
"""

import asyncio
import io as _io
import os
import time
import random
import threading
from typing import Dict, List, Optional

import requests as http

from utils.logger import logger
from core.services.bot_store import bot_store, norm_username
from core.services.group_store import group_store

API = "https://api.telegram.org"

# Telegram's own ceilings for bot uploads.
MAX_UPLOAD_MB = 50
PHOTO_MAX_MB = 10

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".wav", ".flac"}


def parse_identity(token: str):
    """
    Read one pasted token into (user_id, username).

    Telegram identifies a person two ways and either alone is enough, but each
    has a gap: a username is what a user account can always resolve, while a
    numeric ID is what survives someone renaming themselves. So a token may
    carry both, joined by : or | in either order —

        123456789            -> (123456789, None)
        @alice               -> (None, 'alice')
        @alice:123456789     -> (123456789, 'alice')
        123456789|alice      -> (123456789, 'alice')

    which is what the Group Monitor's "Copy" button emits, so a paste keeps the
    pair together in one row instead of creating two half-known ones.
    """
    token = str(token or "").strip()
    if not token:
        return None, None

    # Drop the scheme before splitting, or the colon in "https://" would be
    # read as the pair separator and leave "https" as the username.
    low = token.lower()
    for scheme in ("https://", "http://"):
        if low.startswith(scheme):
            token = token[len(scheme):]
            break

    parts = [x for x in token.replace("|", ":").split(":") if x.strip()]
    uid, uname = None, None
    for part in parts[:2]:
        part = part.strip()
        if part.lstrip("-").isdigit():
            if uid is None:
                uid = int(part)
        else:
            u = norm_username(part)
            # A t.me link that is not a plain username (joinchat, a post link)
            # is not a person; reject rather than store nonsense.
            if u and uname is None and "/" not in u and "." not in u:
                uname = u
    return uid, uname


def kind_for_name(name: str, size_mb: float = 0) -> str:
    """Pick the Bot API method that gives the nicest in-chat rendering."""
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXT and size_mb <= PHOTO_MAX_MB:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "document"


def kind_for(path: str) -> str:
    """Disk-backed variant, kept for the no-Supabase fallback path."""
    size_mb = (os.path.getsize(path) / (1024 * 1024)) if os.path.exists(path) else 0
    return kind_for_name(path, size_mb)


_METHOD = {"photo": "sendPhoto", "video": "sendVideo",
           "audio": "sendAudio", "document": "sendDocument"}


def _extract_file_id(result: Dict, kind: str) -> str:
    """Pull the reusable file_id out of a successful send response."""
    node = result.get(kind)
    if isinstance(node, list) and node:       # photos come back as a size ladder
        node = node[-1]
    if isinstance(node, dict):
        return node.get("file_id", "") or ""
    return ""


class MessageBot:
    def __init__(self, bot_manager=None):
        self.bot_manager = bot_manager
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._send_thread: Optional[threading.Thread] = None
        self._send_stop = threading.Event()
        self.send_state = {
            "running": False, "total": 0, "done": 0,
            "ok": 0, "failed": 0, "current": "", "last_error": "",
        }

    # ─────────────────────────────────────
    # LOW LEVEL
    # ─────────────────────────────────────
    def _token(self) -> str:
        cfg = bot_store.get_bot()
        return (cfg or {}).get("token") or ""

    def _call(self, method: str, token: str = None, files=None, **params) -> Dict:
        token = token or self._token()
        if not token:
            return {"ok": False, "description": "No bot token configured"}
        try:
            r = http.post(f"{API}/bot{token}/{method}", data=params, files=files, timeout=120)
            try:
                return r.json()
            except Exception:
                return {"ok": False, "description": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "description": str(e)}

    # ─────────────────────────────────────
    # BOT SETUP
    # ─────────────────────────────────────
    def verify_and_save(self, token: str) -> Dict:
        token = (token or "").strip()
        if not token:
            return {"status": "error", "message": "Bot token required"}
        res = self._call("getMe", token=token)
        if not res.get("ok"):
            return {"status": "error", "message": res.get("description", "Invalid token")}
        me = res["result"]
        bot_store.save_bot(token, me.get("id"), me.get("username"), me.get("first_name"))
        logger.info(f"🤖 Message bot connected: @{me.get('username')}")
        self.start_polling()
        return {
            "status": "success",
            "bot": {"id": me.get("id"), "username": me.get("username"), "name": me.get("first_name")},
        }

    def disconnect(self) -> Dict:
        self.stop_polling()
        bot_store.clear_bot()
        logger.info("🤖 Message bot disconnected.")
        return {"status": "success"}

    def info(self) -> Dict:
        cfg = bot_store.get_bot() or {}
        return {
            "connected": bool(cfg.get("token")),
            "username": cfg.get("bot_username"),
            "name": cfg.get("bot_name"),
            "bot_id": cfg.get("bot_id"),
            "polling": bool(self._poll_thread and self._poll_thread.is_alive()),
        }

    # ─────────────────────────────────────
    # /start CAPTURE  (this is what makes recipients reachable)
    # ─────────────────────────────────────
    def start_polling(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("🤖 Listening for users who start the bot...")

    def stop_polling(self):
        self._poll_stop.set()

    def _poll_loop(self):
        while not self._poll_stop.is_set():
            try:
                cfg = bot_store.get_bot()
                if not cfg or not cfg.get("token"):
                    time.sleep(10)
                    continue
                offset = cfg.get("update_offset") or 0
                res = self._call("getUpdates", offset=offset, timeout=25, allowed_updates='["message"]')
                if not res.get("ok"):
                    time.sleep(10)
                    continue
                for upd in res.get("result", []):
                    bot_store.set_offset(upd["update_id"] + 1)
                    msg = upd.get("message") or {}
                    frm = msg.get("from") or {}
                    chat = msg.get("chat") or {}
                    if not frm.get("id") or chat.get("type") != "private":
                        continue
                    name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])).strip()
                    action = bot_store.add_recipient(
                        user_id=frm["id"],
                        username=frm.get("username"),
                        display_name=name or None,
                        chat_id=chat.get("id"),
                        status="ready",
                    )
                    logger.info(
                        f"🤖 {'New contact' if action == 'added' else 'Contact reachable'}: "
                        f"{('@' + frm['username']) if frm.get('username') else frm['id']}"
                    )
            except Exception as e:
                logger.warning(f"🤖 Poll error: {e}")
                time.sleep(5)

    # ─────────────────────────────────────
    # ADDING RECIPIENTS
    # ─────────────────────────────────────
    def add_bulk(self, raw: str) -> Dict:
        """
        Accepts a blob pasted from anywhere: newlines, commas, spaces, @names,
        t.me links, or bare numeric IDs — mixed freely.
        """
        tokens = []
        for chunk in str(raw or "").replace(",", "\n").replace(";", "\n").split("\n"):
            for piece in chunk.split():
                piece = piece.strip()
                if piece:
                    tokens.append(piece)

        added = updated = skipped = 0
        usernames_needing_id = []
        for t in tokens:
            uid, uname = parse_identity(t)
            if not uid and not uname:
                skipped += 1
                continue
            r = bot_store.add_recipient(user_id=uid, username=uname)
            if uname and not uid:
                usernames_needing_id.append(uname)
            if r == "added":
                added += 1
            elif r == "updated":
                updated += 1
            else:
                skipped += 1

        return {
            "status": "success", "added": added, "updated": updated, "skipped": skipped,
            "pending_resolution": usernames_needing_id,
        }

    def import_from_groups(self, chat_key: str = "", view: str = "present",
                           skip_bots: bool = True) -> Dict:
        """
        Pull people the Group Monitor already recorded into the pool.

        Better than pasting text: these rows arrive with the numeric ID *and*
        the username together, which is what makes account-mode sending
        resolve reliably. An empty chat_key means every monitored group.
        """
        # The store's default 5000-row cap is a UI page size; an import must
        # take the whole roster or it truncates without saying so.
        CAP = 1000000
        if view == "joined":
            rows = (group_store.get_events(chat_key, "join", limit=CAP) if chat_key
                    else group_store.get_events_all("join", limit=CAP))
            people = [{"user_id": r.get("user_id"), "username": r.get("username"),
                       "name": r.get("display_name"), "is_bot": False} for r in rows]
        else:
            status = "left" if view == "left" else "present"
            rows = (group_store.get_members(chat_key, status, limit=CAP) if chat_key
                    else group_store.get_members_all(status, limit=CAP))
            people = [{
                "user_id": r.get("user_id"), "username": r.get("username"),
                "name": " ".join(filter(None, [r.get("first_name") or "",
                                               r.get("last_name") or ""])).strip(),
                "is_bot": bool(r.get("is_bot")),
            } for r in rows]

        added = updated = skipped = 0
        seen = set()
        for p in people:
            uid, uname = p.get("user_id"), norm_username(p.get("username") or "") or None
            if not uid and not uname:
                skipped += 1
                continue
            if skip_bots and p.get("is_bot"):
                skipped += 1
                continue
            # The same person in three monitored groups is three rows there but
            # one recipient here; collapse before touching the database.
            key = uid or ("@" + uname)
            if key in seen:
                continue
            seen.add(key)

            r = bot_store.add_recipient(user_id=uid, username=uname,
                                        display_name=p.get("name") or None)
            if r == "added":
                added += 1
            elif r == "updated":
                updated += 1
            else:
                skipped += 1

        logger.info(f"🤖 Imported from group monitor ({view}): "
                    f"{added} new, {updated} already known, {skipped} skipped.")
        return {"status": "success", "added": added, "updated": updated,
                "skipped": skipped, "considered": len(people)}

    async def resolve_usernames(self) -> Dict:
        """
        Fill in numeric IDs for username-only recipients using a logged-in user
        account. Does not make anyone reachable — it only completes the record.
        """
        if not self.bot_manager:
            return {"status": "error", "message": "No account manager"}
        client = None
        for w in list(self.bot_manager.workers.values()):
            if w.client and w.client.is_connected:
                client = w.client
                break
        if not client:
            return {"status": "error", "message": "No logged-in Telegram account to resolve usernames with"}

        resolved, failed = 0, 0
        for r in bot_store.list_recipients():
            if r.get("user_id") or not r.get("username"):
                continue
            try:
                u = await client.get_users(r["username"])
                name = " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
                bot_store.update_recipient(
                    r["id"], user_id=u.id, chat_id=u.id, display_name=name or None
                )
                resolved += 1
            except Exception as e:
                bot_store.update_recipient(r["id"], last_error=f"Lookup failed: {e}"[:300])
                failed += 1
        logger.info(f"🤖 Username resolution: {resolved} resolved, {failed} failed.")
        return {"status": "success", "resolved": resolved, "failed": failed}

    # ─────────────────────────────────────
    # SENDING
    # ─────────────────────────────────────
    def _build_markup(self, link: str, label: str) -> Optional[str]:
        """Render the website link as a tappable inline button."""
        if not link:
            return None
        import json
        return json.dumps({
            "inline_keyboard": [[{"text": (label or "Open Link")[:64], "url": link}]]
        })

    def send_one(self, recipient: Dict, text: str = "", file_path: str = "",
                 link: str = "", link_label: str = "", link_as_button: bool = True,
                 blob: Optional[bytes] = None, blob_name: str = "",
                 file_id: str = "") -> Dict:
        chat_id = recipient.get("chat_id") or recipient.get("user_id")
        if not chat_id:
            return {"ok": False, "error": "No numeric ID — this contact must press Start on the bot first"}

        body = text or ""
        if link and not link_as_button:
            body = (body + "\n\n" + link).strip()

        markup = self._build_markup(link, link_label) if (link and link_as_button) else None
        params = {"chat_id": chat_id}
        if markup:
            params["reply_markup"] = markup

        has_attachment = bool(file_id or blob or (file_path and os.path.exists(file_path)))
        if has_attachment:
            name = blob_name or os.path.basename(file_path or "file")
            size_mb = (len(blob) / 1048576) if blob else (
                os.path.getsize(file_path) / 1048576 if file_path and os.path.exists(file_path) else 0)
            kind = kind_for_name(name, size_mb)
            method = _METHOD[kind]
            if body:
                params["caption"] = body[:1024]

            try:
                if file_id:
                    # Telegram already holds this file. Referencing it by id skips
                    # the upload entirely, so a campaign transfers the bytes once
                    # rather than once per recipient.
                    params[kind] = file_id
                    res = self._call(method, **params)
                elif blob is not None:
                    res = self._call(method, files={kind: (name, blob)}, **params)
                else:
                    with open(file_path, "rb") as fh:
                        res = self._call(method, files={kind: (name, fh)}, **params)
            except Exception as e:
                return {"ok": False, "error": f"Upload failed: {e}", "kind": kind}

            out = {"ok": bool(res.get("ok")), "error": res.get("description", ""),
                   "kind": kind, "retry_after": (res.get("parameters") or {}).get("retry_after")}
            if out["ok"]:
                out["file_id"] = _extract_file_id(res.get("result") or {}, kind)
            return out

        if not body:
            return {"ok": False, "error": "Nothing to send — add a message, a file or a link"}
        params["text"] = body[:4096]
        params["disable_web_page_preview"] = False
        res = self._call("sendMessage", **params)
        return {"ok": bool(res.get("ok")), "error": res.get("description", ""),
                "kind": "text", "retry_after": (res.get("parameters") or {}).get("retry_after")}

    def send_campaign(self, recipient_ids: List[int], text: str = "", file_path: str = "",
                      link: str = "", link_label: str = "", link_as_button: bool = True,
                      delay: float = 1.5, on_progress=None) -> Dict:
        """Blocking send. Call from a worker thread — start_campaign does that."""
        recips = bot_store.get_recipients_by_ids(recipient_ids)

        # Pull the attachment into memory once. Supabase-stored uploads never
        # touch the VPS disk; only the no-Supabase fallback reads a local path.
        blob = None
        blob_name = ""
        if file_path:
            blob_name = os.path.basename(file_path)
            if not os.path.exists(file_path):
                try:
                    from core.services.persistence import persistence
                    blob = persistence.download_bytes(file_path)
                except Exception as e:
                    logger.error(f"🤖 Could not fetch attachment from storage: {e}")
                if blob is None:
                    self.send_state.update({"running": False, "last_error": "Attachment unavailable"})
                    return {"status": "error", "message": "Attachment could not be retrieved from storage"}
                logger.info(f"🤖 Attachment loaded from storage ({len(blob)/1048576:.1f} MB)")

        # Telegram returns a reusable file_id after the first successful send;
        # every later recipient references it instead of re-uploading.
        cached_file_id = ""

        self.send_state.update({
            "running": True, "total": len(recips), "done": 0,
            "ok": 0, "failed": 0, "current": "", "last_error": "",
        })
        label = (text or os.path.basename(file_path or "") or link or "")[:60]

        for idx, r in enumerate(recips):
            if self._send_stop.is_set():
                logger.info(f"🤖 Campaign stopped by user after {self.send_state['done']} of {len(recips)}.")
                break
            who = ("@" + r["username"]) if r.get("username") else str(r.get("user_id") or r["id"])
            self.send_state["current"] = who
            res = self.send_one(r, text, file_path, link, link_label, link_as_button,
                                blob=blob, blob_name=blob_name, file_id=cached_file_id)

            # Telegram asked us to slow down: wait exactly as long as it said, then
            # retry this recipient once. Ignoring retry_after is what gets bots limited.
            if not res["ok"] and res.get("retry_after"):
                wait = int(res["retry_after"]) + 2
                self.send_state["current"] = f"Rate limited — waiting {wait}s"
                logger.warning(f"🤖 Telegram rate limit hit, sleeping {wait}s before retrying {who}.")
                time.sleep(wait)
                res = self.send_one(r, text, file_path, link, link_label, link_as_button,
                                    blob=blob, blob_name=blob_name, file_id=cached_file_id)

            bot_store.mark_sent(r["id"], res["ok"], res.get("error", ""), label, res.get("kind", ""))

            self.send_state["done"] += 1
            if res["ok"]:
                if res.get("file_id") and not cached_file_id:
                    cached_file_id = res["file_id"]
                    blob = None          # bytes no longer needed; free the memory
                    logger.info("🤖 Attachment cached by Telegram; remaining sends reuse it.")
                self.send_state["ok"] += 1
            else:
                self.send_state["failed"] += 1
                self.send_state["last_error"] = res.get("error", "")
                logger.warning(f"🤖 Send to {who} failed: {res.get('error')}")

            if on_progress:
                try:
                    on_progress(dict(self.send_state))
                except Exception:
                    pass

            # Pause between recipients. Jitter keeps the pattern from looking
            # machine-regular, which is part of what trips spam detection.
            if idx < len(recips) - 1:
                pause = max(0.0, delay) + random.uniform(0, max(0.5, delay * 0.4))
                self.send_state["current"] = f"Waiting {pause:.1f}s..."
                slept = 0.0
                while slept < pause and not self._send_stop.is_set():
                    time.sleep(min(0.25, pause - slept))
                    slept += 0.25

        self.send_state["running"] = False
        self.send_state["current"] = ""
        self.send_state["stopped"] = self._send_stop.is_set()
        logger.info(
            f"🤖 Campaign finished: {self.send_state['ok']} sent, {self.send_state['failed']} failed."
        )
        try:
            from core.services.persistence import persistence
            persistence.backup_bot_db()
        except Exception:
            pass
        return dict(self.send_state)

    # ─────────────────────────────────────
    # SENDING AS A USER ACCOUNT
    # ─────────────────────────────────────
    # A bot cannot open a chat; a user account can. Everything below is the
    # second delivery path, running on _BOT_LOOP because that is where every
    # Pyrogram client lives.

    def available_senders(self) -> List[Dict]:
        """Who can send right now — the bot, plus every connected user account."""
        out = []
        cfg = bot_store.get_bot() or {}
        if cfg.get("token"):
            out.append({
                "id": "bot", "kind": "bot",
                "label": "Bot: @" + (cfg.get("bot_username") or "bot"),
                "note": "Only reaches people who pressed Start on the bot",
            })
        if self.bot_manager:
            for w in list(self.bot_manager.workers.values()):
                if w.client and w.client.is_connected:
                    out.append({
                        "id": w.clean_phone, "kind": "account",
                        "label": w.phone,
                        "note": "Reaches anyone, but mass DMs can get the account limited",
                    })
        return out

    def _account_client(self, phone: str = ""):
        """The Pyrogram client to send from, or (None, reason)."""
        if not self.bot_manager:
            return None, "No account manager available"
        if phone:
            w = self.bot_manager.get_worker(phone)
            if not w:
                return None, f"Account {phone} is not loaded"
            if not (w.client and w.client.is_connected):
                return None, f"Account {phone} is not connected"
            return w.client, ""
        for w in list(self.bot_manager.workers.values()):
            if w.client and w.client.is_connected:
                return w.client, ""
        return None, "No logged-in Telegram account available"

    async def _send_one_as_account(self, client, recipient: Dict, text: str = "",
                                   link: str = "", link_label: str = "",
                                   blob: Optional[bytes] = None, blob_name: str = "",
                                   file_path: str = "", file_id: str = "") -> Dict:
        """
        Deliver one message from a user account.

        Identity handling is the whole trick. A username resolves server-side,
        so it works for someone this account has never met. A numeric ID only
        resolves from the session's peer cache — which does hold everyone whose
        group roster this account has synced, but not strangers. So try the
        username first and fall back to the ID, and only fall through on a
        resolution failure, never on a delivery failure (that would double-send).
        """
        from pyrogram.errors import (
            FloodWait, PeerFlood, UserPrivacyRestricted, UserIsBlocked,
            UserDeactivated, InputUserDeactivated,
        )

        uname = (recipient.get("username") or "").strip()
        uid = recipient.get("user_id")
        candidates = []
        if uname:
            candidates.append(uname)
        if uid:
            candidates.append(uid)
        if not candidates:
            return {"ok": False, "error": "No username or user ID on this contact", "kind": "text"}

        # A user account cannot attach an inline keyboard — only bots can. The
        # link has to ride along as text.
        body = text or ""
        if link:
            body = (body + "\n\n" + (link_label + ": " if link_label else "") + link).strip()

        kind = "text"
        if file_id or blob is not None or (file_path and os.path.exists(file_path)):
            kind = kind_for_name(blob_name or os.path.basename(file_path or "file"),
                                 (len(blob) / 1048576) if blob else 0)

        last_err = ""
        for target in candidates:
            try:
                if kind == "text":
                    if not body:
                        return {"ok": False, "error": "Nothing to send", "kind": "text"}
                    await client.send_message(target, body[:4096],
                                              disable_web_page_preview=False)
                    return {"ok": True, "error": "", "kind": "text"}

                # Media. A fresh stream per attempt — an upload consumes it.
                if file_id:
                    media = file_id
                elif blob is not None:
                    media = _io.BytesIO(blob)
                    media.name = blob_name or "file"
                else:
                    media = file_path

                caption = body[:1024] if body else None
                sender = {
                    "photo": client.send_photo, "video": client.send_video,
                    "audio": client.send_audio, "document": client.send_document,
                }[kind]
                msg = await sender(target, media, caption=caption)

                # Telegram now holds the file; later recipients reference it by
                # id so a campaign uploads the bytes once, not once per person.
                node = getattr(msg, kind, None)
                new_id = getattr(node, "file_id", "") if node else ""
                return {"ok": True, "error": "", "kind": kind, "file_id": new_id or ""}

            except FloodWait as e:
                return {"ok": False, "kind": kind, "error": f"FloodWait ({e.value}s)",
                        "retry_after": e.value}
            except PeerFlood:
                # Telegram has flagged this account for unsolicited messaging.
                # Pushing on makes the limit worse, so this aborts the campaign.
                return {"ok": False, "kind": kind, "fatal": True,
                        "error": "PeerFlood — Telegram flagged this account for spam. "
                                 "Stop sending and let it rest."}
            except UserPrivacyRestricted:
                return {"ok": False, "kind": kind,
                        "error": "Their privacy settings block messages from non-contacts"}
            except UserIsBlocked:
                return {"ok": False, "kind": kind, "error": "This user blocked the account"}
            except (UserDeactivated, InputUserDeactivated):
                return {"ok": False, "kind": kind, "error": "Account deleted or deactivated"}
            except Exception as e:
                msg_l = str(e).lower()
                resolution_failed = any(t in msg_l for t in (
                    "peer id invalid", "peer_id_invalid", "username_not_occupied",
                    "username_invalid", "id not found", "peer not found",
                )) or isinstance(e, (KeyError, ValueError))
                last_err = str(e)
                if resolution_failed:
                    continue      # try the other identifier
                return {"ok": False, "kind": kind, "error": last_err}

        hint = ("could not be looked up — a numeric ID only works if this account "
                "has seen the person before (e.g. in a group it monitors)")
        return {"ok": False, "kind": kind, "error": f"{last_err or 'Unknown user'} ({hint})"}

    async def send_campaign_as_account(self, recipient_ids: List[int], account_phone: str = "",
                                       text: str = "", file_path: str = "",
                                       link: str = "", link_label: str = "",
                                       delay: float = 8.0, on_progress=None) -> Dict:
        """Account-mode campaign. Runs as a task on _BOT_LOOP, not a thread."""
        client, why = self._account_client(account_phone)
        if not client:
            self.send_state.update({"running": False, "last_error": why})
            return {"status": "error", "message": why}

        recips = bot_store.get_recipients_by_ids(recipient_ids)

        blob, blob_name = None, ""
        if file_path:
            blob_name = os.path.basename(file_path)
            if not os.path.exists(file_path):
                try:
                    from core.services.persistence import persistence
                    blob = persistence.download_bytes(file_path)
                except Exception as e:
                    logger.error(f"🤖 Could not fetch attachment from storage: {e}")
                if blob is None:
                    self.send_state.update({"running": False,
                                            "last_error": "Attachment unavailable"})
                    return {"status": "error", "message": "Attachment could not be retrieved"}

        cached_file_id = ""
        self.send_state.update({
            "running": True, "total": len(recips), "done": 0,
            "ok": 0, "failed": 0, "current": "", "last_error": "", "stopped": False,
        })
        label = (text or blob_name or link or "")[:60]
        logger.info(f"👤 Account campaign starting: {len(recips)} recipient(s) "
                    f"from {account_phone or 'first available account'}.")

        for idx, r in enumerate(recips):
            if self._send_stop.is_set():
                logger.info(f"👤 Campaign stopped by user after {self.send_state['done']}.")
                break

            who = ("@" + r["username"]) if r.get("username") else str(r.get("user_id") or r["id"])
            self.send_state["current"] = who
            res = await self._send_one_as_account(
                client, r, text=text, link=link, link_label=link_label,
                blob=blob, blob_name=blob_name, file_path=file_path,
                file_id=cached_file_id,
            )

            # Telegram told us exactly how long to wait. Honour it and retry once.
            if not res["ok"] and res.get("retry_after"):
                wait = int(res["retry_after"]) + 2
                self.send_state["current"] = f"Rate limited — waiting {wait}s"
                logger.warning(f"👤 FloodWait {wait}s before retrying {who}.")
                await asyncio.sleep(wait)
                res = await self._send_one_as_account(
                    client, r, text=text, link=link, link_label=link_label,
                    blob=blob, blob_name=blob_name, file_path=file_path,
                    file_id=cached_file_id,
                )

            bot_store.mark_sent(r["id"], res["ok"], res.get("error", ""), label,
                                res.get("kind", ""))
            self.send_state["done"] += 1
            if res["ok"]:
                if res.get("file_id") and not cached_file_id:
                    cached_file_id = res["file_id"]
                    blob = None
                self.send_state["ok"] += 1
            else:
                self.send_state["failed"] += 1
                self.send_state["last_error"] = res.get("error", "")
                logger.warning(f"👤 Send to {who} failed: {res.get('error')}")

            if on_progress:
                try:
                    on_progress(dict(self.send_state))
                except Exception:
                    pass

            # PeerFlood is not a per-recipient problem, it is a verdict on the
            # account. Continuing would deepen the restriction.
            if res.get("fatal"):
                self.send_state["last_error"] = res.get("error", "")
                logger.error("👤 Campaign aborted: Telegram flagged the account (PeerFlood).")
                break

            if idx < len(recips) - 1:
                pause = max(0.0, delay) + random.uniform(0, max(0.5, delay * 0.4))
                self.send_state["current"] = f"Waiting {pause:.1f}s..."
                slept = 0.0
                while slept < pause and not self._send_stop.is_set():
                    await asyncio.sleep(min(0.25, pause - slept))
                    slept += 0.25

        self.send_state["running"] = False
        self.send_state["current"] = ""
        self.send_state["stopped"] = self._send_stop.is_set()
        logger.info(f"👤 Account campaign finished: {self.send_state['ok']} sent, "
                    f"{self.send_state['failed']} failed.")
        try:
            from core.services.persistence import persistence
            persistence.backup_bot_db()
        except Exception:
            pass
        return dict(self.send_state)

    def stop_campaign(self) -> Dict:
        """Halt an in-flight campaign after the current recipient."""
        if not self.send_state.get("running"):
            return {"status": "error", "message": "Nothing is sending"}
        self._send_stop.set()
        logger.info("🤖 Campaign stop requested.")
        return {"status": "success", "message": "Stopping after the current message"}

    def start_campaign(self, sender: str = "bot", account_phone: str = "", **kwargs) -> Dict:
        """
        Kick off a campaign on whichever engine the caller chose.

        sender="bot"     -> Bot API, on a worker thread (plain HTTP).
        sender="account" -> Pyrogram user account, as a task on _BOT_LOOP,
                            because that is the only loop its client lives on.
        """
        if self.send_state.get("running"):
            return {"status": "error", "message": "A send is already in progress"}
        if not kwargs.get("recipient_ids"):
            return {"status": "error", "message": "Select at least one recipient"}

        n = len(kwargs["recipient_ids"])

        if sender == "account":
            client, why = self._account_client(account_phone)
            if not client:
                return {"status": "error", "message": why}
            # link_as_button is a bot-only capability; drop it before it reaches
            # the account engine so the signature cannot silently disagree.
            kwargs.pop("link_as_button", None)
            self._send_stop.clear()
            self.send_state["running"] = True          # claim the slot synchronously
            try:
                from api.routes import _BOT_LOOP
                asyncio.run_coroutine_threadsafe(
                    self.send_campaign_as_account(account_phone=account_phone, **kwargs),
                    _BOT_LOOP,
                )
            except Exception as e:
                self.send_state["running"] = False
                return {"status": "error", "message": f"Could not start: {e}"}
            return {"status": "success",
                    "message": f"Sending to {n} recipient(s) from {account_phone or 'your account'}"}

        if not self._token():
            return {"status": "error", "message": "Connect a bot first"}
        self._send_stop.clear()
        self.send_state["running"] = True          # claim the slot synchronously
        self._send_thread = threading.Thread(
            target=self.send_campaign, kwargs=kwargs, daemon=True
        )
        self._send_thread.start()
        return {"status": "success", "message": f"Sending to {n} recipient(s)"}
