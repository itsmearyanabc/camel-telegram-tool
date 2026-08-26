"""
Message Bot Storage Layer
──────────────────────────
Holds the bot token, the permanent recipient list, and the send log.

Recipients persist until explicitly deleted — adding the same person twice is a
no-op update, never a duplicate row. A recipient is keyed by numeric user_id
once known, otherwise by lowercased username, so a person added by @username
and later resolved to an ID collapses into a single row.

Same threading rules as GroupStore: short-lived connections under an RLock.
"""

import os
import sqlite3
import threading
import time
from typing import List, Dict, Optional

from utils.logger import logger

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "message_bot.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_config (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    token         TEXT,
    bot_id        INTEGER,
    bot_username  TEXT,
    bot_name      TEXT,
    update_offset INTEGER DEFAULT 0,
    added_at      REAL
);

CREATE TABLE IF NOT EXISTS recipients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER,
    username     TEXT,
    display_name TEXT,
    chat_id      INTEGER,
    status       TEXT DEFAULT 'pending',
    last_error   TEXT,
    added_at     REAL,
    last_sent    REAL,
    sent_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS send_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER,
    label        TEXT,
    kind         TEXT,
    ok           INTEGER,
    error        TEXT,
    ts           REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_recip_uid  ON recipients (user_id)  WHERE user_id  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_recip_uname ON recipients (username) WHERE username IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sendlog_ts ON send_log (ts DESC);
"""

# What a recipient's status means, in the UI's words:
#   pending — added, but the bot cannot message them until they press Start
#   ready   — they have messaged the bot, so delivery will work
#   blocked — they blocked the bot or deleted the chat
#   failed  — last send raised an error worth showing
VALID_STATUS = {"pending", "ready", "blocked", "failed"}


def _now() -> float:
    return time.time()


def _sync_cloud():
    """Queue a debounced push of this DB to Supabase after a mutation."""
    try:
        from core.services.persistence import persistence
        persistence.schedule_backup("bot")
    except Exception:
        pass


def norm_username(u: str) -> str:
    """@Alice / t.me/Alice / https://t.me/Alice  ->  alice"""
    u = str(u or "").strip()
    for p in ("https://", "http://"):
        if u.lower().startswith(p):
            u = u[len(p):]
    for p in ("t.me/", "telegram.me/"):
        if u.lower().startswith(p):
            u = u[len(p):]
    u = u.split("?")[0].strip("/")
    if u.startswith("@"):
        u = u[1:]
    return u.lower()


