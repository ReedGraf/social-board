"""Platform settings, editable by admins at /admin. Stored in the database's
meta table. Read once per request (no shared cache), so several server
processes on one database always agree."""
import threading

from . import db

DEFAULTS = {
    "site_name": "social board",
    "tagline": "Posts from every community on this board.",
    "user_prefix": "u",           # default shown before usernames
    "community_prefix": "r",      # default shown before community names
    "custom_user_prefix": "1",    # may users pick their own prefix?
    "custom_community_prefix": "1",
    "open_registration": "1",
    "community_creation": "anyone",  # or "admins"
}

_local = threading.local()


def _values():
    v = getattr(_local, "v", None)
    if v is None:
        rows = db.conn().execute("SELECT key, value FROM meta WHERE key LIKE ?", ("set.%",)).fetchall()
        v = _local.v = dict(DEFAULTS)
        v.update({r["key"][4:]: r["value"] for r in rows if r["key"][4:] in DEFAULTS})
    return v


def reset():
    """Forget this thread's copy; called at the end of each request."""
    _local.v = None


def get(key):
    return _values()[key]


def flag(key):
    return get(key) == "1"


def save(values):
    with db.tx() as c:
        c.executemany("INSERT INTO meta VALUES(?, ?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                      [("set." + k, str(v)) for k, v in values.items() if k in DEFAULTS])
    reset()


def all():
    return dict(_values())


def shown(stored, kind, conf=None):
    """The prefix displayed (and used in URLs) for a user's or community's stored prefix,
    honoring the admin's lock. `kind` is "user" or "community"; `conf` defaults to the live settings."""
    conf = conf or _values()
    return stored if conf["custom_%s_prefix" % kind] == "1" else conf["%s_prefix" % kind]
