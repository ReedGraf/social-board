"""HTML rendering helpers. Plain f-strings; every user value goes through e()."""
import html
import re
from functools import lru_cache
import secrets
import time

from . import awards, db, settings
from .web import CONF, static_url

e = html.escape


def ago(ts):
    d = max(0, int(time.time()) - ts)
    for size, unit in ((31536000, "year"), (2592000, "month"), (86400, "day"),
                       (3600, "hour"), (60, "minute")):
        if d >= size:
            n = d // size
            return "%d %s%s ago" % (n, unit, "" if n == 1 else "s")
    return "just now"


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def uname(name, prefix):
    """"prefix/name" for a user, as the platform settings say it should display."""
    return "%s/%s" % (settings.shown(prefix, "user"), name)


def cname(name, prefix):
    return "%s/%s" % (settings.shown(prefix, "community"), name)


def user_url(name, prefix):
    """A user's page lives at its displayed name, e.g. /blu/alice."""
    return "/" + uname(name, prefix)


def comm_url(name, prefix):
    return "/" + cname(name, prefix)


def user_link(name, prefix, admin=False):
    if name is None:
        return '<span class="author gone">[deleted]</span>'
    cls = 'author admin" title="admin' if admin else "author"  # admins' names are red everywhere
    return '<a class="%s" href="%s">%s</a>' % (cls, e(user_url(name, prefix)), e(uname(name, prefix)))


COLOR_RX = re.compile(r"#[0-9a-f]{6}")


def color_class(color):
    """Class for a community's chosen color. page() turns the ones it finds into CSS."""
    return " cc-" + color[1:] if color and COLOR_RX.fullmatch(color) else ""


