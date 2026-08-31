import asyncio
import time
import random
import traceback
from typing import List, Dict, Optional
from pyrogram import Client, filters, handlers
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, PeerFlood, UserPrivacyRestricted,
    ChatWriteForbidden, UserBannedInChannel, AuthKeyUnregistered
)

from utils.logger import logger
from core.services.progress_tracker import ProgressTracker
from core.services.loop_manager import LoopManager
from core.services.config_service import config_service


class BotWorker:
    """
    High-level session worker.
    Uses dedicated services for dispatching, progress tracking, and loop management.
    """
    # Class-level defaults — safety net
    cooldown_until = 0
    current_msg_id = None
    current_from_chat = None
    last_dispatch_time = None
    def __init__(self, client: Client, phone: str, clean_phone: str, 
                 targets: List[str], source_channel: str, loop_interval: int,
                 global_semaphore: asyncio.Semaphore, msg_delay: int = 5):
        self.client = client
        self.phone = phone
        self.clean_phone = clean_phone
        self.targets = [t.strip() for t in targets if t.strip()]
        self.source_channel = str(source_channel).strip()
        self.loop_interval = max(1, int(loop_interval))
        self.global_semaphore = global_semaphore
        self.msg_delay = max(0, int(msg_delay))
        
        # Services
        self.progress = ProgressTracker()
        self.scheduler = LoopManager(phone)
        self.worker_manager = LoopManager(f"{phone}_worker")
        
        self.is_running = False
        self.queue = asyncio.Queue()
        self._dispatch_lock = asyncio.Lock()
        
        # State tracking
        self.current_msg_id = None
        self.current_from_chat = None
        self.cooldown_until = 0
        self.last_dispatch_time = None
        
        # Per-target delivery results for the current campaign, keyed by target.
        # The aggregate counters in ProgressTracker say how many failed; this
        # says WHICH, and why, so a dead channel can be found and removed.
        self.target_results: Dict[str, dict] = {}
        self._run_dirty = False
        # One dialog sweep per worker is enough to fill the peer cache.
        self._peers_warmed = False

        # Idempotency & Coordination
        self.last_processed_msg = None
        self._handler = None
        self._new_msg_event = asyncio.Event()

    async def start(self):
        """Ensure no duplicate starts and return safe response."""
        if self.is_running:
            return False, "Already running"

        self.is_running = True
        await self.worker_manager.start_loop(self._process_queue)
        await self._setup_monitor()

        # Everything slow happens off to the side, so the Start button returns
        # immediately instead of racing run_async's 30s budget.
        asyncio.ensure_future(self._prime())

        logger.info(f"[{self.phone}] Worker started successfully.")
        return True, "Started"

    async def _prime(self):
        """
        Get the worker into a state where it can actually forward.

        Three things, in this order, because each unblocks the next:

        1. WARM THE PEER CACHE. Pyrogram cannot even parse an update from a
           channel whose access_hash it does not know — handle_updates() raises
           and the message is discarded before any handler sees it. So a cold
           session does not merely fail to forward, it never NOTICES the new
           post at all. Warming has to happen before we sit waiting for updates,
           not lazily on the first dispatch.

        2. ADOPT THE LATEST SOURCE MESSAGE. The handler only ever sees posts
           made after it attaches, so the natural order — write the post, then
           press Start — captured nothing and the campaign sat idle forever with
           no explanation. Reading the newest message already in the channel
           makes that order work.

        3. DISPATCH IMMEDIATELY if we now have something. On a resumed campaign
           this also beats an idle host putting the process to sleep before the
           first interval elapses.
        """
        try:
            if not self._peers_warmed:
                await self._warm_peer_cache()

            if not self.current_msg_id:
                await self._adopt_latest_source_message()

            if self.current_msg_id:
                logger.info(f"[{self.phone}] Priming dispatch for message "
                            f"#{self.current_msg_id}.")
                await self.trigger_dispatch()
            else:
                await self.progress.set_action(
                    "Waiting for a new message in the source channel...")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{self.phone}] Priming failed: {e}")

    async def _adopt_latest_source_message(self):
        """
        Take the newest message already sitting in the source channel.

        Without this the worker is deaf to anything posted before it started,
        which is the order almost everyone uses.
        """
        ok, why = await self._ensure_source_peer()
        if not ok:
            logger.warning(f"[{self.phone}] Cannot read the source to adopt a "
                           f"message: {why}")
            return False
        try:
            async for m in self.client.get_chat_history(self.current_from_chat, limit=1):
                if not m or getattr(m, "empty", False):
                    continue
                self.current_msg_id = m.id
                self.current_from_chat = m.chat.id
                self._persist_state()
                logger.info(f"[{self.phone}] Adopted existing source message "
                            f"#{m.id} (posted before start).")
                return True
            logger.info(f"[{self.phone}] Source channel has no messages yet.")
        except Exception as e:
            logger.warning(f"[{self.phone}] Could not read source history: {e}")
        return False

    async def stop(self):
        self.is_running = False
        await self.worker_manager.stop_loop()
        await self.scheduler.stop_loop()
        await self._remove_monitor()
        await self.progress.set_action("Stopped")
        logger.info(f"[{self.phone}] Worker stopped.")

    async def update_settings(self, source: str, interval: int, targets: List[str], delay: int = 5):
        self.source_channel = str(source).strip()
        self.loop_interval = max(1, int(interval))
        self.targets = [t.strip() for t in targets if t.strip()]
        self.msg_delay = max(0, int(delay))

        # Drop verdicts for targets that no longer exist, and give newly added
        # ones a row so the pool shows them straight away.
        self.target_results = {t: self.target_results[t]
                               for t in self.targets if t in self.target_results}
        for t in self.targets:
            self.target_results.setdefault(
                t, {"status": "idle", "error": "", "ts": None, "attempts": 0})

        await self._remove_monitor()
        await self._setup_monitor()
        if self.is_running:
            await self._start_scheduler()

    async def update_targets(self, targets: List[str]):
        """
        Swap the target list only.

        Deliberately does NOT touch the source handler or the re-forward
        scheduler: the pool saves on every edit, and re-installing the monitor
        (and restarting the interval countdown) on each checkbox would be both
        wasteful and a way to keep pushing the next loop out of reach.
        """
        self.targets = [t.strip() for t in targets if t.strip()]
        self.target_results = {t: self.target_results[t]
                               for t in self.targets if t in self.target_results}
        for t in self.targets:
            self.target_results.setdefault(
                t, {"status": "idle", "error": "", "ts": None, "attempts": 0})
        logger.info(f"[{self.phone}] Target pool updated: {len(self.targets)} target(s).")

    async def _start_scheduler(self):
        await self.scheduler.start_loop(self._reforward_scheduler)

    def _get_resolved_source(self):
        if self.source_channel and self.source_channel.strip():
            return self.source_channel
        config = config_service.load()
        return config.get("source_channel", "").strip()

    @staticmethod
    def _source_refs(source: str, known_id=None):
        """
        Every way we can name the source chat, most resolvable first.

        A username is resolved by Telegram on request. A numeric id only works
        if its access_hash is already in this session's local peer cache.
        """
        refs = []
        src = str(source or "").strip()
        if src:
            low = src.lower()
            for scheme in ("https://", "http://"):
                if low.startswith(scheme):
                    src = src[len(scheme):]
                    break
            if "t.me/" in src.lower():
                src = src.split("t.me/")[-1].split("/")[0]
            src = src.split("?")[0].strip("/").lstrip("@")
            if src and not src.lstrip("-").isdigit():
                refs.append(src)
            elif src:
                try:
                    refs.append(int(src))
                except ValueError:
                    pass
        if known_id is not None:
            try:
                kid = int(known_id)
                if kid not in refs:
                    refs.append(kid)
            except (TypeError, ValueError):
                pass
        return refs

    @staticmethod
    def _known_username_for(chat_id):
        """
        Ask the Group Monitor whether it knows a username for this chat id.

        The two features watch the same chats, and a username is worth far more
        than an id: Telegram resolves it server-side, no access_hash needed.
        """
        try:
            from core.services.group_store import group_store
            for g in group_store.list_groups():
                if str(g.get("chat_id")) == str(chat_id) and g.get("username"):
                    return g["username"]
        except Exception:
            pass
        return None

    async def _warm_peer_cache(self):
        """
        Fill the session's peer cache by sweeping the account's dialog list.

        A numeric channel id resolves only when its access_hash is known.
        Telegram will not supply that from the id alone — Pyrogram falls back to
        channels.GetChannels with access_hash=0 and gets back
        "[400 CHANNEL_INVALID]" — so the hash has to come from somewhere the
        account legitimately sees the chat. Its own dialog list is exactly that,
        and one sweep caches every chat it belongs to.

        Needed because the cache lives inside the .session file: a restore
        (every boot on a RAM-backed install) brings back a file whose cache
        predates these chats.
        """
        try:
            n = 0
            async for _ in self.client.get_dialogs():
                n += 1
            logger.info(f"[{self.phone}] Peer cache warmed from {n} dialog(s).")
            self._peers_warmed = True
            return n
        except Exception as e:
            logger.warning(f"[{self.phone}] Could not warm the peer cache: {e}")
            self._peers_warmed = True     # do not retry in a tight loop
            return 0

    async def _try_refs(self, refs):
        """First ref that resolves wins. Returns (chat, error)."""
        last = ""
        for ref in refs:
            try:
                return await self.client.get_chat(ref), ""
            except Exception as e:
                last = str(e)
        return None, last

    async def _ensure_source_peer(self):
        """
        Make the SOURCE chat resolvable before forwarding anything.

        forward_messages() takes from_chat_id, and a bare numeric id only works
        when the access_hash sits in this session's peer cache. That cache lives
        inside the .session file, so restoring a session from backup — which a
        RAM-backed (zero-disk) install does on every boot — can leave it cold.

        When that happens EVERY target fails with the same
        "Peer id invalid: <source id>", which reads like a broken target list
        but is nothing of the sort. Resolving the source by username re-caches
        it and the whole campaign starts working again.

        Returns (ok, error).
        """
        refs = self._source_refs(self._get_resolved_source(), self.current_from_chat)
        if not refs:
            return False, "No source channel configured"

        # A username the Group Monitor already knows beats any numeric id.
        for candidate in (self.current_from_chat, refs[0] if refs else None):
            uname = self._known_username_for(candidate) if candidate else None
            if uname and uname not in refs:
                refs.insert(0, uname)
                break

        chat, last = await self._try_refs(refs)

        # Still nothing: the cache is cold. Sweep dialogs once, then retry —
        # this is what recovers a numeric-only source after a session restore.
        if chat is None and not self._peers_warmed:
            logger.info(f"[{self.phone}] Source {refs} unresolved; warming the peer cache.")
            if await self._warm_peer_cache():
                chat, last = await self._try_refs(refs)

        if chat is not None:
            if self.current_from_chat and chat.id != self.current_from_chat:
                # Telegram is the authority; a supergroup migration moves the id.
                logger.info(f"[{self.phone}] Source chat id changed "
                            f"{self.current_from_chat} -> {chat.id}; updating.")
                self.current_from_chat = chat.id
                self._persist_state()
            elif not self.current_from_chat:
                self.current_from_chat = chat.id
            return True, ""

        return False, last or "Could not resolve the source channel"

    async def _setup_monitor(self):
        if not self.client.is_connected: return

        # A handler still hanging here means the previous detach failed. Try
        # once more; if it still will not go, keep using it rather than
        # registering a duplicate that would double every forward.
        if self._handler:
            try:
                self.client.remove_handler(self._handler, group=1)
                self._handler = None
            except Exception as e:
                logger.error(
                    f"[{self.phone}] Source handler is stuck ({e}); reusing it "
                    f"instead of attaching a second one.")
                return

        resolved = self._get_resolved_source()
        if not resolved:
            logger.warning(f"[{self.phone}] No source channel configured - cannot monitor")
            return

        async def dynamic_filter(_, __, m: Message):
            if not m.chat: return False
            target = resolved.lower().replace("@", "").strip()
            if "t.me/" in target:
                target = target.split("t.me/")[-1].split("/")[0]
            if "joinchat/" in target:
                target = target.split("joinchat/")[-1].split("/")[0]
            
            return str(m.chat.id) == resolved.strip() or (m.chat.username or "").lower() == target
            
        async def on_new_message(client, message: Message):
            logger.info(f"[{self.phone}] New message detected in source! ID: #{message.id}")
            await self.trigger_dispatch(message.chat.id, message.id)
            
        self._handler = handlers.MessageHandler(on_new_message, filters.create(dynamic_filter))
        self.client.add_handler(self._handler, group=1)
        logger.info(f"[{self.phone}] Monitoring source channel: {resolved} (waiting for new messages...)")

    async def _remove_monitor(self):
        """
        Detach the source-channel handler.

        The reference is dropped only once Pyrogram confirms removal. Clearing
        it on failure would strand a live handler with nothing pointing at it,
        and the next _setup_monitor would stack a second one — after which every
        new source message would dispatch twice.
        """
        if not self._handler:
            return
        if not self.client.is_connected:
            # Nothing can fire while disconnected, and the handler dies with the
            # session, so drop it and let a reconnect start clean.
            self._handler = None
            return
        try:
            self.client.remove_handler(self._handler, group=1)
            self._handler = None
        except Exception as e:
            logger.warning(f"[{self.phone}] Could not detach source handler: {e}")

    def _reset_target_results(self):
        """Every target starts a campaign queued, with last run's verdict cleared."""
        self.target_results = {
            t: {"status": "queued", "error": "", "ts": None, "attempts": 0}
            for t in self.targets
        }

    def _mark_target(self, target: str, status: str, error: str = ""):
        row = self.target_results.setdefault(
            target, {"status": "queued", "error": "", "ts": None, "attempts": 0})
        row["status"] = status
        row["error"] = error or ""
        row["ts"] = time.time()
        if status == "sending":
            row["attempts"] = row.get("attempts", 0) + 1
        self._run_dirty = True

    def _persist_target_results(self):
        """
        Save the last verdict per target once a campaign drains.

        Written at the end of a run rather than per target: each save rewrites
        config.json and triggers a cloud sync, far too heavy to do once per
        delivery.
        """
        try:
            config = config_service.load()
            settings = config.setdefault("account_settings", {}).setdefault(self.clean_phone, {})
            settings["target_results"] = {
                t: {"status": r.get("status"), "error": r.get("error", ""), "ts": r.get("ts")}
                for t, r in self.target_results.items()
                if t in self.targets
            }
            config_service.save(config)
        except Exception as e:
            logger.warning(f"[{self.phone}] Could not save target results: {e}")

    def _persist_state(self):
        """Save current campaign state to config for crash recovery."""
        try:
            config = config_service.load()
            settings = config.setdefault("account_settings", {}).setdefault(self.clean_phone, {})
            settings["last_msg_id"] = self.current_msg_id
            settings["last_from_chat"] = self.current_from_chat
            config_service.save(config)
        except Exception as e:
            logger.warning(f"[{self.phone}] State persist failed: {e}")

    async def trigger_dispatch(self, from_chat_id=None, message_id=None):
        """Dispatch a message to all targets. Called automatically by monitor or manually by user."""
        async with self._dispatch_lock:
            is_new_message = bool(message_id and from_chat_id)

            # If no message specified, this is a manual re-dispatch of the last message
            if not message_id or not from_chat_id:
                if self.current_msg_id and self.current_from_chat:
                    message_id = self.current_msg_id
                    from_chat_id = self.current_from_chat
                    logger.info(f"[{self.phone}] Manual re-dispatch of last message #{message_id}")
                else:
                    await self.progress.set_action("Waiting for source message... (Start the loop first)")
                    logger.info(f"[{self.phone}] No message to dispatch yet - waiting for source channel")
                    return False
                
            if not self.targets:
                await self.progress.set_action("Error: No targets configured")
                return False

            # Do this once per campaign, not once per target: if the source
            # cannot be resolved, every single forward fails identically and
            # the reason belongs on the source, not smeared across the pool.
            ok, why = await self._ensure_source_peer()
            if not ok:
                msg = (f"Source channel unreachable ({why}). "
                       f"Check the account is still in it.")
                logger.error(f"[{self.phone}] {msg}")
                await self.progress.set_action(f"❌ {msg}")
                self._reset_target_results()
                for t in self.targets:
                    self._mark_target(t, "failed", msg)
                self._persist_target_results()
                return False

            # The resolver may have followed a migration. For a re-forward the
            # resolved id is the right one; for a live message the handler's own
            # chat id is authoritative, since message_id belongs to that chat.
            if not is_new_message and self.current_from_chat:
                from_chat_id = self.current_from_chat
                
            # Queue Flush: clear pending sends without replacing the queue object
            while not self.queue.empty():
                try: 
                    self.queue.get_nowait()
                    self.queue.task_done()
                except: 
                    break
                
            self.last_processed_msg = message_id
            self.current_msg_id = message_id
            self.current_from_chat = from_chat_id

            # Persist state on NEW messages so campaigns survive restarts
            if is_new_message:
                self._persist_state()
            
            # Reset progress tracking for new batch
            await self.progress.reset(len(self.targets))
            self._reset_target_results()

            # Queue up all targets
            for target in self.targets:
                await self.queue.put(target)
            
            logger.info(f"[{self.phone}] Dispatch queued: msg #{message_id} -> {len(self.targets)} targets")
                
            # Trigger scheduler reset
            self._new_msg_event.set()
            if self.is_running and not self.scheduler.is_running:
                await self._start_scheduler()
            
            return True

    async def _reforward_scheduler(self):
        try:
            while self.is_running:
                self._new_msg_event.clear()
                try:
                    await asyncio.wait_for(self._new_msg_event.wait(), timeout=self.loop_interval * 60)
                    continue 
                except asyncio.TimeoutError:
                    if self.current_msg_id:
                        logger.info(f"[{self.phone}] Loop trigger: Re-forwarding...")
                        await self.trigger_dispatch()
        except asyncio.CancelledError: pass

    async def _process_queue(self):
        """Queue processor with optimized progress tracking and delay control."""
        while self.is_running:
            try:
                # Handle Cooldown
                while self.cooldown_until > time.monotonic():
                    rem = int(self.cooldown_until - time.monotonic())
                    await self.progress.set_action(f"Cooldown: {rem}s")
                    await asyncio.sleep(1)
                
                target = await self.queue.get()
                self._mark_target(target, "sending")

                async with self.global_semaphore:
                    success, err = await self._send_msg(target)

                if success:
                    self.last_dispatch_time = time.time()
                    self._mark_target(target, "sent")
                    await self.progress.mark_success(target)
                else:
                    logger.error(f"[{self.phone}] Delivery to {target} failed: {err}")
                    self._mark_target(target, "failed", err)
                    await self.progress.mark_failure(target, err)
                
                # Bug Fix 2: Apply delay AFTER EACH MESSAGE
                if not self.queue.empty() and self.is_running:
                    jitter = random.randint(1, 3) if self.msg_delay > 0 else 0
                    total_delay = self.msg_delay + jitter
                    if total_delay > 0:
                        await self.progress.set_action(f"Next in {total_delay}s...")
                        await asyncio.sleep(total_delay)
                
                self.queue.task_done()
                
                if self.queue.empty():
                    await self.progress.set_action("Idle (Waiting for new source msg or interval)")
                    # Campaign drained — write the verdicts once, not per target.
                    if self._run_dirty:
                        self._run_dirty = False
                        self._persist_target_results()
                    
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"[{self.phone}] Worker error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(5)

    async def _send_msg(self, target: str):
        """Bug Fix 7: Meaningful error handling."""
        for attempt in range(1, 4):
            try:
                await asyncio.wait_for(
                    self.client.forward_messages(
                        chat_id=target, 
                        from_chat_id=self.current_from_chat, 
                        message_ids=self.current_msg_id
                    ),
                    timeout=15
                )
                logger.info(f"[{self.phone}] Delivered to {target}")
                return True, ""
            except AuthKeyUnregistered:
                await self.stop()
                return False, "Session Expired"
            except FloodWait as e:
                self.cooldown_until = time.monotonic() + e.value + 5
                return False, f"FloodWait ({e.value}s)"
            except (PeerFlood, UserPrivacyRestricted, ChatWriteForbidden, UserBannedInChannel) as e:
                return False, type(e).__name__
            except asyncio.TimeoutError:
                # wait_for cancelled us locally, but Telegram may well have
                # accepted the forward already. Retrying would post the same
                # message to the group a second time, which is exactly what
                # gets a channel reported — so report it and move on.
                logger.warning(
                    f"[{self.phone}] Forward to {target} timed out after 15s; "
                    f"not retrying (it may still have been delivered).")
                return False, "Timed out (not retried — may have sent)"
            except Exception as e:
                err_str = str(e)
                if "MESSAGE_ID_INVALID" in err_str or "MessageIdInvalid" in err_str:
                    return False, "MessageIdInvalid"
                if "FORBIDDEN" in err_str or "RESTRICTED" in err_str or "BANNED" in err_str:
                    return False, "Permission Denied"
                # A peer-invalid naming the SOURCE is not this target's fault.
                if "peer id invalid" in err_str.lower():
                    if str(self.current_from_chat) in err_str:
                        return False, ("Source channel not resolvable "
                                       "— the account may have left it")
                    return False, "Target not resolvable (check the @username)"
                if attempt < 3: await asyncio.sleep(2 ** attempt)
                else: return False, err_str
        return False, "Max Retries"

    def to_dict(self):
        stats = self.progress.get_stats()
        cd_val = getattr(self, 'cooldown_until', 0) or 0
        cd_rem = max(0, int(cd_val - time.monotonic()))
        return {
            "phone": self.phone, "clean_phone": self.clean_phone,
            "is_running": self.is_running,
            "state": "sending" if stats["progress"] < 100 and stats["total"] > 0 else "idle",
            "sent": stats["sent"], "errors": stats["failed"], "total": stats["total"],
            "last_action": stats["last_action"], "progress": stats["progress"],
            "targets_count": len(self.targets), "source_channel": self.source_channel,
            "loop_interval": self.loop_interval, "is_loop_active": self.is_running,
            "cooldown_remaining": cd_rem, "msg_delay": self.msg_delay,
            "last_dispatch_time": getattr(self, "last_dispatch_time", None),
            "targets": list(self.targets),
            # Ordered to match the target list so the pool table is stable
            # between refreshes instead of reshuffling under the cursor.
            "target_results": [
                dict(self.target_results.get(
                    t, {"status": "idle", "error": "", "ts": None, "attempts": 0}),
                    target=t)
                for t in self.targets
            ],
        }