class BotStore:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def checkpoint(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
            except Exception as e:
                logger.warning(f"🤖 WAL checkpoint skipped: {e}")
            finally:
                conn.close()

    # ─────────────────────────────────────
    # BOT CONFIG
    # ─────────────────────────────────────
    def save_bot(self, token, bot_id=None, bot_username=None, bot_name=None):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO bot_config (id, token, bot_id, bot_username, bot_name, added_at)"
                    " VALUES (1,?,?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET token=excluded.token, bot_id=excluded.bot_id,"
                    " bot_username=excluded.bot_username, bot_name=excluded.bot_name",
                    (token, bot_id, bot_username, bot_name, _now()),
                )
                conn.commit()
            finally:
                conn.close()
        _sync_cloud()

    def get_bot(self) -> Optional[Dict]:
        with self._lock:
            conn = self._conn()
            try:
                r = conn.execute("SELECT * FROM bot_config WHERE id=1").fetchone()
                return dict(r) if r else None
            finally:
                conn.close()

    def clear_bot(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM bot_config WHERE id=1")
                conn.commit()
            finally:
                conn.close()
        _sync_cloud()

    def set_offset(self, offset: int):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("UPDATE bot_config SET update_offset=? WHERE id=1", (offset,))
                conn.commit()
            finally:
                conn.close()

    # ─────────────────────────────────────
    # RECIPIENTS
    # ─────────────────────────────────────
    def add_recipient(self, user_id=None, username=None, display_name=None,
                      chat_id=None, status=None) -> str:
        """
        Upsert one recipient. Returns 'added' | 'updated'.
        Matching is by user_id first, then username, so the same person added
        twice in different forms collapses into one row.
        """
        username = norm_username(username) if username else None
        if not user_id and not username:
            return "skipped"

        with self._lock:
            conn = self._conn()
            try:
                row = None
                if user_id:
                    row = conn.execute("SELECT * FROM recipients WHERE user_id=?", (user_id,)).fetchone()
                if row is None and username:
                    row = conn.execute("SELECT * FROM recipients WHERE username=?", (username,)).fetchone()

                if row is None:
                    conn.execute(
                        "INSERT INTO recipients (user_id,username,display_name,chat_id,status,added_at)"
                        " VALUES (?,?,?,?,?,?)",
                        (user_id, username, display_name, chat_id or user_id,
                         status or ("ready" if chat_id else "pending"), _now()),
                    )
                    conn.commit()
                    _sync_cloud()
                    return "added"

                # Merge: never blank out something we already know.
                conn.execute(
                    "UPDATE recipients SET"
                    " user_id=COALESCE(?,user_id), username=COALESCE(?,username),"
                    " display_name=COALESCE(?,display_name), chat_id=COALESCE(?,chat_id),"
                    " status=COALESCE(?,status)"
                    " WHERE id=?",
                    (user_id, username, display_name, chat_id or user_id, status, row["id"]),
                )
                conn.commit()
                _sync_cloud()
                return "updated"
            finally:
                conn.close()

    def list_recipients(self, search: str = "", status: str = "") -> List[Dict]:
        sql = "SELECT * FROM recipients WHERE 1=1"
        params = []
        if search:
            sql += (" AND (IFNULL(username,'') LIKE ? OR IFNULL(display_name,'') LIKE ?"
                    " OR CAST(IFNULL(user_id,'') AS TEXT) LIKE ?)")
            like = f"%{search.lower()}%"
            params += [like, like, like]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY added_at DESC"
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

    def get_recipients_by_ids(self, ids: List[int]) -> List[Dict]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    f"SELECT * FROM recipients WHERE id IN ({marks})", ids
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def delete_recipients(self, ids: List[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(f"DELETE FROM recipients WHERE id IN ({marks})", ids)
                conn.commit()
                _sync_cloud()
                return cur.rowcount
            finally:
                conn.close()

    def update_recipient(self, rid: int, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(f"UPDATE recipients SET {cols} WHERE id=?", (*fields.values(), rid))
                conn.commit()
            finally:
                conn.close()

    def mark_sent(self, rid: int, ok: bool, error: str = "", label: str = "", kind: str = ""):
        with self._lock:
            conn = self._conn()
            try:
                if ok:
                    conn.execute(
                        "UPDATE recipients SET last_sent=?, sent_count=sent_count+1,"
                        " status='ready', last_error=NULL WHERE id=?",
                        (_now(), rid),
                    )
                else:
                    status = "blocked" if _is_blocked(error) else "failed"
                    conn.execute(
                        "UPDATE recipients SET status=?, last_error=? WHERE id=?",
                        (status, error[:300], rid),
                    )
                conn.execute(
                    "INSERT INTO send_log (recipient_id,label,kind,ok,error,ts) VALUES (?,?,?,?,?,?)",
                    (rid, label, kind, 1 if ok else 0, error[:300], _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def recent_sends(self, limit: int = 100) -> List[Dict]:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT s.*, r.username, r.display_name, r.user_id FROM send_log s"
                    " LEFT JOIN recipients r ON r.id = s.recipient_id"
                    " ORDER BY s.ts DESC LIMIT ?", (limit,)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def totals(self) -> Dict:
        with self._lock:
            conn = self._conn()
            try:
                q = lambda s, *a: conn.execute(s, a).fetchone()[0]
                return {
                    "recipients": q("SELECT COUNT(*) FROM recipients"),
                    "ready": q("SELECT COUNT(*) FROM recipients WHERE status='ready'"),
                    "pending": q("SELECT COUNT(*) FROM recipients WHERE status='pending'"),
                    "blocked": q("SELECT COUNT(*) FROM recipients WHERE status IN ('blocked','failed')"),
                    "sent": q("SELECT COUNT(*) FROM send_log WHERE ok=1"),
                }
            finally:
                conn.close()


def _is_blocked(err: str) -> bool:
    e = (err or "").lower()
    return "blocked" in e or "deactivated" in e or "chat not found" in e


bot_store = BotStore()
