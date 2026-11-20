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
- **Universal**: Leech Destination (PM / Group / PM + Group), Prefix, Suffix, Remove words, Replace pairs
- **Leech**: Send as Media/Document, Thumbnail, Auto Thumbnail, Leech Caption template, **Caption Font**
  (Default, Bold, Italic, Underline, Strikethrough, Mono/Code, Spoiler, Preformatted), Screenshots, Leech Splits
- Direct set: `/us -s font bold`, `/us -s prefix [GRP]`

Caption variables: `{filename} {size} {duration} {resolution} {ext}`.

## Groups
Leech inside a group and the files are delivered to your bot PM (start the bot once). The group gets a
short "completed" message. Change this in `/us` -> Universal -> Leech Destination.

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
