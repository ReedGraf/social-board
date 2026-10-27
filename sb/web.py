"""Minimal HTTP layer on top of http.server: routing, forms, cookies, sessions."""
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import traceback
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import db, settings
from .security import token_hash

MAX_BODY = 12 * 1024 * 1024
MAX_ADMIN_UPLOAD = 130 * 1024 * 1024
SESSION_DAYS = 30

# Filled in by board.py at startup.
CONF = {}

ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class HTTPError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message)
        self.status, self.message = status, message


class Response:
    def __init__(self, body=b"", status=200, ctype="text/html; charset=utf-8", headers=None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status, self.ctype = status, ctype
        self.headers = headers or []


class Stream:
    """A long-lived text/event-stream response; `chunks` is an iterator of strings."""
    def __init__(self, chunks):
        self.chunks = chunks


def redirect(url, status=303):
    return Response(b"", status, headers=[("Location", url)])


def json_response(obj):
    return Response(json.dumps(obj), ctype="application/json")


class Request:
    def __init__(self, h):
        self.h = h
        self.method = h.command
        u = urlsplit(h.path)
        self.path = u.path
        self.full_path = h.path
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.form, self.files, self.file_lists = {}, {}, {}
        self.cookies = cookies.SimpleCookie()
        try:
            self.cookies.load(h.headers.get("Cookie", ""))
        except cookies.CookieError:
            pass
        self.ip = h.client_address[0]
        if CONF.get("trust_proxy"):
            # Behind Caddy (on this machine), the visitor's address is the last X-Forwarded-For entry.
            fwd = h.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            self.ip = fwd or self.ip
        self.user = self.session = None
        self.set_cookies = []

    def read_body(self):
        n = int(self.h.headers.get("Content-Length") or 0)
        # Admins uploading ad videos get a bigger allowance; everyone else keeps the normal cap.
        limit = MAX_ADMIN_UPLOAD if self.path == "/admin/images" and self.user and self.user["is_admin"] else MAX_BODY
        if n > limit:
            # We won't read the body, so the connection can't be reused: its leftover bytes
            # would be parsed as the next request.
            self.h.close_connection = True
            raise HTTPError(413, "That upload is too large.")
        body = self.h.rfile.read(n) if n else b""
        ctype = self.h.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            m = re.search(r'boundary="?([^";]+)"?', ctype)
            if not m:
                raise HTTPError(400, "Bad form.")
            self.form, self.files, self.file_lists = parse_multipart(body, m.group(1).encode())
        else:
            self.form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"),
                                                      keep_blank_values=True).items()}

    def get(self, key, default=""):
        return self.form.get(key, default)


def parse_multipart(body, boundary):
    form, files, file_lists = {}, {}, {}
    for part in body.split(b"--" + boundary):
        if not part or part.startswith(b"--"):
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head = head.decode("utf-8", "replace")
        name = re.search(r'\bname="([^"]*)"', head)
        if not name:
            continue
        if re.search(r'\bfilename="', head):
            if data:
                files[name.group(1)] = data
                file_lists.setdefault(name.group(1), []).append(data)  # <input type=file multiple>
        else:
            form[name.group(1)] = data.decode("utf-8", "replace")
    return form, files, file_lists


# --- sessions ---------------------------------------------------------------

def cookie_name():
    # Cookies are shared between ports on one host, so the name is per-instance:
    # two boards on the same machine never clobber each other's logins.
    return "sb_" + CONF["instance"][:12]


def load_session(req):
    morsel = req.cookies.get(cookie_name())
    if not morsel:
        return
    row = db.conn().execute(
        "SELECT s.token_hash, s.csrf, u.*, (SELECT count(*) FROM notifications n WHERE n.user_id=u.id AND n.read=0) AS unread "
        "FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.instance=? AND s.expires>? AND u.banned=0",
        (token_hash(morsel.value), CONF["instance"], db.now())).fetchone()
    if row:
        req.user, req.session = row, row["token_hash"]


