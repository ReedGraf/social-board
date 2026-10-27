"""Real-time "new post" events, carried by PostgreSQL LISTEN/NOTIFY (no Redis).

Submitting a post runs pg_notify('new_post', ...) inside its transaction, so the
event fires only once the post is committed, and every server process attached
to the database hears it. One background thread per process listens and wakes
the browsers' event streams, which wait on a condition variable."""
import threading
import time

import psycopg

from . import db

CHANNEL = "new_post"

_cond = threading.Condition()
_seq = 0
_recent = []  # (seq, post_id, created), newest last; enough to catch up a waking stream


def notify(c, post_id, created):
    """Call inside the transaction that creates the post."""
    c.execute("SELECT pg_notify(?, ?)", (CHANNEL, "%s %d" % (post_id, created)))


def _publish(payload):
    global _seq
    try:
        pid, created = payload.split()
        created = int(created)
    except ValueError:
        return
    with _cond:
        _seq += 1
        _recent.append((_seq, pid, created))
        del _recent[:-200]
        _cond.notify_all()


def current():
    with _cond:
        return _seq


def wait(after, timeout):
    """Block until there are events newer than `after` (or timeout). Returns (events, new_after)."""
    with _cond:
        _cond.wait_for(lambda: _seq > after, timeout)
        return [(pid, created) for s, pid, created in _recent if s > after], _seq


def _listen_forever():
    while True:
        try:
            with psycopg.connect(db.DSN, autocommit=True) as c:
                c.execute("LISTEN " + CHANNEL)
                for n in c.notifies():
                    _publish(n.payload)
        except Exception as ex:  # database restarted or unreachable: retry quietly
            print("live updates: lost database connection (%s); retrying" % type(ex).__name__)
            time.sleep(3)


def start():
    threading.Thread(target=_listen_forever, name="live-listener", daemon=True).start()
