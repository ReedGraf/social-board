"""Page handlers."""
import json
import os
import random
import re
from urllib.parse import quote, unquote, urlsplit

from . import awards, db, live, media, notify, security, settings
from .db import PREFIX_MAX, conn, new_id, now, tx
from .tpl import _bar_cache as community_bar_cache
from .tpl import COLOR_RX, DARK_BG, FORMATTING_HELP, contrast, for_dark
from .tpl import (ago, avatar, cname, comm_link, comm_url, csrf, e, field_error, karma, md, page,
                  plural, uname, user_link, user_url)
from .web import (CONF, HTTPError, Response, Stream, end_session, json_response, redirect, route,
                  start_session)

PER_PAGE = 25
MAX_IMAGE = 8 * 1024 * 1024
MAX_AVATAR = 2 * 1024 * 1024         # a file in the default-avatars folder
MAX_AVATAR_UPLOAD = 8 * 1024 * 1024  # what a user may upload; it's shrunk to 256px WebP anyway

USER_RX = re.compile(r"[A-Za-z0-9_-]{3,20}")
COMM_RX = re.compile(r"[A-Za-z0-9_]{3,21}")
PREFIX_RX = re.compile(r"[A-Za-z0-9_]{1,%d}" % PREFIX_MAX)
# Prefixes that would shadow real two-segment URLs (/p/<id>, /img/<id>, ...).
RESERVED = {"p", "img", "static", "comment", "settings", "admin", "live", "ads"}

auth_limiter = security.RateLimiter(10, 300)    # login / register / recover per IP
write_limiter = security.RateLimiter(30, 60)    # posts / comments / communities per user

SORTS = {"hot": "p.hot DESC", "new": "p.created DESC", "top": "p.score DESC, p.created DESC"}


def error_page(req, status, message):
    titles = {400: "bad request", 403: "forbidden", 404: "page not found", 413: "too large",
              429: "slow down", 500: "server error"}
    body = '<div class="errorpage"><h1>%d &mdash; %s</h1><p>%s</p><p><a href="/">go home</a></p></div>' % (
        status, titles.get(status, "error"), e(message))
    return page(req, titles.get(status, "error"), body, status=status)


def safe_next(n):
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def login_redirect(req):
    target = req.full_path if req.method == "GET" else safe_next(req.get("next", "/"))
    return redirect("/login?next=" + quote(target))


def page_num(req):
    try:
        return max(1, min(int(req.query.get("page", 1)), 10000))
    except ValueError:
        return 1


def valid_prefix(p, kind):
    if not PREFIX_RX.fullmatch(p):
        return "Prefix must be 1-%d letters, numbers or underscores." % PREFIX_MAX
    # Users can't take the communities' default prefix and vice versa, so u/x and r/x stay distinct.
    reserved = RESERVED | {settings.get("community_prefix" if kind == "user" else "user_prefix").lower()}
    if p.lower() in reserved:
        return '"%s" is reserved, pick another prefix.' % p
    return ""


def is_mod(req, community_owner):
    return bool(req.user) and (req.user["is_admin"] or req.user["id"] == community_owner)


def rate_check(limiter, key):
    if not limiter.allow(key):
        raise HTTPError(429, "Too many attempts. Wait a few minutes and try again.")


# --- shared fragments -----------------------------------------------------------

def vote_box(req, target_id, score, myvote, cls="midcol"):
    state = {1: " likes", -1: " dislikes"}.get(myvote, "")
    return f"""<form class="vote {cls}{state}" method="post" action="/vote">{csrf(req)}
<input type="hidden" name="id" value="{e(target_id)}"><input type="hidden" name="next" value="{e(req.full_path)}">
<button class="arrow up" name="dir" value="1" title="upvote" aria-label="upvote" aria-pressed="{'true' if myvote == 1 else 'false'}">&#9650;</button>
<div class="score" aria-label="score">{score}</div>
<button class="arrow down" name="dir" value="-1" title="downvote" aria-label="downvote" aria-pressed="{'true' if myvote == -1 else 'false'}">&#9660;</button></form>"""


def thumb(p):
    # Thumbnails link to the same place as the title, so keep them out of the tab order.
    href = "/p/%s" % e(p["id"])
    skip = 'tabindex="-1" aria-hidden="true"'
    if p["kind"] == "text":
        return '<a class="thumbnail self" href="%s" %s></a>' % (href, skip)
    return '<a class="thumbnail" href="%s" %s><img src="/img/%s" width="70" height="70" alt="" loading="lazy"></a>' % (
        href, skip, e(p["thumb_id"]))


def award_link(req, target_id, author_id):
    """"give award" for logged-in users, on anything that isn't their own."""
    if not req.user or not author_id or req.user["id"] == author_id:
        return ""
    return '<li><a href="/award/%s?next=%s">give award</a></li>' % (e(target_id), e(quote(req.full_path)))


def post_row(req, p, rank=None, show_comm=True, awarded=None):
    domain = "self.%s" % e(p["cname"]) if p["kind"] == "text" else "image"
    comm = " to " + comm_link(p["cname"], p["cprefix"], color=p["ccolor"]) if show_comm else ""
    buttons = '<li><a class="comments" href="/p/%s">%s</a></li>' % (
        e(p["id"]), plural(p["comment_count"], "comment") if p["comment_count"] else "comment") + award_link(req, p["id"], p["author_id"])
    if req.user and (req.user["id"] == p["author_id"] or is_mod(req, p["owner_id"])):
        buttons += ('<li><form class="inline confirm" method="post" action="/p/%s/delete">%s'
                    '<button class="linkbtn">delete</button></form></li>') % (e(p["id"]), csrf(req))
    rank_html = '<span class="rank">%d</span>' % rank if rank else ""
    return f"""<div class="thing" id="t_{e(p['id'])}">{rank_html}
{vote_box(req, p['id'], p['score'], p['myvote'])}
{thumb(p)}
<div class="entry">
<p class="title"><a class="title" href="/p/{e(p['id'])}">{e(p['title'])}</a> <span class="domain">({domain})</span></p>
<p class="tagline">submitted <time title="{p['created']}">{ago(p['created'])}</time> by {user_link(p['author'], p['aprefix'], p['aadmin'])}{comm}{awards.badges(awarded)}</p>
<ul class="buttons">{buttons}</ul>
</div></div>"""


POST_SELECT = """SELECT p.*, u.name AS author, u.prefix AS aprefix, u.is_admin AS aadmin, c.name AS cname,
  c.prefix AS cprefix, c.color AS ccolor, c.owner_id, v.value AS myvote
FROM posts p JOIN communities c ON c.id=p.community_id
LEFT JOIN users u ON u.id=p.author_id
LEFT JOIN votes v ON v.target_id=p.id AND v.user_id=?"""


def listing(req, where="", args=(), sort="hot", show_comm=True, empty="There doesn't seem to be anything here."):
    pg = page_num(req)
    uid = req.user["id"] if req.user else ""
    rows = conn().execute(POST_SELECT + " WHERE p.deleted=0 %s ORDER BY %s LIMIT ? OFFSET ?" % (where, SORTS[sort]),
                          (uid, *args, PER_PAGE + 1, (pg - 1) * PER_PAGE)).fetchall()
    start = (pg - 1) * PER_PAGE
    aw = awards.counts([p["id"] for p in rows[:PER_PAGE]])
    out = [post_row(req, p, start + i + 1, show_comm, aw.get(p["id"])) for i, p in enumerate(rows[:PER_PAGE])]
    if not out:
        out.append('<p class="empty">%s</p>' % e(empty))
    out.append(pager(pg, len(rows) > PER_PAGE))
    return '<div class="sitetable">%s</div>' % "\n".join(out)


def pager(pg, more):
    """The "view more: ‹ prev | next ›" links under a listing."""
    nav = (['<a href="?page=%d">&lsaquo; prev</a>' % (pg - 1)] if pg > 1 else []) + \
          (['<a href="?page=%d">next &rsaquo;</a>' % (pg + 1)] if more else [])
    return '<div class="nav-buttons">view more: %s</div>' % " | ".join(nav) if nav else ""


def sort_tabs(base, sort):
    return [(s, base.rstrip("/") + ("/" + s if s != "hot" else "") or "/", s == sort) for s in SORTS]


def create_box(req, cls):
    if settings.get("community_creation") == "admins" and not (req.user and req.user["is_admin"]):
        return ""
    return '<div class="spacer"><a class="%s" href="/communities/new">Create your own community</a></div>' % cls


def mod_box(req, c):
    """A clear "Edit community" button for whoever can edit it (creator or admin)."""
    if not is_mod(req, c["owner_id"]):
        return ""
    return ('<div class="spacer"><a class="morelink small modtools" href="%s/edit">Edit community</a></div>'
            % e(comm_url(c["name"], c["prefix"])))


# --- folder-backed images: advertisements and default avatars -------------------------------------

IMAGE_FOLDERS = {"ads": ("ads_dir", "advertisements", 5 * 1024 * 1024),
                 "avatars": ("avatars_dir", "default avatars", MAX_AVATAR)}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
VIDEO_EXTENSIONS = {"video/mp4": "mp4", "video/webm": "webm", "video/ogg": "ogv"}
MAX_AD_VIDEO = 60 * 1024 * 1024
AD_FILE_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "m4v", "webm", "ogv"}
CHUNK = 2 * 1024 * 1024  # largest slice of a video sent per range request


def sniff_video(head):
    """MIME type of a video browsers can play inline, from its first bytes; None otherwise."""
    if head[4:8] == b"ftyp":
        # MP4 family; QuickTime (.mov, brand "qt  ") isn't reliably playable, so it's left out.
        return None if head[8:12] == b"qt  " else "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if head.startswith(b"OggS"):
        return "video/ogg"
    return None


def sniff_media(head):
    return db.sniff_image(head) or sniff_video(head)


def folder_images(kind):
    """Names of the image files in an admin-managed folder."""
    folder = CONF[IMAGE_FOLDERS[kind][0]]
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(folder, n))]


def is_video_name(name):
    return name.rsplit(".", 1)[-1].lower() in ("mp4", "m4v", "webm", "ogv")


def ad_media(src, name, cls=""):
    """An ad as an <img>, or for videos a muted, looping, autoplaying <video> with no controls."""
    if is_video_name(name):
        # "muted" here is only the no-JavaScript fallback (browsers autoplay muted video only);
        # app.js turns the sound on as soon as the browser allows. playsinline: no iPhone fullscreen.
        return ('<video%s src="%s" width="300" height="300" autoplay muted loop playsinline preload="metadata" '
                'disablepictureinpicture aria-label="advertisement"></video>' % (cls, e(src)))
    return '<img%s src="%s" width="300" height="300" alt="advertisement" loading="lazy">' % (cls, e(src))


# Ad click-through links, kept beside the ad files (a hidden file, so it isn't listed as an ad)
# so they move with the folder: {"file name": "https://..."}.
def ad_links_path():
    return os.path.join(CONF["ads_dir"], ".links.json")


