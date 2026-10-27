# social board

A small old-reddit-style forum. You can make communities, post text or images with captions, comment, and vote.

It stores everything in **PostgreSQL**. There's no Redis or other cache server; the only caching is a 30-second in-memory copy of the community strip at the top of each page. It needs **Python 3.10+** plus two packages: `psycopg` (database) and `Pillow` (image conversion). There's no JS framework and no build step. Pages are rendered on the server, and a typical page is a few KB of HTML plus one cached stylesheet (~2.7 KB gzipped) and an optional 2 KB script.

## Setup

1. Have a PostgreSQL server (13 or newer) and an empty database for the board. The board creates its own tables and the `citext` extension on first start, so the database user must own the database.
2. Install the driver into a project-local virtualenv:
   ```sh
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
   `board.py` switches into `.venv` by itself, so you can keep typing `python3 board.py`.
3. Point the board at the database and start it:
   ```sh
   export BOARD_DB=postgresql://user:password@localhost/social_board   # or pass --db URL
   python3 board.py serve                 # http://127.0.0.1:8080 and your LAN IP
   python3 board.py serve --host 127.0.0.1 --port 9000 --name "my board"
   ```
   Instead of exporting `BOARD_DB` every time, you can put the URL in `data/database_url`, a one-line file; keep it `chmod 600` since it holds the password. Without any of these, the board uses `postgresql:///social_board`: a database called `social_board`, reached through the local Unix socket as your OS user.

## Accounts

- You sign up with only a username and a password. No email is needed.
- At signup you're shown a **12-word recovery phrase**, and it's shown only that once. To reset a forgotten password, go to `/recover` and enter your username and phrase. After a recovery you get a new phrase and the old one stops working. You can also generate a new phrase in preferences.
- Passwords and recovery phrases are hashed with **scrypt** (N=2^17, r=8, p=1, 16-byte random salt), following OWASP's recommendation. The cost parameters are stored with each hash, so old hashes are upgraded automatically at login if you ever raise them. Only a SHA-256 of each session token is stored.
- Logins, signups and recoveries are rate limited per IP. Posting is rate limited per user. Every form has a CSRF token, cookies are `HttpOnly; SameSite=Lax`, and pages are served with a strict Content-Security-Policy.
- Viewing is open to everyone. Posting, commenting, voting and creating communities require a login.

## Custom prefixes

Each user picks their own prefix to replace `u/` (for example `dude/alice`), and each community's creator picks one to replace `r/` (for example `board/gaming`). A prefix can be 1–8 letters, digits or `_`.

Why 8: PostgreSQL stores text with variable length, so storage wasn't the limit. 8 ASCII characters is still a full word, and it keeps taglines like `submitted by xxxxxxxx/username to xxxxxxxx/community` on one line in the compact old-reddit layout. The limit is enforced in the schema with a `CHECK` constraint too, so a merged database can't bring in longer prefixes. To change it, edit `PREFIX_MAX` in `sb/db.py`.

The prefix is also the address: a community shown as `gold/UWEC` lives at `/gold/UWEC`, and its sort tabs and edit page live under it (`/gold/UWEC/new`, `/gold/UWEC/edit`). User pages work the same way. When an admin locks prefixes, the locked prefix is used everywhere: in names, links, page addresses and form targets. Old `/r/name` and `/u/name` links, and links from before a prefix change, redirect to the current address.

**Telling communities apart:** community names are bold in post taglines. A community's creator (or an admin) can give it a **name color** on the create or edit form. That color is used wherever the community's name appears: the front page, taglines, the sidebar, the communities list and the strip at the top of every page. Colors that would be hard to read in either theme are refused: too light for the white background (contrast below 3:1), or too dark to tell apart on the dark background (below 2:1, which rules out black, navy and charcoal). Saturated colors like pure blue or purple are allowed, and dark mode lightens them automatically. Because the page's Content-Security-Policy forbids inline `style=""`, each page carries one small `<style>` block containing only the colors it uses, allowed through by a per-response nonce.

**Admins' names are red** everywhere they appear.

A user and a community can share a name, but not an address. Any prefix choice that would put both at the same `/prefix/name` is refused, whether a user, a community creator or an admin makes it.

## Formatting (markdown)

Posts, comments, community descriptions and bios use markdown. A **formatting help** cheat sheet sits under the post and comment boxes.

