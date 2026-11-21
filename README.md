# Leech Bot (WZML-X style)

Telegram leech bot for direct links and yt-dlp sites. Built on `wzgram` (WzPyro) with a
multi-session fast uploader, live WZML-X style status, per-user settings and PM delivery.

## Commands (every command has a short form)

| Command | Short | What it does |
|---|---|---|
| `/leech <link>` | `/l` | Leech a link to Telegram |
| `/usetting` | `/us` | Personal settings panel |
| `/status` | `/s` | Live task status (pages, refresh) |
| `/cancel [id]` | `/c`, `/c_ID` | Cancel a task |
| `/cancelall` | `/ca` | Cancel all your tasks |
| `/stats` | `/st` | Bot and server stats |
| `/ping` | - | Response time |
| `/help` | `/h` | Command guide |
| `/authorize` | `/a` | Owner: authorize a user / group |
| `/unauthorize` | `/ua` | Owner: remove authorization |

The command menu is registered automatically on every start - nothing to set in BotFather.

Leech arguments: `-z` zip, `-e` extract, `-n new name`, `-t thumbnail link`.

## Settings (`/us`)
- **Universal**: YT-DLP Options, Daily Tasks, Last Bot Used, MediaInfo Mode, User Bot PM (always forced - files
  from a group always go to the user's bot PM, users cannot change it)
- **Mirror**: RClone (upload `rclone.conf`; stored only for now)
- **Leech**: Send As Media/Document, Thumbnail, Leech Split Size, Equal Splits, Prefix, Suffix, Remove, Replace,
  Caption, Caption Font, **Auto Rename**, Screenshots
- The **Reset Setting** button only appears after you changed something and asks Yes / No first.
- Direct set: `/us -s font bold`, `/us -s prefix [GRP]\s`, reply to a photo with `/us -s thumb`

## Thumbnails (HD)
Priority: `-t` link (this leech only) > your saved thumbnail > a frame taken from the video itself.
Thumbnails are kept in HD (`THUMB_MAX`, default 1280 px).

## Auto Rename
Set a format in `/us` -> Leech -> Leech Auto Rename. Placeholders:
`{name} {year} {season} {episode} {quality} {resolution} {audio} {lib} {ott} {languages} {subtitles}
{shortlang} {shortsub} {duration} {extension} {raw_name} {file_name} {file_caption}`

- `{name}` never contains site tags like `[Toonworld4all]`.
- `{resolution}` is plain `720p` / `1080p` (no bit depth).
- `{shortlang}`: one audio = that language (`Hindi`), two = `Dual`, three or more = `Multi`.
- Order applied to every file: Auto Rename -> underscores to spaces -> Remove / Replace -> Prefix / Suffix.
  A name given with `-n` is always used exactly as typed.

Caption variables: `{filename} {size} {duration} {resolution} {ext}` (safe HTML tags like `<b>` are allowed).

## Deploy on Railway
1. Push these files to GitHub (files at the repo root).
2. Railway -> New Project -> Deploy from GitHub repo (the Dockerfile is detected automatically).
3. Variables: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` (see `.env.example` for optional ones).
4. Attach a Volume at `/data` so settings, thumbnails and the authorized list survive redeploys.

## Upload speed
- `wzgram` + WarpCrypto (Rust crypto) + uvloop.
- Parallel uploader: `UPLOAD_SESSIONS` (default 8) x `UPLOAD_WORKERS` (default 4). If speed is not
  better on your server, try 12 x 4; if you see flood errors, lower it. `FAST_UPLOAD=0` disables it.
- If the fast path ever fails, the file is uploaded with the normal library uploader.
- Real speed is limited by your server's network and Telegram's datacenter. Bots can upload max 2GB per
  file; bigger files are split automatically.