def ad_links():
    try:
        with open(ad_links_path()) as f:
            links = json.load(f)
        return links if isinstance(links, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ad_links(links):
    tmp = ad_links_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(links, f, indent=1, sort_keys=True)
    os.replace(tmp, ad_links_path())  # atomic: a crash mid-write can't leave a broken file


def ad_box(req):
    """A square under the sidebar box showing a random image or video from the ads folder;
    clicking it opens the ad's link (if an admin set one) in a new tab."""
    names = [n for n in folder_images("ads") if n.rsplit(".", 1)[-1].lower() in AD_FILE_TYPES]
    if names:
        name = random.choice(names)
        media_html, url = ad_media("/ads/" + quote(name), name), ad_links().get(name)
        if url:
            host = urlsplit(url).hostname or url
            return ('<div class="spacer adbox"><span class="adlabel">advertisement &middot; %s</span>'
                    '<a class="adlink" href="%s" target="_blank" rel="sponsored noopener noreferrer" title="%s">%s</a></div>'
                    % (e(host), e(url), e(url), media_html))
        return '<div class="spacer adbox"><span class="adlabel">advertisement</span>%s</div>' % media_html
    if req.user and req.user["is_admin"]:  # only admins see the empty slot, with a pointer to fill it
        return ('<div class="spacer adbox"><span class="adlabel">advertisement</span>'
                '<div class="adempty">ad space &mdash; add images under <a href="/admin#images">admin &rsaquo; images</a></div></div>')
    return ""


@route("GET", r"/(ads|default-avatars)/([^/]+)")
def folder_image(req, kind, name):
    kind = "ads" if kind == "ads" else "avatars"
    name = unquote(name)  # files dropped in by hand can have spaces etc. in their names
    if name not in folder_images(kind):  # only names we listed: no path tricks
        raise HTTPError(404, "No such image.")
    path = os.path.join(CONF[IMAGE_FOLDERS[kind][0]], name)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        mime = db.sniff_image(f.read(16)) if kind == "avatars" else sniff_media(f.read(16))
        if not mime:
            raise HTTPError(404, "No such image.")
        headers = [("Cache-Control", "public, max-age=3600"), ("Accept-Ranges", "bytes")]
        # Byte ranges: browsers stream video this way (Safari requires it). Open-ended ranges get
        # at most CHUNK bytes per response, so a big video is never read into memory whole.
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", req.h.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else start + CHUNK - 1
            else:  # "bytes=-N": the last N bytes
                start, end = max(0, size - int(m.group(2))), size - 1
            end = min(end, size - 1, start + CHUNK - 1)
            if start >= size or start > end:
                return Response(b"", 416, ctype=mime, headers=headers + [("Content-Range", "bytes */%d" % size)])
            f.seek(start)
            return Response(f.read(end - start + 1), 206, ctype=mime,
                            headers=headers + [("Content-Range", "bytes %d-%d/%d" % (start, end, size))])
        f.seek(0)
        return Response(f.read(), ctype=mime, headers=headers)


def submit_box(cname=""):
    q = "&r=" + quote(cname) if cname else ""
    return ('<div class="spacer"><a class="morelink" href="/submit?kind=text%s">Submit a new text post</a></div>'
            '<div class="spacer"><a class="morelink" href="/submit?kind=image%s">Submit a new image post</a></div>') % (q, q)


# --- front page & communities ----------------------------------------------------

@route("GET", r"/(hot|new|top)?")
def front(req, sort):
    sort = sort or "hot"
    side = submit_box() + create_box(req, "morelink small") + (
        '<div class="spacer sidecontentbox"><h3>%s</h3>%s'
        '<p><a href="/communities">browse all communities &rsaquo;</a></p></div>' % (
            e(settings.get("site_name")), md(settings.get("tagline")))) + ad_box(req)
    body = listing(req, sort=sort)
    if wants_live(req) and sort == "new" and page_num(req) == 1:  # only the "new" sort updates live
        # app.js opens /live and slides new posts in at the top
        body = body.replace('<div class="sitetable">', '<div class="sitetable" data-live="/live" data-since="%d">' % now(), 1)
    return page(req, settings.get("site_name"), body, side=side, tabs=sort_tabs("/", sort))


def wants_live(req):
    return not req.user or bool(req.user["live_updates"])


@route("GET", r"/live")
def live_stream(req):
    """Server-sent events: one "post" event per new post. Catches up from ?since= first,
    so posts made while the tab was hidden (stream closed) still arrive."""
    if not wants_live(req):
        return Response(b"", 204)  # tells EventSource to stop reconnecting
    try:
        since = int(req.query.get("since", 0))
    except ValueError:
        since = 0
    after = live.current()
    missed = [(r["id"], r["created"]) for r in conn().execute(
        "SELECT id, created FROM posts WHERE created>=? AND deleted=0 ORDER BY created LIMIT 50",
        (max(since, now() - 3600),))]

    def events():
        nonlocal after
        yield "retry: 5000\n\n"
        for pid, created in missed:
            yield "event: post\ndata: %s %d\n\n" % (pid, created)
        while True:
            new, after = live.wait(after, 25)
            if not new:
                yield ": ping\n\n"  # keeps the connection open and notices closed tabs
            for pid, created in new:
                yield "event: post\ndata: %s %d\n\n" % (pid, created)
    return Stream(events())


@route("GET", r"/live/post/([0-9a-z]+)")
def live_post(req, pid):
    """One listing row, rendered for this viewer, for app.js to insert."""
    p = conn().execute(POST_SELECT + " WHERE p.id=? AND p.deleted=0",
                       (req.user["id"] if req.user else "", pid)).fetchone()
    if not p:
        return Response(b"", 204)
    row = post_row(req, p, rank=1, awarded=awards.counts([p["id"]]).get(p["id"]))
    if p["ccolor"] and COLOR_RX.fullmatch(p["ccolor"]):
        # The page's color stylesheet doesn't know this community; tell app.js the right shade.
        dark = not req.user or req.user["dark_mode"]
        row = row.replace('<div class="thing"', '<div data-cc="%s" class="thing"' % (for_dark(p["ccolor"]) if dark else p["ccolor"]), 1)
    return Response(row)


def get_community(name):
    c = conn().execute("SELECT c.*, u.name AS owner, u.prefix AS oprefix, u.is_admin AS oadmin FROM communities c "
                       "LEFT JOIN users u ON u.id=c.owner_id WHERE c.name=?", (name,)).fetchone()
    if not c:
        raise HTTPError(404, "No community by that name. Maybe create it?")
    return c


def community(req, name, sort):
    sort = sort or "hot"
    c = get_community(name)
    n = conn().execute("SELECT count(*) FROM posts WHERE community_id=? AND deleted=0", (c["id"],)).fetchone()[0]
    url = comm_url(c["name"], c["prefix"])
    side = submit_box(c["name"]) + mod_box(req, c) + f"""<div class="spacer sidecontentbox">
<h3>{comm_link(c['name'], c['prefix'], 'title', c['color'])}</h3>
{('<p class="sr-title">' + e(c['title']) + '</p>') if c['title'] else ''}
<p class="stat"><b>{plural(n, 'post')}</b></p>
{md(c['description']) if c['description'] else ''}
<p class="tagline">created {ago(c['created'])} by {user_link(c['owner'], c['oprefix'], c['oadmin'])}</p></div>""" + ad_box(req)
    body = listing(req, "AND p.community_id=?", (c["id"],), sort, show_comm=False,
                   empty="No posts here yet. Be the first!")
    return page(req, cname(c["name"], c["prefix"]), body, side=side,
                pagename=cname(c["name"], c["prefix"]), pagehref=url, tabs=sort_tabs(url, sort))


@route("GET", r"/communities/?")
def communities(req):
    rows = conn().execute(
        "SELECT c.*, (SELECT count(*) FROM posts p WHERE p.community_id=c.id AND p.deleted=0) AS n "
        "FROM communities c ORDER BY n DESC, c.name").fetchall()
    items = "".join(
        f"""<div class="thing comm"><div class="entry"><p class="title">{comm_link(r['name'], r['prefix'], 'title', r['color'])}
{(': ' + e(r['title'])) if r['title'] else ''}</p>
<p class="tagline">{plural(r['n'], 'post')} &middot; created {ago(r['created'])}</p></div></div>"""
        for r in rows) or '<p class="empty">No communities yet.</p>'
    side = create_box(req, "morelink")
    return page(req, "communities", '<h2 class="pagetitle">communities</h2>' + items, side=side)


def community_form(req, action, c=None, err="", f=None):
    f = f or {}
    v = lambda k, d="": e(f.get(k, c[k] if c else d))
    prefix_field = "" if not settings.flag("custom_community_prefix") else f"""<div class="roundfield"><label>prefix</label>
<input name="prefix" value="{v('prefix', settings.get('community_prefix'))}" required maxlength="{PREFIX_MAX}" class="short">
<span class="hint">shown before the name, like <b>{e(settings.get('community_prefix'))}</b>/name. 1-{PREFIX_MAX} letters, numbers or underscores.</span></div>"""
    color = f.get("color", c["color"] if c else "") if f.get("default_color") != "1" else ""
    color_field = f"""<div class="roundfield"><label>name color</label>
<div class="colorrow"><input type="color" name="color" value="{e(color or '#336699')}">
<label class="check"><input type="checkbox" name="default_color" value="1"{'' if color else ' checked'}> use the default link color</label></div>
<span class="hint">how this community's name shows up in listings and on the front page. Very light and very dark colors are refused so the name stays readable in both light and dark mode.</span></div>"""
    name_field = "" if c else f"""<div class="roundfield"><label>name</label>
<input name="name" value="{v('name')}" required pattern="[A-Za-z0-9_]{{3,21}}" maxlength="21">
<span class="hint">3-21 letters, numbers or underscores. Can't be changed later.</span></div>"""
    return f"""<form class="formtabs" method="post" action="{action}">{csrf(req)}{field_error(err)}
{name_field}
{prefix_field}
<div class="roundfield"><label>title</label><input name="title" value="{v('title')}" maxlength="100"></div>
<div class="roundfield"><label>description <span class="hint">(sidebar text)</span></label>
<textarea name="description" rows="6" maxlength="2000">{v('description')}</textarea></div>
{color_field}
<button class="btn">{'save' if c else 'create'}</button></form>"""


def check_can_create(req):
    if settings.get("community_creation") == "admins" and not req.user["is_admin"]:
        raise HTTPError(403, "Only admins can create communities on this board.")


@route("GET", r"/communities/new")
def community_new(req):
    if not req.user:
        return login_redirect(req)
    check_can_create(req)
    return page(req, "create a community", '<h2 class="pagetitle">create a community</h2>' + community_form(req, "/communities/new"))


def community_fields(req, current=None):
    if settings.flag("custom_community_prefix"):
        prefix = req.get("prefix").strip() or current or settings.get("community_prefix")
    else:
        prefix = current or settings.get("community_prefix")
    title, desc = req.get("title").strip(), req.get("description").strip()
    err = valid_prefix(prefix, "community") if prefix != current else ""
    if len(title) > 100 or len(desc) > 2000:
        err = "Title or description is too long."
    color = "" if req.get("default_color") == "1" else req.get("color").strip().lower()
    if color and not COLOR_RX.fullmatch(color):
        err = err or "Pick a color, or use the default."
    elif color and contrast(color, "#ffffff") < 3:
        err = err or "That color is too light to read on the white background. Pick a darker one."
    elif color and contrast(color, DARK_BG) < 2:
        # Dark mode lightens colors a bit, but near-black ones (black, navy, charcoal) would wash out.
        err = err or "That color is too dark to tell apart on the dark background. Pick a lighter one."
    return prefix, title, desc, color, err


@route("POST", r"/communities/new")
def community_create(req):
    if not req.user:
        return login_redirect(req)
    check_can_create(req)
    rate_check(write_limiter, req.user["id"])
    name = req.get("name").strip()
    prefix, title, desc, color, err = community_fields(req)
    if not COMM_RX.fullmatch(name):
        err = "Name must be 3-21 letters, numbers or underscores."
    elif conn().execute("SELECT 1 FROM communities WHERE name=?", (name,)).fetchone():
        err = "That community already exists."
    err = err or address_clash(name, comm_prefix=prefix)
    if err:
        return page(req, "create a community", '<h2 class="pagetitle">create a community</h2>' +
                    community_form(req, "/communities/new", err=err, f=req.form))
    conn().execute("INSERT INTO communities(id, name, prefix, title, description, owner_id, created, color)"
                   " VALUES(?,?,?,?,?,?,?,?)", (new_id(), name, prefix, title, desc, req.user["id"], now(), color))
    community_bar_cache[0] = 0  # show the new community in the top strip right away
    return redirect(comm_url(name, prefix))


def community_edit(req, name):
    c = get_community(name)
    if not is_mod(req, c["owner_id"]):
        raise HTTPError(403, "Only the community's creator can edit it.")
    return page(req, "edit community", '<h2 class="pagetitle">edit %s</h2>' % comm_link(c["name"], c["prefix"], color=c["color"]) +
                community_form(req, comm_url(c["name"], c["prefix"]) + "/edit", c))


def community_save(req, name):
    c = get_community(name)
    if not is_mod(req, c["owner_id"]):
        raise HTTPError(403, "Only the community's creator can edit it.")
    prefix, title, desc, color, err = community_fields(req, c["prefix"])
    err = err or address_clash(c["name"], comm_prefix=prefix)
    if err:
        return page(req, "edit community", community_form(req, comm_url(c["name"], c["prefix"]) + "/edit", c, err, req.form))
    conn().execute("UPDATE communities SET prefix=?, title=?, description=?, color=? WHERE id=?",
                   (prefix, title, desc, color, c["id"]))
    community_bar_cache[0] = 0
    return redirect(comm_url(c["name"], prefix))


# --- submitting ------------------------------------------------------------------

def submit_form(req, kind, cur="", err="", f=None):
    f = f or {}
    comms = conn().execute("SELECT name, prefix FROM communities ORDER BY name").fetchall()
    if not comms:
        return '<p class="empty">There are no communities yet. <a href="/communities/new">Create one</a> first.</p>'
    cur = f.get("community", cur)
    # No default: the community is only filled in when the user came from one (?r=name).
    picked = any(c["name"].lower() == cur.lower() for c in comms)
    opts = '<option value=""%s>choose a community</option>' % ("" if picked else " selected") + "".join(
        '<option value="%s"%s>%s</option>' % (
        e(c["name"]), " selected" if c["name"].lower() == cur.lower() else "", e(cname(c["name"], c["prefix"])))
        for c in comms)
    tabs = "".join('<li%s><a href="/submit?kind=%s&amp;r=%s">%s</a></li>' % (
        ' class="selected"' if k == kind else "", k, quote(cur), label)
        for k, label in (("text", "text"), ("image", "image")))
    if kind == "text":
        fields = f"""<div class="roundfield"><label>title <span class="req">*</span></label>
<textarea name="title" rows="2" maxlength="300" required>{e(f.get('title', ''))}</textarea></div>
<div class="roundfield"><label>text <span class="hint">(optional)</span></label>
<textarea name="body" rows="10" maxlength="40000">{e(f.get('body', ''))}</textarea>
{FORMATTING_HELP}</div>"""
    else:
        fields = f"""<div class="roundfield"><label>image <span class="req">*</span></label>
<input type="file" name="image" accept="image/png,image/jpeg,image/gif,image/webp" required>
<span class="hint">png, jpeg, gif or webp, up to {MAX_IMAGE // 1024 // 1024} MB. It's converted to a compressed WebP.</span></div>
<div class="roundfield"><label>caption <span class="req">*</span></label>
<textarea name="title" rows="2" maxlength="300" required>{e(f.get('title', ''))}</textarea></div>"""
    return f"""<ul class="tabmenu formtab">{tabs}</ul>
<form class="formtabs" method="post" action="/submit" enctype="multipart/form-data">{csrf(req)}{field_error(err)}
<input type="hidden" name="kind" value="{kind}">
{fields}
<div class="roundfield"><label>choose a community <span class="req">*</span></label><select name="community" required>{opts}</select></div>
<button class="btn">submit</button> <span class="hint">ctrl+enter to send</span></form>"""


@route("GET", r"/submit")
def submit(req):
    if not req.user:
        return login_redirect(req)
    kind = "image" if req.query.get("kind") == "image" else "text"
    return page(req, "submit", '<h2 class="pagetitle">submit to %s</h2>' % e(settings.get("site_name")) +
                submit_form(req, kind, req.query.get("r", "")))


@route("POST", r"/submit")
def submit_post(req):
    if not req.user:
        return login_redirect(req)
    rate_check(write_limiter, req.user["id"])
    kind = "image" if req.get("kind") == "image" else "text"
    title, body = req.get("title").strip(), req.get("body").strip()
    c = conn().execute("SELECT id, name FROM communities WHERE name=?", (req.get("community"),)).fetchone()
    image = req.files.get("image")
    err = ""
    if not c:
        err = "Pick a community."
    elif not title or len(title) > 300:
        err = "A %s is required (300 characters max)." % ("caption" if kind == "image" else "title")
    elif len(body) > 40000:
        err = "Text is too long (40000 characters max)."
    elif kind == "image" and not image:
        err = "Choose an image to upload."
    elif kind == "image" and (len(image) > MAX_IMAGE or not db.sniff_image(image)):
        err = "That file isn't a supported image, or it's over %d MB." % (MAX_IMAGE // 1024 // 1024)
    elif kind == "image":
        # Everything is stored as compressed WebP; encode before the transaction so it isn't held open.
        image = media.optimize(image, media.POST_MAX)
        if image is None:
            err = "That image couldn't be read. Try saving it again as a PNG or JPEG."
        else:
            thumb = media.thumbnail(image)  # None for animations: they're their own (moving) thumbnail
    if err:
        return page(req, "submit", '<h2 class="pagetitle">submit</h2>' + submit_form(req, kind, err=err, f=req.form))
    pid, t = new_id(), now()
    with tx() as cx:
        image_id = thumb_id = None
        if kind == "image":
            image_id = db.store_image(cx, image)
            thumb_id = db.store_image(cx, thumb) if thumb else image_id
            body = ""
        cx.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,?,?,1,?,0,0,?)",
                   (pid, c["id"], req.user["id"], kind, title, body, image_id, thumb_id, db.hot(1, t), t))
        cx.execute("INSERT INTO votes VALUES(?,?,1)", (req.user["id"], pid))
        notify.mentions(cx, title + "\n" + body, req.user["id"], pid, pid)
        live.notify(cx, pid, t)
    return redirect("/p/" + pid)