| you type | you get |
|---|---|
| `*italic*` `**bold**` `~~strike~~` | *italic* **bold** ~~strike~~ |
| `[text](https://example.com)` | a link; bare `https://` addresses are linked automatically |
| `# Heading` … `###### Heading` | headings |
| `- item`, `1. item` | lists; indent two spaces to nest |
| `> quote` | quotes, which can contain other formatting and nest (`>>`) |
| `` `code` ``, or ```` ``` ```` on its own line before and after a block | code |
| `---` | a horizontal line |
| `a \| b` then `--- \| ---:` then rows | tables, with `:---:` / `---:` for centre / right alignment |
| `\*` | a literal `*` (backslash escapes) |

- **Safe by design.** The renderer is about 150 lines of Python in `sb/tpl.py`, with no markdown library. Every piece of text is escaped first, and the only tags in the output are ones the renderer itself creates, so typed `<script>` just shows as text.
- **Links** must be `http(s)://`, `mailto:` or on the board itself; `javascript:` and `data:` links are left as plain text.
- **Spammed formatting can't slow it down.** Styles and link texts can span at most 300 characters, so a 40,000-character post full of unclosed `**` renders in about a tenth of a second, and rendered text is cached.
- A blank line starts a new paragraph; a single line break is kept as a line break.

## Awards

Like Reddit, you can give **awards** to other people's posts and comments, using **award karma**. Award karma is a spendable balance, kept separately from regular karma:

| | regular karma | award karma |
|---|---|---|
| what it is | sum of your posts' and comments' scores | a balance you spend |
| starts at | 0 | **100** for every account |
| upvotes on your posts/comments | +1 | +1, paid **once per person per item**: un-voting and re-voting doesn't pay again, and your own automatic upvote doesn't count |
| downvotes | −1 | no effect |
| giving an award | no effect | − the award's price |

| award | price | recipient also gets |
|---|---|---|
| Helpful | 25 | — |
| Wholesome | 30 | — |
| Silver | 50 | — |
| Gold | 100 | +25 award karma |
| Platinum | 500 | +100 award karma |

- **Giving:** click **give award** under any post or comment that isn't yours. The award page shows your balance and greys out awards you can't afford yet.
- **Spending is safe under concurrency:** two awards sent at the same instant can't both spend the same karma, and the database also refuses to let a balance go negative.
- **Where awards show:** as small icons with counts next to the post or comment.
  - Profiles have a public **awards received** trophy case.
  - Your balance shows in the header next to your karma, and on your own profile; only you see it.
- **Deleted accounts:** if a giver or receiver deletes their account, the awards they gave or got stay visible.
- **Changing awards:** the award list (names, prices, bonuses, icons) is one table at the top of `sb/awards.py`. Icons are SVGs in `static/`.
- **Moving boards:** awards, balances and payout history are included in `export` / `merge`.

## Live front page

When the front page is sorted by **new** (first page) and you're at the top, new posts slide in as they're posted, highlighted briefly. If you've scrolled down, the list stays still and a "N new posts ↑" button appears instead; click it, or scroll back to the top, to see them. The hot and top sorts never update live. Users can turn this off under **preferences → display**.

How it works: creating a post runs PostgreSQL's `pg_notify` inside the same transaction, so the event fires only once the post is saved. Each server process keeps one `LISTEN` connection and passes events to open front pages over server-sent events (`/live`). There's no Redis or other message broker, and several server processes on one database all see every post. The stream only stays open while the tab is visible. When you come back, it catches up on posts from the past hour that you missed.

## Keyboard and accessibility

- **Ctrl+Enter** (⌘+Enter on Mac) sends whatever form you're typing in: comments, replies, new posts, settings. A plain Enter still makes a new line, and an empty required field shows the browser's usual warning instead of sending.
- The first **Tab** on any page reveals a **"Skip to main content"** link. Press Enter on it to jump past the header and community strip straight to the posts.
- Every focused element gets a clear orange outline. Thumbnails, which link to the same place as the title next to them, are left out of the tab order, so each post takes fewer Tab presses.
- Pages use landmark elements (`header`, `nav`, `main`, `aside`, `footer`), which screen readers can jump between. Vote buttons are labelled "upvote" and "downvote" and report whether they're pressed. The current sort tab is marked as the current page.

## Dark mode

Dark mode is on by default, for visitors and new accounts. Users can switch to the light theme under **preferences → display**. Community name colors are chosen to be readable on white, so in dark mode each one is automatically lightened just enough to be readable on the dark background (WCAG 4.5:1); light mode shows the creator's exact color.

## Advertisements

On the front page, community pages and post pages, a square under the sidebar's info box shows a random image or video from the `ads/` folder.

