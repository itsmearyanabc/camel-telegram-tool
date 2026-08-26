"""
Group Monitor Storage Layer
────────────────────────────
SQLite-backed store for monitored Telegram groups and their membership data.

Why SQLite and not config.json:
  • Rosters can run to thousands of rows per group — JSON rewrites get slow.
  • We need an append-only event log (joins/leaves) with ordering.
  • It is a single file, so it backs up to Supabase exactly like .session files do.

Thread safety: Flask request threads and the _BOT_LOOP thread both touch this,
so every call takes a short-lived connection guarded by an RLock.
"""

import os
import sqlite3
import threading
import time
from typing import List, Dict, Optional

from utils.logger import logger

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "group_monitor.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    chat_key      TEXT PRIMARY KEY,
    chat_id       INTEGER,
    title         TEXT,
    username      TEXT,
    chat_type     TEXT,
    source_ref    TEXT,
    account_phone TEXT,
    is_active     INTEGER DEFAULT 1,
    added_at      REAL,
    last_sync     REAL,
    last_error    TEXT,
    member_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS members (
    chat_key    TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    phone       TEXT,
    is_bot      INTEGER DEFAULT 0,
    is_premium  INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'present',
    first_seen  REAL,
    last_seen   REAL,
    joined_at   REAL,
    left_at     REAL,
    PRIMARY KEY (chat_key, user_id)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key     TEXT NOT NULL,
    user_id      INTEGER,
    username     TEXT,
    display_name TEXT,
    event_type   TEXT,
    source       TEXT,
    ts           REAL
);