# --- post page, comments, votes ----------------------------------------------------

def get_post(req, pid):
    p = conn().execute(POST_SELECT + " WHERE p.id=?", (req.user["id"] if req.user else "", pid)).fetchone()
    if not p:
        raise HTTPError(404, "That post doesn't exist.")
    return p


def comment_html(req, c, kids, post, aw, depth=0):
    deleted = c["deleted"]
    who = '<span class="author gone">[deleted]</span>' if deleted else user_link(c["author"], c["aprefix"], c["aadmin"])
    if c["author_id"] and c["author_id"] == post["author_id"] and not deleted:
        who = who.replace('class="author"', 'class="author submitter" title="submitter"')
    buttons = '<li><a href="#c_%s">permalink</a></li>' % e(c["id"])
    if not deleted:
        buttons += award_link(req, c["id"], c["author_id"])
    if req.user and not deleted:
        buttons += f"""<li><details class="reply"><summary>reply</summary>
<form method="post" action="/p/{e(post['id'])}/comment">{csrf(req)}<input type="hidden" name="parent" value="{e(c['id'])}">
<textarea name="body" rows="4" maxlength="10000" required></textarea><button class="btn">save</button></form></details></li>"""
        if req.user["id"] == c["author_id"] or is_mod(req, post["owner_id"]):
            buttons += ('<li><form class="inline confirm" method="post" action="/comment/%s/delete">%s'
                        '<button class="linkbtn">delete</button></form></li>') % (e(c["id"]), csrf(req))
    children = "".join(comment_html(req, k, kids, post, aw, depth + 1) for k in kids.get(c["id"], ()))
    avatar_html = "" if deleted else avatar(c["avatar_id"], 20, "cavatar")
    body = '<div class="md"><p class="gone">[deleted]</p></div>' if deleted else md(c["body"])
    return f"""<div class="comment{' odd' if depth % 2 else ''}" id="c_{e(c['id'])}"><details open>
<summary class="tagline"><span class="expand"></span> {avatar_html}{who} <span class="score">{plural(c['score'], 'point')}</span> <time>{ago(c['created'])}</time>{awards.badges(aw.get(c['id']))}</summary>
<div class="cbody">{'' if deleted else vote_box(req, c['id'], c['score'], c['myvote'], 'cvote')}
<div class="centry">{body}<ul class="buttons">{buttons}</ul></div></div>
<div class="child">{children}</div></details></div>"""


@route("GET", r"/p/([0-9a-z]+)/?")
def post_page(req, pid):
    p = get_post(req, pid)
    if p["kind"] == "image" and p["image_id"]:
        content = ('<div class="media"><a href="/img/%s"><img src="/img/%s" alt="%s"></a>'
                   '<a class="btn download" href="/img/%s?download=%s" download>Download image</a></div>') % (
            e(p["image_id"]), e(p["image_id"]), e(p["title"]), e(p["image_id"]), quote(download_name(p["title"])))
    elif p["body"]:
        content = '<div class="usertext">%s</div>' % md(p["body"])
    else:
        content = ""
    rows = conn().execute(
        "SELECT c.*, u.name AS author, u.prefix AS aprefix, u.is_admin AS aadmin, u.avatar_id, v.value AS myvote FROM comments c "
        "LEFT JOIN users u ON u.id=c.author_id LEFT JOIN votes v ON v.target_id=c.id AND v.user_id=? "
        "WHERE c.post_id=? ORDER BY c.score DESC, c.created",
        (req.user["id"] if req.user else "", pid)).fetchall()
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    aw = awards.counts([p["id"]] + [r["id"] for r in rows])
    tree = "".join(comment_html(req, c, kids, p, aw) for c in kids.get(None, ()))
    if p["deleted"]:
        form = ""
    elif req.user:
        form = f"""<form class="commentform" method="post" action="/p/{e(pid)}/comment">{csrf(req)}
<textarea name="body" rows="5" maxlength="10000" required></textarea>{FORMATTING_HELP}<button class="btn">save</button>
<span class="hint">ctrl+enter to send</span></form>"""
    else:
        form = '<p class="loginprompt"><a href="/login?next=/p/%s">log in</a> or <a href="/register">sign up</a> to comment.</p>' % e(pid)
    head = ('<div class="thing deleted"><p class="title gone">[deleted]</p></div>' if p["deleted"]
            else post_row(req, p, awarded=aw.get(p["id"])))
    c = get_community(p["cname"])
    side = submit_box(c["name"]) + mod_box(req, c) + f"""<div class="spacer sidecontentbox"><h3>{comm_link(c['name'], c['prefix'], 'title', c['color'])}</h3>
{('<p class="sr-title">' + e(c['title']) + '</p>') if c['title'] else ''}{md(c['description']) if c['description'] else ''}</div>""" + ad_box(req)
    body = f"""{head}{content}
<div class="commentarea"><div class="panestack-title"><span class="title">{plural(p['comment_count'], 'comment')}</span></div>
{form}{tree or '<p class="empty">no comments yet</p>'}</div>"""
    return page(req, p["title"], body, side=side, pagename=cname(c["name"], c["prefix"]),
                pagehref=comm_url(c["name"], c["prefix"]))


