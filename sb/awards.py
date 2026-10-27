"""Awards: spend award karma to give an award to someone's post or comment.

Award karma is a balance of its own, separate from regular karma (which is just
the sum of vote scores and goes up and down):
  - every account starts with STARTING_BALANCE;
  - +1 each time another user upvotes one of your posts or comments, paid once
    per voter per item, so un-voting and re-voting can't farm it; downvotes
    never take it away;
  - giving an award costs its price; some awards also pay the recipient."""
from html import escape as e

from . import db
from .web import static_url

STARTING_BALANCE = 100  # also the column default in db.SCHEMA
UPVOTE_PAYOUT = 1

# key: (name, cost, what the recipient gets, icon) - in display order, cheapest first
AWARDS = {
    "helpful":   ("Helpful", 25, 0, "award-helpful.svg"),
    "wholesome": ("Wholesome", 30, 0, "award-wholesome.svg"),
    "silver":    ("Silver", 50, 0, "award-silver.svg"),
    "gold":      ("Gold", 100, 25, "award-gold.svg"),
    "platinum":  ("Platinum", 500, 100, "award-platinum.svg"),
}


class AwardError(Exception):
    pass


def icon(key, size=16):
    name = AWARDS[key][0]
    return '<img class="awardicon" src="%s" width="%d" height="%d" alt="%s" title="%s">' % (
        static_url(AWARDS[key][3]), size, size, name, name)


def counts(target_ids):
    """{target_id: {award: count}} for a page's worth of posts/comments, in one query."""
    out = {}
    if target_ids:
        for r in db.conn().execute("SELECT target_id, award, count(*) AS n FROM awards_given "
                                   "WHERE target_id = ANY(?) GROUP BY target_id, award", (list(target_ids),)):
            out.setdefault(r["target_id"], {})[r["award"]] = r["n"]
    return out


def badges(awarded):
    """The little row of award icons (with counts) shown on a post or comment."""
    if not awarded:
        return ""
    parts = ['<span class="award">%s%s</span>' % (icon(k), "&times;%d" % awarded[k] if awarded[k] > 1 else "")
             for k in AWARDS if k in awarded]
    total = sum(awarded.values())
    return '<span class="awards" aria-label="%d award%s">%s</span>' % (total, "" if total == 1 else "s", "".join(parts))


def on_upvote(cx, voter_id, target_id, author_id):
    """Pay the author +1 award karma, once per voter per item. Call inside the vote's transaction."""
    if not author_id or author_id == voter_id:
        return
    paid = cx.execute("INSERT INTO upvote_payouts VALUES(?, ?) ON CONFLICT DO NOTHING", (voter_id, target_id)).rowcount
    if paid:
        cx.execute("UPDATE users SET award_karma = award_karma + ? WHERE id=?", (UPVOTE_PAYOUT, author_id))


def give(giver_id, award, target_id):
    """Spend the giver's award karma on `award` for a post or comment. Returns the target's post id."""
    if award not in AWARDS:
        raise AwardError("Pick an award.")
    _, cost, bonus, _ = AWARDS[award]
    with db.tx() as cx:
        t = (cx.execute("SELECT id AS post_id, author_id FROM posts WHERE id=? AND deleted=0", (target_id,)).fetchone()
             or cx.execute("SELECT post_id, author_id FROM comments WHERE id=? AND deleted=0", (target_id,)).fetchone())
        if not t or not t["author_id"]:
            raise AwardError("That's been deleted.")
        if t["author_id"] == giver_id:
            raise AwardError("You can't award your own post or comment.")
        # Lock the giver's row: two awards at once can't both spend the same karma.
        balance = cx.execute("SELECT award_karma FROM users WHERE id=? FOR UPDATE", (giver_id,)).fetchone()[0]
        if balance < cost:
            raise AwardError("%s costs %d award karma; you have %d." % (AWARDS[award][0], cost, balance))
        cx.execute("UPDATE users SET award_karma = award_karma - ? WHERE id=?", (cost, giver_id))
        if bonus:
            cx.execute("UPDATE users SET award_karma = award_karma + ? WHERE id=?", (bonus, t["author_id"]))
        cx.execute("INSERT INTO awards_given VALUES(?,?,?,?,?,?)",
                   (db.new_id(), award, giver_id, t["author_id"], target_id, db.now()))
    return t["post_id"]