CREATE INDEX IF NOT EXISTS idx_members_chat   ON members (chat_key, status);
CREATE INDEX IF NOT EXISTS idx_events_chat    ON events  (chat_key, event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_user    ON events  (chat_key, user_id);
"""


def _now() -> float:
    return time.time()


def _sync_cloud():
    """Queue a debounced push of this DB to Supabase after a structural change."""
    try:
        from core.services.persistence import persistence
        persistence.schedule_backup("group")
    except Exception:
        pass


class GroupStore:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init_schema()

    # ─────────────────────────────────────
    # LOW LEVEL
    # ─────────────────────────────────────
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

    # ─────────────────────────────────────
    # GROUPS
    # ─────────────────────────────────────
    def upsert_group(self, chat_key: str, **fields) -> None:
        with self._lock:
            conn = self._conn()
            try:
                existing = conn.execute(
                    "SELECT chat_key FROM groups WHERE chat_key=?", (chat_key,)
                ).fetchone()
                if existing:
                    if fields:
                        cols = ", ".join(f"{k}=?" for k in fields)
                        conn.execute(
                            f"UPDATE groups SET {cols} WHERE chat_key=?",
                            (*fields.values(), chat_key),
                        )
                else:
                    fields.setdefault("added_at", _now())
                    fields.setdefault("is_active", 1)
                    cols = ", ".join(["chat_key"] + list(fields.keys()))
                    marks = ", ".join(["?"] * (len(fields) + 1))
                    conn.execute(
                        f"INSERT INTO groups ({cols}) VALUES ({marks})",
                        (chat_key, *fields.values()),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_group(self, chat_key: str) -> Optional[Dict]:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM groups WHERE chat_key=?", (chat_key,)
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_groups(self) -> List[Dict]:
        """All monitored groups with live join/leave/present counters."""
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("SELECT * FROM groups ORDER BY added_at ASC").fetchall()
                out = []
                for r in rows:
                    g = dict(r)
                    k = g["chat_key"]
                    g["present_count"] = conn.execute(
                        "SELECT COUNT(*) FROM members WHERE chat_key=? AND status='present'", (k,)
                    ).fetchone()[0]
                    g["left_count"] = conn.execute(
                        "SELECT COUNT(*) FROM members WHERE chat_key=? AND status='left'", (k,)
                    ).fetchone()[0]
                    g["join_events"] = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE chat_key=? AND event_type='join'", (k,)
                    ).fetchone()[0]
                    g["leave_events"] = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE chat_key=? AND event_type='leave'", (k,)
                    ).fetchone()[0]
                    out.append(g)
                return out
            finally:
                conn.close()

    def delete_group(self, chat_key: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM events  WHERE chat_key=?", (chat_key,))
                conn.execute("DELETE FROM members WHERE chat_key=?", (chat_key,))
                conn.execute("DELETE FROM groups  WHERE chat_key=?", (chat_key,))
                conn.commit()
            finally:
                conn.close()
        _sync_cloud()

    def set_active(self, chat_key: str, active: bool) -> None:
        self.upsert_group(chat_key, is_active=1 if active else 0)

    def has_baseline(self, chat_key: str) -> bool:
        """True once a first roster snapshot has been taken for this group."""
        with self._lock:
            conn = self._conn()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM members WHERE chat_key=?", (chat_key,)
                ).fetchone()[0]
                return n > 0
            finally:
                conn.close()

    # ─────────────────────────────────────
    # MEMBERS + EVENTS
    # ─────────────────────────────────────
    def record_event(self, chat_key, user_id, username, display_name, event_type, source):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO events (chat_key,user_id,username,display_name,event_type,source,ts)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (chat_key, user_id, username, display_name, event_type, source, _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_present(self, chat_key: str, user: Dict, source: str = "live") -> bool:
        """
        Record a user as present in the group.
        Returns True if this was a genuine new join (i.e. an event was logged).
        """
        now = _now()
        is_new_join = False
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT status FROM members WHERE chat_key=? AND user_id=?",
                    (chat_key, user["user_id"]),
                ).fetchone()
                is_new_join = (row is None) or (row["status"] != "present")

                if row is None:
                    conn.execute(
                        "INSERT INTO members (chat_key,user_id,username,first_name,last_name,"
                        "phone,is_bot,is_premium,status,first_seen,last_seen,joined_at)"
                        " VALUES (?,?,?,?,?,?,?,?, 'present', ?,?,?)",
                        (
                            chat_key, user["user_id"], user.get("username"),
                            user.get("first_name"), user.get("last_name"), user.get("phone"),
                            1 if user.get("is_bot") else 0, 1 if user.get("is_premium") else 0,
                            now, now, now,
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE members SET username=COALESCE(?,username),"
                        " first_name=COALESCE(?,first_name), last_name=COALESCE(?,last_name),"
                        " phone=COALESCE(?,phone), status='present', last_seen=?,"
                        " joined_at=CASE WHEN status!='present' THEN ? ELSE joined_at END,"
                        " left_at=NULL"
                        " WHERE chat_key=? AND user_id=?",
                        (
                            user.get("username"), user.get("first_name"), user.get("last_name"),
                            user.get("phone"), now, now, chat_key, user["user_id"],
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

        # Baseline snapshots seed the roster without flooding the join log.
        if is_new_join and source != "baseline":
            self.record_event(
                chat_key, user["user_id"], user.get("username"),
                _display_name(user), "join", source,
            )
        return is_new_join and source != "baseline"

    def mark_left(self, chat_key: str, user: Dict, source: str = "live") -> bool:
        """Record a user as having left. Returns True if an event was logged."""
        now = _now()
        already_left = True
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT status, username, first_name, last_name FROM members"
                    " WHERE chat_key=? AND user_id=?",
                    (chat_key, user["user_id"]),
                ).fetchone()

                if row is None:
                    # Someone we never saw join — still worth recording.
                    conn.execute(
                        "INSERT INTO members (chat_key,user_id,username,first_name,last_name,"
                        "phone,is_bot,is_premium,status,first_seen,last_seen,left_at)"
                        " VALUES (?,?,?,?,?,?,?,?, 'left', ?,?,?)",
                        (
                            chat_key, user["user_id"], user.get("username"),
                            user.get("first_name"), user.get("last_name"), user.get("phone"),
                            1 if user.get("is_bot") else 0, 1 if user.get("is_premium") else 0,
                            now, now, now,
                        ),
                    )
                    already_left = False
                else:
                    already_left = row["status"] == "left"
                    if not already_left:
                        conn.execute(
                            "UPDATE members SET status='left', left_at=?, last_seen=?,"
                            " username=COALESCE(?,username), first_name=COALESCE(?,first_name),"
                            " last_name=COALESCE(?,last_name)"
                            " WHERE chat_key=? AND user_id=?",
                            (
                                now, now, user.get("username"), user.get("first_name"),
                                user.get("last_name"), chat_key, user["user_id"],
                            ),
                        )
                    # Carry forward stored identity for a nicer event label.
                    user.setdefault("username", row["username"])
                    user.setdefault("first_name", row["first_name"])
                    user.setdefault("last_name", row["last_name"])
                conn.commit()
            finally:
                conn.close()

        if not already_left:
            self.record_event(
                chat_key, user["user_id"], user.get("username"),
                _display_name(user), "leave", source,
            )
        return not already_left

    def present_user_ids(self, chat_key: str) -> set:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT user_id FROM members WHERE chat_key=? AND status='present'", (chat_key,)
                ).fetchall()
                return {r["user_id"] for r in rows}
            finally:
                conn.close()

    def get_members(self, chat_key: str, status: str = "present",
                    search: str = "", limit: int = 5000) -> List[Dict]:
        order = "joined_at DESC" if status == "present" else "left_at DESC"
        sql = "SELECT * FROM members WHERE chat_key=? AND status=?"
        params = [chat_key, status]
        if search:
            sql += " AND (IFNULL(username,'') LIKE ? OR IFNULL(first_name,'') LIKE ?" \
                   " OR IFNULL(last_name,'') LIKE ? OR CAST(user_id AS TEXT) LIKE ?)"
            like = f"%{search}%"
            params += [like, like, like, like]
        sql += f" ORDER BY {order} LIMIT ?"
        params.append(limit)
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

    def get_events(self, chat_key: str, event_type: str = "join",
                   search: str = "", limit: int = 5000) -> List[Dict]:
        sql = "SELECT * FROM events WHERE chat_key=? AND event_type=?"
        params = [chat_key, event_type]
        if search:
            sql += " AND (IFNULL(username,'') LIKE ? OR IFNULL(display_name,'') LIKE ?" \
                   " OR CAST(user_id AS TEXT) LIKE ?)"
            like = f"%{search}%"
            params += [like, like, like]
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

    def checkpoint(self) -> None:
        """Fold the WAL back into the main .db file so a file-level backup is complete."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
            except Exception as e:
                logger.warning(f"👁 WAL checkpoint skipped: {e}")
            finally:
                conn.close()

    def totals(self) -> Dict:
        with self._lock:
            conn = self._conn()
            try:
                q = lambda s: conn.execute(s).fetchone()[0]
                return {
                    "groups": q("SELECT COUNT(*) FROM groups"),
                    "active": q("SELECT COUNT(*) FROM groups WHERE is_active=1"),
                    "present": q("SELECT COUNT(*) FROM members WHERE status='present'"),
                    "left": q("SELECT COUNT(*) FROM members WHERE status='left'"),
                    "joins": q("SELECT COUNT(*) FROM events WHERE event_type='join'"),
                    "leaves": q("SELECT COUNT(*) FROM events WHERE event_type='leave'"),
                }
            finally:
                conn.close()


def _display_name(user: Dict) -> str:
    name = " ".join(
        filter(None, [user.get("first_name") or "", user.get("last_name") or ""])
    ).strip()
    return name or (f"@{user['username']}" if user.get("username") else f"ID {user.get('user_id')}")


# Singleton
group_store = GroupStore()
