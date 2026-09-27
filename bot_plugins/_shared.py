# =============================================================================
#  CipherElite — Shared helpers for bot plugins
#  Target path:  bot_plugins/_shared.py
#
#  Imported by:  bot_plugins/moderation.py, bot_plugins/chatguard.py,
#                bot_plugins/joinmod.py
#  (each file tries `from bot_plugins._shared import ...` first, then falls
#   back to `from _shared import ...` — this file satisfies the first form
#   when it sits in the bot_plugins/ folder.)
#
#  Author:  CipherElite Dev (@rishabhops)
#  Repository:  https://github.com/rishabhops/CipherElite
# =============================================================================

import re
from datetime import datetime, timedelta, timezone

# Indian Standard Time (UTC+5:30) — used by all timestamps shown to users.
IST = timezone(timedelta(hours=5, minutes=30))

# Telegram message length limit (leave some headroom for edits/appends).
_MSG_LIMIT = 3800

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# ── text helpers ─────────────────────────────────────────────────────────────
def clip(text, limit=_MSG_LIMIT):
    """Truncate text to `limit` characters, appending an ellipsis when cut."""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def user_label(user):
    """Human-readable name for a user/channel object (plain text)."""
    if user is None:
        return "unknown"
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    full = (first + " " + last).strip()
    if full:
        return full
    title = getattr(user, "title", None)
    if title:
        return str(title)
    uname = getattr(user, "username", None)
    if uname:
        return "@" + uname
    uid = getattr(user, "id", None)
    return str(uid) if uid is not None else "unknown"


# ── time helpers ─────────────────────────────────────────────────────────────
def now_str():
    """Current IST timestamp as a display string."""
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def fmt_when(deadline):
    """Format a (UTC-naive) deadline datetime for display; None -> 'forever'."""
    if deadline is None:
        return "forever"
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def parse_duration(text):
    """Parse '30s / 5m / 2h / 1d / 1w / 90' into seconds. None if invalid.

    A bare number is treated as seconds. Suffixed forms accept an optional
    space between the number and the unit.
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    m = _DURATION_RE.match(text)
    if m:
        return int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    if text.isdigit():
        return int(text)
    return None


def human_duration(seconds):
    """Format seconds as '1d 2h 3m' style text. None -> 'forever'."""
    if seconds is None:
        return "forever"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "forever"
    if seconds <= 0:
        return "0s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts)


def until(seconds):
    """Deadline datetime for Telethon's until_date (naive UTC). None = forever."""
    if seconds is None:
        return None
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return now_utc + timedelta(seconds=int(seconds))


# ── permission helpers ───────────────────────────────────────────────────────
def group_only(event):
    """True when the event happened in a group/channel (not a private chat)."""
    try:
        return not event.is_private
    except Exception:
        return False


async def is_admin(event, user_id):
    """True when user_id is a creator/admin of the chat (owner counts)."""
    try:
        if user_id is None:
            return False
        perms = await event.client.get_permissions(event.chat, user_id)
        return bool(
            getattr(perms, "is_creator", False) or getattr(perms, "is_admin", False)
        )
    except Exception:
        return False


async def require_admin(event, owner_id=None):
    """Gate for admin-only commands. Replies with an error when denied."""
    try:
        sender_id = event.sender_id
        if owner_id is not None and sender_id == owner_id:
            return True
        if await is_admin(event, sender_id):
            return True
        await event.reply("❌ **Admin rights required** for this command.")
        return False
    except Exception:
        try:
            await event.reply("❌ **Could not verify your admin rights.**")
        except Exception:
            pass
        return False


# ── target resolution ────────────────────────────────────────────────────────
async def target_from_event(event, arg=""):
    """Resolve the target user of a moderation command.

    Resolution order: explicit @username / numeric id in `arg`, then the
    replied-to message's sender. Returns a `(user, error)` tuple — `error` is
    a user-facing string when something went wrong, otherwise None.
    """
    try:
        arg = (arg or "").strip()

        if arg:
            if arg.startswith("@"):
                try:
                    user = await event.client.get_entity(arg)
                    return user, None
                except Exception:
                    return None, f"❌ **User `{arg}` not found.**"
            if arg.isdigit():
                try:
                    user = await event.client.get_entity(int(arg))
                    return user, None
                except Exception:
                    return None, f"❌ **User with id `{arg}` not found.**"
            return None, f"❌ **`{arg}` is not a @username or id.**"

        if event.reply_to_msg_id:
            replied = await event.get_reply_message()
            if replied is not None and replied.sender_id is not None:
                return replied.sender, None

        return None, None
    except Exception as e:
        return None, f"❌ **Could not resolve the target:** `{str(e)[:120]}`"


# ── message helpers ──────────────────────────────────────────────────────────
async def safe_edit(message, text):
    """Edit a previously-sent message, ignoring any failure."""
    try:
        await message.edit(clip(text))
    except Exception:
        pass


async def safe_delete(message):
    """Delete a message, ignoring any failure."""
    try:
        await message.delete()
    except Exception:
        pass
