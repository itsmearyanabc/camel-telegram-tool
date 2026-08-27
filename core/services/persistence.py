"""
Supabase Storage Persistence Layer
───────────────────────────────────
Backs up config.json and .session files to Supabase Storage so data
survives Render container restarts.

Design:
  • If SUPABASE_URL / SUPABASE_KEY are not set → everything is a no-op.
  • Uses the REST API directly with `requests` (no heavy SDK needed).
  • All methods are synchronous and safe to call from Flask/gevent.
"""

import os
import json
import threading
import requests as http_requests   # alias to avoid shadowing
from utils.logger import logger
from utils.paths import (CONFIG_PATH, SESSIONS_DIR, GROUP_DB, BOT_DB,
                         session_file, ensure_dirs)

BUCKET = "telegram-sessions"

class PersistenceManager:
    def __init__(self):
        # Fresh read from environ to handle late-loading and strip whitespace
        self.url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        self.key = os.environ.get("SUPABASE_KEY", "").strip()
        
        self.enabled = bool(self.url and self.key)
        
        if not self.enabled:
            missing = []
            if not self.url: missing.append("SUPABASE_URL")
            if not self.key: missing.append("SUPABASE_KEY")
            logger.warning(f"☁️ Supabase not configured (Missing: {', '.join(missing)}) – data will NOT persist across restarts.")
        else:
            logger.info("☁️ Supabase persistence enabled.")

        self._headers = {
            "Authorization": f"Bearer {self.key}",
            "apikey": self.key,
        }
        self._base = f"{self.url}/storage/v1/object"
        # Debounced backup timers, keyed by what is being backed up.
        self._backup_timers = {}
        self._backup_lock = threading.Lock()
        self._ensure_bucket()

    # ─────────────────────────────────────
    # LOW-LEVEL HELPERS
    # ─────────────────────────────────────
    def _ensure_bucket(self):
        """Create the storage bucket if it doesn't exist."""
        if not self.enabled:
            return
        try:
            resp = http_requests.post(
                f"{self.url}/storage/v1/bucket",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"id": BUCKET, "name": BUCKET, "public": False},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                logger.info(f"☁️ Created storage bucket: {BUCKET}")
            # 409 = already exists, which is fine
        except Exception as e:
            logger.warning(f"☁️ Bucket check skipped: {e}")

    def _upload(self, remote_path, local_path, content_type="application/octet-stream"):
        """Upload a local file to Supabase Storage (upsert)."""
        if not self.enabled:
            return False
        try:
            with open(local_path, "rb") as f:
                data = f.read()
            resp = http_requests.post(
                f"{self._base}/{BUCKET}/{remote_path}",
                headers={
                    **self._headers,
                    "Content-Type": content_type,
                    "x-upsert": "true",          # overwrite if exists
                },
                data=data,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                logger.info(f"☁️ Backed up → {remote_path}")
                return True
            else:
                logger.error(f"☁️ Upload failed {remote_path}: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"☁️ Upload error {remote_path}: {e}")
            return False

    def _download(self, remote_path, local_path):
        """Download a file from Supabase Storage to local disk."""
        if not self.enabled:
            return False
        try:
            resp = http_requests.get(
                f"{self._base}/{BUCKET}/{remote_path}",
                headers=self._headers,
                timeout=30,
            )
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"☁️ Restored ← {remote_path}")
                return True
            # 400/404 = file doesn't exist in cloud yet, not an error
            if resp.status_code not in (400, 404):
                logger.warning(f"☁️ Download failed {remote_path}: {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"☁️ Download error {remote_path}: {e}")
            return False

    def _delete(self, remote_path):
        """Delete a file from Supabase Storage."""
        if not self.enabled:
            return False
        try:
            resp = http_requests.delete(
                f"{self._base}/{BUCKET}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"prefixes": [remote_path]},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"☁️ Deleted cloud file: {remote_path}")
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"☁️ Delete error {remote_path}: {e}")
            return False

    def upload_bytes(self, remote_path, data: bytes, content_type="application/octet-stream"):
        """Upload raw bytes straight to Storage — no local file involved."""
        if not self.enabled:
            return False
        try:
            resp = http_requests.post(
                f"{self._base}/{BUCKET}/{remote_path}",
                headers={**self._headers, "Content-Type": content_type, "x-upsert": "true"},
                data=data,
                timeout=180,
            )
            if resp.status_code in (200, 201):
                logger.info(f"☁️ Stored → {remote_path} ({len(data)/1048576:.1f} MB)")
                return True
            logger.error(f"☁️ Store failed {remote_path}: {resp.status_code} {resp.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"☁️ Store error {remote_path}: {e}")
            return False

    def download_bytes(self, remote_path):
        """Fetch an object into memory. Returns bytes, or None."""
        if not self.enabled:
            return None
        try:
            resp = http_requests.get(
                f"{self._base}/{BUCKET}/{remote_path}", headers=self._headers, timeout=180
            )
            return resp.content if resp.status_code == 200 else None
        except Exception as e:
            logger.error(f"☁️ Fetch error {remote_path}: {e}")
            return None

    def delete_path(self, remote_path):
        """Delete one object from Storage."""
        return self._delete(remote_path)

    def _list_files(self, prefix=""):
        """List files in a bucket path."""
        if not self.enabled:
            return []
        try:
            resp = http_requests.post(
                f"{self._base}/list/{BUCKET}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"prefix": prefix, "limit": 1000},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logger.error(f"☁️ List error: {e}")
            return []

    # ─────────────────────────────────────
    # DEBOUNCED BACKUP
    # ─────────────────────────────────────
    def schedule_backup(self, kind: str, delay: float = 4.0):
        """
        Queue a cloud backup a few seconds from now, replacing any pending one
        for the same target.

        Without this, edits only reached Supabase on graceful shutdown or at the
        end of a campaign/sync — so a hard container restart (which is how Render
        stops things) would resurrect deleted rows and lose new ones. Debouncing
        means a burst of edits costs one upload, not one per row.
        """
        if not self.enabled:
            return
        fn = {
            "bot": self.backup_bot_db,
            "group": self.backup_group_db,
            "config": self.backup_config,
        }.get(kind)
        if not fn:
            return
        with self._backup_lock:
            pending = self._backup_timers.get(kind)
            if pending:
                pending.cancel()
            timer = threading.Timer(delay, self._run_scheduled, args=(kind, fn))
            timer.daemon = True
            self._backup_timers[kind] = timer
            timer.start()

    def _run_scheduled(self, kind, fn):
        with self._backup_lock:
            self._backup_timers.pop(kind, None)
        try:
            fn()
        except Exception as e:
            logger.warning(f"☁️ Scheduled {kind} backup failed: {e}")

    def flush_backups(self):
        """Run any pending debounced backups immediately (used at shutdown)."""
        with self._backup_lock:
            pending = list(self._backup_timers.items())
            self._backup_timers.clear()
        for kind, timer in pending:
            timer.cancel()
        for kind, _ in pending:
            fn = {"bot": self.backup_bot_db, "group": self.backup_group_db,
                  "config": self.backup_config}.get(kind)
            if fn:
                try:
                    fn()
                except Exception:
                    pass

    # ─────────────────────────────────────
    # HIGH-LEVEL API
    # ─────────────────────────────────────
    def backup_config(self):
        """Push config.json to Supabase."""
        if os.path.exists(CONFIG_PATH):
            # Safety Check: Don't backup if the file is suspiciously small (default is ~150-200 bytes)
            if os.path.getsize(CONFIG_PATH) < 100:
                logger.warning("☁️ Config file suspiciously small, skipping cloud backup to prevent data loss.")
                return False
            return self._upload("config/config.json", CONFIG_PATH, "application/json")
        return False

    def restore_config(self):
        """Pull config.json from Supabase to local disk."""
        return self._download("config/config.json", CONFIG_PATH)

    def backup_session(self, clean_phone):
        """Push a single .session file to Supabase."""
        local = session_file(clean_phone)
        if os.path.exists(local):
            return self._upload(f"sessions/session_{clean_phone}.session", local)
        return False

    def restore_all_sessions(self):
        """Pull ALL .session files from Supabase to local disk."""
        files = self._list_files("sessions")
        restored = 0
        for f in files:
            name = f.get("name", "")
            if name.endswith(".session"):
                if self._download(f"sessions/{name}", os.path.join(SESSIONS_DIR, name)):
                    restored += 1
        if restored:
            logger.info(f"☁️ Restored {restored} session file(s) from cloud.")
        return restored

    def delete_session(self, clean_phone):
        """Remove a session file from Supabase."""
        return self._delete(f"sessions/session_{clean_phone}.session")

    def backup_group_db(self):
        """Push the group monitor database to Supabase."""
        local = GROUP_DB
        if not os.path.exists(local):
            return False
        # Fold the WAL into the main file first, or the upload misses recent writes.
        try:
            from core.services.group_store import group_store
            group_store.checkpoint()
        except Exception as e:
            logger.warning(f"☁️ Group DB checkpoint skipped: {e}")
        return self._upload("data/group_monitor.db", local)

    def restore_group_db(self):
        """Pull the group monitor database from Supabase."""
        ok = self._download("data/group_monitor.db", GROUP_DB)
        if ok:
            # The local empty DB created at import leaves -wal/-shm behind; stale
            # sidecars against a freshly downloaded file confuse SQLite.
            for sidecar in (GROUP_DB + "-wal", GROUP_DB + "-shm"):
                try:
                    if os.path.exists(sidecar):
                        os.remove(sidecar)
                except Exception:
                    pass
        return ok

    def backup_bot_db(self):
        """Push the message bot database (token + recipients) to Supabase."""
        local = BOT_DB
        if not os.path.exists(local):
            return False
        try:
            from core.services.bot_store import bot_store
            bot_store.checkpoint()
        except Exception as e:
            logger.warning(f"☁️ Bot DB checkpoint skipped: {e}")
        return self._upload("data/message_bot.db", local)

    def restore_bot_db(self):
        """Pull the message bot database from Supabase."""
        ok = self._download("data/message_bot.db", BOT_DB)
        if ok:
            for sidecar in (BOT_DB + "-wal", BOT_DB + "-shm"):
                try:
                    if os.path.exists(sidecar):
                        os.remove(sidecar)
                except Exception:
                    pass
        return ok

    def restore_all(self):
        """Full restore: config + sessions + group monitor DB + message bot DB."""
        if not self.enabled:
            return
        ensure_dirs()
        logger.info("☁️ Restoring data from Supabase...")
        self.restore_config()
        self.restore_all_sessions()
        self.restore_group_db()
        self.restore_bot_db()
        logger.info("☁️ Cloud restore complete.")


# Singleton — imported by other modules
persistence = PersistenceManager()
