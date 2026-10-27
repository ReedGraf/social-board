"""God mode: an admin-only dashboard (linked from the footer) to look up any user and
adjust them directly: karma, award karma, name, prefix, bio, avatar, sessions, password,
ban, admin rights, deletion. Every action is written to admin_log."""
import secrets
from urllib.parse import quote

from . import db, security
from .db import conn, new_id, now, tx
from .tpl import ago, avatar, csrf, e, field_error, karma, page, plural, user_link
from .views import (USER_RX, address_clash, default_avatar, delete_account, get_user, login_redirect,
                    require_admin, set_banned, valid_prefix)
from .web import HTTPError, redirect, route

MAX_ADJUST = 1_000_000


def log(req, target, action, detail=""):
    conn().execute("INSERT INTO admin_log VALUES(?,?,?,?,?,?)",
                   (new_id(), req.user["name"], target, action, detail, now()))


def back(name, msg):
    return redirect("/admin/god?u=%s&msg=%s" % (quote(name), quote(msg)))


def amount(req, key="amount"):
    try:
        n = int(req.get(key).strip())
    except ValueError:
        raise HTTPError(400, "Enter a whole number.")
    if abs(n) > MAX_ADJUST:
        raise HTTPError(400, "That's more than %d at once." % MAX_ADJUST)
    return n


def not_self(req, u, what):
    if u["id"] == req.user["id"]:
        raise HTTPError(400, "You can't %s your own account." % what)


def summary(u):
    pk, ck, adj = karma(u["id"])
    uid = u["id"]
    c = conn().execute(
        "SELECT (SELECT count(*) FROM posts WHERE author_id=? AND deleted=0) AS posts,"
        " (SELECT count(*) FROM comments WHERE author_id=? AND deleted=0) AS comments,"
        " (SELECT count(*) FROM awards_given WHERE receiver_id=?) AS got,"
        " (SELECT count(*) FROM awards_given WHERE giver_id=?) AS gave,"
        " (SELECT count(*) FROM sessions WHERE user_id=? AND expires>?) AS sessions",
        (uid, uid, uid, uid, uid, now())).fetchone()
    flags = "".join(['<span class="godflag admin">admin</span>' if u["is_admin"] else "",
                     '<span class="godflag banned">banned</span>' if u["banned"] else ""])
    return f"""<div class="godcard">{avatar(u['avatar_id'], 64, 'bigavatar')}<div>
<h3>{user_link(u['name'], u['prefix'], u['is_admin'])} {flags}</h3>
<p class="hint">joined {ago(u['created'])} &middot; {plural(c['posts'], 'post')} &middot; {plural(c['comments'], 'comment')}
&middot; {c['sessions']} active login{'' if c['sessions'] == 1 else 's'}</p>
<table class="godstats">
<tr><th>karma</th><td><b>{pk + ck + adj}</b> <span class="hint">= {pk} post + {ck} comment {'%+d' % adj} admin adjustment</span></td></tr>
<tr><th>award karma</th><td><b>{u['award_karma']}</b> <span class="hint">awards received {c['got']}, given {c['gave']}</span></td></tr>
</table></div></div>"""


def form(req, u, action, inner, button, danger=False, confirm=""):
    cls = ' class="confirm" data-confirm="%s"' % e(confirm) if confirm else ""
    return f"""<form method="post" action="/admin/god/{action}"{cls}>{csrf(req)}<input type="hidden" name="u" value="{e(u['name'])}">
{inner}<button class="btn{' danger' if danger else ''}">{button}</button></form>"""


