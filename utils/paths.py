"""
Where runtime state lives.

Everything the app writes — Telegram sessions, the two SQLite databases,
config.json, temporary uploads — sits under STATE_DIR. It defaults to the
working directory, which is the ordinary behaviour.

Point STATE_DIR at a tmpfs (e.g. /run/telegram-tool) and the app writes
nothing to persistent disk: Supabase becomes the durable store and state is
restored into RAM at boot. See deploy/zero-disk.sh.

Only these paths matter — the application code and virtualenv still live on
disk, because Python has to load them from somewhere.
"""

import os

STATE_DIR = (os.environ.get("STATE_DIR") or ".").rstrip("/\\") or "."


def state_path(*parts) -> str:
    return os.path.join(STATE_DIR, *parts)


SESSIONS_DIR = state_path("sessions")
DATA_DIR = state_path("data")
UPLOAD_DIR = state_path("data", "uploads")
LOGS_DIR = state_path("logs")
CONFIG_PATH = state_path("config.json")
GROUP_DB = state_path("data", "group_monitor.db")
BOT_DB = state_path("data", "message_bot.db")


def session_base(clean_phone: str) -> str:
    """Path Pyrogram is given as its session name (no .session suffix)."""
    return os.path.join(SESSIONS_DIR, f"session_{clean_phone}")


def session_file(clean_phone: str) -> str:
    """The actual .session file on the filesystem."""
    return session_base(clean_phone) + ".session"


def ensure_dirs() -> None:
    for d in (SESSIONS_DIR, DATA_DIR, UPLOAD_DIR):
        os.makedirs(d, exist_ok=True)


def is_ephemeral() -> bool:
    """True when state lives in RAM, so Supabase is the only durable copy."""
    return STATE_DIR.startswith(("/run", "/dev/shm", "/tmp"))
