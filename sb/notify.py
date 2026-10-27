"""Notifications: replies, mentions and awards, each of which a user can switch off.

They're written in the same transaction as the thing that caused them, and only if the
recipient wants that kind (users.notify_off lists the kinds they switched off). Pages
poll /notifications/count every POLL_SECONDS to update the header's unread count."""
import re

from . import db, settings

POLL_SECONDS = 30
KEEP_DAYS = 90  # read notifications older than this are removed when the server starts

# kind: (preference label, what the row says after the actor's name)
KINDS = {
    "post_reply":    ("replies to my posts", "replied to your post"),
    "comment_reply": ("replies to my comments", "replied to your comment on"),
    "mention":       ("mentions of my name (like u/name)", "mentioned you in"),
    "award":         ("awards I receive", "gave you an award on"),
}

MAX_MENTIONS = 10  # per post or comment, so one message can't page the whole board
_MENTION = re.compile(r"(?<![\w/])([A-Za-z0-9_]{1,%d})/([A-Za-z0-9_-]{3,20})(?![\w-])" % db.PREFIX_MAX)


def add(cx, user_id, kind, actor_id, target_id, post_id, detail=""):
    """Notify user_id unless they're the actor or switched this kind off. Returns True if sent."""
    if not user_id or user_id == actor_id:
        return False
    return bool(cx.execute(
        "INSERT INTO notifications SELECT ?,?,?,?,?,?,?,0,? FROM users "
        "WHERE id=? AND NOT ? = ANY(string_to_array(notify_off, ','))",
        (db.new_id(), user_id, kind, actor_id, target_id, post_id, detail, db.now(), user_id, kind)).rowcount)


def mentions(cx, text, actor_id, target_id, post_id, skip=()):
    """Notify everyone written as <their shown prefix>/<name> in text (e.g. u/alice)."""
    found = {}
    for prefix, name in _MENTION.findall(text):
        found.setdefault(name.lower(), set()).add(prefix.lower())
        if len(found) >= MAX_MENTIONS:
            break
    if not found:
        return
    for u in cx.execute("SELECT id, name, prefix FROM users WHERE name = ANY(?::citext[]) AND banned=0", (list(found),)):
        if u["id"] not in skip and settings.shown(u["prefix"], "user").lower() in found[u["name"].lower()]:
            add(cx, u["id"], "mention", actor_id, target_id, post_id)


def prune(cx):
    cx.execute("DELETE FROM notifications WHERE read=1 AND created<?", (db.now() - KEEP_DAYS * 86400,))