def actions(req, u):
    me, other_admin = u["id"] == req.user["id"], bool(u["is_admin"]) and u["id"] != req.user["id"]
    num = '<input type="number" name="amount" required step="1" min="-%d" max="%d" class="short" placeholder="e.g. 50 or -50">' % (MAX_ADJUST, MAX_ADJUST)
    tiles = [
        ("karma", "Adds to (or, with a minus, takes from) their karma, on top of what votes give them.",
         form(req, u, "karma", num, "adjust karma")),
        ("award karma", "Spendable on awards. It can't go below 0.",
         form(req, u, "award", num + '<select name="mode"><option value="add">add / subtract</option><option value="set">set to exactly</option></select>',
              "update award karma")),
        ("rename", "Their pages move to the new name; old links stop working.",
         form(req, u, "rename", '<input name="new" required maxlength="20" pattern="[A-Za-z0-9_\\-]{3,20}" value="%s">' % e(u["name"]),
              "rename")),
        ("prefix", "Stored even if prefixes are locked (it shows once they're unlocked).",
         form(req, u, "prefix", '<input name="prefix" required maxlength="%d" class="short" value="%s">' % (db.PREFIX_MAX, e(u["prefix"])),
              "set prefix")),
        ("profile", "Clear what they wrote about themselves, or reset their picture.",
         form(req, u, "bio", "", "clear bio") + " " +
         form(req, u, "avatar", '<input type="hidden" name="to" value="random">', "random avatar") + " " +
         form(req, u, "avatar", '<input type="hidden" name="to" value="none">', "remove avatar")),
        ("sessions", "Logs them out on every device." + (" (Your current login is kept.)" if me else ""),
         form(req, u, "logout", "", "log out everywhere")),
        ("password", "Sets a random temporary password, shown to you once, and logs them out. Their recovery phrase still works.",
         form(req, u, "password", "", "reset password", confirm="Reset this user's password?")),
    ]
    if not me:
        tiles.append(("admin rights", "Admins can moderate everything and open god mode.",
                      form(req, u, "admin", "", "remove admin" if u["is_admin"] else "make admin",
                           confirm="Remove admin rights?" if u["is_admin"] else "Make this user an admin?")))
    if not me and not other_admin:
        purge = '<label class="check"><input type="checkbox" name="purge" value="1"> also delete their posts and comments</label>'
        tiles.append(("ban", "Banned users are logged out and can't log in, post, comment or vote.",
                      form(req, u, "ban", ('<input type="hidden" name="action" value="unban">' if u["banned"] else
                                           '<input type="hidden" name="action" value="ban">' + purge),
                           "unban" if u["banned"] else "ban", confirm="" if u["banned"] else "Ban this user?")))
        tiles.append(("delete account", "Removes the account; the name becomes free. Can't be undone.",
                      form(req, u, "delete", purge, "delete account", danger=True, confirm="Delete this account? It can't be undone.")))
    elif other_admin:
        tiles.append(("ban / delete", "Remove their admin rights first.", ""))
    return '<div class="godgrid">%s</div>' % "".join(
        '<section class="godtile"><h4>%s</h4><p class="hint">%s</p>%s</section>' % (e(t), e(h), f) for t, h, f in tiles)


def god_page(req, name="", note="", err="", secret=""):
    u = conn().execute("SELECT * FROM users WHERE name=?", (name,)).fetchone() if name else None
    if name and not u:
        err = err or "No user named %s." % name
    rows = conn().execute("SELECT * FROM admin_log ORDER BY created DESC LIMIT 25").fetchall()
    logtable = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        ago(r["created"]), e(r["admin"]), e(r["target"]), e(r["action"]), e(r["detail"])) for r in rows)
    secret_box = ('<div class="notice godsecret">Temporary password for <b>%s</b>: <code>%s</code><br>'
                  '<span class="hint">Shown only this once. Give it to them; they can change it in preferences.</span></div>'
                  % (e(u["name"]), e(secret))) if secret and u else ""
    body = f"""<h2 class="pagetitle">god mode</h2>
<form class="godsearch" method="get" action="/admin/god"><input name="u" value="{e(name)}" placeholder="username" required
 aria-label="username" autofocus> <button class="btn">look up</button></form>
{('<div class="notice">%s</div>' % e(note)) if note else ''}{field_error(err)}{secret_box}
{(summary(u) + actions(req, u)) if u else ''}
<h3 class="godloghead">recent god-mode actions</h3>
{('<div class="tablewrap"><table class="godlog"><tr><th>when</th><th>admin</th><th>user</th><th>action</th><th>detail</th></tr>%s</table></div>'
  % logtable) if rows else '<p class="hint">nothing yet</p>'}"""
    return page(req, "god mode", body)


@route("GET", r"/admin/god")
def god(req):
    if not req.user:
        return login_redirect(req)
    require_admin(req)
    return god_page(req, req.query.get("u", "").strip(), note=req.query.get("msg", ""))


