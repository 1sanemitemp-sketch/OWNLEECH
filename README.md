# Leech Bot

Telegram leech bot (no torrents): direct links via aria2, video sites via yt-dlp, fast multi-session upload.

## Files you edit
- `config.envx` is the SAMPLE (kept in the repo). On your server copy it to `config.env` and edit that file:
  `config.env` is git-ignored, so your secrets never get pushed and `git pull` never overwrites it.
- Railway variables with the same name always override `config.env`.

## /start (everybody)
Shows `ST_TEXT` and two buttons from `ST_BUTTON_1` / `ST_BUTTON_2` (format `Text:URL`).
Every other command does nothing for users who are not authorized. Authorized users and authorized groups
(`AUTHORIZED_CHATS`, `/a`, `/ua`) can use everything; the owner is never limited.

## Commands  (add `COMMAND_SUFFIX=1` to get /leech1 /l1 /us1 /st1 ... , `/start` never changes)
| Command | What |
|---|---|
| `/leech link [-z] [-e] [-n name] [-t thumb_link]` (`/l`) | leech |
| `/usetting` (`/us`) | personal settings |
| `/status` (`/s`) | live status. A new task always posts a fresh status message |
| `/cancel` (`/c`), `/c_ID`, `/cancelall` (`/ca`) | cancel |
| `/stats` (`/st`) | Bot Stats, OS Stats, Repo Stats, Bot Limits |
| `/botsettings` (`/bs`) | **owner only** - change limits live |
| `/authorize` (`/a`), `/unauthorize` (`/ua`) | **owner only** |

## Limits (config.env or /botsettings, 0 = unlimited, owner never limited)
`USER_MAX_TASKS`, `BOT_MAX_TASKS`, `LEECH_LIMIT`, `DIRECT_LIMIT`, `YTDLP_LIMIT` (GB), `DAILY_TASK_LIMIT`,
`UPLOAD_PARALLEL`. There is no queue: when a limit is reached the new task is refused immediately.

## Status
Shows every task at once: Download -> Extract -> Archive -> Split -> Upload, each with its own progress.
Upload shows one overall progress for all files of the task.

## Settings (`/us`)
- Universal: YT-DLP Options, Daily Tasks, Last Bot Used, MediaInfo Mode, User Bot PM (forced)
- Mirror: RClone
- Leech: Send As Media/Document, Thumbnail, Split Size, Equal Splits, Prefix, Suffix, Remove, Replace,
  Caption, Caption Font, Auto Rename, Screenshots

Remove: `Love|Power|R:www\.example\.com[\s_-]*` (case-sensitive, `R:` = regex, `\|` = literal bar)
Replace (one rule per line): `OLD|NEW|CASE|TIMES` e.g. `.| |0` / `Cinemas|` / `HD|FullHD|1|1`
Caption variables: `{filename} {size} {duration} {quality} {languages} {subtitles} {md5_hash}`, `\s`, `\|`,
and after the first `|` replacements `key:new_key:count`. Prefix / Suffix / Caption accept HTML tags.
Auto Rename placeholders: `{name} {year} {season} {episode} {quality} {resolution} {audio} {lib} {ott}
{languages} {subtitles} {shortlang} {shortsub} {duration} {extension} {raw_name} {file_name} {file_caption}`.
Order applied to a file name: Auto Rename -> underscores to spaces -> Remove -> Replace -> Prefix / Suffix.
A name given with `-n` is used exactly as typed.

## Deploy on Railway
1. Upload all files of this folder to the root of your GitHub repo (replace old ones, see the list in the chat).
2. Railway -> Deploy from GitHub repo (the Dockerfile is detected).
3. Variables: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` (the rest is optional).
4. Attach a Volume with mount path `/data` so settings and thumbnails survive redeploys.

## Deploy on a VPS (Docker)
```
git clone <repo-url> bot
cd bot
cp config.envx config.env
nano config.env          # fill API_ID, API_HASH, BOT_TOKEN, OWNER_ID ... then CTRL+O, ENTER, CTRL+X
docker compose up -d --build && docker compose logs -f
```
Settings and thumbnails are kept in the Docker volume `data`. Update later:
`git pull && docker compose up -d --build`. After editing `config.env` run the same `up -d --build`.
