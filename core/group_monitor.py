"""
Group Monitor Engine
─────────────────────
Watches Telegram groups through an already-authenticated user account and records
who is in the group, who joined, and who left.

Two capture paths run together, because neither alone is complete:

  1. LIVE  — Pyrogram handlers fire the moment someone joins or leaves.
             • ChatMemberUpdatedHandler is the precise signal, but Telegram only
               delivers it to accounts with admin rights in the chat.
             • Service messages (new_chat_members / left_chat_member) reach any
               member, but large supergroups stop emitting them past a size cap.

  2. SYNC  — A periodic full roster fetch diffed against the stored roster. This
             is what catches everything the live path missed, and it is the only
             path that works when the watching account is an ordinary member.

The first sync of a group is a BASELINE: it seeds the roster without writing
join events, so the joined log means "joined since monitoring started" rather
than dumping the entire pre-existing membership into it.

Everything here runs on _BOT_LOOP, same as BotWorker. Handlers are registered in
handler group 3 so they can never disturb the forwarding worker's group 1 handler.
"""

import asyncio
import time
from typing import Dict, List, Optional

from pyrogram import filters, handlers
from pyrogram.types import Message

from utils.logger import logger
from core.services.group_store import group_store

# Handler group reserved for the monitor — must not collide with BotWorker (group 1).
_HANDLER_GROUP = 3

# Chat member statuses that mean "still in the group".
_PRESENT_STATUSES = {"member", "administrator", "owner", "creator", "restricted"}
_GONE_STATUSES = {"left", "banned", "kicked"}


def normalize_ref(ref: str) -> str:
    """Turn any of @name / t.me/name / https://t.me/joinchat/x / -100123 into a lookup ref."""
    ref = str(ref or "").strip()
    if not ref:
        return ""
    low = ref.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            ref = ref[len(prefix):]
            low = ref.lower()
    if low.startswith("t.me/"):
        ref = ref[5:]
    elif low.startswith("telegram.me/"):
        ref = ref[12:]
    # joinchat / + invite links must keep their full form for Pyrogram
    if ref.startswith("joinchat/") or ref.startswith("+"):
        return ref.split("?")[0].strip("/")
    ref = ref.split("?")[0].strip("/")
    if ref.startswith("@"):
        ref = ref[1:]
    # Numeric chat id (possibly negative) stays numeric
    if ref.lstrip("-").isdigit():
        return ref
    return ref


def _status_str(member) -> str:
    """Pyrogram may hand back an enum or a plain string depending on the fork."""
    st = getattr(member, "status", None)
    if st is None:
        return ""
    return str(getattr(st, "value", st)).lower().replace("chatmemberstatus.", "")


def _user_dict(u) -> Optional[Dict]:
    if not u:
        return None
    return {
        "user_id": u.id,
        "username": getattr(u, "username", None),
        "first_name": getattr(u, "first_name", None),
        "last_name": getattr(u, "last_name", None),
        # Only ever populated when the user is in the watching account's contacts.
        "phone": getattr(u, "phone_number", None),
        "is_bot": bool(getattr(u, "is_bot", False)),
        "is_premium": bool(getattr(u, "is_premium", False)),
    }