@route("POST", r"/admin/god/(karma|award|rename|prefix|bio|avatar|logout|password|admin|ban|delete)")
def god_action(req, action):
    require_admin(req)
    u = get_user(req.get("u").strip())
    name, uid = u["name"], u["id"]
    if action == "karma":
        n = amount(req)
        conn().execute("UPDATE users SET karma_adjust = karma_adjust + ? WHERE id=?", (n, uid))
        log(req, name, "karma", "%+d" % n)
        return back(name, "Karma %+d." % n)
    if action == "award":
        n = amount(req)
        if req.get("mode") == "set":
            new = max(0, n)
        else:
            new = max(0, u["award_karma"] + n)
        conn().execute("UPDATE users SET award_karma=? WHERE id=?", (new, uid))
        log(req, name, "award karma", "%d -> %d" % (u["award_karma"], new))
        return back(name, "Award karma is now %d." % new)
    if action == "rename":
        new = req.get("new").strip()
        if new == name:
            return back(name, "That's already their name.")
        if not USER_RX.fullmatch(new):
            return god_page(req, name, err="Usernames are 3-20 letters, numbers, - or _.")
        taken = conn().execute("SELECT 1 FROM users WHERE name=? AND id<>?", (new, uid)).fetchone()
        err = "That username is taken." if taken else address_clash(new, user_prefix=u["prefix"])
        if err:
            return god_page(req, name, err=err)
        conn().execute("UPDATE users SET name=? WHERE id=?", (new, uid))
        log(req, name, "rename", "to %s" % new)
        return back(new, "Renamed %s to %s." % (name, new))
    if action == "prefix":
        prefix = req.get("prefix").strip()
        err = valid_prefix(prefix, "user") or address_clash(name, user_prefix=prefix)
        if err:
            return god_page(req, name, err=err)
        conn().execute("UPDATE users SET prefix=? WHERE id=?", (prefix, uid))
        log(req, name, "prefix", "%s -> %s" % (u["prefix"], prefix))
        return back(name, "Prefix set to %s." % prefix)
    if action == "bio":
        conn().execute("UPDATE users SET bio='' WHERE id=?", (uid,))
        log(req, name, "clear bio")
        return back(name, "Bio cleared.")
    if action == "avatar":
        with tx() as cx:
            new = default_avatar(cx) if req.get("to") == "random" else None
            cx.execute("UPDATE users SET avatar_id=? WHERE id=?", (new, uid))
            if u["avatar_id"] != new:
                db.drop_image_if_unused(cx, u["avatar_id"])
        log(req, name, "avatar", "random default" if new else "removed")
        return back(name, "Avatar reset." if new else "Avatar removed.")
    if action == "logout":
        keep = req.session if uid == req.user["id"] else ""
        n = conn().execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (uid, keep)).rowcount
        log(req, name, "log out everywhere", plural(n, "session"))
        return back(name, "Ended %s." % plural(n, "login"))
    if action == "password":
        temp = secrets.token_urlsafe(9)  # 12 characters
        pw_hash = security.hash_secret(temp)
        keep = req.session if uid == req.user["id"] else ""
        with tx() as cx:
            cx.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, uid))
            cx.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (uid, keep))
        log(req, name, "reset password")  # the password itself is never logged
        return god_page(req, name, note="Password reset; they've been logged out.", secret=temp)
    if action == "admin":
        not_self(req, u, "change admin rights on")
        conn().execute("UPDATE users SET is_admin=? WHERE id=?", (int(not u["is_admin"]), uid))
        log(req, name, "remove admin" if u["is_admin"] else "make admin")
        return back(name, "%s is %s an admin." % (name, "no longer" if u["is_admin"] else "now"))
    # ban / delete: never yourself or another admin
    not_self(req, u, action)
    if u["is_admin"]:
        return god_page(req, name, err="Remove their admin rights first.")
    purge = req.get("purge") == "1"
    if action == "ban":
        banned = req.get("action") == "ban"
        set_banned(u, banned, purge)
        log(req, name, "ban" if banned else "unban", "and deleted their posts" if banned and purge else "")
        return back(name, "Banned." if banned else "Unbanned.")
    delete_account(u, purge)
    log(req, name, "delete account", "and their posts" if purge else "")
    return redirect("/admin/god?msg=%s" % quote("Deleted %s." % name))
