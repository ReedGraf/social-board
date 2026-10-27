#!/usr/bin/env python3
"""social board - a small old-reddit-style forum on PostgreSQL. Needs Python 3.10+, psycopg and Pillow.

  python3 board.py [--db URL] serve [--host H] [--port P] [--avatars DIR] [--name NAME]
  python3 board.py [--db URL] export OUT.db     snapshot this board into one portable file
  python3 board.py [--db URL] merge OTHER.db    load a snapshot (or an old SQLite board.db) into this board
  python3 board.py [--db URL] admin USERNAME    toggle admin rights
  python3 board.py [--db URL] ban USERNAME      toggle a ban
  python3 board.py [--db URL] settings show | set KEY=VALUE ...   platform settings (same rules as /admin)
  python3 board.py [--db URL] create-user NAME [--admin] [--password-stdin]   prints the recovery phrase

The database URL comes from --db, else $BOARD_DB, else the file data/database_url,
else postgresql:///social_board.
"""
import argparse
import errno
import json
import os
import re
import secrets
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import psycopg  # noqa: F401
    import PIL  # noqa: F401
except ImportError:
    venv = os.path.join(HERE, ".venv")
    venv_python = os.path.join(venv, "bin", "python")
    if os.path.exists(venv_python) and os.path.realpath(sys.prefix) != os.path.realpath(venv):
        os.execv(venv_python, [venv_python] + sys.argv)  # rerun inside the project's virtualenv
    sys.exit("psycopg/Pillow aren't installed. Run:\n  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")

from sb import db, web  # noqa: E402

DEFAULT_DB = "postgresql:///social_board"


def instance_id(data):
    """A per-instance id, used for cookie names and to bind sessions.
    It's tied to this host + folder, so a copied data folder gets its own identity."""
    path = os.path.join(data, "instance.json")
    here = {"host": socket.gethostname(), "path": os.path.abspath(data)}
    try:
        with open(path) as f:
            saved = json.load(f)
        if saved.get("host") == here["host"] and saved.get("path") == here["path"]:
            return saved["id"]
        print("data folder was copied or moved: giving this instance a new identity")
    except (OSError, ValueError, KeyError):
        pass
    here["id"] = secrets.token_hex(8)
    with open(path, "w") as f:
        json.dump(here, f)
    return here["id"]


def read_db_file(data):
    try:
        with open(os.path.join(data, "database_url")) as f:
            return f.read().strip()
    except OSError:
        return None


