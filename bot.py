import asyncio
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
from glob import glob
from pathlib import Path

import aiohttp
import psutil
import yt_dlp
from PIL import Image
from pyrogram import Client, enums, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import BotCommand
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Kb

import config as C

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("leechbot")

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

app = Client(
    "leechbot",
    api_id=C.API_ID,
    api_hash=C.API_HASH,
    bot_token=C.BOT_TOKEN,
    parse_mode=enums.ParseMode.HTML,
    in_memory=True,
    workers=16,
    max_concurrent_transmissions=8,
)

BOOT = time.time()
SESSION: aiohttp.ClientSession = None
SEM = asyncio.Semaphore(C.MAX_CONCURRENT)
TASKS = {}
PENDING = {}
BG = set()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

# ------------------------------------------------------------------ helpers


def hsize(n):
    n = float(n or 0)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def htime(s):
    s = int(s)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return (f"{d}d " if d else "") + (f"{h}h " if h else "") + (f"{m}m " if m else "") + f"{s}s"


def bar(p):
    f = int(min(max(p, 0), 100) / 10)
    return "■" * f + "□" * (10 - f)


def spawn(coro):
    t = asyncio.ensure_future(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)
    return t


async def run(*cmd, cwd=None):
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
    )
    out, err = await p.communicate()
    return p.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


def list_files(d):
    out = []
    for root, _, fs in os.walk(d):
        for f in fs:
            if not f.endswith(".aria2"):
                out.append(os.path.join(root, f))
    return sorted(out)


def authorized(m):
    uid = m.from_user.id if m.from_user else 0
    ids = C.AUTHORIZED_CHATS | AUTH
    return uid == C.OWNER_ID or uid in ids or m.chat.id in ids


# ------------------------------------------------------------------ user settings

DATA = Path(C.DATA_DIR)
THUMBS = DATA / "thumbs"
THUMBS.mkdir(parents=True, exist_ok=True)
PERSISTENT = os.path.ismount(C.DATA_DIR)
DB = DATA / "users.json"
try:
    USERS = json.loads(DB.read_text())
except Exception:
    USERS = {}
AUTH_DB = DATA / "auth.json"
try:
    AUTH = set(json.loads(AUTH_DB.read_text()))
except Exception:
    AUTH = set()


def save_auth():
    tmp = AUTH_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(AUTH)))
    os.replace(tmp, AUTH_DB)


DEFAULT = {"as_doc": False, "prefix": "", "suffix": ""}


def us(uid):
    return {**DEFAULT, **USERS.get(str(uid), {})}


def us_set(uid, k, v):
    USERS.setdefault(str(uid), {})[k] = v
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(USERS))
    os.replace(tmp, DB)


def thumb_of(uid):
    p = THUMBS / f"{uid}.jpg"
    return str(p) if p.exists() else None


def prep_thumb(src, dst):
    im = Image.open(src).convert("RGB")
    im.thumbnail((320, 320))
    im.save(dst, "JPEG", quality=85)


def us_text(uid):
    s = us(uid)
    return (
        "<b>⚙️ User Settings</b>\n\n"
        f"<b>Upload as:</b> {'Document' if s['as_doc'] else 'Media'}\n"
        f"<b>Thumbnail:</b> {'✅ Set' if thumb_of(uid) else '❌ Not set'}\n"
        f"<b>Prefix:</b> <code>{html.escape(s['prefix']) or 'None'}</code>\n"
        f"<b>Suffix:</b> <code>{html.escape(s['suffix']) or 'None'}</code>"
    )


def us_kb(uid):
    s = us(uid)
    return Kb(
        [
            [Btn(f"📤 Upload as: {'Document' if s['as_doc'] else 'Media'}", callback_data=f"us:{uid}:type")],
            [
                Btn("🖼 Thumbnail", callback_data=f"us:{uid}:thumb"),
                Btn("🗑 Del Thumb", callback_data=f"us:{uid}:delthumb"),
            ],
            [
                Btn("🔤 Prefix", callback_data=f"us:{uid}:prefix"),
                Btn("🔡 Suffix", callback_data=f"us:{uid}:suffix"),
            ],
            [Btn("✖️ Close", callback_data=f"us:{uid}:close")],
        ]
    )


