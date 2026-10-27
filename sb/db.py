"""PostgreSQL storage (images included, as BYTEA). IDs are random strings so two
boards' data can be merged without key collisions.

Boards move between instances as a portable snapshot: `export` writes one
SQLite file (Python reads it with no extra software) and `merge` loads one."""
import hashlib
import math
import os
import queue
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import lru_cache

import psycopg
from psycopg.pq import TransactionStatus

PREFIX_MAX = 8  # length cap for the customizable "u" / "r" prefixes

IntegrityError = psycopg.IntegrityError

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS citext;  -- case-insensitive names

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS images(
  id      TEXT PRIMARY KEY,
  sha256  TEXT NOT NULL UNIQUE,
  mime    TEXT NOT NULL,
  data    BYTEA NOT NULL,
  created BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS users(
  id            TEXT PRIMARY KEY,
  name          CITEXT NOT NULL UNIQUE,
  prefix        TEXT NOT NULL DEFAULT 'u' CHECK(length(prefix) BETWEEN 1 AND {PREFIX_MAX}),
  pw_hash       TEXT NOT NULL,
  recovery_hash TEXT NOT NULL,
  avatar_id     TEXT REFERENCES images(id),
  bio           TEXT NOT NULL DEFAULT '',
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created       BIGINT NOT NULL,
  banned        INTEGER NOT NULL DEFAULT 0,
  live_updates  INTEGER NOT NULL DEFAULT 1,  -- preference: new posts appear on the front page live
  dark_mode     INTEGER NOT NULL DEFAULT 1,  -- preference: dark theme (the default)
  award_karma   INTEGER NOT NULL DEFAULT 100 CHECK(award_karma >= 0),  -- spendable on awards; not regular karma
  karma_adjust  INTEGER NOT NULL DEFAULT 0   -- admin (god mode) adjustment added to vote-based karma
);
-- columns added after the first release; no-ops on new databases
ALTER TABLE users ADD COLUMN IF NOT EXISTS live_updates INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS dark_mode INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS award_karma INTEGER NOT NULL DEFAULT 100 CHECK(award_karma >= 0);
ALTER TABLE users ADD COLUMN IF NOT EXISTS karma_adjust INTEGER NOT NULL DEFAULT 0;

-- Every god-mode action, newest last. Names (not ids) so entries survive renames and deletions.
CREATE TABLE IF NOT EXISTS admin_log(
  id      TEXT PRIMARY KEY,
  admin   TEXT NOT NULL,
  target  TEXT NOT NULL,
  action  TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT '',
  created BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_log_created ON admin_log(created DESC);

CREATE TABLE IF NOT EXISTS communities(
  id          TEXT PRIMARY KEY,
  name        CITEXT NOT NULL UNIQUE,
  prefix      TEXT NOT NULL DEFAULT 'r' CHECK(length(prefix) BETWEEN 1 AND {PREFIX_MAX}),
  title       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  owner_id    TEXT REFERENCES users(id),
  created     BIGINT NOT NULL,
  color       TEXT NOT NULL DEFAULT ''   -- '#rrggbb' chosen by the creator, or '' for the default
);
ALTER TABLE communities ADD COLUMN IF NOT EXISTS color TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS posts(
  id            TEXT PRIMARY KEY,
  community_id  TEXT NOT NULL REFERENCES communities(id),
  author_id     TEXT REFERENCES users(id),
  kind          TEXT NOT NULL CHECK(kind IN ('text', 'image')),
  title         TEXT NOT NULL,             -- the caption, for image posts
  body          TEXT NOT NULL DEFAULT '',
  image_id      TEXT REFERENCES images(id),
  thumb_id      TEXT REFERENCES images(id),
  score         INTEGER NOT NULL DEFAULT 0,
  hot           DOUBLE PRECISION NOT NULL DEFAULT 0,
  comment_count INTEGER NOT NULL DEFAULT 0,
  deleted       INTEGER NOT NULL DEFAULT 0,
  created       BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_hot    ON posts(hot DESC);
CREATE INDEX IF NOT EXISTS posts_new    ON posts(created DESC);
CREATE INDEX IF NOT EXISTS posts_top    ON posts(score DESC);
CREATE INDEX IF NOT EXISTS posts_c_hot  ON posts(community_id, hot DESC);
CREATE INDEX IF NOT EXISTS posts_c_new  ON posts(community_id, created DESC);
CREATE INDEX IF NOT EXISTS posts_author ON posts(author_id, created DESC);

CREATE TABLE IF NOT EXISTS comments(
  id        TEXT PRIMARY KEY,
  post_id   TEXT NOT NULL REFERENCES posts(id),
  parent_id TEXT REFERENCES comments(id),
  author_id TEXT REFERENCES users(id),
  body      TEXT NOT NULL,
  score     INTEGER NOT NULL DEFAULT 0,
  deleted   INTEGER NOT NULL DEFAULT 0,
  created   BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_post   ON comments(post_id);
CREATE INDEX IF NOT EXISTS comments_author ON comments(author_id, created DESC);

CREATE TABLE IF NOT EXISTS votes(
  user_id   TEXT NOT NULL REFERENCES users(id),
  target_id TEXT NOT NULL,
  value     INTEGER NOT NULL CHECK(value IN (-1, 1)),
  PRIMARY KEY(user_id, target_id)
);

-- Awards given to posts and comments (target_id is either). The award types and prices
-- live in sb/awards.py; a deleted giver/receiver becomes NULL so the award stays visible.
CREATE TABLE IF NOT EXISTS awards_given(
  id          TEXT PRIMARY KEY,
  award       TEXT NOT NULL,
  giver_id    TEXT REFERENCES users(id),
  receiver_id TEXT REFERENCES users(id),
  target_id   TEXT NOT NULL,
  created     BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS awards_target   ON awards_given(target_id);
CREATE INDEX IF NOT EXISTS awards_receiver ON awards_given(receiver_id);

-- Which upvotes already paid their +1 award karma, so un-voting and re-voting can't farm it.
CREATE TABLE IF NOT EXISTS upvote_payouts(
  voter_id  TEXT NOT NULL,
  target_id TEXT NOT NULL,
  PRIMARY KEY(voter_id, target_id)
);

-- Sessions are bound to the instance that issued them, so two boards sharing
-- one hostname (different ports) never accept each other's cookies.
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id),
  instance   TEXT NOT NULL,
  csrf       TEXT NOT NULL,
  expires    BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
"""

# Snapshot file layout (SQLite). Same tables and column order as above, minus sessions.
SNAPSHOT_SCHEMA = f"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE images(id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, mime TEXT NOT NULL,
  data BLOB NOT NULL, created INTEGER NOT NULL);
CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, prefix TEXT NOT NULL,
  pw_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL, avatar_id TEXT, bio TEXT NOT NULL,
  is_admin INTEGER NOT NULL, created INTEGER NOT NULL, banned INTEGER NOT NULL DEFAULT 0,
  live_updates INTEGER NOT NULL DEFAULT 1, dark_mode INTEGER NOT NULL DEFAULT 1, award_karma INTEGER NOT NULL DEFAULT 100,
  karma_adjust INTEGER NOT NULL DEFAULT 0);
CREATE TABLE communities(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, prefix TEXT NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL, owner_id TEXT, created INTEGER NOT NULL,
  color TEXT NOT NULL DEFAULT '');
CREATE TABLE posts(id TEXT PRIMARY KEY, community_id TEXT NOT NULL, author_id TEXT, kind TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL, image_id TEXT, thumb_id TEXT, score INTEGER NOT NULL,
  hot REAL NOT NULL, comment_count INTEGER NOT NULL, deleted INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE comments(id TEXT PRIMARY KEY, post_id TEXT NOT NULL, parent_id TEXT, author_id TEXT,
  body TEXT NOT NULL, score INTEGER NOT NULL, deleted INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE votes(user_id TEXT NOT NULL, target_id TEXT NOT NULL, value INTEGER NOT NULL,
  PRIMARY KEY(user_id, target_id));
CREATE TABLE awards_given(id TEXT PRIMARY KEY, award TEXT NOT NULL, giver_id TEXT, receiver_id TEXT,
  target_id TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE upvote_payouts(voter_id TEXT NOT NULL, target_id TEXT NOT NULL, PRIMARY KEY(voter_id, target_id));
"""
SNAPSHOT_TABLES = ("images", "users", "communities", "posts", "comments", "votes", "awards_given", "upvote_payouts")

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_id():
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))  # ~52 bits


def now():
    return int(time.time())


def hot(score, created):
    """Reddit's hot rank. Doesn't depend on the current time, so it can be stored and indexed."""
    sign = (score > 0) - (score < 0)
    return round(sign * math.log10(max(abs(score), 1)) + (created - 1134028003) / 45000, 7)


# --- connections ----------------------------------------------------------------

class Row(dict):
    """Row readable by column name or position, like sqlite3.Row."""
    __slots__ = ("_v",)

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._v = values

    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else dict.__getitem__(self, k)

    def __iter__(self):  # tuple(row) gives the values, as with sqlite3.Row
        return iter(self._v)


def _row_factory(cursor):
    names = [c.name for c in cursor.description or ()]
    return lambda values: Row(names, values)


@lru_cache(maxsize=512)
def _pg(sql):
    # Queries are written with "?" placeholders; psycopg wants "%s" (and literal % doubled).
    return sql.replace("%", "%%").replace("?", "%s")


class Conn:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        if params is None:
            return self.raw.execute(sql)
        return self.raw.execute(_pg(sql), params)

    def executemany(self, sql, seq):
        with self.raw.cursor() as cur:
            cur.executemany(_pg(sql), seq)


DSN = None
POOL_MAX = 16
_pool = queue.LifoQueue()
_local = threading.local()


def connect():
    return Conn(psycopg.connect(DSN, autocommit=True, row_factory=_row_factory))


def conn():
    """The connection for the current request/thread, borrowed from a small pool."""
    c = getattr(_local, "c", None)
    if c is None:
        while True:
            try:
                c = _pool.get_nowait()
            except queue.Empty:
                c = connect()
                break
            if not c.raw.closed and c.raw.info.transaction_status == TransactionStatus.IDLE:
                break
            c.raw.close()
        _local.c = c
    return c


def release():
    """Return this thread's connection to the pool (called at the end of each request)."""
    c = getattr(_local, "c", None)
    _local.c = None
    if c is None or c.raw.closed or c.raw.broken:
        return
    if c.raw.info.transaction_status != TransactionStatus.IDLE or _pool.qsize() >= POOL_MAX:
        c.raw.close()
    else:
        _pool.put(c)


@contextmanager
def tx(c=None):
    c = c or conn()
    with c.raw.transaction():
        yield c


def init():
    c = connect()
    c.execute(SCHEMA)
    c.execute("INSERT INTO meta VALUES('schema', '2') ON CONFLICT DO NOTHING")
    c.raw.close()


# --- images -----------------------------------------------------------------

def sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def store_image(c, data):
    """Insert image bytes (deduplicated by content); returns its id, or None if not an image."""
    mime = sniff_image(data)
    if not mime:
        return None
    sha = hashlib.sha256(data).hexdigest()
    iid = new_id()
    row = c.execute("INSERT INTO images VALUES(?,?,?,?,?) ON CONFLICT (sha256) DO UPDATE SET sha256=EXCLUDED.sha256 "
                    "RETURNING id", (iid, sha, mime, data, now())).fetchone()
    return row["id"]


def drop_image_if_unused(c, iid):
    if iid and not c.execute(
            "SELECT 1 FROM posts WHERE image_id=? OR thumb_id=? "
            "UNION ALL SELECT 1 FROM users WHERE avatar_id=? LIMIT 1", (iid, iid, iid)).fetchone():
        c.execute("DELETE FROM images WHERE id=?", (iid,))


# --- moving data between instances -------------------------------------------

def _columns(src, table):
    return [r[1] for r in src.execute("PRAGMA table_info(%s)" % table)]


def export(out_path):
    """Write the whole board (minus logins) to one portable SQLite file."""
    try:
        _export(out_path)
    except BaseException:
        if os.path.exists(out_path):
            os.remove(out_path)  # don't leave a half-written snapshot behind
        raise


def _export(out_path):
    out = sqlite3.connect(out_path)
    out.executescript(SNAPSHOT_SCHEMA)
    c = connect()
    out.executemany("INSERT INTO meta VALUES(?,?)",
                    [tuple(r) for r in c.execute("SELECT key, value FROM meta")])
    for table in SNAPSHOT_TABLES:
        cols = _columns(out, table)
        sql = "INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" * len(cols)))
        # Server-side cursor so a large image table streams instead of loading at once.
        with c.raw.transaction(), c.raw.cursor(name="export_" + table) as cur:
            cur.itersize = 200
            cur.execute("SELECT %s FROM %s" % (",".join(cols), table))
            for batch in iter(lambda: cur.fetchmany(200), []):
                out.executemany(sql, [tuple(r) for r in batch])
    out.commit()
    out.execute("VACUUM")
    out.close()
    c.raw.close()


