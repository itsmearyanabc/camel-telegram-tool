"""
Widen Pyrogram's channel-id range to match today's Telegram.

THE PROBLEM
───────────
pyrogram 2.0.106 hard-codes, in pyrogram/utils.py:

    MIN_CHANNEL_ID = -1002147483647          # -100 followed by 2**31
    MAX_CHANNEL_ID = -1000000000000

and get_peer_type() rejects anything outside that window:

    if MIN_CHANNEL_ID <= peer_id < MAX_CHANNEL_ID:
        return "channel"
    raise ValueError(f"Peer id invalid: {peer_id}")

That bound assumed a channel's raw id fits in 32 bits. Telegram has since moved
well past it — a channel created recently gets an id like -1004451192738, whose
raw part (4_451_192_738) needs 33 bits. Every such chat is rejected outright:

    ValueError: Peer id invalid: -1004451192738

It surfaces as "Peer id invalid" on forwards, roster syncs, and as unhandled
Client.handle_updates() task exceptions — anywhere a numeric id is resolved that
is not already in the session's peer cache. (A cached peer is returned by
get_peer_by_id before the range check runs, which is why the same account can
work for a while and then fail after its session is restored from a backup.)

WHY PATCH RATHER THAN UPGRADE
─────────────────────────────
Official Pyrogram has had no release since 2.0.106, so there is no newer version
to move to. The maintained forks fix this, but this project deliberately moved
OFF an unofficial fork (see requirements.txt) because the library holds Telegram
session keys. Widening one integer is a far smaller thing to audit.

THE BOUND
─────────
Channels get MAX_CHANNEL_ID - raw_id, so allowing a 40-bit raw id covers roughly
a trillion channels — generous headroom against Telegram's current allocation.
It cannot collide with the chat range: get_peer_type tests MIN_CHAT_ID
(-2_147_483_647) first, and that window is nowhere near -1e12.

Remove this module the day a maintained official release ships the wider bound.
"""

from utils.logger import logger

# 2**40 of headroom below MAX_CHANNEL_ID.
_HEADROOM = 2 ** 40

_applied = False


def apply() -> bool:
    """Widen the bound in-place. Idempotent; safe to call from several modules."""
    global _applied
    if _applied:
        return True

    try:
        from pyrogram import utils as pyro_utils
    except Exception as e:                      # pyrogram missing entirely
        logger.error(f"Pyrogram compat patch skipped — cannot import pyrogram: {e}")
        return False

    try:
        current = int(getattr(pyro_utils, "MIN_CHANNEL_ID"))
        max_channel = int(getattr(pyro_utils, "MAX_CHANNEL_ID"))
    except Exception as e:
        logger.error(f"Pyrogram compat patch skipped — bounds not found: {e}")
        return False

    wanted = max_channel - _HEADROOM
    if current <= wanted:
        # Already wide enough (a future release fixed it upstream).
        _applied = True
        return True

    pyro_utils.MIN_CHANNEL_ID = wanted
    _applied = True
    logger.info(
        f"🔧 Pyrogram channel-id range widened: {current} -> {wanted} "
        f"(2.0.106 rejects modern channel ids such as -1004451192738)."
    )
    return True


def supports(peer_id) -> bool:
    """True if Pyrogram would now accept this id. Used by diagnostics."""
    try:
        from pyrogram import utils as pyro_utils
        pyro_utils.get_peer_type(int(peer_id))
        return True
    except Exception:
        return False


apply()