# ------------------------------------------------------------------ task model


class Cancelled(Exception):
    pass


class Task:
    def __init__(self, user, chat_id, link, z, e, name, thumb_link):
        self.id = secrets.token_hex(3)
        self.uid = user.id
        self.mention = user.mention
        self.chat_id = chat_id
        self.link = link
        self.zip = z
        self.extract = e
        self.new_name = name
        self.thumb_link = thumb_link
        self.thumb = None
        self.dir = os.path.join(C.DOWNLOAD_DIR, self.id)
        self.status = "Queued"
        self.engine = "-"
        self.name = ""
        self.total = 0
        self.done = 0
        self.speed = 0
        self.cancel = False
        self.gid = None
        self.start = time.time()
        self.msg = None
        self.finished = False
        self.lock = asyncio.Lock()
        self._c = 0
        self._t = time.time()


def render(t):
    pct = (t.done / t.total * 100) if t.total else 0
    eta = ((t.total - t.done) / t.speed) if t.speed else 0
    return (
        f"<b>{html.escape(t.name or 'Fetching info...')}</b>\n"
        f"<b>┌ Status:</b> {t.status} | <b>Engine:</b> {t.engine}\n"
        f"<b>├</b> {bar(pct)} {pct:.1f}%\n"
        f"<b>├ Processed:</b> {hsize(t.done)} of {hsize(t.total)}\n"
        f"<b>├ Speed:</b> {hsize(t.speed)}/s | <b>ETA:</b> {htime(eta)}\n"
        f"<b>├ Elapsed:</b> {htime(time.time() - t.start)}\n"
        f"<b>├ User:</b> {t.mention}\n"
        f"<b>└ Cancel:</b> /cancel_{t.id}"
    )


def sys_stats():
    os.makedirs(C.DOWNLOAD_DIR, exist_ok=True)
    du = shutil.disk_usage(C.DOWNLOAD_DIR)
    dl = sum(t.speed for t in TASKS.values() if t.status == "Downloading")
    ul = sum(t.speed for t in TASKS.values() if t.status.startswith("Uploading"))
    return (
        f"\n\n<b>CPU:</b> {psutil.cpu_percent()}% | <b>RAM:</b> {psutil.virtual_memory().percent}% "
        f"| <b>FREE:</b> {hsize(du.free)}\n"
        f"<b>DL:</b> {hsize(dl)}/s | <b>UL:</b> {hsize(ul)}/s | <b>UP:</b> {htime(time.time() - BOOT)}"
    )


def status_text():
    if not TASKS:
        return "<b>No active tasks.</b>" + sys_stats()
    ts = list(TASKS.values())[:5]
    return "\n\n".join(render(t) for t in ts) + f"\n\n<b>Tasks:</b> {len(TASKS)}" + sys_stats()


def cancel_kb(t):
    return Kb([[Btn("❌ Cancel", callback_data=f"cx:{t.id}")]])


async def updater(t):
    while not t.finished:
        await asyncio.sleep(5)
        if t.finished:
            break
        async with t.lock:
            if t.finished:
                break
            try:
                await t.msg.edit_text(render(t), reply_markup=cancel_kb(t))
            except MessageNotModified:
                pass
            except Exception as e:
                log.debug("edit fail %s", e)


async def final_edit(t, text):
    async with t.lock:
        t.finished = True
        try:
            await t.msg.edit_text(text, reply_markup=None)
        except Exception as e:
            log.warning("final edit fail %s", e)


# ------------------------------------------------------------------ aria2

ARIA_URL = "http://127.0.0.1:6800/jsonrpc"