def merge(src_path):
    """Load a snapshot (from `export`, or an older SQLite board.db) into this board.
    Rows already present (same id) are skipped, so merging twice is harmless.
    Same-named communities are combined; same-named different users get renamed.
    Platform settings are copied only where this board hasn't set its own."""
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True)
    src.row_factory = sqlite3.Row
    user_cols = _columns(src, "users")  # older snapshots lack the newer columns
    comm_cols = _columns(src, "communities")
    stats = dict(images=0, users=0, renamed=[], communities=0, posts=0, comments=0, votes=0)
    c = connect()
    exists = lambda table, rid: c.execute("SELECT 1 FROM %s WHERE id=?" % table, (rid,)).fetchone()
    with tx(c):
        img = {}
        for r in src.execute("SELECT id, data FROM images"):
            if exists("images", r["id"]):
                img[r["id"]] = r["id"]
                continue
            data = bytes(r["data"])
            known = c.execute("SELECT id FROM images WHERE sha256=?", (hashlib.sha256(data).hexdigest(),)).fetchone()
            img[r["id"]] = known["id"] if known else store_image(c, data)
            stats["images"] += not known

        for r in src.execute("SELECT * FROM users"):
            if exists("users", r["id"]):
                continue
            name, n = r["name"], 1
            while c.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
                n += 1
                name = "%s_%d" % (r["name"][:20 - len(str(n)) - 1], n)
            if name != r["name"]:
                stats["renamed"].append((r["name"], name))
            c.execute("INSERT INTO users(id, name, prefix, pw_hash, recovery_hash, avatar_id, bio, is_admin, created,"
                      " banned, live_updates, dark_mode, award_karma, karma_adjust) VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
                      (r["id"], name, r["prefix"], r["pw_hash"], r["recovery_hash"], img.get(r["avatar_id"]),
                       r["bio"], r["created"], r["banned"] if "banned" in user_cols else 0,
                       r["live_updates"] if "live_updates" in user_cols else 1,
                       r["dark_mode"] if "dark_mode" in user_cols else 1,
                       r["award_karma"] if "award_karma" in user_cols else 100,
                       r["karma_adjust"] if "karma_adjust" in user_cols else 0))
            stats["users"] += 1

        comms = {}
        for r in src.execute("SELECT * FROM communities"):
            row = c.execute("SELECT id FROM communities WHERE id=? OR name=?", (r["id"], r["name"])).fetchone()
            if row:
                comms[r["id"]] = row["id"]
                continue
            c.execute("INSERT INTO communities(id, name, prefix, title, description, owner_id, created, color)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (r["id"], r["name"], r["prefix"], r["title"], r["description"], r["owner_id"], r["created"],
                       r["color"] if "color" in comm_cols else ""))
            comms[r["id"]] = r["id"]
            stats["communities"] += 1

        for r in src.execute("SELECT * FROM posts"):
            if exists("posts", r["id"]):
                continue
            c.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,0,0,0,?,?)",
                      (r["id"], comms[r["community_id"]], r["author_id"], r["kind"], r["title"], r["body"],
                       img.get(r["image_id"]), img.get(r["thumb_id"]), r["deleted"], r["created"]))
            stats["posts"] += 1

        # Insert without parents first, then link them, so reply order never trips the foreign key.
        parents = []
        for r in src.execute("SELECT * FROM comments"):
            if exists("comments", r["id"]):
                continue
            c.execute("INSERT INTO comments VALUES(?,?,NULL,?,?,0,?,?)",
                      (r["id"], r["post_id"], r["author_id"], r["body"], r["deleted"], r["created"]))
            if r["parent_id"]:
                parents.append((r["parent_id"], r["id"]))
            stats["comments"] += 1
        c.executemany("UPDATE comments SET parent_id=? WHERE id=?", parents)

        before = c.execute("SELECT count(*) FROM votes").fetchone()[0]
        c.executemany("INSERT INTO votes VALUES(?,?,?) ON CONFLICT DO NOTHING",
                      [tuple(r) for r in src.execute("SELECT user_id, target_id, value FROM votes")])
        stats["votes"] = c.execute("SELECT count(*) FROM votes").fetchone()[0] - before

        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "awards_given" in tables:  # older snapshots predate awards
            c.executemany("INSERT INTO awards_given VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [tuple(r) for r in src.execute("SELECT * FROM awards_given")])
            c.executemany("INSERT INTO upvote_payouts VALUES(?,?) ON CONFLICT DO NOTHING",
                          [tuple(r) for r in src.execute("SELECT voter_id, target_id FROM upvote_payouts")])

        c.executemany("INSERT INTO meta VALUES(?,?) ON CONFLICT DO NOTHING",
                      [tuple(r) for r in src.execute("SELECT key, value FROM meta WHERE key LIKE 'set.%'")])

        # Recompute denormalized counters from scratch.
        c.execute("UPDATE posts SET score=(SELECT coalesce(sum(value),0) FROM votes WHERE target_id=posts.id),"
                  " comment_count=(SELECT count(*) FROM comments WHERE post_id=posts.id)")
        c.execute("UPDATE comments SET score=(SELECT coalesce(sum(value),0) FROM votes WHERE target_id=comments.id)")
        c.executemany("UPDATE posts SET hot=? WHERE id=?",
                      [(hot(r["score"], r["created"]), r["id"]) for r in c.execute("SELECT id, score, created FROM posts")])
    src.close()
    c.raw.close()
    return stats