@route("POST", r"/p/([0-9a-z]+)/comment")
def comment_post(req, pid):
    if not req.user:
        return login_redirect(req)
    rate_check(write_limiter, req.user["id"])
    body, parent = req.get("body").strip(), req.get("parent") or None
    p = get_post(req, pid)
    if p["deleted"]:
        raise HTTPError(403, "That post was deleted.")
    if not body or len(body) > 10000:
        raise HTTPError(400, "Comments must be 1-10000 characters.")
    if parent:
        replied = conn().execute("SELECT author_id FROM comments WHERE id=? AND post_id=?", (parent, pid)).fetchone()
        if not replied:
            raise HTTPError(400, "The comment you replied to doesn't exist.")
    to, kind = (replied["author_id"], "comment_reply") if parent else (p["author_id"], "post_reply")
    cid, uid = new_id(), req.user["id"]
    with tx() as cx:
        cx.execute("INSERT INTO comments VALUES(?,?,?,?,?,1,0,?)", (cid, pid, parent, uid, body, now()))
        cx.execute("INSERT INTO votes VALUES(?,?,1)", (uid, cid))
        cx.execute("UPDATE posts SET comment_count=comment_count+1 WHERE id=?", (pid,))
        notify.add(cx, to, kind, uid, cid, pid)
        notify.mentions(cx, body, uid, cid, pid, skip={to})  # a reply already told them
    return redirect("/p/%s#c_%s" % (pid, cid))


@route("POST", r"/p/([0-9a-z]+)/delete")
def post_delete(req, pid):
    p = get_post(req, pid)
    if not req.user or not (req.user["id"] == p["author_id"] or is_mod(req, p["owner_id"])):
        raise HTTPError(403, "You can't delete that.")
    with tx() as cx:
        cx.execute("UPDATE posts SET deleted=1, title='[deleted]', body='', image_id=NULL, thumb_id=NULL WHERE id=?", (pid,))
        db.drop_image_if_unused(cx, p["image_id"])
        db.drop_image_if_unused(cx, p["thumb_id"])
    return redirect(comm_url(p["cname"], p["cprefix"]))


@route("POST", r"/comment/([0-9a-z]+)/delete")
def comment_delete(req, cid):
    c = conn().execute("SELECT c.*, co.owner_id FROM comments c JOIN posts p ON p.id=c.post_id "
                       "JOIN communities co ON co.id=p.community_id WHERE c.id=?", (cid,)).fetchone()
    if not c:
        raise HTTPError(404, "That comment doesn't exist.")
    if not req.user or not (req.user["id"] == c["author_id"] or is_mod(req, c["owner_id"])):
        raise HTTPError(403, "You can't delete that.")
    conn().execute("UPDATE comments SET deleted=1, body='' WHERE id=?", (cid,))
    return redirect("/p/%s#c_%s" % (c["post_id"], cid))


@route("POST", r"/vote")
def vote(req):
    if not req.user:
        return login_redirect(req) if "fetch" not in req.h.headers.get("X-Requested-With", "") \
            else json_response({"login": True})
    tid, uid = req.get("id"), req.user["id"]
    try:
        want = {"1": 1, "-1": -1}[req.get("dir")]
    except KeyError:
        raise HTTPError(400, "Bad vote.")
    with tx() as cx:
        # Lock the voted-on row so concurrent votes on it apply one at a time.
        p = (cx.execute("SELECT 'posts' AS t, score, created, author_id FROM posts WHERE id=? AND deleted=0 FOR UPDATE", (tid,)).fetchone()
             or cx.execute("SELECT 'comments' AS t, score, created, author_id FROM comments WHERE id=? AND deleted=0 FOR UPDATE",
                           (tid,)).fetchone())
        if not p:
            raise HTTPError(404, "That's gone.")
        old = cx.execute("SELECT value FROM votes WHERE user_id=? AND target_id=?", (uid, tid)).fetchone()
        old = old[0] if old else 0
        new = 0 if old == want else want  # clicking the same arrow again clears the vote
        if new == 1:
            awards.on_upvote(cx, uid, tid, p["author_id"])  # +1 award karma for the author, once per voter
        if new:
            cx.execute("INSERT INTO votes VALUES(?,?,?) ON CONFLICT (user_id, target_id) DO UPDATE SET value=EXCLUDED.value",
                       (uid, tid, new))
        else:
            cx.execute("DELETE FROM votes WHERE user_id=? AND target_id=?", (uid, tid))
        score = p["score"] + new - old
        if p["t"] == "posts":
            cx.execute("UPDATE posts SET score=?, hot=? WHERE id=?", (score, db.hot(score, p["created"]), tid))
        else:
            cx.execute("UPDATE comments SET score=? WHERE id=?", (score, tid))
    if "fetch" in req.h.headers.get("X-Requested-With", ""):
        return json_response({"score": score, "vote": new})
    return redirect(safe_next(req.get("next")))


# --- images -----------------------------------------------------------------------------

def download_name(caption):
    """A safe file name (no extension) from a post's caption, e.g. "Social night 2026!" -> "Social-night-2026"."""
    return re.sub(r"[^A-Za-z0-9]+", "-", caption).strip("-")[:60] or "image"


@route("GET", r"/img/([0-9a-z]+)")
def image(req, iid):
    # Image ids never change content, so they can be cached forever.
    if req.h.headers.get("If-None-Match") == '"%s"' % iid:
        return Response(b"", 304, ctype="application/octet-stream", headers=[("ETag", '"%s"' % iid)])
    r = conn().execute("SELECT mime, data FROM images WHERE id=?", (iid,)).fetchone()
    if not r:
        raise HTTPError(404, "No such image.")
    headers = [("Cache-Control", "public, max-age=31536000, immutable"), ("ETag", '"%s"' % iid)]
    if "download" in req.query:  # the post page's "Download image" button
        name = download_name(req.query["download"]) + "." + EXTENSIONS[r["mime"]]
        headers.append(("Content-Disposition", 'attachment; filename="%s"' % name))
    return Response(r["data"], ctype=r["mime"], headers=headers)


# --- users ---------------------------------------------------------------------------------

def get_user(name):
    u = conn().execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if not u:
        raise HTTPError(404, "No user by that name.")
    return u


def profile(req, name, show):
    u = get_user(name)
    pk, ck, adj = karma(u["id"])
    base = user_url(u["name"], u["prefix"])
    tabs = [("submitted", base, not show), ("comments", base + "/comments", bool(show))]
    if show:
        pg = page_num(req)
        rows = conn().execute(
            "SELECT c.*, p.title, p.id AS pid, co.name AS cname, co.prefix AS cprefix, co.color AS ccolor FROM comments c "
            "JOIN posts p ON p.id=c.post_id JOIN communities co ON co.id=p.community_id "
            "WHERE c.author_id=? AND c.deleted=0 ORDER BY c.created DESC LIMIT ? OFFSET ?",
            (u["id"], PER_PAGE + 1, (pg - 1) * PER_PAGE)).fetchall()
        items = "".join(f"""<div class="thing ucomment"><p class="parent"><a class="title" href="/p/{e(r['pid'])}">{e(r['title'])}</a>
in {comm_link(r['cname'], r['cprefix'], color=r['ccolor'])}</p>
<p class="tagline">{user_link(u['name'], u['prefix'], u['is_admin'])} {plural(r['score'], 'point')} {ago(r['created'])}</p>
{md(r['body'])}<ul class="buttons"><li><a href="/p/{e(r['pid'])}#c_{e(r['id'])}">context</a></li></ul></div>"""
                        for r in rows[:PER_PAGE]) or '<p class="empty">No comments yet.</p>'
        body = '<div class="sitetable">%s%s</div>' % (items, pager(pg, len(rows) > PER_PAGE))
    else:
        body = listing(req, "AND p.author_id=?", (u["id"],), "new", empty="Nothing posted yet.")
    mine = req.user and req.user["id"] == u["id"]
    side = f"""<div class="spacer sidecontentbox profile">{avatar(u['avatar_id'], 128, 'bigavatar')}
<h1{' class="admin" title="admin"' if u['is_admin'] else ''}>{e(uname(u['name'], u['prefix']))}</h1>
<p class="karma"><b>{pk}</b> post karma</p><p class="karma"><b>{ck}</b> comment karma</p>{('<p class="karma"><b>%+d</b> bonus karma</p>' % adj) if adj else ''}
{md(u['bio']) if u['bio'] else ''}
{trophy_case(u, mine)}
<p class="tagline">joined {ago(u['created'])}{' &middot; admin' if u['is_admin'] else ''}{' &middot; <b class="banned">banned</b>' if u['banned'] else ''}</p>
{'<p><a href="/settings">edit profile</a></p>' if mine else ''}</div>{admin_tools(req, u)}"""
    return page(req, uname(u["name"], u["prefix"]), body, side=side,
                pagename=uname(u["name"], u["prefix"]), pagehref=base, tabs=tabs)


# --- moderation: banning and deleting users (admins only) ---------------------------------------

def admin_tools(req, u):
    if not (req.user and req.user["is_admin"]) or req.user["id"] == u["id"]:
        return ""
    if u["is_admin"]:
        return ('<div class="spacer sidecontentbox admintools"><h3>admin tools</h3><p class="hint">This user is an admin. '
                '<a href="/admin">Remove their admin rights</a> before banning or deleting them.</p></div>')
    url = e(user_url(u["name"], u["prefix"]))
    purge = ('<label class="check"><input type="checkbox" name="purge" value="1"> '
             'also delete all their posts and comments</label>')
    if u["banned"]:
        ban = f"""<form method="post" action="{url}/ban">{csrf(req)}<input type="hidden" name="action" value="unban">
<button class="btn">unban</button></form>"""
    else:
        ban = f"""<form class="confirm" data-confirm="Ban this user?" method="post" action="{url}/ban">{csrf(req)}<input type="hidden" name="action" value="ban">
<p class="hint">Banned users are logged out and can't log in, post, comment or vote. The name stays taken.</p>
{purge}<button class="btn">ban user</button></form>"""
    return f"""<div class="spacer sidecontentbox admintools"><h3>admin tools</h3>{ban}
<form class="confirm" data-confirm="Delete this account? It can't be undone." method="post" action="{url}/delete">{csrf(req)}
<p class="hint">Deleting removes the account; their posts stay up as [deleted] unless you remove them too. The name becomes free again.</p>
{purge}<button class="btn danger">delete account</button></form></div>"""