- **Images:** PNG, JPEG, GIF or WebP.
- **Videos:** MP4 (H.264 plays everywhere), WebM or Ogg. They autoplay **with sound** and loop, with no controls, inline on phones too. Browsers refuse to autoplay sound until a visitor has interacted with the site. When that happens the video plays muted, and the sound comes on at the visitor's first click, tap or key press anywhere on the page. Visitors whose system is set to reduce motion get the video paused instead.
- QuickTime `.mov` files aren't reliably playable in browsers, so convert them first, for example with `ffmpeg -i in.mov -c:v libx264 -pix_fmt yuv420p -movflags +faststart out.mp4`.
- Videos are streamed with HTTP byte ranges in 2 MB pieces, so a large file never has to be loaded into memory at once.
- Through **admin → images**, ad images can be up to 5 MB and videos up to 60 MB. You can also copy bigger files into the folder directly.
- **Click-through links:** under each ad in **admin → images**, enter a web address (`http://` or `https://`) and click **add link**. Clicking that ad then opens the address in a new tab, and the label reads "advertisement · example.com" so visitors can see where it goes. Clear the field and click **update** to remove the link. Links are stored in `ads/.links.json`, next to the files, so they move with the folder. Deleting an ad deletes its link. Choose a different folder with `--ads DIR`. It's labelled "advertisement". If the folder is empty, visitors see nothing, and admins see an empty "ad space" box that links to where they can add images.

## Default avatars

Each new account gets a random image from the `avatars/` folder. Choose a different folder with `--avatars DIR`. Drop PNG, JPEG, GIF or WebP files (2 MB max) in there; the folder is re-read at every signup, so no restart is needed. Admins can also manage both the avatar and ad folders in the browser: **admin → images** shows previews and lets you upload several files at once or remove one. Files that aren't supported images, or are over the size limit, are skipped with a message. The chosen image is copied into the database, so moving the database never breaks an avatar. Users can upload their own avatar, re-roll a random default, or remove theirs in preferences.

## Moving posts to another instance

Everything, including images, is in the board's PostgreSQL database. To move it, write a **snapshot**: a single portable file holding the users, communities, posts, comments, votes, images and platform settings, but no logins.

```sh
python3 board.py --db URL_A export snapshot.db     # safe while board A is running
python3 board.py --db URL_B merge snapshot.db      # safe while board B is running
```

The snapshot is an SQLite file, so you can carry it on a USB stick and open it with any SQLite tool. Python reads it without extra software. `merge` also accepts the `data/board.db` file left by the old SQLite version of this board.

- To copy a whole board, merge the snapshot into a new, empty database.
- To combine boards, merge into one that already has content. IDs are random strings instead of 1, 2, 3…, so two boards' rows don't collide. Rows that are already present are skipped, so merging twice is harmless.
  - Communities with the same name are combined.
  - If two different people have the same username, the incoming one is renamed (`alice` → `alice_2`) and keeps their password and phrase.
  - Admin rights don't carry over in a merge. Re-grant them with `board.py admin`.
  - The target keeps any platform settings it has already saved. Settings it never set come from the snapshot.

For a byte-for-byte copy, standard `pg_dump` and `pg_restore` work too.

## Running several boards (same machine or same LAN)

- **Ports:** if 8080 is taken, the server moves up to the next free port and prints it. Pass `--port` to require a specific one.
- **Cookies:** browsers share cookies across ports on the same host. To keep logins separate, each instance has a random ID (`data/instance.json`) and uses it in its cookie name.
- **Copied data folders:** the instance ID is tied to the hostname and folder path, so a copied `data/` folder gets a new identity on first start. Sessions are bound to the instance that issued them, and snapshots never include them.
- **Databases:** give each board its own database, either a separate database name or a separate PostgreSQL server. Two boards pointed at the *same* database aren't corrupted, because PostgreSQL handles concurrent writers. They simply become one board with two front ends, each with its own logins.

## Admin

```sh
python3 board.py admin alice     # toggle admin from the command line
python3 board.py ban spammer     # toggle a ban from the command line
```

Admins see an **admin** link in the header that opens `/admin`, the platform settings page:

- **Site:** the site name and the front-page sidebar blurb.
- **User names:** the default user prefix (normally `u`) and whether users may choose their own. When custom prefixes are off, everyone is shown with the default, but their stored choice isn't deleted, so it comes back if you turn custom prefixes on again. An "also switch existing users" box moves everyone still on the old default to the new one.
- **Community names:** the same options for the `r` prefix.
- **Access:** open or close sign-ups, and choose whether any logged-in user or only admins can create communities.
- **Admins:** grant or remove admin rights by username. You can't remove yourself.
- **Users:** look up a user by name, and see the list of banned users with unban buttons.
- **Images:** upload or remove advertisement images and default avatars.

These settings are stored in the database. `serve --name` only sets the default site name, and a name saved in `/admin` overrides it.