async def rpc(method, *params):
    payload = {"jsonrpc": "2.0", "id": "x", "method": method, "params": [f"token:{C.ARIA_SECRET}", *params]}
    async with SESSION.post(ARIA_URL, json=payload) as r:
        d = await r.json()
    if "error" in d:
        raise RuntimeError(d["error"].get("message", "aria2 error"))
    return d["result"]


async def start_aria():
    await asyncio.create_subprocess_exec(
        "aria2c",
        "--enable-rpc",
        f"--rpc-secret={C.ARIA_SECRET}",
        "--rpc-listen-port=6800",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--max-concurrent-downloads=10",
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--follow-torrent=false",
        "--check-certificate=false",
        "--disk-cache=64M",
        "--max-tries=5",
        "--retry-wait=2",
        "--connect-timeout=30",
        "--timeout=60",
        "--summary-interval=0",
        "--console-log-level=error",
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    for _ in range(30):
        try:
            await rpc("aria2.getVersion")
            return
        except Exception:
            await asyncio.sleep(0.5)
    raise RuntimeError("aria2 start hoy nai")


async def dl_aria(t):
    t.engine = "Aria2"
    gid = await rpc("aria2.addUri", [t.link], {"dir": t.dir})
    t.gid = gid
    while True:
        if t.cancel:
            try:
                await rpc("aria2.remove", gid)
            except Exception:
                pass
            raise Cancelled
        st = await rpc(
            "aria2.tellStatus", gid, ["status", "totalLength", "completedLength", "downloadSpeed", "errorMessage", "files"]
        )
        t.total = int(st["totalLength"])
        t.done = int(st["completedLength"])
        t.speed = int(st["downloadSpeed"])
        if st.get("files") and st["files"][0].get("path"):
            t.name = os.path.basename(st["files"][0]["path"])
        s = st["status"]
        if s == "complete":
            return
        if s == "error":
            raise RuntimeError(st.get("errorMessage", "download error"))
        if s == "removed":
            raise Cancelled
        await asyncio.sleep(2)


def ytdl_sync(t):
    def hook(d):
        if t.cancel:
            raise yt_dlp.utils.DownloadCancelled("cancel")
        if d["status"] == "downloading":
            t.total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            t.done = d.get("downloaded_bytes", 0)
            t.speed = d.get("speed") or 0
            t.name = os.path.basename(d.get("filename", "")) or t.name

    opts = {
        "outtmpl": f"{t.dir}/%(title).150B.%(ext)s",
        "progress_hooks": [hook],
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "quiet": True,
        "no_warnings": True,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "retries": 5,
    }
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([t.link])


async def dl_ytdlp(t):
    t.engine = "yt-dlp"
    try:
        await asyncio.to_thread(ytdl_sync, t)
    except Exception:
        if t.cancel:
            raise Cancelled
        raise


async def is_direct(link):
    try:
        async with SESSION.get(
            link, headers=UA, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            return not r.headers.get("Content-Type", "").lower().startswith("text/html")
    except Exception:
        return True


async def download(t):
    if await is_direct(t.link):
        try:
            await dl_aria(t)
            return
        except Cancelled:
            raise
        except Exception as e1:
            try:
                await dl_ytdlp(t)
            except Cancelled:
                raise
            except Exception:
                raise RuntimeError(str(e1))
    else:
        await dl_ytdlp(t)


# ------------------------------------------------------------------ processing

ARCH = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso")


async def extract(t):
    for _ in range(2):  # 2 pass: .tar.gz er jonno
        found = False
        for f in list_files(t.dir):
            if f.lower().endswith(ARCH):
                found = True
                out = os.path.join(t.dir, "extracted")
                rc, _, err = await run("7z", "x", f, f"-o{out}", "-y")
                if rc != 0:
                    raise RuntimeError("Extract fail: " + err[-200:])
                os.remove(f)
        if not found:
            break
    return list_files(t.dir)


async def make_zip(t, files):
    base = t.new_name or (Path(files[0]).stem if len(files) == 1 else (t.name or t.id))
    base = re.sub(r"\.zip$", "", base, flags=re.I)
    out_dir = t.dir + "_z"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, base + ".zip")
    rc, _, err = await run("7z", "a", "-tzip", "-mx=1", out, ".", cwd=t.dir)
    if rc != 0:
        raise RuntimeError("Zip fail: " + err[-200:])
    shutil.rmtree(t.dir, ignore_errors=True)
    t.dir = out_dir
    return [out]


def rename_one(t, f):
    ext = os.path.splitext(f)[1]
    n = t.new_name if os.path.splitext(t.new_name)[1] else t.new_name + ext
    new = os.path.join(os.path.dirname(f), n)
    os.rename(f, new)
    return new


def decorate(path, s):
    if not (s["prefix"] or s["suffix"]):
        return path
    d, n = os.path.split(path)
    stem, ext = os.path.splitext(n)
    new = os.path.join(d, f"{s['prefix']}{stem}{s['suffix']}{ext}")
    os.rename(path, new)
    return new


async def split_big(path):
    if os.path.getsize(path) <= C.SPLIT_SIZE:
        return [path]
    rc, _, err = await run("split", "-b", str(C.SPLIT_SIZE), "-d", "--numeric-suffixes=1", "-a", "3", path, path + ".")
    if rc != 0:
        raise RuntimeError("Split fail: " + err[-200:])
    os.remove(path)
    return sorted(glob(glob_escape(path) + ".[0-9][0-9][0-9]"))


def glob_escape(p):
    return re.sub(r"([\[\]\*\?])", r"[\1]", p)


async def probe(path):
    rc, out, _ = await run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", path,
    )
    try:
        j = json.loads(out)
        s = (j.get("streams") or [{}])[0]
        return int(float(j["format"]["duration"])), int(s.get("width", 0)), int(s.get("height", 0))
    except Exception:
        return 0, 0, 0