def contrast(c1, c2):
    """WCAG contrast ratio between two #rrggbb colors (1 = identical, 21 = black on white)."""
    def lum(c):
        def lin(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    hi, lo = sorted((lum(c1), lum(c2)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


DARK_BG = "#1a1a1b"


def for_dark(color):
    """Lighten a community color (chosen to read on white) until it reads on the dark background."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    for step in range(11):
        t = step / 10
        mixed = "#%02x%02x%02x" % tuple(round(v + (255 - v) * t) for v in (r, g, b))
        if contrast(mixed, DARK_BG) >= 4.5:
            return mixed
    return "#ffffff"


def comm_link(name, prefix, cls="subreddit", color=""):
    return '<a class="%s%s" href="%s">%s</a>' % (cls, color_class(color), e(comm_url(name, prefix)), e(cname(name, prefix)))


def csrf(req):
    return '<input type="hidden" name="csrf" value="%s">' % e(req.user["csrf"]) if req.user else ""


def avatar(image_id, size, cls="avatar"):
    if not image_id:
        return '<span class="%s blank s%d"></span>' % (cls, size)
    return '<img class="%s s%d" src="/img/%s" width="%d" height="%d" alt="">' % (cls, size, e(image_id), size, size)


# --- markdown ------------------------------------------------------------------------
# Headings, lists (nested by indenting), > quotes, ``` code blocks, tables, --- rules,
# **bold**, *italic*, ~~strike~~, `code`, [links](https://...), bare links, \escapes.
# Safe by construction: every piece of user text is escaped before it's placed, and the
# only tags in the output are the ones built here. Links must be http(s), mailto or local.

_ESCAPABLE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~>|])")
_CODE = re.compile(r"`([^`\n]+)`")
# Spans are capped (SPAN chars) so spammed, unclosed markers can't make rendering quadratic.
SPAN = 300
_LINK = re.compile(r"\[([^\]\n]{1,%d})\]\(\s*([^\s()]{1,2000})\s*\)" % SPAN)
# Stop bare links at escaped quotes/brackets (they were escaped from the user's text).
_AUTOLINK = re.compile(r"https?://(?:(?!&(?:quot|lt|gt|#x27);)[^\s<>\x00])+")
_STASHED = re.compile("\x00(\\d+)\x00")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
MAX_DEPTH = 8  # nested quotes/lists deeper than this are shown as plain text


def _safe_url(url):
    low = url.lower()
    return low.startswith(("http://", "https://", "mailto:", "/", "#")) and not low.startswith("/\\")


_EMPHASIS = [(re.compile(pat.replace("SPAN", str(SPAN))), repl) for pat, repl in (
    (r"\*\*(?=\S)(.{1,SPAN}?)(?<=\S)\*\*", r"<strong>\1</strong>"),
    (r"(?<!\w)__(?=\S)(.{1,SPAN}?)(?<=\S)__(?!\w)", r"<strong>\1</strong>"),
    (r"(?<![\w*])\*(?=\S)(.{1,SPAN}?)(?<=\S)\*(?![\w*])", r"<em>\1</em>"),
    (r"(?<![\w_])_(?=\S)(.{1,SPAN}?)(?<=\S)_(?![\w_])", r"<em>\1</em>"),
    (r"~~(?=\S)(.{1,SPAN}?)(?<=\S)~~", r"<del>\1</del>"))]


def _emphasis(s):
    for rx, repl in _EMPHASIS:
        s = rx.sub(repl, s)
    return s


def _inline(text):
    """Markdown for one line of raw text -> safe HTML."""
    keep = []

    def stash(html):
        keep.append(html)
        return "\x00%d\x00" % (len(keep) - 1)

    def link(m):
        if not _safe_url(m.group(2)):
            return m.group(0)
        return stash('<a href="%s" rel="nofollow noopener noreferrer">%s</a>' % (m.group(2), _emphasis(m.group(1))))

    def autolink(m):
        url = m.group(0)
        trail = re.search(r"[.,;:!?)\]'*_~]*$", url).group(0)
        url = url[:len(url) - len(trail)] if trail else url
        return stash('<a href="%s" rel="nofollow noopener noreferrer">%s</a>' % (url, url)) + trail

    s = e(text.replace("\x00", ""))
    s = _CODE.sub(lambda m: stash("<code>%s</code>" % m.group(1)), s)
    s = _ESCAPABLE.sub(lambda m: stash(m.group(1)), s)
    s = _LINK.sub(link, s)
    s = _AUTOLINK.sub(autolink, s)
    s = _emphasis(s)
    while "\x00" in s:  # stashed pieces can contain other stashed pieces (code inside link text)
        s = _STASHED.sub(lambda m: keep[int(m.group(1))], s)
    return s


def _starts_block(line):
    return bool(_FENCE.match(line) or _HEADING.match(line) or _HR.match(line) or _LIST_ITEM.match(line)
                or line.lstrip().startswith(">"))


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _blocks(lines, depth=0):
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:  # ``` code block: shown exactly as typed
            i += 1
            code = []
            while i < len(lines) and not lines[i].lstrip().startswith(fence.group(1)):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % e("\n".join(code)))
            continue
        heading = _HEADING.match(line)
        if heading:
            n = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (n, _inline(heading.group(2)), n))
            i += 1
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">") and depth < MAX_DEPTH:
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % _blocks(quote, depth + 1))
            continue
        item = _LIST_ITEM.match(line)
        if item and depth < MAX_DEPTH:
            indent, ordered = len(item.group(1)), item.group(2)[0].isdigit()
            items = []
            while i < len(lines):
                m = _LIST_ITEM.match(lines[i])
                if m and len(m.group(1)) <= indent + 1:  # a sibling item
                    if m.group(2)[0].isdigit() != ordered:
                        break
                    items.append([m.group(3)])
                    i += 1
                elif items and lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) > indent:
                    items[-1].append(lines[i][indent + 2:] if lines[i][:indent + 2].isspace() else lines[i].lstrip())
                    i += 1  # indented: belongs to the item above (continuation or a nested list)
                else:
                    break
            def render(item_lines):
                first, rest = _inline(item_lines[0]), item_lines[1:]
                nested = [j for j, l in enumerate(rest) if _starts_block(l)]
                if not nested:
                    return "<br>".join([first] + [_inline(l) for l in rest])
                k = nested[0]
                return "<br>".join([first] + [_inline(l) for l in rest[:k]]) + _blocks(rest[k:], depth + 1)
            start = int(re.match(r"\d+", item.group(2)).group(0)) if ordered else 1
            tag = "ol" if ordered else "ul"
            out.append("<%s%s>%s</%s>" % (tag, ' start="%d"' % start if start != 1 else "",
                                          "".join("<li>%s</li>" % render(it) for it in items), tag))
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            head, aligns = _cells(line), []
            for c in _cells(lines[i + 1]):
                aligns.append("c" if c.startswith(":") and c.endswith(":") else "r" if c.endswith(":") else "")
            cell = lambda tag, c, k: "<%s%s>%s</%s>" % (tag, ' class="al-%s"' % aligns[k] if k < len(aligns) and aligns[k] else "",
                                                       _inline(c), tag)
            rows, i = [], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            out.append('<div class="tablewrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
                "".join(cell("th", c, k) for k, c in enumerate(head)),
                "".join("<tr>%s</tr>" % "".join(cell("td", c, k) for k, c in enumerate(r[:len(head)])) for r in rows)))
            continue
        para = [line]  # a paragraph runs until a blank line or the start of another block
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i])
            i += 1
        out.append("<p>%s</p>" % "<br>".join(_inline(l.strip()) for l in para))
    return "".join(out)


@lru_cache(maxsize=512)  # pages re-render the same posts/comments constantly; text in, HTML out
def md(text):
    return '<div class="md">%s</div>' % _blocks(text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n"))


FORMATTING_HELP = """<details class="mdhelp"><summary>formatting help</summary><table>
<tr><th>you type</th><th>you see</th></tr>
<tr><td><code>*italic*</code> &nbsp; <code>**bold**</code> &nbsp; <code>~~strike~~</code></td><td><em>italic</em> <strong>bold</strong> <del>strike</del></td></tr>
<tr><td><code>[link text](https://example.com)</code></td><td><a href="https://example.com" rel="nofollow noopener noreferrer">link text</a></td></tr>
<tr><td><code># Heading</code> &nbsp; <code>## Smaller</code></td><td><b>Heading</b></td></tr>
<tr><td><code>- item</code> or <code>1. item</code> (indent two spaces to nest)</td><td>&bull; item</td></tr>
<tr><td><code>&gt; quoted text</code></td><td>quote</td></tr>
<tr><td><code>`code`</code>, or <code>```</code> on lines before and after a block</td><td><code>code</code></td></tr>
<tr><td><code>---</code></td><td>a horizontal line</td></tr>
<tr><td><code>a | b</code> / <code>--- | ---</code> / <code>1 | 2</code></td><td>a table</td></tr>
<tr><td><code>\\*not italic\\*</code></td><td>*not italic*</td></tr>
</table><p class="hint">A blank line starts a new paragraph. Links to http(s) addresses are linked automatically.</p></details>"""


# --- layout -------------------------------------------------------------------

_bar_cache = [0, []]


def community_bar():
    """Top strip of popular communities; cached briefly since it's on every page."""
    if time.time() - _bar_cache[0] > 30:
        rows = db.conn().execute(
            "SELECT c.name, c.prefix, c.color FROM communities c LEFT JOIN posts p ON p.community_id=c.id "
            "GROUP BY c.id ORDER BY count(p.id) DESC, c.name LIMIT 25").fetchall()
        _bar_cache[:] = [time.time(), [(r["name"], r["prefix"], r["color"]) for r in rows]]
    # Links are built per request so a prefix change in /admin shows up immediately.
    return " - ".join('<a class="%s" href="%s">%s</a>' % (color_class(c).strip(), e(comm_url(n, p)), e(n))
                      for n, p, c in _bar_cache[1])


def karma(user_id):
    """(post karma, comment karma, admin adjustment). The total karma is their sum."""
    r = db.conn().execute(
        "SELECT (SELECT coalesce(sum(score),0)::bigint FROM posts WHERE author_id=? AND deleted=0),"
        " (SELECT coalesce(sum(score),0)::bigint FROM comments WHERE author_id=? AND deleted=0),"
        " (SELECT karma_adjust FROM users WHERE id=?)",
        (user_id, user_id, user_id)).fetchone()
    return r[0], r[1], r[2] or 0


def page(req, title, body, *, side="", pagename="", pagehref="", tabs=None, status=200):
    from .web import Response
    u = req.user
    dark = not u or bool(u["dark_mode"])  # dark is the default, also for visitors
    if u:
        total = sum(karma(u["id"]))
        right = ('<a%s href="%s">%s</a> (<b title="karma">%d</b>)'
                 ' <span class="awardbal" title="award karma: spend it on awards">%s<b>%d</b></span>'
                 '%s <span class="sep">|</span> <a href="/settings">preferences</a>'
                 ' <span class="sep">|</span> <form class="inline" method="post" action="/logout">%s'
                 '<button class="linkbtn">logout</button></form>') % (
                     ' class="admin"' if u["is_admin"] else "", e(user_url(u["name"], u["prefix"])), e(u["name"]), total,
                     awards.icon("gold", 12).replace('title="Gold"', 'title="award karma"').replace('alt="Gold"', 'alt="award karma"'),
                     u["award_karma"],
                     ' <span class="sep">|</span> <a href="/admin">admin</a>' if u["is_admin"] else "", csrf(req))
    elif settings.flag("open_registration"):
        right = 'want to join? <a href="/login">log in</a> or <a href="/register">sign up</a> in seconds.'
    else:
        right = '<a href="/login">log in</a>'
    tabhtml = ""
    if tabs:
        tabhtml = '<ul class="tabmenu">%s</ul>' % "".join(
            '<li%s><a href="%s"%s>%s</a></li>' % (' class="selected"' if sel else "", e(href),
                                                   ' aria-current="page"' if sel else "", e(label))
            for label, href, sel in tabs)
    pn = ('<span class="pagename"><a href="%s">%s</a></span>' % (e(pagehref), e(pagename))) if pagename else ""
    side_html = '<aside class="side" aria-label="sidebar">%s</aside>' % side if side else ""
    bar = community_bar()
    # Community colors: inline style="" is blocked by our CSP, so emit one <style> block
    # holding just the colors on this page, allowed by a per-response nonce. !important because the
    # community's chosen color should beat the generic link colors (tagline, sidebar, top strip).
    colors = sorted(set(re.findall(r"\bcc-([0-9a-f]{6})\b", body + side + bar)))
    nonce = secrets.token_urlsafe(12) if colors else None
    shade = (lambda c: for_dark("#" + c)) if dark else (lambda c: "#" + c)
    color_css = ('<style nonce="%s">%s</style>' % (nonce, "".join("a.cc-%s{color:%s!important}" % (c, shade(c)) for c in colors))
                 if colors else "")
    doc = f"""<!doctype html>
<html lang="en" data-theme="{'dark' if dark else 'light'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="{static_url('style.css')}">
<script src="{static_url('app.js')}" defer></script>
{color_css}</head><body>
<a class="skip-link" href="#main">Skip to main content</a>
<nav id="sr-bar" aria-label="communities"><a href="/">front</a> - <a href="/communities">communities</a> <span class="sep">|</span> {bar}</nav>
<header id="header">
<div id="header-left"><a id="logo" href="/">{e(settings.get('site_name'))}</a>{pn}{tabhtml}</div>
<div id="header-right">{right}</div>
</header>
<div class="main"><main id="main" class="content" tabindex="-1">{body}</main>{side_html}</div>
<footer class="footer">{e(settings.get('site_name'))} &middot; instance {e(CONF['instance'][:8])}{' &middot; <a class="godlink" href="/admin/god">god mode</a>' if u and u['is_admin'] else ''}</footer>
</body></html>"""
    resp = Response(doc, status)
    resp.style_nonce = nonce
    return resp


def field_error(msg):
    return '<div class="error">%s</div>' % e(msg) if msg else ""