def moderation_target(req, name):
    require_admin(req)
    u = get_user(name)
    if u["id"] == req.user["id"]:
        raise HTTPError(400, "You can't do that to your own account.")
    if u["is_admin"]:
        raise HTTPError(400, "Remove their admin rights first.")
    return u


def purge_content(cx, uid):
    """Soft-delete everything a user posted, the same way an author's own delete does."""
    images = cx.execute("SELECT image_id, thumb_id FROM posts WHERE author_id=? AND deleted=0", (uid,)).fetchall()
    cx.execute("UPDATE posts SET deleted=1, title='[deleted]', body='', image_id=NULL, thumb_id=NULL WHERE author_id=?", (uid,))
    cx.execute("UPDATE comments SET deleted=1, body='' WHERE author_id=?", (uid,))
    for r in images:
        db.drop_image_if_unused(cx, r["image_id"])
        db.drop_image_if_unused(cx, r["thumb_id"])


def user_ban(req, name):
    u = moderation_target(req, name)
    set_banned(u, req.get("action") == "ban", req.get("purge") == "1")
    return redirect(user_url(u["name"], u["prefix"]))


def set_banned(u, banned, purge=False):
    with tx() as cx:
        cx.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), u["id"]))
        if banned:
            cx.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
            if purge:
                purge_content(cx, u["id"])


def user_delete(req, name):
    delete_account(moderation_target(req, name), req.get("purge") == "1")
    return redirect("/admin?msg=deleted")


def delete_account(u, purge=False):
    uid = u["id"]
    with tx() as cx:
        if purge:
            purge_content(cx, uid)
        # Their votes go with the account, so rescore whatever they voted on.
        targets = [r["target_id"] for r in cx.execute("SELECT target_id FROM votes WHERE user_id=?", (uid,))]
        cx.execute("DELETE FROM votes WHERE user_id=?", (uid,))
        for table in ("posts", "comments"):
            cx.execute("UPDATE %s SET score=(SELECT coalesce(sum(value),0) FROM votes WHERE target_id=%s.id) "
                       "WHERE id = ANY(?)" % (table, table), (targets,))
        cx.executemany("UPDATE posts SET hot=? WHERE id=?", [
            (db.hot(r["score"], r["created"]), r["id"])
            for r in cx.execute("SELECT id, score, created FROM posts WHERE id = ANY(?)", (targets,))])
        # Whatever stays up is shown as [deleted]; communities they made stay, moderated by admins.
        cx.execute("UPDATE posts SET author_id=NULL WHERE author_id=?", (uid,))
        cx.execute("UPDATE comments SET author_id=NULL WHERE author_id=?", (uid,))
        cx.execute("UPDATE communities SET owner_id=NULL WHERE owner_id=?", (uid,))
        cx.execute("UPDATE awards_given SET giver_id=NULL WHERE giver_id=?", (uid,))
        cx.execute("UPDATE awards_given SET receiver_id=NULL WHERE receiver_id=?", (uid,))
        cx.execute("DELETE FROM upvote_payouts WHERE voter_id=?", (uid,))
        cx.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        cx.execute("DELETE FROM users WHERE id=?", (uid,))
        db.drop_image_if_unused(cx, u["avatar_id"])


def trophy_case(u, mine):
    got = {r["award"]: r["n"] for r in conn().execute(
        "SELECT award, count(*) AS n FROM awards_given WHERE receiver_id=? GROUP BY award", (u["id"],))}
    case = ('<div class="trophies"><h3>awards received</h3>%s</div>' % "".join(
        '<span class="trophy">%s %s<b>&times;%d</b></span>' % (awards.icon(k, 24), e(awards.AWARDS[k][0]), got[k])
        for k in awards.AWARDS if k in got)) if got else ""
    balance = ('<p class="karma"><b>%d</b> award karma <span class="hint">(only you see this)</span></p>' % u["award_karma"]
               if mine else "")
    return balance + case


# --- giving awards ----------------------------------------------------------------------------

def award_target(tid):
    """The post or comment being awarded, with enough to preview it."""
    t = conn().execute("SELECT 'post' AS kind, p.id AS post_id, p.title AS text, p.author_id, u.name, u.prefix "
                       "FROM posts p LEFT JOIN users u ON u.id=p.author_id WHERE p.id=? AND p.deleted=0", (tid,)).fetchone()
    t = t or conn().execute("SELECT 'comment' AS kind, c.post_id, c.body AS text, c.author_id, u.name, u.prefix "
                            "FROM comments c LEFT JOIN users u ON u.id=c.author_id WHERE c.id=? AND c.deleted=0", (tid,)).fetchone()
    if not t or not t["author_id"]:
        raise HTTPError(404, "That's been deleted.")
    return t


def award_page(req, tid, err=""):
    t = award_target(tid)
    if t["author_id"] == req.user["id"]:
        raise HTTPError(400, "You can't award your own post or comment.")
    balance = req.user["award_karma"]
    nxt = safe_next(req.query.get("next") or req.get("next") or "/p/" + t["post_id"])
    preview = e(t["text"] if len(t["text"]) <= 200 else t["text"][:200] + "...")
    options = "".join(f"""<label class="awardopt{' cant' if cost > balance else ''}">
<input type="radio" name="award" value="{k}" required{' disabled' if cost > balance else ''}>
{awards.icon(k, 32)}<span class="aname">{e(name)}</span><span class="acost">{cost} award karma</span>
<span class="hint">{('the recipient gets %d award karma' % bonus) if bonus else '&nbsp;'}</span></label>"""
                      for k, (name, cost, bonus, _) in awards.AWARDS.items())
    body = f"""<h2 class="pagetitle">give an award</h2>{field_error(err)}
<div class="awardtarget"><span class="hint">{t['kind']} by</span> {user_link(t['name'], t['prefix'])}
<blockquote>{preview}</blockquote></div>
<form class="formtabs" method="post" action="/award/{e(tid)}">{csrf(req)}<input type="hidden" name="next" value="{e(nxt)}">
<p>You have <b>{balance}</b> award karma. You earn 1 every time someone upvotes your posts or comments.</p>
<fieldset class="awardgrid"><legend class="hint">choose an award</legend>{options}</fieldset>
<button class="btn">give award</button> <a href="{e(nxt)}">cancel</a></form>"""
    return page(req, "give an award", body)


@route("GET", r"/award/([0-9a-z]+)")
def award_form(req, tid):
    if not req.user:
        return login_redirect(req)
    return award_page(req, tid)


@route("POST", r"/award/([0-9a-z]+)")
def award_give(req, tid):
    if not req.user:
        return login_redirect(req)
    rate_check(write_limiter, req.user["id"])
    try:
        awards.give(req.user["id"], req.get("award"), tid)
    except awards.AwardError as ex:
        return award_page(req, tid, str(ex))
    return redirect(safe_next(req.get("next") or "/"))


def default_avatar(cx):
    """Copy a random image from the admin's avatar folder into the database."""
    folder = CONF["avatars_dir"]
    try:
        names = [n for n in os.listdir(folder) if not n.startswith(".")]
    except OSError:
        return None
    random.shuffle(names)
    for n in names:
        path = os.path.join(folder, n)
        if os.path.isfile(path) and os.path.getsize(path) <= MAX_AVATAR:
            with open(path, "rb") as f:
                webp = media.optimize(f.read(), media.AVATAR_MAX)
            if webp:
                return db.store_image(cx, webp)
    return None


def auth_page(req, title, inner):
    return page(req, title, '<div class="authbox"><h2 class="pagetitle">%s</h2>%s</div>' % (e(title), inner))


def register_form(req, err="", f=None):
    f = f or {}
    dp = settings.get("user_prefix")
    prefix_field = "" if not settings.flag("custom_user_prefix") else f"""<div class="roundfield"><label>prefix</label>
<input name="prefix" value="{e(f.get('prefix', dp))}" maxlength="{PREFIX_MAX}" class="short" required>
<span class="hint">what shows before your name instead of <b>{e(dp)}</b>/ &mdash; up to {PREFIX_MAX} characters. You can change it later.</span></div>"""
    return f"""<form class="formtabs" method="post" action="/register">{field_error(err)}
<div class="roundfield"><label>username</label><input name="name" value="{e(f.get('name', ''))}" required
 pattern="[A-Za-z0-9_\\-]{{3,20}}" maxlength="20" autocomplete="username" autofocus>
<span class="hint">3-20 letters, numbers, - or _</span></div>
{prefix_field}
<div class="roundfield"><label>password</label><input type="password" name="password" required minlength="8" maxlength="256" autocomplete="new-password"></div>
<div class="roundfield"><label>verify password</label><input type="password" name="password2" required minlength="8" maxlength="256" autocomplete="new-password"></div>
<p class="hint">No email needed. You'll get a recovery phrase instead &mdash; it's the only way back in if you forget your password.</p>
<button class="btn">sign up</button> <span class="hint">already have an account? <a href="/login">log in</a></span></form>"""


def check_registration_open():
    if not settings.flag("open_registration"):
        raise HTTPError(403, "Sign-ups are closed on this board right now.")


@route("GET", r"/register")
def register(req):
    check_registration_open()
    return auth_page(req, "create an account", register_form(req))


def check_password(pw, pw2):
    if pw != pw2:
        return "Passwords don't match."
    if not 8 <= len(pw) <= 256:
        return "Password must be 8-256 characters."
    return ""


def phrase_page(req, user, phrase, title):
    words = "".join("<li>%s</li>" % e(w) for w in phrase.split())
    body = f"""<div class="phrasebox"><h2 class="pagetitle">{e(title)}</h2>
<p>This is your <b>recovery phrase</b> for <b>{e(uname(user['name'], user['prefix']))}</b>. If you forget your password,
it's the only way to get back into your account &mdash; there's no email on file.</p>
<p><b>Write it down and keep it somewhere safe. It won't be shown again.</b></p>
<ol class="phrase">{words}</ol>
<p><a class="btn" href="/">I've saved it &mdash; continue</a></p></div>"""
    resp = page(req, "recovery phrase", body)
    resp.headers.append(("Cache-Control", "no-store"))
    return resp


@route("POST", r"/register")
def register_post(req):
    check_registration_open()
    rate_check(auth_limiter, req.ip)
    name = req.get("name").strip()
    prefix = (settings.flag("custom_user_prefix") and req.get("prefix").strip()) or settings.get("user_prefix")
    pw, pw2 = req.get("password"), req.get("password2")
    err = check_password(pw, pw2) or valid_prefix(prefix, "user")
    if not USER_RX.fullmatch(name):
        err = "Username must be 3-20 letters, numbers, - or _."
    elif conn().execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        err = "That username is taken."
    err = err or address_clash(name, user_prefix=prefix)
    if err:
        return auth_page(req, "create an account", register_form(req, err, req.form))
    phrase = security.new_phrase()
    pw_hash, rec_hash = security.hash_secret(pw), security.hash_secret(phrase)
    uid = new_id()
    try:
        with tx() as cx:
            cx.execute("INSERT INTO users(id, name, prefix, pw_hash, recovery_hash, avatar_id, created) VALUES(?,?,?,?,?,?,?)",
                       (uid, name, prefix, pw_hash, rec_hash, default_avatar(cx), now()))
    except db.IntegrityError:  # someone grabbed the name while we were hashing
        return auth_page(req, "create an account", register_form(req, "That username is taken.", req.form))
    start_session(req, uid)
    return phrase_page(req, req.user, phrase, "welcome, %s!" % name)


