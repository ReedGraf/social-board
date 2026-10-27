"""Image optimization: everything the board stores or serves as an image becomes a
compressed WebP, scaled down to what it's displayed at. Animated GIF/WebP stay animated."""
import io
import os
import threading

from PIL import Image, ImageOps, ImageSequence

# Refuse absurd pixel counts (decompression bombs) instead of eating all the memory.
Image.MAX_IMAGE_PIXELS = 60_000_000
# Encoding is CPU- and memory-heavy; a couple at a time is plenty for this server.
_slots = threading.BoundedSemaphore(2)

POST_MAX = 2048    # longest side of a post image
AD_MAX = 600       # ads are shown at 300px; 2x for sharp high-DPI screens
AVATAR_MAX = 256
THUMB_SIZE = (140, 100)  # listing thumbnails are shown at 70x50
QUALITY = 80

_ERRORS = (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError)


def _has_alpha(im):
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _fit(im, max_side):
    if max(im.size) > max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def optimize(data, max_side=POST_MAX, quality=QUALITY):
    """Re-encode any image as a compressed WebP no larger than max_side. Returns None if it
    isn't a readable image. A WebP that doesn't need shrinking is returned unchanged."""
    with _slots:
        try:
            src = Image.open(io.BytesIO(data))
            # A WebP that's already within size is kept exactly as it is: re-encoding a lossy WebP
            # again (say, on every restart) would shave a little more quality off each time.
            if src.format == "WEBP" and max(src.size) <= max_side:
                src.load()  # still make sure it actually decodes
                return data
            if getattr(src, "n_frames", 1) > 1:
                frames, durations = [], []
                for frame in ImageSequence.Iterator(src):
                    frames.append(_fit(frame.convert("RGBA"), max_side))
                    durations.append(frame.info.get("duration", 100))
                # Animations: try lossless and lossy (both diffing frames) and keep the smaller.
                # GIF-sourced frames usually win lossless, which also means zero quality loss.
                out = None
                for opts in (dict(lossless=True), dict(quality=quality)):
                    attempt = io.BytesIO()
                    frames[0].save(attempt, "WEBP", save_all=True, append_images=frames[1:], duration=durations,
                                   loop=0, method=4, minimize_size=True, **opts)
                    if out is None or attempt.tell() < out.tell():
                        out = attempt
            else:
                im = ImageOps.exif_transpose(src)  # phone photos: rotate upright (metadata is dropped)
                im = im.convert("RGBA" if _has_alpha(im) else "RGB")
                fitted = _fit(im, max_side)
                out = io.BytesIO()
                fitted.save(out, "WEBP", quality=quality, method=5)
        except _ERRORS:
            return None
    return out.getvalue()


def thumbnail(data):
    """A small cropped WebP for listings, or None for animations (they're their own thumbnail)."""
    with _slots:
        try:
            with Image.open(io.BytesIO(data)) as src:
                if getattr(src, "n_frames", 1) > 1:
                    return None
                im = ImageOps.exif_transpose(src)
                im = im.convert("RGBA" if _has_alpha(im) else "RGB")
                im = ImageOps.fit(im, THUMB_SIZE, Image.LANCZOS)
                out = io.BytesIO()
                im.save(out, "WEBP", quality=75, method=5)
                return out.getvalue()
        except _ERRORS:
            return None


# --- one-time conversion of what's already stored ---------------------------------------

def convert_folder(folder, max_side):
    """Turn every image file in an admin folder into an optimized .webp. Originals are kept in
    <folder>/.originals so nothing is lost. Videos and other files are left alone."""
    from .db import sniff_image
    done = []
    try:
        names = [n for n in os.listdir(folder) if not n.startswith(".")]
    except OSError:
        return done
    for name in names:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            data = f.read()
        if not sniff_image(data):
            continue
        webp = optimize(data, max_side)
        if webp is None or webp is data:
            continue
        stem = name.rsplit(".", 1)[0]
        target = os.path.join(folder, stem + ".webp")
        n = 1
        while os.path.exists(target) and os.path.abspath(target) != os.path.abspath(path):
            n += 1
            target = os.path.join(folder, "%s-%d.webp" % (stem, n))
        backup_dir = os.path.join(folder, ".originals")
        os.makedirs(backup_dir, exist_ok=True)
        os.replace(path, os.path.join(backup_dir, name))
        with open(target, "wb") as f:
            f.write(webp)
        done.append((name, os.path.basename(target), len(data), len(webp)))
    return done


def convert_database(c):
    """Re-encode every stored image that isn't WebP yet. Converted images get new ids (their
    URLs are cached as immutable), and every post/user pointing at the old one is moved over."""
    from . import db
    stats = dict(converted=0, before=0, after=0, failed=0)
    ids = [r["id"] for r in c.execute("SELECT id FROM images WHERE mime <> 'image/webp'").fetchall()]
    for iid in ids:
        r = c.execute("SELECT data FROM images WHERE id=?", (iid,)).fetchone()
        if not r:
            continue
        data = bytes(r["data"])
        role = c.execute("SELECT (SELECT count(*) FROM posts WHERE image_id=?) AS post,"
                         " (SELECT count(*) FROM users WHERE avatar_id=?) AS avatar", (iid, iid)).fetchone()
        max_side = POST_MAX if role["post"] or not role["avatar"] else AVATAR_MAX
        webp = optimize(data, max_side)
        if webp is None:
            stats["failed"] += 1
            continue
        with db.tx(c):
            new = db.store_image(c, webp)
            c.execute("UPDATE posts SET image_id=? WHERE image_id=?", (new, iid))
            c.execute("UPDATE posts SET thumb_id=? WHERE thumb_id=?", (new, iid))
            c.execute("UPDATE users SET avatar_id=? WHERE avatar_id=?", (new, iid))
            c.execute("DELETE FROM images WHERE id=?", (iid,))
            if role["post"] and thumbnail(webp) is None:  # animated: it's its own (moving) thumbnail
                old = [r["thumb_id"] for r in c.execute("SELECT thumb_id FROM posts WHERE image_id=? AND thumb_id<>?", (new, new))]
                c.execute("UPDATE posts SET thumb_id=? WHERE image_id=?", (new, new))
                for t in old:
                    db.drop_image_if_unused(c, t)
        stats["converted"] += 1
        stats["before"] += len(data)
        stats["after"] += len(webp)
    # Every image post has a thumbnail (older imports may not): make the missing ones.
    for r in c.execute("SELECT p.id, p.image_id, i.data FROM posts p JOIN images i ON i.id=p.image_id "
                       "WHERE p.kind='image' AND p.thumb_id IS NULL").fetchall():
        t = thumbnail(bytes(r["data"]))
        with db.tx(c):
            c.execute("UPDATE posts SET thumb_id=? WHERE id=?", (db.store_image(c, t) if t else r["image_id"], r["id"]))
    return stats