### God mode

Admins get a red **god mode** link in every page's footer, which opens `/admin/god`. Look up a username to see a summary card: total karma and how it's made up, award karma, posts, comments, awards and active logins. Then act on that user:

| tile | what it does |
|---|---|
| karma | add or subtract any amount. Stored as an admin adjustment on top of vote karma, shown on their profile as "bonus karma" |
| award karma | add or subtract (stops at 0), or set an exact balance |
| rename | change their username; their pages move to the new name |
| prefix | set their prefix (kept even while prefixes are locked) |
| profile | clear their bio; give them a random default avatar or remove theirs |
| sessions | log them out on every device |
| password | set a random temporary password, shown to you **once** (never stored or logged), and log them out; their recovery phrase still works |
| admin rights | make or remove an admin |
| ban | ban or unban, optionally deleting their posts and comments |
| delete account | delete the account, optionally with their posts and comments |

You can't ban, delete or demote yourself. Other admins can't be banned or deleted until their admin rights are removed. Every action is recorded in an **audit log** at the bottom of the page: who, to whom, what, and when. It keeps names rather than ids, so entries survive renames and deletions. Rows are never removed; the page shows the latest 25.

### Banning and deleting users

Admins see **admin tools** in the sidebar of other users' profiles:

- **Ban:** the user is logged out everywhere and can't log in, recover the account, post, comment or vote. Their name stays taken, so they can't sign up again under it, and their posts stay up. Tick "also delete all their posts and comments" to remove those too. A banned user is only told about the ban after entering the correct password, so a ban doesn't reveal which accounts exist. Unban from the profile or from `/admin`.
- **Delete account:** the account is removed and the name becomes free. Their posts and comments stay up with the author shown as `[deleted]`, unless you tick the box to remove them. Their votes are removed and the scores they affected are recalculated. Communities they created stay up and are moderated by admins.

You can't ban or delete yourself or another admin. Remove their admin rights first.

Admins can delete any post or comment. A community's creator can edit its title, description, prefix and color, using the **Edit community** button at the top of the sidebar on the community's pages, and can delete posts and comments in it. Authors can delete their own posts and comments.

## Layout

```
board.py          CLI: serve / export / merge / admin / ban
requirements.txt  psycopg and Pillow (the only dependencies)
sb/db.py          PostgreSQL schema, connection pool, image storage, export + merge
sb/settings.py    platform settings (edited at /admin)
sb/media.py       image optimization: everything becomes a right-sized, compressed WebP
sb/god.py         god mode: the admin dashboard for editing any user, plus its audit log
sb/awards.py      award types and prices, award karma (earning on upvotes, spending on awards)
sb/live.py        real-time new-post events (PostgreSQL LISTEN/NOTIFY → server-sent events)
sb/security.py    scrypt hashing, recovery phrases, rate limiting
sb/web.py         HTTP server, routing, sessions, CSRF, static files
sb/views.py       pages
sb/tpl.py         layout + escaping + light markdown
static/           style.css, app.js (optional: in-place voting, live front page, ctrl+enter, ad sound), award icons
avatars/          default avatar images (also managed at admin → images)
ads/              advertisement images (also managed at admin → images)
data/             created at first run: instance.json (this instance's identity)
```

Without JavaScript everything still works; votes just reload the page.

## Images are WebP

Every image the board stores or serves is a compressed **WebP**, sized for where it's shown:

| what | max size | notes |
|---|---|---|
| post images | 2048 px | quality 80 |
| listing thumbnails | 140×100 | cropped, made by the server |
| avatars | 256 px | uploads up to 8 MB are fine; they're shrunk |
| ads | 600 px | shown at 300 px, so sharp on high-DPI screens |

- **Animations stay animated.** Animated GIFs become animated WebP, encoded lossless and lossy with the smaller result kept, which for GIFs is usually lossless and smaller than the GIF. An animated post is its own (moving) thumbnail.
- **Phone photos** are rotated upright, and their metadata (camera, **GPS location**) is stripped.
- **A WebP that's already within size is never re-encoded,** so quality doesn't erode with repeated saves or restarts.
- Corrupt files and "decompression bombs" (tiny files that expand to enormous images) are refused.

**Converting what's already there** happens automatically at startup and only does work the first time:

- **Database images** are converted and given new ids, so browsers' cached copies don't linger, and every post and avatar is moved to the new image.
- **Files in `ads/` and `avatars/`** are converted in place. The originals go to a hidden `.originals/` folder in each, so nothing is lost. Files dropped into those folders later are converted on the next start, and uploads through **admin → images** are converted immediately.
- **Videos** are left alone.

The download button on a post saves the stored WebP.