def create_account(name, password, prefix=None, admin=False):
    """Create an account; returns its recovery phrase. Raises ValueError with a reason if it can't."""
    prefix = prefix or settings.get("user_prefix")
    err = check_password(password, password) or valid_prefix(prefix, "user")
    if not USER_RX.fullmatch(name):
        err = "Username must be 3-20 letters, numbers, - or _."
    elif conn().execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        err = "That username is taken."
    err = err or address_clash(name, user_prefix=prefix)
    if err:
        raise ValueError(err)
    phrase = security.new_phrase()
    pw_hash, rec_hash = security.hash_secret(password), security.hash_secret(phrase)
    with tx() as cx:
        cx.execute("INSERT INTO users(id, name, prefix, pw_hash, recovery_hash, avatar_id, is_admin, created) "
                   "VALUES(?,?,?,?,?,?,?,?)", (new_id(), name, prefix, pw_hash, rec_hash, default_avatar(cx), int(admin), now()))
    return phrase


def login_form(req, err="", f=None):
    f = f or {}
    nxt = f.get("next", req.query.get("next", "/"))
    return f"""<form class="formtabs" method="post" action="/login">{field_error(err)}
<input type="hidden" name="next" value="{e(nxt)}">
<div class="roundfield"><label>username</label><input name="name" value="{e(f.get('name', ''))}" required autocomplete="username" autofocus></div>
<div class="roundfield"><label>password</label><input type="password" name="password" required autocomplete="current-password"></div>
<button class="btn">log in</button>
<p class="hint"><a href="/recover">forgot your password?</a> &middot; new here? <a href="/register">sign up</a></p></form>"""


@route("GET", r"/login")
def login(req):
    return auth_page(req, "log in", login_form(req))


@route("POST", r"/login")
def login_post(req):
    rate_check(auth_limiter, req.ip)
    u = conn().execute("SELECT * FROM users WHERE name=?", (req.get("name").strip(),)).fetchone()
    pw = req.get("password")
    if not u:
        security.burn_time(pw)
    if not u or not security.verify_secret(pw, u["pw_hash"]):
        return auth_page(req, "log in", login_form(req, "Wrong username or password.", req.form))
    if u["banned"]:
        return auth_page(req, "log in", login_form(req, "This account has been banned.", req.form))
    if security.needs_rehash(u["pw_hash"]):
        conn().execute("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_secret(pw), u["id"]))
    start_session(req, u["id"])
    return redirect(safe_next(req.get("next")))


@route("POST", r"/logout")
def logout(req):
    end_session(req)
    return redirect("/")


def recover_form(err="", f=None):
    f = f or {}
    return f"""<form class="formtabs" method="post" action="/recover">{field_error(err)}
<div class="roundfield"><label>username</label><input name="name" value="{e(f.get('name', ''))}" required autocomplete="username"></div>
<div class="roundfield"><label>recovery phrase</label><textarea name="phrase" rows="3" required autocomplete="off" spellcheck="false"></textarea>
<span class="hint">the {security.PHRASE_WORDS} words you got when you signed up</span></div>
<div class="roundfield"><label>new password</label><input type="password" name="password" required minlength="8" maxlength="256" autocomplete="new-password"></div>
<div class="roundfield"><label>verify new password</label><input type="password" name="password2" required minlength="8" maxlength="256" autocomplete="new-password"></div>
<p class="hint">You'll get a new recovery phrase afterwards; the old one stops working.</p>
<button class="btn">reset password</button></form>"""


@route("GET", r"/recover")
def recover(req):
    return auth_page(req, "recover your account", recover_form())


@route("POST", r"/recover")
def recover_post(req):
    rate_check(auth_limiter, req.ip)
    u = conn().execute("SELECT * FROM users WHERE name=?", (req.get("name").strip(),)).fetchone()
    phrase = security.normalize_phrase(req.get("phrase"))
    if not u:
        security.burn_time(phrase)
    if not u or not security.verify_secret(phrase, u["recovery_hash"]):
        return auth_page(req, "recover your account", recover_form("That username and phrase don't match.", req.form))
    if u["banned"]:
        return auth_page(req, "recover your account", recover_form("This account has been banned.", req.form))
    err = check_password(req.get("password"), req.get("password2"))
    if err:
        return auth_page(req, "recover your account", recover_form(err, req.form))
    new = security.new_phrase()
    pw_hash, rec_hash = security.hash_secret(req.get("password")), security.hash_secret(new)
    with tx() as cx:
        cx.execute("UPDATE users SET pw_hash=?, recovery_hash=? WHERE id=?", (pw_hash, rec_hash, u["id"]))
        cx.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    start_session(req, u["id"])
    return phrase_page(req, u, new, "password reset")


# --- notifications ---------------------------------------------------------------------------

NOTIFICATION_SELECT = (
    "SELECT n.*, a.name AS actor, a.prefix AS aprefix, a.is_admin AS aadmin, p.title, p.body AS pbody, "
    "p.deleted AS pdeleted, c.id AS cid, c.body AS cbody, c.deleted AS cdeleted FROM notifications n "
    "LEFT JOIN users a ON a.id=n.actor_id JOIN posts p ON p.id=n.post_id LEFT JOIN comments c ON c.id=n.target_id ")


def notification_row(n):
    on_comment = n["cid"] is not None
    who = user_link(n["actor"], n["aprefix"], n["aadmin"])
    if n["kind"] == "award" and n["detail"] in awards.AWARDS:
        says = "%s gave you %s on" % (who, e(awards.AWARDS[n["detail"]][0]))
        what = awards.icon(n["detail"], 32)
    else:
        says = "%s %s" % (who, e(notify.KINDS.get(n["kind"], ("", "notified you about"))[1]))
        text = n["cbody"] if on_comment else n["pbody"]
        what = ('<p class="gone">[deleted]</p>' if (n["cdeleted"] if on_comment else n["pdeleted"])
                else "<p>%s</p>" % e(text if len(text) <= 300 else text[:300] + "...") if text else "")
    ptitle = "[deleted]" if n["pdeleted"] else e(n["title"])
    href = "/p/%s" % n["post_id"] + ("#c_%s" % n["cid"] if on_comment else "")
    return f"""<div class="notification{'' if n['read'] else ' unread'}" id="n_{e(n['id'])}" data-t="{n['created']}">
<p class="ntitle">{says} <a href="/p/{e(n['post_id'])}">{ptitle}</a> <time>{ago(n['created'])}</time></p>
{'<div class="nbody">%s</div>' % what if what else ''}
<ul class="buttons"><li><a href="{e(href)}">{'context' if on_comment else 'view post'}</a></li></ul></div>"""


@route("GET", r"/notifications")
def notifications_page(req):
    if not req.user:
        return login_redirect(req)
    pg, uid = page_num(req), req.user["id"]
    rows = conn().execute(NOTIFICATION_SELECT + "WHERE n.user_id=? ORDER BY n.created DESC, n.id DESC LIMIT ? OFFSET ?",
                          (uid, PER_PAGE + 1, (pg - 1) * PER_PAGE)).fetchall()
    # Seen now: the rows above still show which were new, and the header count drops to 0.
    conn().execute("UPDATE notifications SET read=1 WHERE user_id=? AND read=0", (uid,))
    req.user["unread"] = 0
    items = "\n".join(notification_row(n) for n in rows[:PER_PAGE])
    # Page 1 picks up new notifications by itself (app.js), from this timestamp on.
    live = ' data-since="%d"' % (rows[0]["created"] if rows else now()) if pg == 1 else ""
    body = f"""<h2 class="pagetitle">notifications</h2>
<p class="hint">New ones show up here without reloading. Choose what you're notified about in <a href="/settings#notifications">preferences</a>.</p>
<div class="notifications"{live}>{items or '<p class="empty">Nothing yet. Replies, mentions and awards will show up here.</p>'}</div>
{pager(pg, len(rows) > PER_PAGE)}"""
    return page(req, "notifications", body)


@route("GET", r"/notifications/count")
def notifications_count(req):
    """Polled by every page (app.js) to keep the header's unread count current."""
    if not req.user:
        return json_response({"login": True})
    resp = json_response({"unread": req.user["unread"], "poll": notify.POLL_SECONDS})
    resp.headers.append(("Cache-Control", "no-store"))
    return resp


@route("GET", r"/notifications/new")
def notifications_new(req):
    """Rows created since ?since= for an open notifications page, which counts them as read."""
    if not req.user:
        raise HTTPError(403, "Log in first.")
    try:
        since = int(req.query.get("since", ""))
    except ValueError:
        raise HTTPError(400, "Bad timestamp.")
    rows = conn().execute(NOTIFICATION_SELECT + "WHERE n.user_id=? AND n.created>=? ORDER BY n.created DESC, n.id DESC LIMIT 50",
                          (req.user["id"], since)).fetchall()
    if rows:
        conn().execute("UPDATE notifications SET read=1 WHERE id = ANY(?)", ([n["id"] for n in rows],))
    resp = Response("\n".join(notification_row(n) for n in rows))
    resp.headers.append(("Cache-Control", "no-store"))
    return resp


# --- settings ------------------------------------------------------------------------------

MESSAGES = {"profile": "Profile saved.", "prefs": "Preferences saved.", "notifications": "Notification preferences saved.", "avatar": "Avatar updated.", "password": "Password changed.",
            "badpw": "Current password is wrong.", "badimg": "That isn't a supported image, or it's over 8 MB.",
            "noavatars": "No default avatars are available.", "mismatch": "New passwords don't match or are under 8 characters."}


@route("GET", r"/settings")
def settings_page(req, err=""):
    if not req.user:
        return login_redirect(req)
    u = req.user
    msg = MESSAGES.get(req.query.get("msg", ""), "")
    prefix_field = "" if not settings.flag("custom_user_prefix") else f"""<div class="roundfield"><label>prefix</label>
<input name="prefix" value="{e(u['prefix'])}" maxlength="{PREFIX_MAX}" class="short" required>
<span class="hint">shown as <b>prefix</b>/{e(u['name'])}, 1-{PREFIX_MAX} letters, numbers or underscores</span></div>"""
    off = u["notify_off"].split(",")
    notify_boxes = "".join('<label class="check"><input type="checkbox" name="notify_%s" value="1"%s> %s</label>'
                           % (k, "" if k in off else " checked", e(label)) for k, (label, _) in notify.KINDS.items())
    note = '<div class="%s">%s</div>' % ("error" if req.query.get("msg") in ("badpw", "badimg", "noavatars", "mismatch") else "notice", e(msg)) if msg else ""
    body = f"""<h2 class="pagetitle">preferences</h2>{note}{field_error(err)}
<form class="formtabs" method="post" action="/settings/profile">{csrf(req)}<h3>profile</h3>
{prefix_field}
<div class="roundfield"><label>about you</label><textarea name="bio" rows="4" maxlength="500">{e(u['bio'])}</textarea></div>
<button class="btn">save</button></form>

<form class="formtabs" method="post" action="/settings/avatar" enctype="multipart/form-data">{csrf(req)}<h3>avatar</h3>
<div class="avatarrow">{avatar(u['avatar_id'], 64, 'bigavatar')}
<div><input type="file" name="avatar" accept="image/png,image/jpeg,image/gif,image/webp">
<span class="hint">square images look best; up to 8 MB (shrunk to 256px)</span><br>
<button class="btn" name="action" value="upload">upload</button>
<button class="btn" name="action" value="random" formnovalidate>random default</button>
<button class="btn" name="action" value="remove" formnovalidate>remove</button></div></div></form>

<form class="formtabs" method="post" action="/settings/prefs">{csrf(req)}<h3>display</h3>
<div class="roundfield"><label class="check"><input type="checkbox" name="dark_mode" value="1"{' checked' if u['dark_mode'] else ''}>
dark mode</label>
<label class="check"><input type="checkbox" name="live_updates" value="1"{' checked' if u['live_updates'] else ''}>
show new posts as they're posted when the front page is sorted by <b>new</b></label>
<span class="hint">at the top of the page new posts appear right away; further down, a "new posts" button shows up instead</span></div>
<button class="btn">save</button></form>

<form class="formtabs" id="notifications" method="post" action="/settings/notifications">{csrf(req)}<h3>notifications</h3>
<fieldset class="roundfield"><legend>notify me about</legend>{notify_boxes}</fieldset>
<span class="hint">they're listed at <a href="/notifications">notifications</a>; the count in the header updates by itself</span><br>
<button class="btn">save</button></form>

<form class="formtabs" method="post" action="/settings/password">{csrf(req)}<h3>change password</h3>
<div class="roundfield"><label>current password</label><input type="password" name="current" required autocomplete="current-password"></div>
<div class="roundfield"><label>new password</label><input type="password" name="password" required minlength="8" maxlength="256" autocomplete="new-password"></div>
<div class="roundfield"><label>verify new password</label><input type="password" name="password2" required minlength="8" maxlength="256" autocomplete="new-password"></div>
<button class="btn">change password</button> <span class="hint">logs out your other sessions</span></form>

<form class="formtabs" method="post" action="/settings/recovery">{csrf(req)}<h3>recovery phrase</h3>
<p class="hint">Lost your phrase? Get a new one. The old phrase stops working immediately.</p>
<div class="roundfield"><label>current password</label><input type="password" name="current" required autocomplete="current-password"></div>
<button class="btn">generate new phrase</button></form>"""
    return page(req, "preferences", body)


@route("POST", r"/settings/prefs")
def settings_prefs(req):
    if not req.user:
        return login_redirect(req)
    conn().execute("UPDATE users SET live_updates=?, dark_mode=? WHERE id=?",
                   (int(req.get("live_updates") == "1"), int(req.get("dark_mode") == "1"), req.user["id"]))
    return redirect("/settings?msg=prefs")


@route("POST", r"/settings/notifications")
def settings_notifications(req):
    if not req.user:
        return login_redirect(req)
    off = ",".join(k for k in notify.KINDS if req.get("notify_" + k) != "1")
    conn().execute("UPDATE users SET notify_off=? WHERE id=?", (off, req.user["id"]))
    return redirect("/settings?msg=notifications")


@route("POST", r"/settings/profile")
def settings_profile(req):
    if not req.user:
        return login_redirect(req)
    bio = req.get("bio").strip()
    prefix = req.get("prefix").strip() if settings.flag("custom_user_prefix") else req.user["prefix"]
    err = ((valid_prefix(prefix, "user") or address_clash(req.user["name"], user_prefix=prefix))
           if prefix != req.user["prefix"] else "") or ("Bio is too long." if len(bio) > 500 else "")
    if err:
        return settings_page(req, err)
    conn().execute("UPDATE users SET prefix=?, bio=? WHERE id=?", (prefix, bio, req.user["id"]))
    return redirect("/settings?msg=profile")


@route("POST", r"/settings/avatar")
def settings_avatar(req):
    if not req.user:
        return login_redirect(req)
    action, old = req.get("action"), req.user["avatar_id"]
    upload = req.files.get("avatar")
    if action not in ("remove", "random"):
        upload = media.optimize(upload, media.AVATAR_MAX) if upload and len(upload) <= MAX_AVATAR_UPLOAD else None
        if not upload:
            return redirect("/settings?msg=badimg")
    with tx() as cx:
        if action == "remove":
            new = None
        elif action == "random":
            new = default_avatar(cx)
            if not new:
                return redirect("/settings?msg=noavatars")
        else:
            new = db.store_image(cx, upload)
        cx.execute("UPDATE users SET avatar_id=? WHERE id=?", (new, req.user["id"]))
        if old != new:
            db.drop_image_if_unused(cx, old)
    return redirect("/settings?msg=avatar")


@route("POST", r"/settings/password")
def settings_password(req):
    if not req.user:
        return login_redirect(req)
    rate_check(auth_limiter, req.ip)
    if not security.verify_secret(req.get("current"), req.user["pw_hash"]):
        return redirect("/settings?msg=badpw")
    if check_password(req.get("password"), req.get("password2")):
        return redirect("/settings?msg=mismatch")
    pw_hash = security.hash_secret(req.get("password"))
    with tx() as cx:
        cx.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, req.user["id"]))
        cx.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (req.user["id"], req.session))
    return redirect("/settings?msg=password")