async def video_thumb(path, dur, tid):
    out = os.path.join(C.DOWNLOAD_DIR, f"{tid}_th.jpg")
    rc, _, _ = await run(
        "ffmpeg", "-y", "-ss", str(max(dur // 2, 0)), "-i", path, "-frames:v", "1", "-vf", "scale=320:-1", out
    )
    return out if rc == 0 and os.path.exists(out) else None


async def fetch_thumb(t):
    try:
        raw = os.path.join(C.DOWNLOAD_DIR, f"{t.id}_raw")
        async with SESSION.get(t.thumb_link, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.read()
        with open(raw, "wb") as f:
            f.write(data)
        out = os.path.join(C.DOWNLOAD_DIR, f"{t.id}_thumb.jpg")
        prep_thumb(raw, out)
        os.remove(raw)
        t.thumb = out
    except Exception as e:
        log.warning("thumb link fail: %s", e)


# ------------------------------------------------------------------ upload


async def send(t, f, thumb, as_doc, prog):
    cap = f"<code>{html.escape(os.path.basename(f))}</code>"
    mime = mimetypes.guess_type(f)[0] or ""
    size = os.path.getsize(f)
    kw = dict(chat_id=t.chat_id, caption=cap, progress=prog)
    if not as_doc and mime.startswith("video"):
        dur, w, h = await probe(f)
        th = thumb or await video_thumb(f, dur, t.id)
        return await app.send_video(video=f, duration=dur, width=w, height=h, thumb=th, supports_streaming=True, **kw)
    if not as_doc and mime.startswith("audio"):
        return await app.send_audio(audio=f, thumb=thumb, **kw)
    if not as_doc and mime.startswith("image") and size < 10 * 1024 * 1024:
        return await app.send_photo(photo=f, **kw)
    return await app.send_document(document=f, thumb=thumb, force_document=True, **kw)


async def upload_all(t, files, s):
    thumb = t.thumb or thumb_of(t.uid)

    async def prog(cur, total):
        if t.cancel:
            app.stop_transmission()
        now = time.time()
        t.done = cur
        if now - t._t >= 1:
            t.speed = (cur - t._c) / (now - t._t)
            t._c, t._t = cur, now

    for i, f in enumerate(files, 1):
        if t.cancel:
            raise Cancelled
        t.name = os.path.basename(f)
        t.total = os.path.getsize(f)
        t.done = t.speed = t._c = 0
        t._t = time.time()
        t.status = f"Uploading ({i}/{len(files)})"
        await send(t, f, thumb, s["as_doc"], prog)


async def run_task(t):
    s = us(t.uid)
    try:
        async with SEM:
            if t.cancel:
                raise Cancelled
            os.makedirs(t.dir, exist_ok=True)
            if t.thumb_link:
                await fetch_thumb(t)
            t.status = "Downloading"
            await download(t)
            if t.cancel:
                raise Cancelled
            t.status = "Processing"
            t.speed = 0
            files = list_files(t.dir)
            if not files:
                raise RuntimeError("Kono file download hoy nai")
            if not t.name:
                t.name = os.path.basename(files[0])
            if t.extract:
                files = await extract(t)
            if t.zip:
                files = await make_zip(t, files)
            elif t.new_name and len(files) == 1:
                files = [rename_one(t, files[0])]
            files = [decorate(f, s) for f in files]
            final = []
            for f in files:
                final += await split_big(f)
            await upload_all(t, final, s)
            total = sum(os.path.getsize(f) for f in final)
            await final_edit(
                t,
                f"✅ <b>Leech Completed</b>\n\n<b>Name:</b> <code>{html.escape(t.name)}</code>\n"
                f"<b>Files:</b> {len(final)}\n<b>Size:</b> {hsize(total)}\n"
                f"<b>Time:</b> {htime(time.time() - t.start)}\n<b>By:</b> {t.mention}",
            )
    except Cancelled:
        await final_edit(t, f"❌ <b>Task cancelled.</b> ({t.mention})")
    except Exception as e:
        if t.cancel:
            await final_edit(t, f"❌ <b>Task cancelled.</b> ({t.mention})")
        else:
            log.exception("task failed")
            await final_edit(t, f"❌ <b>Failed:</b> <code>{html.escape(str(e)[:500])}</code>")
    finally:
        t.finished = True
        TASKS.pop(t.id, None)
        shutil.rmtree(t.dir, ignore_errors=True)
        shutil.rmtree(t.dir + "_z", ignore_errors=True)
        for p in glob(os.path.join(C.DOWNLOAD_DIR, f"{t.id}_*")):
            try:
                os.remove(p)
            except Exception:
                pass


# ------------------------------------------------------------------ command parsing

FLAGS = {"-z", "-zip", "-e", "-n", "-t"}


def parse(tokens):
    link = None
    z = e = False
    name = thumb = None
    i = 0
    while i < len(tokens):
        tk = tokens[i]
        if tk in ("-z", "-zip"):
            z = True
        elif tk == "-e":
            e = True
        elif tk == "-n":
            parts = []
            i += 1
            while i < len(tokens) and tokens[i] not in FLAGS:
                parts.append(tokens[i])
                i += 1
            name = re.sub(r"[\\/]", "_", " ".join(parts)).strip() or None
            continue
        elif tk == "-t":
            i += 1
            thumb = tokens[i] if i < len(tokens) else None
        elif (tk.startswith("http://") or tk.startswith("https://") or tk.startswith("magnet:")) and not link:
            link = tk
        i += 1
    return link, z, e, name, thumb


HELP = (
    "<b>📥 Leech Bot</b>\n\n"
    "<code>/leech link</code> - download kore Telegram e upload\n\n"
    "<b>Args:</b>\n"
    "<code>-z</code> zip\n<code>-e</code> extract\n<code>-n</code> new name\n<code>-t</code> thumbnail link\n\n"
    "<b>Example:</b>\n<code>/leech https://site.com/file.mkv -n My Movie.mkv -t https://site.com/t.jpg</code>\n"
    "<code>/leech https://site.com/a.zip -e</code>\n\n"
    "/status - task status\n/usetting - settings\n/cancel_ID - task cancel"
)


# ------------------------------------------------------------------ handlers


@app.on_message(filters.command(["start", "help"]))
async def start_cmd(_, m):
    await m.reply_text(HELP)


@app.on_message(filters.command(["leech", "l"]))
async def leech_cmd(_, m):
    if not authorized(m):
        return await m.reply_text("❌ Tumi authorized na.")
    tokens = (m.text or "").split()[1:]
    if m.reply_to_message and (m.reply_to_message.text or m.reply_to_message.caption):
        tokens += (m.reply_to_message.text or m.reply_to_message.caption).split()
    link, z, e, name, thumb = parse(tokens)
    if not link:
        return await m.reply_text(HELP)
    if link.startswith("magnet:") or link.lower().split("?")[0].endswith(".torrent"):
        return await m.reply_text("❌ Torrent/Magnet supported na.")
    t = Task(m.from_user, m.chat.id, link, z, e, name, thumb)
    t.msg = await m.reply_text("⏳ <b>Task queue te add holo...</b>", reply_markup=None)
    TASKS[t.id] = t
    spawn(run_task(t))
    spawn(updater(t))


@app.on_message(filters.command("status"))
async def status_cmd(_, m):
    if not authorized(m):
        return
    await m.reply_text(status_text(), reply_markup=Kb([[Btn("🔄 Refresh", callback_data="st")]]))


@app.on_callback_query(filters.regex(r"^st$"))
async def status_cb(_, q):
    try:
        await q.message.edit_text(status_text(), reply_markup=Kb([[Btn("🔄 Refresh", callback_data="st")]]))
    except MessageNotModified:
        pass
    await q.answer("Refreshed")


async def do_cancel(q_or_m_uid, tid):
    t = TASKS.get(tid)
    if not t:
        return "Task paoa jay nai."
    if q_or_m_uid not in (t.uid, C.OWNER_ID):
        return "Eta tomar task na!"
    t.cancel = True
    if t.gid:
        try:
            await rpc("aria2.remove", t.gid)
        except Exception:
            pass
    return "Cancelling..."


@app.on_callback_query(filters.regex(r"^cx:"))
async def cancel_cb(_, q):
    await q.answer(await do_cancel(q.from_user.id, q.data.split(":")[1]), show_alert=True)


@app.on_message(filters.regex(r"^/cancel_(\w+)"))
async def cancel_msg(_, m):
    res = await do_cancel(m.from_user.id, m.matches[0].group(1))
    await m.reply_text(res)


def target_id(m):
    args = (m.text or "").split()[1:]
    if args:
        try:
            return int(args[0])
        except ValueError:
            return None
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id
    if m.chat.type != enums.ChatType.PRIVATE:
        return m.chat.id
    return None


AUTH_USAGE = (
    "Use:\n<code>/a ID</code> - user ba group authorize\n"
    "Kono user er message e reply kore <code>/a</code> - oi user authorize\n"
    "Group e <code>/a</code> - ei group authorize\n"
    "<code>/ua</code> same vabe unauthorize"
)


@app.on_message(filters.command("a"))
async def auth_cmd(_, m):
    if not m.from_user or m.from_user.id != C.OWNER_ID:
        return
    tid = target_id(m)
    if tid is None:
        return await m.reply_text(AUTH_USAGE)
    if tid in AUTH or tid in C.AUTHORIZED_CHATS or tid == C.OWNER_ID:
        return await m.reply_text(f"✅ <code>{tid}</code> already authorized.")
    AUTH.add(tid)
    save_auth()
    await m.reply_text(f"✅ <code>{tid}</code> authorized hoyeche.")


@app.on_message(filters.command("ua"))
async def unauth_cmd(_, m):
    if not m.from_user or m.from_user.id != C.OWNER_ID:
        return
    tid = target_id(m)
    if tid is None:
        return await m.reply_text(AUTH_USAGE)
    if tid == C.OWNER_ID:
        return await m.reply_text("❌ Owner ke unauthorize kora jay na.")
    if tid in AUTH:
        AUTH.discard(tid)
        save_auth()
        return await m.reply_text(f"🚫 <code>{tid}</code> unauthorized hoyeche.")
    if tid in C.AUTHORIZED_CHATS:
        return await m.reply_text("⚠️ Eta Railway variable (AUTHORIZED_CHATS) e ache, ota theke remove koro.")
    await m.reply_text(f"<code>{tid}</code> authorized chilo na.")


@app.on_message(filters.command("usetting"))
async def usetting_cmd(_, m):
    if not authorized(m):
        return await m.reply_text("❌ Tumi authorized na.")
    uid = m.from_user.id
    await m.reply_text(us_text(uid), reply_markup=us_kb(uid))


@app.on_callback_query(filters.regex(r"^us:"))
async def us_cb(_, q):
    _, uid, act = q.data.split(":")
    uid = int(uid)
    if q.from_user.id != uid:
        return await q.answer("Eta tomar settings na!", show_alert=True)
    PENDING.pop(uid, None)
    if act == "type":
        us_set(uid, "as_doc", not us(uid)["as_doc"])
    elif act == "thumb":
        PENDING[uid] = "thumb"
        return await q.answer("Ekhon ekta photo pathao (ei chat e).", show_alert=True)
    elif act == "delthumb":
        p = THUMBS / f"{uid}.jpg"
        if p.exists():
            p.unlink()
    elif act in ("prefix", "suffix"):
        PENDING[uid] = act
        return await q.answer(f"Ekhon {act} text pathao. Clear korte '-' pathao.", show_alert=True)
    elif act == "close":
        await q.message.delete()
        return
    try:
        await q.message.edit_text(us_text(uid), reply_markup=us_kb(uid))
    except MessageNotModified:
        pass
    await q.answer()


async def _pending(_, __, m):
    return bool(m.from_user) and m.from_user.id in PENDING


@app.on_message(filters.create(_pending) & ~filters.regex(r"^/"))
async def pending_h(_, m):
    uid = m.from_user.id
    kind = PENDING.pop(uid)
    if kind == "thumb":
        if not (m.photo or (m.document and (m.document.mime_type or "").startswith("image"))):
            PENDING[uid] = kind
            return await m.reply_text("❌ Ekta photo pathao.")
        raw = await m.download(file_name=str(THUMBS / f"{uid}_raw"))
        prep_thumb(raw, THUMBS / f"{uid}.jpg")
        os.remove(raw)
        await m.reply_text("✅ Thumbnail set hoye geche. /usetting")
    else:
        val = (m.text or "").strip()
        us_set(uid, kind, "" if val in ("-", "none", "None") else val)
        await m.reply_text(f"✅ {kind.capitalize()} set hoye geche. /usetting")


# ------------------------------------------------------------------ main


async def main():
    global SESSION
    shutil.rmtree(C.DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(C.DOWNLOAD_DIR, exist_ok=True)
    SESSION = aiohttp.ClientSession()
    await start_aria()
    await app.start()
    await app.set_bot_commands(
        [
            BotCommand("leech", "Link leech korun"),
            BotCommand("status", "Task status"),
            BotCommand("usetting", "User settings"),
            BotCommand("help", "Help"),
        ]
    )
    if PERSISTENT:
        log.info("Persistent volume mounted at %s", C.DATA_DIR)
    else:
        log.warning("%s e volume mount nai! Restart dile settings/thumbnail hariye jabe.", C.DATA_DIR)
    log.info("Bot started")
    from pyrogram import idle

    await idle()
    await app.stop()


if __name__ == "__main__":
    loop.run_until_complete(main())