def start_session(req, user_id):
    from .security import new_token
    token = new_token()
    db.conn().execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                      (token_hash(token), user_id, CONF["instance"], new_token(),
                       db.now() + SESSION_DAYS * 86400))
    req.set_cookies.append("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s"
                           % (cookie_name(), token, SESSION_DAYS * 86400, secure_flag()))
    req.cookies[cookie_name()] = token
    load_session(req)  # so the rest of this response renders as logged in


def end_session(req):
    if req.session:
        db.conn().execute("DELETE FROM sessions WHERE token_hash=?", (req.session,))
    req.set_cookies.append("%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax%s" % (cookie_name(), secure_flag()))


def secure_flag():
    # Served over HTTPS (behind Caddy): never send the login cookie over plain HTTP.
    return "; Secure" if CONF.get("secure_cookies") else ""


def check_csrf(req):
    origin = req.h.headers.get("Origin")
    if origin and origin != "null" and urlsplit(origin).netloc != req.h.headers.get("Host"):
        raise HTTPError(403, "Cross-site request refused.")
    if req.user and not hmac.compare_digest(req.get("csrf"), req.user["csrf"]):
        raise HTTPError(403, "Your form expired. Go back, reload the page and try again.")


# --- static files -------------------------------------------------------------

STATIC = {}


def load_static(folder):
    for name in os.listdir(folder):
        with open(os.path.join(folder, name), "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        STATIC[name] = (data, gzip.compress(data, 9), ctype, hashlib.sha1(data).hexdigest()[:10])


def static_url(name):
    return "/static/%s?v=%s" % (name, STATIC[name][3])


# --- server -------------------------------------------------------------------

SECURITY_HEADERS = [
    ("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
                                "script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    timeout = 30  # drop stalled/idle connections so they don't pin threads
    server_version = "board"
    sys_version = ""

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        from .views import error_page
        req = Request(self)
        try:
            self.handle_request(req, error_page)
        finally:
            settings.reset()
            db.release()

    def handle_request(self, req, error_page):
        try:
            if req.method == "GET" and req.path.startswith("/static/"):
                return self.send_static(req.path[8:])
            load_session(req)
            if req.method == "POST":
                req.read_body()
                check_csrf(req)
            for method, rx, fn in ROUTES:
                m = rx.match(req.path)
                if m and method == req.method:
                    resp = fn(req, *m.groups())
                    if resp is not None:  # None: "not mine", let a later route have it
                        break
            else:
                raise HTTPError(404, "There's nothing here.")
            if isinstance(resp, Stream):
                # Streams stay open for a long time: give the DB connection back first.
                settings.reset()
                db.release()
                return self.send_stream(resp)
        except HTTPError as e:
            resp = error_page(req, e.status, e.message)
        except Exception:
            traceback.print_exc()
            try:
                resp = error_page(req, 500, "Something broke on our end.")
            except Exception:  # e.g. the database is down: the styled error page needs it too
                resp = Response("500 - something broke on our end", 500, "text/plain; charset=utf-8")
        self.send(req, resp)

    def send(self, req, resp):
        body = resp.body
        gz = (len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
              and resp.ctype.split(";")[0] in ("text/html", "application/json"))
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        self.send_header("Content-Length", str(len(body)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
        if resp.ctype.startswith(("text/html", "application/json")):
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-store" if req.user else "no-cache")
        headers = SECURITY_HEADERS
        nonce = getattr(resp, "style_nonce", None)
        if nonce:  # let this page's community-color <style> block through
            headers = [(k, v.replace("style-src 'self'", "style-src 'self' 'nonce-%s'" % nonce)) for k, v in headers]
        for k, v in headers + resp.headers:
            self.send_header(k, v)
        for c in req.set_cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # browser gave up (navigated away, cancelled media)
            self.close_connection = True

    def send_stream(self, resp):
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # in case a proxy sits in front
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        try:
            for chunk in resp.chunks:
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except OSError:  # browser went away
            pass

    def send_static(self, name):
        if name not in STATIC:
            self.send_error(404)
            return
        data, gz, ctype, _ = STATIC[name]
        use_gz = "gzip" in self.headers.get("Accept-Encoding", "")
        body = gz if use_gz else data
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Vary", "Accept-Encoding")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