@route("POST", r"/settings/recovery")
def settings_recovery(req):
    if not req.user:
        return login_redirect(req)
    rate_check(auth_limiter, req.ip)
    if not security.verify_secret(req.get("current"), req.user["pw_hash"]):
        return redirect("/settings?msg=badpw")
    phrase = security.new_phrase()
    conn().execute("UPDATE users SET recovery_hash=? WHERE id=?", (security.hash_secret(phrase), req.user["id"]))
    return phrase_page(req, req.user, phrase, "new recovery phrase")


# --- platform settings (admins only) ------------------------------------------------------------

def require_admin(req):
    if not req.user or not req.user["is_admin"]:
        raise HTTPError(403, "Admins only.")


def admin_page(req, err="", f=None):
    f = f or {}
    v = lambda k: e(f.get(k, settings.get(k)))
    chk = lambda k: " checked" if (f.get(k) == "1" if f else settings.flag(k)) else ""
    sel = lambda k, val: " selected" if f.get(k, settings.get(k)) == val else ""
    counts = conn().execute("SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM communities),"
                            " (SELECT count(*) FROM posts WHERE deleted=0), (SELECT count(*) FROM comments WHERE deleted=0)").fetchone()
    admins = conn().execute("SELECT name, prefix FROM users WHERE is_admin=1 ORDER BY name").fetchall()
    admin_rows = "".join(
        '<li>%s%s</li>' % (user_link(a["name"], a["prefix"], True), "" if a["name"] == req.user["name"] else
                           ' <form class="inline" method="post" action="/admin/admins">%s<input type="hidden" name="name" value="%s">'
                           '<input type="hidden" name="action" value="revoke"><button class="linkbtn revoke">[remove]</button></form>'
                           % (csrf(req), e(a["name"])))
        for a in admins)
    banned = conn().execute("SELECT name, prefix FROM users WHERE banned=1 ORDER BY name").fetchall()
    banned_rows = "".join(
        '<li>%s <form class="inline" method="post" action="%s/ban">%s<input type="hidden" name="action" value="unban">'
        '<button class="linkbtn revoke">[unban]</button></form></li>' % (
            user_link(b["name"], b["prefix"]), e(user_url(b["name"], b["prefix"])), csrf(req))
        for b in banned) or '<li class="hint">nobody is banned</li>'
    msg = {"saved": "Settings saved.", "granted": "Admin added.", "revoked": "Admin removed.",
           "nouser": "No user by that name.", "deleted": "Account deleted.", "uploaded": "Images added.",
           "imgdeleted": "Image removed.", "linked": "Ad link saved.", "unlinked": "Ad link removed.",
           "badlink": "Ad links must be full web addresses starting with http:// or https://.", "badimg": "Some files weren't supported images or videos, or were too large; they were skipped. (QuickTime .mov needs converting to mp4.)",
           "noimg": "Choose at least one image."}.get(req.query.get("msg", ""), "")
    note = '<div class="%s">%s</div>' % ("error" if req.query.get("msg") in ("nouser", "badimg", "noimg", "badlink") else "notice",
                                         e(msg)) if msg else ""
    body = f"""<h2 class="pagetitle">platform settings</h2>{note}{field_error(err)}
<p class="hint adminstats">{plural(counts[0], 'user')} &middot; {counts[1]} {'community' if counts[1] == 1 else 'communities'}
&middot; {plural(counts[2], 'post')} &middot; {plural(counts[3], 'comment')}</p>
<form class="formtabs" method="post" action="/admin/settings">{csrf(req)}
<h3>site</h3>
<div class="roundfield"><label>site name</label><input name="site_name" value="{v('site_name')}" required maxlength="40"></div>
<div class="roundfield"><label>front page blurb <span class="hint">(sidebar text, light markdown)</span></label>
<textarea name="tagline" rows="3" maxlength="500">{v('tagline')}</textarea></div>

<h3>user names</h3>
<div class="roundfield"><label>default user prefix</label>
<input name="user_prefix" value="{v('user_prefix')}" required maxlength="{PREFIX_MAX}" class="short">
<span class="hint">shown as <b>prefix</b>/username</span>
<label class="check"><input type="checkbox" name="custom_user_prefix" value="1"{chk('custom_user_prefix')}> let users choose their own prefix</label>
<label class="check"><input type="checkbox" name="apply_user_prefix" value="1"> also switch existing users who use the old default</label>
<span class="hint">when users can't choose, everyone is shown with the default, and their own choice comes back if you allow it again</span></div>

<h3>community names</h3>
<div class="roundfield"><label>default community prefix</label>
<input name="community_prefix" value="{v('community_prefix')}" required maxlength="{PREFIX_MAX}" class="short">
<span class="hint">shown as <b>prefix</b>/community</span>
<label class="check"><input type="checkbox" name="custom_community_prefix" value="1"{chk('custom_community_prefix')}> let community creators choose their own prefix</label>
<label class="check"><input type="checkbox" name="apply_community_prefix" value="1"> also switch existing communities that use the old default</label></div>

<h3>access</h3>
<div class="roundfield">
<label class="check"><input type="checkbox" name="open_registration" value="1"{chk('open_registration')}> anyone can sign up</label>
<label>who can create communities</label>
<select name="community_creation"><option value="anyone"{sel('community_creation', 'anyone')}>any logged-in user</option>
<option value="admins"{sel('community_creation', 'admins')}>admins only</option></select></div>
<button class="btn">save settings</button></form>

<div class="formtabs"><h3>admins</h3><ul class="adminlist">{admin_rows}</ul>
<form method="post" action="/admin/admins">{csrf(req)}<input type="hidden" name="action" value="grant">
<div class="roundfield"><label>make a user an admin</label><input name="name" required placeholder="username"></div>
<button class="btn">add admin</button></form></div>

<div class="formtabs"><h3>users</h3>
<form method="get" action="/admin/user"><div class="roundfield"><label>find a user to ban or delete</label>
<input name="name" required placeholder="username"></div><button class="btn">open profile</button></form>
<h3 class="banlist-title">banned users</h3><ul class="adminlist">{banned_rows}</ul></div>
{image_manager(req)}"""
    return page(req, "platform settings", body)


@route("GET", r"/admin")
def admin(req):
    if not req.user:
        return login_redirect(req)
    require_admin(req)
    return admin_page(req)