class GroupMonitorManager:
    """Owns every monitored group: handler registration, syncing, lifecycle."""

    def __init__(self, bot_manager):
        self.bot_manager = bot_manager
        self._handlers: Dict[str, tuple] = {}   # chat_key -> (client, [handler, ...])
        self._lock = asyncio.Lock()
        self._sync_task: Optional[asyncio.Task] = None
        self.sync_interval_min = 30
        self._syncing: set = set()

    # ─────────────────────────────────────
    # CLIENT RESOLUTION
    # ─────────────────────────────────────
    def _pick_client(self, account_phone: str = ""):
        """Return (client, clean_phone) for the account that should watch a group."""
        if account_phone:
            worker = self.bot_manager.get_worker(account_phone)
            if worker and worker.client and worker.client.is_connected:
                return worker.client, worker.clean_phone
        # Fall back to any connected account.
        for w in list(self.bot_manager.workers.values()):
            if w.client and w.client.is_connected:
                return w.client, w.clean_phone
        return None, ""

    def available_accounts(self) -> List[Dict]:
        return [
            {"phone": w.phone, "clean_phone": w.clean_phone}
            for w in list(self.bot_manager.workers.values())
            if w.client and w.client.is_connected
        ]

    # ─────────────────────────────────────
    # ADD / REMOVE
    # ─────────────────────────────────────
    async def add_group(self, ref: str, account_phone: str = "") -> Dict:
        ref = normalize_ref(ref)
        if not ref:
            return {"status": "error", "message": "Empty group reference"}

        client, clean_phone = self._pick_client(account_phone)
        if not client:
            return {"status": "error", "message": "No logged-in Telegram account available. Log in an account first."}

        try:
            lookup = int(ref) if ref.lstrip("-").isdigit() else ref
            chat = await client.get_chat(lookup)
        except Exception as e:
            return {"status": "error", "message": f"Could not resolve group: {e}"}

        chat_key = str(chat.id)
        if group_store.get_group(chat_key):
            return {"status": "error", "message": "This group is already being monitored"}

        group_store.upsert_group(
            chat_key,
            chat_id=chat.id,
            title=getattr(chat, "title", None) or ref,
            username=getattr(chat, "username", None),
            chat_type=str(getattr(getattr(chat, "type", None), "value", getattr(chat, "type", ""))),
            source_ref=ref,
            account_phone=clean_phone,
            member_count=getattr(chat, "members_count", 0) or 0,
            is_active=1,
        )
        logger.info(f"👁 Group monitor added: {getattr(chat, 'title', ref)} ({chat_key})")

        await self.attach(chat_key)
        # Baseline snapshot immediately so the UI is populated right away.
        asyncio.ensure_future(self.sync_group(chat_key))
        return {"status": "success", "chat_key": chat_key, "title": getattr(chat, "title", ref)}

    async def remove_group(self, chat_key: str) -> None:
        await self.detach(chat_key)
        group_store.delete_group(chat_key)
        logger.info(f"👁 Group monitor removed: {chat_key}")

    async def set_active(self, chat_key: str, active: bool) -> None:
        group_store.set_active(chat_key, active)
        if active:
            await self.attach(chat_key)
        else:
            await self.detach(chat_key)

    # ─────────────────────────────────────
    # LIVE HANDLERS
    # ─────────────────────────────────────
    async def attach(self, chat_key: str) -> bool:
        async with self._lock:
            if chat_key in self._handlers:
                return True
            group = group_store.get_group(chat_key)
            if not group or not group.get("is_active"):
                return False

            client, _ = self._pick_client(group.get("account_phone") or "")
            if not client:
                return False

            chat_id = group["chat_id"]

            async def _member_update(_client, update):
                try:
                    if not update.chat or update.chat.id != chat_id:
                        return
                    old, new = update.old_chat_member, update.new_chat_member
                    user = _user_dict(getattr(new, "user", None) or getattr(old, "user", None))
                    if not user:
                        return
                    new_st, old_st = _status_str(new), _status_str(old)
                    if new is None or new_st in _GONE_STATUSES:
                        if group_store.mark_left(chat_key, user, "live"):
                            logger.info(f"👁 [{group['title']}] LEFT: {user.get('username') or user['user_id']}")
                    elif new_st in _PRESENT_STATUSES and (old is None or old_st in _GONE_STATUSES or old_st == ""):
                        if group_store.mark_present(chat_key, user, "live"):
                            logger.info(f"👁 [{group['title']}] JOINED: {user.get('username') or user['user_id']}")
                    else:
                        group_store.mark_present(chat_key, user, "refresh")
                except Exception as e:
                    logger.warning(f"👁 member-update handler error: {e}")

            async def _service_message(_client, message: Message):
                try:
                    if not message.chat or message.chat.id != chat_id:
                        return
                    for u in (message.new_chat_members or []):
                        d = _user_dict(u)
                        if d and group_store.mark_present(chat_key, d, "live"):
                            logger.info(f"👁 [{group['title']}] JOINED: {d.get('username') or d['user_id']}")
                    if message.left_chat_member:
                        d = _user_dict(message.left_chat_member)
                        if d and group_store.mark_left(chat_key, d, "live"):
                            logger.info(f"👁 [{group['title']}] LEFT: {d.get('username') or d['user_id']}")
                except Exception as e:
                    logger.warning(f"👁 service-message handler error: {e}")

            registered = []
            try:
                h1 = handlers.ChatMemberUpdatedHandler(_member_update)
                client.add_handler(h1, group=_HANDLER_GROUP)
                registered.append(h1)
            except Exception as e:
                logger.warning(f"👁 ChatMemberUpdated unavailable ({e}); relying on service messages + sync.")

            try:
                h2 = handlers.MessageHandler(
                    _service_message,
                    filters.new_chat_members | filters.left_chat_member,
                )
                client.add_handler(h2, group=_HANDLER_GROUP)
                registered.append(h2)
            except Exception as e:
                logger.warning(f"👁 Service-message handler failed: {e}")

            if registered:
                self._handlers[chat_key] = (client, registered)
                logger.info(f"👁 Live monitoring attached: {group['title']}")
                return True
            return False

    async def detach(self, chat_key: str) -> None:
        async with self._lock:
            entry = self._handlers.pop(chat_key, None)
            if not entry:
                return
            client, hs = entry
            for h in hs:
                try:
                    client.remove_handler(h, group=_HANDLER_GROUP)
                except Exception:
                    pass

    async def attach_all(self) -> None:
        """Re-attach every active group. Safe to call repeatedly."""
        for g in group_store.list_groups():
            if g.get("is_active"):
                try:
                    await self.attach(g["chat_key"])
                except Exception as e:
                    logger.warning(f"👁 attach failed for {g['chat_key']}: {e}")

    # ─────────────────────────────────────
    # PEER RESOLUTION
    # ─────────────────────────────────────
    @staticmethod
    def _lookup_refs(group: Dict) -> List:
        """
        Every way we know to name this chat, best first.

        A username (or invite link) is resolved by Telegram on request, so it
        works even from a session that has never seen the chat. A bare numeric
        id only works if the peer's access_hash is already in the session's
        local cache — and that cache lives inside the .session file, so it is
        lost whenever a session is restored from a backup taken before the chat
        was first seen. On a RAM-backed (zero-disk) install that happens on
        every restart, which is why the id alone is not enough.
        """
        refs = []
        if group.get("username"):
            refs.append(group["username"])
        ref = str(group.get("source_ref") or "").strip()
        if ref and not ref.lstrip("-").isdigit() and ref not in refs:
            refs.append(ref)
        if group.get("chat_id"):
            refs.append(int(group["chat_id"]))
        return refs

    async def _resolve_chat(self, client, group: Dict):
        """
        Get a live Chat, re-caching the peer as a side effect.

        Returns (chat, error). Trying the username first is what repairs a cold
        cache: once get_chat succeeds, the access_hash is stored and later calls
        that take a numeric id start working again.
        """
        last = ""
        for ref in self._lookup_refs(group):
            try:
                return await client.get_chat(ref), ""
            except Exception as e:
                last = str(e)
                continue
        return None, last or "Could not resolve this chat"

    # ─────────────────────────────────────
    # ROSTER SYNC (the reliable path)
    # ─────────────────────────────────────
    async def sync_group(self, chat_key: str) -> Dict:
        if chat_key in self._syncing:
            return {"status": "error", "message": "Sync already in progress"}
        group = group_store.get_group(chat_key)
        if not group:
            return {"status": "error", "message": "Group not monitored"}

        client, _ = self._pick_client(group.get("account_phone") or "")
        if not client:
            group_store.upsert_group(chat_key, last_error="No connected account")
            return {"status": "error", "message": "No connected Telegram account"}

        self._syncing.add(chat_key)
        baseline = not group_store.has_baseline(chat_key)
        source = "baseline" if baseline else "sync"
        seen_ids, joined, left = set(), 0, 0

        try:
            # Resolve first: this both warms the peer cache and catches a group
            # the account has been removed from, with a message that says so.
            chat, err = await self._resolve_chat(client, group)
            if chat is None:
                friendly = (
                    f"Could not resolve this group ({err}). The watching account "
                    f"may have been removed from it, or its username changed."
                )
                group_store.upsert_group(chat_key, last_error=friendly[:300],
                                         last_sync=time.time())
                logger.error(f"👁 [{group['title']}] {friendly}")
                return {"status": "error", "message": friendly}

            # Telegram is the authority on the id and title; a supergroup
            # migration changes the id under us.
            if chat.id != group["chat_id"]:
                logger.info(f"👁 [{group['title']}] chat id changed "
                            f"{group['chat_id']} -> {chat.id}; updating.")
                group_store.upsert_group(chat_key, chat_id=chat.id)
                group["chat_id"] = chat.id

            async for member in client.get_chat_members(chat.id):
                user = _user_dict(getattr(member, "user", None))
                if not user:
                    continue
                seen_ids.add(user["user_id"])
                if group_store.mark_present(chat_key, user, source):
                    joined += 1

            # Anyone previously present but absent from this roster has left.
            if seen_ids:
                for gone_id in (group_store.present_user_ids(chat_key) - seen_ids):
                    if group_store.mark_left(chat_key, {"user_id": gone_id}, source):
                        left += 1

            group_store.upsert_group(
                chat_key, last_sync=time.time(), last_error=None,
                member_count=len(seen_ids),
                title=getattr(chat, "title", None) or group.get("title"),
                username=getattr(chat, "username", None),
            )
            label = "Baseline captured" if baseline else "Synced"
            logger.info(
                f"👁 [{group['title']}] {label}: {len(seen_ids)} members"
                + (f", +{joined} joined, -{left} left" if not baseline else "")
            )
            return {
                "status": "success", "baseline": baseline, "members": len(seen_ids),
                "joined": joined, "left": left,
            }

        except Exception as e:
            msg = str(e)
            group_store.upsert_group(chat_key, last_error=msg[:300], last_sync=time.time())
            logger.error(f"👁 [{group['title']}] Sync failed: {msg}")
            return {"status": "error", "message": msg}
        finally:
            self._syncing.discard(chat_key)

    async def sync_all(self) -> Dict:
        results = {}
        for g in group_store.list_groups():
            if g.get("is_active"):
                results[g["chat_key"]] = await self.sync_group(g["chat_key"])
                await asyncio.sleep(2)   # be gentle with Telegram
        if results:
            try:
                from core.services.persistence import persistence
                persistence.backup_group_db()
            except Exception as e:
                logger.warning(f"👁 Group DB cloud backup skipped: {e}")
        return results

    # ─────────────────────────────────────
    # BACKGROUND LOOP
    # ─────────────────────────────────────
    async def start_background_sync(self) -> None:
        if self._sync_task and not self._sync_task.done():
            return
        self._sync_task = asyncio.get_running_loop().create_task(self._sync_loop())
        logger.info(f"👁 Group monitor background sync every {self.sync_interval_min} min.")

    async def _sync_loop(self) -> None:
        try:
            # Let account sessions settle before the first pass.
            await asyncio.sleep(45)
            while True:
                try:
                    await self.attach_all()
                    await self.sync_all()
                except Exception as e:
                    logger.error(f"👁 Background sync error: {e}")
                await asyncio.sleep(max(5, self.sync_interval_min) * 60)
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        for key in list(self._handlers.keys()):
            await self.detach(key)

    # ─────────────────────────────────────
    # READ MODELS FOR THE UI
    # ─────────────────────────────────────
    def get_state(self) -> List[Dict]:
        out = []
        for g in group_store.list_groups():
            g["live_attached"] = g["chat_key"] in self._handlers
            g["syncing"] = g["chat_key"] in self._syncing
            out.append(g)
        return out