def convert_images(args):
    """One-time (and then no-op) conversion of stored images to compressed WebP."""
    from sb import media
    for folder, max_side in ((args.ads, media.AD_MAX), (args.avatars, media.AVATAR_MAX)):
        for old, new, before, after in media.convert_folder(folder, max_side):
            print("  %s -> %s (%d KB -> %d KB; original kept in .originals/)" % (old, new, before // 1024, after // 1024))
    c = db.connect()
    st = media.convert_database(c)
    c.raw.close()
    if st["converted"]:
        print("  converted %d stored images to WebP: %d KB -> %d KB" % (st["converted"], st["before"] // 1024, st["after"] // 1024))
    if st["failed"]:
        print("  %d stored images couldn't be read and were left as they are" % st["failed"])


def safe_dsn(dsn):
    """The database URL with any password masked, for printing."""
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", dsn)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets sent; just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def serve(args):
    sys.stdout.reconfigure(line_buffering=True)
    os.makedirs(args.data, exist_ok=True)
    db.init()
    iid = instance_id(args.data)
    c = db.connect()
    c.execute("DELETE FROM sessions WHERE expires<?", (db.now(),))
    c.raw.close()
    os.makedirs(args.ads, exist_ok=True)
    web.CONF.update(instance=iid, avatars_dir=os.path.abspath(args.avatars), ads_dir=os.path.abspath(args.ads),
                    trust_proxy=args.trust_proxy, secure_cookies=args.secure_cookies)
    from sb import settings
    if args.name:  # --name only sets the default; an admin's choice in /admin wins
        settings.DEFAULTS["site_name"] = args.name
    web.load_static(os.path.join(HERE, "static"))
    convert_images(args)
    from sb import god, live, views  # noqa: F401 - these register routes
    live.start()  # LISTEN for new posts so open front pages update in real time

    # If the port is taken (another board, or anything else) walk up to the next free one,
    # unless the user asked for a specific port.
    port = args.port or 8080
    for attempt in range(1 if args.port else 50):
        try:
            server = web.Server((args.host, port + attempt), web.Handler)
            break
        except OSError as ex:
            if ex.errno != errno.EADDRINUSE or args.port:
                sys.exit("can't listen on %s:%d: %s" % (args.host, port + attempt, ex.strerror))
    else:
        sys.exit("no free port found from %d" % port)
    port = server.server_address[1]
    print("%s (instance %s) database=%s" % (settings.get("site_name"), iid, safe_dsn(args.db)))
    settings.reset()
    db.release()
    print("  local:  http://127.0.0.1:%d/" % port)
    if args.host in ("0.0.0.0", "") and lan_ip():
        print("  LAN:    http://%s:%d/" % (lan_ip(), port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


BOOL_SETTINGS = ("custom_user_prefix", "custom_community_prefix", "open_registration")


def settings_cmd(args):
    from sb import settings, views  # noqa: F401 - views provides the validation
    current = settings.all()
    if args.action == "show":
        for k in sorted(current):
            print("%s=%s" % (k, current[k]))
        return
    new = dict(current)
    for pair in args.pairs:
        key, sep, value = pair.partition("=")
        key = key.strip().lower()
        if not sep or key not in settings.DEFAULTS:
            sys.exit("unknown setting %r (known: %s)" % (key, ", ".join(sorted(settings.DEFAULTS))))
        if key in BOOL_SETTINGS:  # accept yes/no/true/false/on/off/1/0
            value = {"yes": "1", "true": "1", "on": "1", "1": "1", "no": "0", "false": "0", "off": "0", "0": "0"}.get(value.lower(), value)
        new[key] = value.strip()
    err = views.check_settings(new)
    if err:
        sys.exit("not saved: " + err)
    settings.save({k: v for k, v in new.items() if v != current[k]})
    changed = sorted(k for k in new if new[k] != current[k])
    print("settings saved%s" % (": " + ", ".join(changed) if changed else " (no changes)"))


def create_user_cmd(args):
    from sb import views
    web.CONF["avatars_dir"] = os.path.abspath(args.avatars)
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        import getpass
        password = getpass.getpass("password for %s: " % args.username)
        if getpass.getpass("again: ") != password:
            sys.exit("passwords don't match")
    if db.conn().execute("SELECT 1 FROM users WHERE name=?", (args.username,)).fetchone():
        print("user %s already exists" % args.username, file=sys.stderr)
        sys.exit(3)  # distinct code so scripts can tell "exists" from a real failure
    try:
        phrase = views.create_account(args.username, password, args.prefix, args.admin)
    except ValueError as ex:
        sys.exit("not created: %s" % ex)
    print("created %s%s" % (args.username, " (admin)" if args.admin else ""))
    print("recovery phrase: %s" % phrase)


def main():
    ap = argparse.ArgumentParser(description="social board")
    ap.add_argument("--db", help="PostgreSQL URL (default: $BOARD_DB, data/database_url, or %s)" % DEFAULT_DB)
    ap.add_argument("--data", default=os.path.join(HERE, "data"), help="folder for this instance's identity (default ./data)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web server")
    s.add_argument("--host", default="0.0.0.0", help="interface to bind (default 0.0.0.0 = whole LAN)")
    s.add_argument("--port", type=int, default=0, help="port (default: first free one from 8080)")
    s.add_argument("--avatars", default=os.path.join(HERE, "avatars"),
                   help="folder of default avatar images (default ./avatars)")
    s.add_argument("--ads", default=os.path.join(HERE, "ads"),
                   help="folder of advertisement images (default ./ads; admins can manage it at /admin)")
    s.add_argument("--name", help="default site name (admins can change it at /admin)")
    s.add_argument("--trust-proxy", action="store_true",
                   help="behind a reverse proxy on this machine (Caddy): take visitors' addresses from X-Forwarded-For")
    s.add_argument("--secure-cookies", action="store_true", help="served over HTTPS: mark login cookies Secure")
    e = sub.add_parser("export", help="write a portable copy of the database")
    e.add_argument("out")
    m = sub.add_parser("merge", help="merge another board's database into this one")
    m.add_argument("other")
    a = sub.add_parser("admin", help="toggle admin for a user (or force it with --on / --off)")
    a.add_argument("username")
    a.add_argument("--on", dest="force", action="store_const", const=1)
    a.add_argument("--off", dest="force", action="store_const", const=0)
    b = sub.add_parser("ban", help="toggle a ban for a user")
    b.add_argument("username")
    st = sub.add_parser("settings", help="show or change platform settings (validated like /admin)")
    st.add_argument("action", choices=("show", "set"))
    st.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    cu = sub.add_parser("create-user", help="create an account and print its recovery phrase")
    cu.add_argument("username")
    cu.add_argument("--admin", action="store_true")
    cu.add_argument("--prefix")
    cu.add_argument("--password-stdin", action="store_true", help="read the password from standard input (else: ask)")
    cu.add_argument("--avatars", default=os.path.join(HERE, "avatars"), help="folder of default avatar images")
    args = ap.parse_args()

    args.db = args.db or os.environ.get("BOARD_DB") or read_db_file(args.data) or DEFAULT_DB
    db.DSN = args.db
    try:
        db.init()
    except psycopg.OperationalError as ex:
        sys.exit("can't connect to PostgreSQL at %s:\n  %s" % (safe_dsn(args.db), str(ex).strip()))
    if args.cmd == "serve":
        return serve(args)
    if args.cmd == "export":
        if os.path.exists(args.out):
            sys.exit("%s already exists" % args.out)
        db.export(args.out)
        print("exported to %s" % args.out)
    elif args.cmd == "merge":
        if not os.path.isfile(args.other):
            sys.exit("no such file: %s" % args.other)
        st = db.merge(os.path.abspath(args.other))
        from sb import media
        c = db.connect()
        media.convert_database(c)  # imported images become WebP, image posts get thumbnails
        c.raw.close()
        print("merged: %(users)d users, %(communities)d communities, %(posts)d posts, "
              "%(comments)d comments, %(votes)d votes, %(images)d images" % st)
        for old, new in st["renamed"]:
            print("  username clash: %s was renamed to %s" % (old, new))
    elif args.cmd == "settings":
        return settings_cmd(args)
    elif args.cmd == "create-user":
        return create_user_cmd(args)
    else:
        col = "is_admin" if args.cmd == "admin" else "banned"
        value = "1-%s" % col if getattr(args, "force", None) is None else str(args.force)
        c = db.connect()
        r = c.execute("UPDATE users SET %s=%s WHERE name=? RETURNING name, %s" % (col, value, col),
                      (args.username,)).fetchone()
        if not r:
            sys.exit("no such user")
        if col == "banned" and r[1]:
            c.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE name=?)", (args.username,))
        print("%s is %s %s" % (r[0], "now" if r[1] else "no longer", "an admin" if col == "is_admin" else "banned"))


if __name__ == "__main__":
    main()