@route("POST", r"/admin/settings")
def admin_save(req):
    require_admin(req)
    f = {k: req.get(k).strip() for k in ("site_name", "tagline", "user_prefix", "community_prefix", "community_creation")}
    for k in ("custom_user_prefix", "custom_community_prefix", "open_registration"):
        f[k] = "1" if req.get(k) == "1" else "0"
    err = check_settings(f)
    if err:
        return admin_page(req, err, f)
    up, cp = f["user_prefix"], f["community_prefix"]
    old_up, old_cp = settings.get("user_prefix"), settings.get("community_prefix")
    settings.save(f)
    if req.get("apply_user_prefix") == "1" and up != old_up:
        conn().execute("UPDATE users SET prefix=? WHERE prefix=?", (up, old_up))
    if req.get("apply_community_prefix") == "1" and cp != old_cp:
        conn().execute("UPDATE communities SET prefix=? WHERE prefix=?", (cp, old_cp))
    return redirect("/admin?msg=saved")


def check_settings(f):
    """Why the platform settings `f` (a complete dict) can't be saved, or "" if they can.
    Shared by the admin page and `board.py settings set`."""
    up, cp = f["user_prefix"], f["community_prefix"]
    err = ""
    for k in ("custom_user_prefix", "custom_community_prefix", "open_registration"):
        if f[k] not in ("0", "1"):
            return "%s must be on or off." % k
    if not 1 <= len(f["site_name"]) <= 40:
        err = "Site name must be 1-40 characters."
    elif len(f["tagline"]) > 500:
        err = "Front page blurb is too long."
    elif f["community_creation"] not in ("anyone", "admins"):
        err = "Pick who can create communities."
    elif up.lower() == cp.lower():
        err = "User and community prefixes must be different, or a user and a community could share an address."
    else:
        for label, val in (("User", up), ("Community", cp)):
            if not PREFIX_RX.fullmatch(val):
                err = "%s prefix must be 1-%d letters, numbers or underscores." % (label, PREFIX_MAX)
            elif val.lower() in RESERVED:
                err = '%s prefix "%s" is reserved.' % (label, val)
            if err:
                break
    if not err:
        for r in conn().execute("SELECT u.name, u.prefix AS up, c.prefix AS cp FROM users u "
                                "JOIN communities c ON c.name=u.name").fetchall():
            err = address_clash(r["name"], r["up"], r["cp"], conf=f)
            if err:
                break
    return err


def ad_link_form(req, name, url):
    return f"""<form class="adlinkform" method="post" action="/admin/ads/link">{csrf(req)}<input type="hidden" name="name" value="{e(name)}">
<input type="url" name="url" value="{e(url)}" placeholder="https://... (link)" maxlength="2000" aria-label="link for {e(name)}">
<button class="linkbtn">{'update' if url else 'add link'}</button></form>"""


def image_manager(req):
    sections, links = [], ad_links()
    for kind, (conf_key, label, limit) in IMAGE_FOLDERS.items():
        path = "/ads/" if kind == "ads" else "/default-avatars/"
        tiles = "".join(
            f"""<figure>{ad_media(path + quote(n), n, ' class="tile"') if kind == "ads" else '<img src="%s%s" alt="" loading="lazy">' % (path, e(quote(n)))}<figcaption>{e(n)}
<form class="inline" method="post" action="/admin/images/delete">{csrf(req)}<input type="hidden" name="kind" value="{kind}">
<input type="hidden" name="name" value="{e(n)}"><button class="linkbtn revoke" aria-label="remove {e(n)}">[remove]</button></form>
{ad_link_form(req, n, links.get(n, "")) if kind == "ads" else ""}</figcaption></figure>"""
            for n in folder_images(kind)) or '<p class="hint">none yet</p>'
        what = ("one is picked at random for the square under the sidebar on each page" if kind == "ads"
                else "new accounts get one at random")
        sections.append(f"""<h4>{label}</h4><p class="hint">{what}. Files live in <code>{e(CONF[conf_key])}</code>.</p>
<div class="imagegrid">{tiles}</div>
<form method="post" action="/admin/images" enctype="multipart/form-data">{csrf(req)}<input type="hidden" name="kind" value="{kind}">
<div class="roundfield"><label>add {label}</label><input type="file" name="images" multiple required
 accept="image/png,image/jpeg,image/gif,image/webp{',video/mp4,video/webm,video/ogg' if kind == 'ads' else ''}">
<span class="hint">{'png, jpeg, gif or webp images up to %d MB, or mp4, webm or ogg videos up to %d MB (videos play muted and looping, without controls)' % (limit // 1024 // 1024, MAX_AD_VIDEO // 1024 // 1024) if kind == 'ads' else 'png, jpeg, gif or webp, up to %d MB each' % (limit // 1024 // 1024)}</span></div>
<button class="btn">upload</button></form>""")
    return '<div class="formtabs" id="images"><h3>images</h3>%s</div>' % "".join(sections)


@route("POST", r"/admin/images")
def admin_images_add(req):
    require_admin(req)
    kind = req.get("kind")
    if kind not in IMAGE_FOLDERS:
        raise HTTPError(400, "Unknown image folder.")
    conf_key, _, limit = IMAGE_FOLDERS[kind]
    files = req.file_lists.get("images", [])
    if not files:
        return redirect("/admin?msg=noimg#images")
    os.makedirs(CONF[conf_key], exist_ok=True)
    skipped = 0
    for data in files:
        mime = db.sniff_image(data)
        video = sniff_video(data) if kind == "ads" and not mime else None
        if not (mime or video) or len(data) > (MAX_AD_VIDEO if video else limit):
            skipped += 1
            continue
        if mime:  # images are stored as compressed WebP, sized for where they're shown
            data = media.optimize(data, media.AD_MAX if kind == "ads" else media.AVATAR_MAX)
            if data is None:
                skipped += 1
                continue
        ext = VIDEO_EXTENSIONS[video] if video else "webp"
        with open(os.path.join(CONF[conf_key], "%s-%s.%s" % (kind, new_id(), ext)), "wb") as f:
            f.write(data)
    return redirect("/admin?msg=%s#images" % ("badimg" if skipped else "uploaded"))


@route("POST", r"/admin/images/delete")
def admin_images_delete(req):
    require_admin(req)
    kind, name = req.get("kind"), req.get("name")
    if kind not in IMAGE_FOLDERS or name not in folder_images(kind):  # only names we listed: no path tricks
        raise HTTPError(404, "No such image.")
    os.remove(os.path.join(CONF[IMAGE_FOLDERS[kind][0]], name))
    links = ad_links()
    if kind == "ads" and links.pop(name, None):
        save_ad_links(links)
    return redirect("/admin?msg=imgdeleted#images")


@route("POST", r"/admin/ads/link")
def admin_ad_link(req):
    """Set (or, with an empty URL, remove) where clicking an ad takes people."""
    require_admin(req)
    name, url = req.get("name"), req.get("url").strip()
    if name not in folder_images("ads"):
        raise HTTPError(404, "No such ad.")
    if url and (len(url) > 2000 or not re.match(r"https?://[^\s/$.?#][^\s]*$", url, re.I)):
        return redirect("/admin?msg=badlink#images")
    links = ad_links()
    if url:
        links[name] = url
    else:
        links.pop(name, None)
    save_ad_links(links)
    return redirect("/admin?msg=%s#images" % ("linked" if url else "unlinked"))


@route("GET", r"/admin/user")
def admin_user(req):
    require_admin(req)
    u = conn().execute("SELECT name, prefix FROM users WHERE name=?", (req.query.get("name", "").strip(),)).fetchone()
    return redirect(user_url(u["name"], u["prefix"]) if u else "/admin?msg=nouser")


@route("POST", r"/admin/admins")
def admin_admins(req):
    require_admin(req)
    name, grant = req.get("name").strip(), req.get("action") == "grant"
    if not grant and name.lower() == req.user["name"].lower():
        raise HTTPError(400, "You can't remove yourself as admin.")
    cur = conn().execute("UPDATE users SET is_admin=? WHERE name=?", (int(grant), name))
    if not cur.rowcount:
        return redirect("/admin?msg=nouser")
    return redirect("/admin?msg=" + ("granted" if grant else "revoked"))


# --- /<prefix>/<name>: user and community pages live at their displayed names --------------------

def address_clash(name, user_prefix=None, comm_prefix=None, conf=None):
    """Would the user and the community called `name` end up at the same /prefix/name address?
    Pass the prefix being chosen for one side; the other side's comes from the database."""
    conf = conf or settings.all()
    if user_prefix is None:
        u = conn().execute("SELECT prefix FROM users WHERE name=?", (name,)).fetchone()
        user_prefix = u and u["prefix"]
    if comm_prefix is None:
        c = conn().execute("SELECT prefix FROM communities WHERE name=?", (name,)).fetchone()
        comm_prefix = c and c["prefix"]
    if not (user_prefix and comm_prefix):
        return ""
    url = settings.shown(user_prefix, "user", conf)
    if url.lower() != settings.shown(comm_prefix, "community", conf).lower():
        return ""
    return "That would give the user and the community named %s the same address (/%s/%s). Pick another prefix." % (
        name, url, name)


@route("GET", r"/([A-Za-z0-9_]{1,%d})/([A-Za-z0-9_-]{3,21})(?:/(hot|new|top|comments|edit))?/?" % PREFIX_MAX)
def named_page(req, prefix, name, sub):
    if prefix.lower() in RESERVED:  # e.g. /admin/god: a real page registered after this catch-all
        return None
    conf = settings.all()
    c = conn().execute("SELECT name, prefix FROM communities WHERE name=?", (name,)).fetchone()
    u = conn().execute("SELECT name, prefix FROM users WHERE name=?", (name,)).fetchone()
    if c and settings.shown(c["prefix"], "community", conf).lower() == prefix.lower():
        if sub == "edit":
            return community_edit(req, c["name"])
        if sub != "comments":
            return community(req, c["name"], sub)
    elif u and settings.shown(u["prefix"], "user", conf).lower() == prefix.lower():
        if sub in (None, "comments"):
            return profile(req, u["name"], sub)
    # Old-style /r/name and /u/name links (and links from before a prefix change) still work.
    elif prefix.lower() == "r" and c:
        return redirect(comm_url(c["name"], c["prefix"]) + ("/" + sub if sub else ""), 302)
    elif prefix.lower() == "u" and u:
        return redirect(user_url(u["name"], u["prefix"]) + ("/" + sub if sub else ""), 302)
    raise HTTPError(404, "There's nothing here.")


@route("POST", r"/([A-Za-z0-9_]{1,%d})/([A-Za-z0-9_-]{3,21})/(edit|ban|delete)" % PREFIX_MAX)
def named_action(req, prefix, name, action):
    """Form posts go to the same /prefix/name address as the page they came from."""
    if prefix.lower() in RESERVED:
        return None
    conf = settings.all()
    table, kind = ("communities", "community") if action == "edit" else ("users", "user")
    r = conn().execute("SELECT name, prefix FROM %s WHERE name=?" % table, (name,)).fetchone()
    if not r or settings.shown(r["prefix"], kind, conf).lower() != prefix.lower():
        raise HTTPError(404, "There's nothing here.")
    return {"edit": community_save, "ban": user_ban, "delete": user_delete}[action](req, r["name"])
