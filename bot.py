"""Leech Bot - WZML-X style Telegram leech bot (direct links + yt-dlp).

Features: Aria2 / yt-dlp downloads, zip / extract / rename, auto split for the 2GB bot limit,
multi-session fast uploader, WZML-X style live status, per-user settings panel (/us) with
caption fonts, group leech with delivery to the user's PM, auto command menu.
"""
import asyncio
import html
import inspect
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import shutil
import sys
import time
from glob import glob

import aiohttp
import psutil
import yt_dlp

import config as C
import fastupload
import settings as S
import utils as U
from compat import (
    BLUE,
    GREEN,
    LIB_LABEL,
    RED,
    B,
    Client,
    edit,
    enums,
    errors,
    filters,
    init_styles,
    reply,
    reply_kwargs,
    send,
    tg,
    types,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("leechbot")

# uvloop (if installed) gives noticeably lower overhead under heavy upload / download load
try:
    import uvloop

    loop = uvloop.new_event_loop()
except Exception:  # noqa: BLE001
    loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


def make_client():
    wanted = {
        "api_id": C.API_ID,
        "api_hash": C.API_HASH,
        "bot_token": C.BOT_TOKEN,
        "parse_mode": enums.ParseMode.HTML,
        "in_memory": True,
        "workers": 16,
        "max_concurrent_transmissions": 8,
        "sleep_threshold": 60,
    }
    accepted = set(inspect.signature(Client.__init__).parameters)
    return Client("leechbot", **{k: v for k, v in wanted.items() if k in accepted})


app = make_client()

BOOT = time.time()
SESSION: aiohttp.ClientSession = None
BOT_USERNAME = ""
SEM = asyncio.Semaphore(C.MAX_CONCURRENT)
TASKS = {}
PENDING = {}
STATUS = {}  # chat_id -> {"msg", "page", "text", "until", "reply_to"}
STATUS_LOCK = asyncio.Lock()
BG = set()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
ARIA_PROC = None

AUTH_DB = S.DATA / "auth.json"
try:
    AUTH = set(json.loads(AUTH_DB.read_text()))
except Exception:  # noqa: BLE001
    AUTH = set()


def save_auth():
    tmp = AUTH_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(AUTH)))
    os.replace(tmp, AUTH_DB)


# ------------------------------------------------------------------ helpers
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


def is_private(m):
    return m.chat.type == enums.ChatType.PRIVATE


# ------------------------------------------------------------------ task model
class Cancelled(Exception):
    pass


class Task:
    def __init__(self, m, link, z, e, name, thumb_link, dests):
        user = m.from_user
        self.id = secrets.token_hex(4)
        self.uid = user.id
        self.mention = user.mention
        self.cmd = m
        self.chat_id = m.chat.id
        self.private = is_private(m)
        try:
            self.cmd_link = m.link
        except Exception:  # noqa: BLE001
            self.cmd_link = None
        self.link = link
        self.zip = z
        self.extract = e
        self.new_name = name
        self.thumb_link = thumb_link
        self.thumb = None
        self.dests = dests
        self.dir = os.path.join(C.DOWNLOAD_DIR, self.id)
        self.status = "Queued"
        self.engine = "-"
        self.name = ""
        self.final_name = ""
        self.total = 0
        self.done = 0
        self.speed = 0
        self.cancel = False
        self.gid = None
        self.start = time.time()
        self.finished = False
        self._c = 0
        self._t = time.time()


# ------------------------------------------------------------------ status (WZML-X style)
def render(t):
    pct = (t.done / t.total * 100) if t.total else 0
    elapsed = time.time() - t.start
    eta = ((t.total - t.done) / t.speed) if t.speed and t.total else 0
    src = t.cmd_link or t.link or ""
    link = f' <a href="{html.escape(src, quote=True)}">[Link]</a>' if src.startswith(("http://", "https://")) else ""
    return (
        f"<code>{html.escape(U.shorten(t.name or 'Fetching info...', 60))}</code>\n"
        f"<b>Task By</b> {t.mention} ( <code>#ID{t.uid}</code> ){link}\n"
        f"┟ [{U.bar(pct)}] {pct:.2f}%\n"
        f"┠ <b>Processed</b> → {U.hsize(t.done)} of {U.hsize(t.total)}\n"
        f"┠ <b>Status</b> → {html.escape(t.status)}\n"
        f"┠ <b>Speed</b> → {U.hsize(t.speed)}/s\n"
        f"┠ <b>Time</b> → {U.htime(elapsed)} of {U.htime(elapsed + eta)} ( {U.htime(eta)} )\n"
        f"┠ <b>Engine</b> → {LIB_LABEL}\n"
        f"┠ <b>In Mode</b> → #{html.escape(t.engine)}\n"
        f"┠ <b>Out Mode</b> → #Leech\n"
        f"┖ <b>Stop</b> → /c_{t.id}"
    )


def stats_block(n=None):
    n = len(TASKS) if n is None else n
    running = sum(1 for t in TASKS.values() if t.status != "Queued")
    avail = max(C.MAX_CONCURRENT - running, 0)
    os.makedirs(C.DOWNLOAD_DIR, exist_ok=True)
    du = shutil.disk_usage(C.DOWNLOAD_DIR)
    net = psutil.net_io_counters()
    dl = sum(t.speed for t in TASKS.values() if t.status.startswith("Download"))
    ul = sum(t.speed for t in TASKS.values() if t.status.startswith("Upload"))
    return (
        "<b>⌬ Bot Stats</b>\n"
        f"┟ <b>Tasks</b> → {n} | <b>Avail</b> → {avail}\n"
        f"┠ <b>CPU</b> → {psutil.cpu_percent()}% | <b>F</b> → {U.hsize(du.free)} [{du.free / du.total * 100:.1f}%]\n"
        f"┠ <b>RAM</b> → {psutil.virtual_memory().percent}% | <b>UP</b> → {U.htime(time.time() - BOOT)}\n"
        f"┠ <b>IN</b> → {U.hsize(net.bytes_recv)} | <b>OUT</b> → {U.hsize(net.bytes_sent)}\n"
        f"┖ <b>DL</b> → {U.hsize(dl)}/s | <b>UL</b> → {U.hsize(ul)}/s"
    )


def chat_tasks(chat_id):
    """Tasks shown in a chat: everything started there; in a PM also the user's group tasks."""
    return sorted(
        (t for t in TASKS.values() if t.chat_id == chat_id or (chat_id > 0 and t.uid == chat_id)),
        key=lambda t: t.start,
    )


def status_view(chat_id, page):
    ts = chat_tasks(chat_id)
    if not ts:
        return None
    pages = max(1, math.ceil(len(ts) / C.TASKS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = ts[page * C.TASKS_PER_PAGE : (page + 1) * C.TASKS_PER_PAGE]
    text = "\n\n".join(render(t) for t in chunk) + "\n\n" + stats_block(len(ts))
    if pages > 1:
        rows = [
            [
                B("Prev", f"st|{chat_id}|{page - 1}", RED),
                B(f"♻️ Refresh {page + 1}/{pages}", f"st|{chat_id}|{page}", BLUE),
                B("Next", f"st|{chat_id}|{page + 1}", GREEN),
            ]
        ]
    else:
        rows = [[B("♻️ Refresh", f"st|{chat_id}|0", BLUE)]]
    return text, rows, page


async def ensure_status(chat_id, m):
    """Make sure the chat has a live status message (called when a task starts)."""
    async with STATUS_LOCK:
        if chat_id in STATUS:
            return await refresh_status(chat_id, force=True)
        view = status_view(chat_id, 0)
        if view is None:
            return
        text, rows, _ = view
        msg = await reply(m, text, rows)
        STATUS[chat_id] = {"msg": msg, "page": 0, "text": text, "until": 0, "reply_to": m}


async def refresh_status(chat_id, force=False):
    st = STATUS.get(chat_id)
    if not st or time.time() < st["until"]:
        return
    view = status_view(chat_id, st["page"])
    if view is None:
        STATUS.pop(chat_id, None)
        try:
            await st["msg"].delete()
        except Exception:  # noqa: BLE001
            pass
        return
    text, rows, page = view
    st["page"] = page
    if text == st["text"] and not force:
        return
    try:
        await edit(st["msg"], text, rows)
        st["text"] = text
    except errors.FloodWait as e:
        st["until"] = time.time() + min(getattr(e, "value", 10), 60)
    except Exception as e:  # noqa: BLE001 - message deleted etc.
        log.info("status message lost for %s (%r) - recreating", chat_id, e)
        STATUS.pop(chat_id, None)
        try:
            msg = await reply(st["reply_to"], text, rows)
            STATUS[chat_id] = {**st, "msg": msg, "text": text, "until": 0}
        except Exception as e2:  # noqa: BLE001
            log.warning("could not recreate status message: %r", e2)


async def status_loop():
    while True:
        await asyncio.sleep(C.STATUS_INTERVAL)
        for chat_id in list(STATUS):
            try:
                await refresh_status(chat_id)
            except Exception as e:  # noqa: BLE001
                log.debug("status refresh failed: %r", e)


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
    global ARIA_PROC
    ARIA_PROC = await asyncio.create_subprocess_exec(
        "aria2c",
        "--enable-rpc",
        f"--rpc-secret={C.ARIA_SECRET}",
        "--rpc-listen-port=6800",
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--max-concurrent-downloads=10",
        "--optimize-concurrent-downloads=true",
        "--max-overall-download-limit=0",
        "--max-download-limit=0",
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
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
    raise RuntimeError("aria2 did not start")


async def dl_aria(t):
    t.engine = "Aria2"
    gid = await rpc("aria2.addUri", [t.link], {"dir": t.dir})
    t.gid = gid
    while True:
        if t.cancel:
            try:
                await rpc("aria2.remove", gid)
            except Exception:  # noqa: BLE001
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
        async with SESSION.get(link, headers=UA, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=15)) as r:
            return not r.headers.get("Content-Type", "").lower().startswith("text/html")
    except Exception:  # noqa: BLE001
        return True


async def download(t):
    if await is_direct(t.link):
        try:
            await dl_aria(t)
            return
        except Cancelled:
            raise
        except Exception as e1:  # noqa: BLE001
            try:
                await dl_ytdlp(t)
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                raise RuntimeError(str(e1))
    else:
        await dl_ytdlp(t)


# ------------------------------------------------------------------ processing
ARCH = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso")


async def extract(t):
    for _ in range(2):  # 2 passes handle .tar.gz
        found = False
        for f in list_files(t.dir):
            if f.lower().endswith(ARCH):
                found = True
                out = os.path.join(t.dir, "extracted")
                rc, _, err = await run("7z", "x", f, f"-o{out}", "-y")
                if rc != 0:
                    raise RuntimeError("Extract failed: " + err[-200:])
                os.remove(f)
        if not found:
            break
    return list_files(t.dir)


async def make_zip(t, files):
    base = t.new_name or (os.path.splitext(os.path.basename(files[0]))[0] if len(files) == 1 else (t.name or t.id))
    base = re.sub(r"\.zip$", "", base, flags=re.I)
    out_dir = t.dir + "_z"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, base + ".zip")
    rc, _, err = await run("7z", "a", "-tzip", "-mx=1", out, ".", cwd=t.dir)
    if rc != 0:
        raise RuntimeError("Zip failed: " + err[-200:])
    shutil.rmtree(t.dir, ignore_errors=True)
    t.dir = out_dir
    return [out]


def rename_one(t, f):
    ext = os.path.splitext(f)[1]
    n = t.new_name if os.path.splitext(t.new_name)[1] else t.new_name + ext
    new = os.path.join(os.path.dirname(f), U.safe_filename(n))
    os.rename(f, new)
    return new


def apply_name_rules(files, s):
    if not any(s[k] for k in ("prefix", "suffix", "remove", "replace")):
        return files
    out = []
    for f in files:
        d, n = os.path.split(f)
        new_name = U.apply_rules(n, s)
        if new_name != n:
            new = os.path.join(d, new_name)
            if not os.path.exists(new):
                os.rename(f, new)
                f = new
        out.append(f)
    return out


def glob_escape(p):
    return re.sub(r"([\[\]\*\?])", r"[\1]", p)


async def split_big(path, limit):
    if os.path.getsize(path) <= limit:
        return [path]
    rc, _, err = await run("split", "-b", str(limit), "-d", "--numeric-suffixes=1", "-a", "3", path, path + ".")
    if rc != 0:
        raise RuntimeError("Split failed: " + err[-200:])
    os.remove(path)
    return sorted(glob(glob_escape(path) + ".[0-9][0-9][0-9]"))


async def probe(path):
    rc, out, _ = await run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration", "-of", "json", path,
    )
    try:
        j = json.loads(out)
        s = (j.get("streams") or [{}])[0]
        return int(float(j["format"]["duration"])), int(s.get("width", 0)), int(s.get("height", 0))
    except Exception:  # noqa: BLE001
        return 0, 0, 0


async def video_thumb(path, dur, tid):
    out = os.path.join(C.DOWNLOAD_DIR, f"{tid}_th.jpg")
    rc, _, _ = await run(
        "ffmpeg", "-y", "-ss", str(max(dur // 2, 0)), "-i", path, "-frames:v", "1", "-vf", "scale=320:-1", out
    )
    return out if rc == 0 and os.path.exists(out) else None


async def make_shots(t, path, dur, n):
    outs = []
    if n <= 0 or dur < 10:
        return outs
    for i in range(1, n + 1):
        out = os.path.join(C.DOWNLOAD_DIR, f"{t.id}_ss{i}.jpg")
        rc, _, _ = await run(
            "ffmpeg", "-y", "-ss", str(int(dur * i / (n + 1))), "-i", path,
            "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", out,
        )
        if rc == 0 and os.path.exists(out):
            outs.append(out)
    return outs


async def fetch_thumb(t):
    try:
        raw = os.path.join(C.DOWNLOAD_DIR, f"{t.id}_raw")
        async with SESSION.get(t.thumb_link, headers=UA, timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.read()
        with open(raw, "wb") as f:
            f.write(data)
        out = os.path.join(C.DOWNLOAD_DIR, f"{t.id}_thumb.jpg")
        S.prep_thumb(raw, out)
        os.remove(raw)
        t.thumb = out
    except Exception as e:  # noqa: BLE001
        log.warning("thumb link failed: %s", e)


# ------------------------------------------------------------------ upload
def make_progress(t):
    async def prog(cur, total):
        if t.cancel:
            app.stop_transmission()
        now = time.time()
        t.done = cur
        if now - t._t >= 1:
            t.speed = (cur - t._c) / (now - t._t)
            t._c, t._t = cur, now

    return prog


async def copy_to(sent, chat_id, rk):
    try:
        return await sent.copy(chat_id, **rk)
    except TypeError:
        return await sent.copy(chat_id)


async def send_file(t, f, s, thumb, prog):
    """Upload one file to the first destination, then copy it (instantly) to the others."""
    name = os.path.basename(f)
    size = os.path.getsize(f)
    mime = mimetypes.guess_type(f)[0] or ""
    primary = t.dests[0]
    in_group = lambda cid: cid == t.chat_id and not t.private  # noqa: E731
    rk = reply_kwargs(t.cmd.id) if in_group(primary) else {}
    is_video = mime.startswith("video")
    dur = w = h = 0
    if is_video or mime.startswith("audio"):
        dur, w, h = await probe(f)
    caption = U.render_caption(s, name, size, dur, w, h)
    kw = dict(chat_id=primary, caption=caption, progress=prog, **rk)

    if not s["as_doc"] and is_video:
        th = thumb or (await video_thumb(f, dur, t.id) if s["auto_thumb"] else None)
        sent = await app.send_video(video=f, duration=dur, width=w, height=h, thumb=th, supports_streaming=True, **kw)
    elif not s["as_doc"] and mime.startswith("audio"):
        sent = await app.send_audio(audio=f, duration=dur, thumb=thumb, **kw)
    elif not s["as_doc"] and mime.startswith("image") and size < 10 * 1024 * 1024:
        sent = await app.send_photo(photo=f, **kw)
    else:
        th = thumb or (await video_thumb(f, dur, t.id) if is_video and s["auto_thumb"] else None)
        sent = await app.send_document(document=f, thumb=th, force_document=True, **kw)

    delivered = [(name, sent)]
    for extra in t.dests[1:]:
        try:
            delivered.append((name, await copy_to(sent, extra, reply_kwargs(t.cmd.id) if in_group(extra) else {})))
        except Exception as e:  # noqa: BLE001
            log.warning("copy to %s failed: %r", extra, e)

    if is_video and s["shots"] and not t.cancel:
        await send_shots(t, f, dur, s["shots"], rk)
    return delivered


async def send_shots(t, f, dur, n, rk):
    shots = await make_shots(t, f, dur, n)
    if not shots:
        return
    try:
        first = await app.send_media_group(t.dests[0], [types.InputMediaPhoto(p) for p in shots], **rk)
        for extra in t.dests[1:]:
            try:
                await app.copy_media_group(extra, t.dests[0], first[0].id)
            except Exception as e:  # noqa: BLE001
                log.debug("screenshot copy failed: %r", e)
    except Exception as e:  # noqa: BLE001 - screenshots are optional, never fail the task
        log.warning("screenshots failed: %r", e)


async def upload_all(t, files, s):
    thumb = t.thumb or S.thumb_of(t.uid)
    prog = make_progress(t)
    delivered = []
    for i, f in enumerate(files, 1):
        if t.cancel:
            raise Cancelled
        t.name = os.path.basename(f)
        t.total = os.path.getsize(f)
        t.done = t.speed = t._c = 0
        t._t = time.time()
        t.status = f"Upload ({i}/{len(files)})" if len(files) > 1 else "Upload"
        delivered += await send_file(t, f, s, thumb, prog)
    return delivered


# ------------------------------------------------------------------ task runner
async def notify(t, text, rows=None):
    try:
        return await reply(t.cmd, text, rows)
    except Exception:  # noqa: BLE001 - command message may have been deleted
        try:
            return await send(app, t.chat_id, text, rows)
        except Exception as e:  # noqa: BLE001
            log.warning("could not send final message: %r", e)


def where_text(t):
    to_pm = any(d != t.chat_id for d in t.dests) or t.private
    to_chat = (t.chat_id in t.dests) and not t.private
    if t.private:
        return "PM"
    return "PM + Group" if to_pm and to_chat else ("PM" if to_pm else "Group")


async def finish_ok(t, files, delivered):
    total = sum(os.path.getsize(f) for f in files if os.path.exists(f))
    lines = []
    for fname, msg in delivered:
        if msg.chat.type != enums.ChatType.PRIVATE:
            try:
                link = msg.link
            except Exception:  # noqa: BLE001
                link = None
            if link:
                lines.append(f'<a href="{html.escape(link, quote=True)}">{html.escape(U.shorten(fname, 50))}</a>')
    where = where_text(t)
    text = (
        "✅ <b>Leech Completed</b>\n\n"
        f"<code>{html.escape(U.shorten(t.final_name or t.name, 80))}</code>\n\n"
        f"┌ <b>Size</b> : {U.hsize(total)}\n"
        f"├ <b>Files</b> : {len(files)}\n"
        f"├ <b>Time</b> : {U.htime(time.time() - t.start)}\n"
        f"├ <b>Sent To</b> : {where}\n"
        f"└ <b>By</b> : {t.mention}"
    )
    if lines:
        text += "\n\n<b>Files :</b>\n" + "\n".join(f"{i}. {l}" for i, l in enumerate(lines[:10], 1))
        if len(lines) > 10:
            text += f"\n… and {len(lines) - 10} more"
    rows = None
    if not t.private and "PM" in where and BOT_USERNAME:
        rows = [[B("📥 Open Bot PM", url=f"https://t.me/{BOT_USERNAME}", style=GREEN)]]
    await notify(t, text, rows)


async def run_task(t):
    s = S.get(t.uid)
    try:
        async with SEM:
            if t.cancel:
                raise Cancelled
            os.makedirs(t.dir, exist_ok=True)
            if t.thumb_link:
                await fetch_thumb(t)
            t.status = "Download"
            await download(t)
            if t.cancel:
                raise Cancelled
            t.speed = 0
            files = list_files(t.dir)
            if not files:
                raise RuntimeError("Nothing was downloaded")
            if not t.name:
                t.name = os.path.basename(files[0])
            if t.extract:
                t.status = "Extract"
                files = await extract(t)
            if t.zip:
                t.status = "Archive"
                files = await make_zip(t, files)
            elif t.new_name and len(files) == 1:
                files = [rename_one(t, files[0])]
            if not t.new_name:
                files = apply_name_rules(files, s)
            t.status = "Split"
            limit = S.split_limit(s)
            final = []
            for f in files:
                final += await split_big(f, limit)
            t.final_name = os.path.basename(final[0]) if len(final) == 1 else t.name
            delivered = await upload_all(t, final, s)
            await finish_ok(t, final, delivered)
    except Cancelled:
        await notify(t, f"❌ <b>Task cancelled.</b> ({t.mention})")
    except Exception as e:  # noqa: BLE001
        if t.cancel:
            await notify(t, f"❌ <b>Task cancelled.</b> ({t.mention})")
        else:
            log.exception("task failed")
            await notify(t, f"❌ <b>Task Failed</b> ({t.mention})\n<code>{html.escape(str(e)[:500])}</code>")
    finally:
        t.finished = True
        TASKS.pop(t.id, None)
        shutil.rmtree(t.dir, ignore_errors=True)
        shutil.rmtree(t.dir + "_z", ignore_errors=True)
        for p in glob(os.path.join(C.DOWNLOAD_DIR, f"{t.id}_*")):
            try:
                os.remove(p)
            except Exception:  # noqa: BLE001
                pass
        spawn(refresh_status(t.chat_id, force=True))
        if not t.private:  # a PM status may show this group task too
            spawn(refresh_status(t.uid, force=True))


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
            name = U.safe_filename(" ".join(parts)) if parts else None
            continue
        elif tk == "-t":
            i += 1
            thumb = tokens[i] if i < len(tokens) else None
        elif (tk.startswith("http://") or tk.startswith("https://") or tk.startswith("magnet:")) and not link:
            link = tk
        i += 1
    return link, z, e, name, thumb


HELP = (
    "<b>📥 Leech Bot — Commands</b>\n"
    "<i>Every command has a short form.</i>\n\n"
    "┌ /leech or /l — leech a link to Telegram\n"
    "├ /usetting or /us — your personal settings\n"
    "├ /status or /s — live status of running tasks\n"
    "├ /cancel or /c — cancel a task (<code>/c_ID</code> works too)\n"
    "├ /cancelall or /ca — cancel all your tasks\n"
    "├ /stats or /st — bot &amp; server stats\n"
    "├ /ping — check response time\n"
    "└ /help or /h — this message\n\n"
    "<b>Owner only</b>\n"
    "┌ /authorize or /a — authorize a user / group\n"
    "└ /unauthorize or /ua — remove authorization\n\n"
    "<b>Leech arguments</b>\n"
    "┌ <code>-z</code> zip the files\n"
    "├ <code>-e</code> extract archives\n"
    "├ <code>-n</code> new file name\n"
    "└ <code>-t</code> thumbnail link\n\n"
    "<b>Examples</b>\n"
    "<code>/l https://site.com/movie.mkv</code>\n"
    "<code>/l https://site.com/movie.mkv -n My Movie.mkv -t https://site.com/t.jpg</code>\n"
    "<code>/l https://site.com/a.zip -e</code>\n\n"
    "<i>Leech inside a group and the files arrive in your bot PM (change it in /us).</i>"
)

USER_COMMANDS = [
    ("leech", "Leech a link to Telegram • short: /l"),
    ("usetting", "Open your settings • short: /us"),
    ("status", "Live task status • short: /s"),
    ("cancel", "Cancel a task • short: /c (also /c_ID)"),
    ("cancelall", "Cancel all your tasks • short: /ca"),
    ("stats", "Bot & server stats • short: /st"),
    ("ping", "Check bot response time"),
    ("help", "Show all commands • short: /h"),
]
OWNER_COMMANDS = USER_COMMANDS + [
    ("authorize", "Authorize a user or group • short: /a"),
    ("unauthorize", "Remove authorization • short: /ua"),
]


async def setup_commands():
    """Register the command menu automatically on every start - no BotFather setup needed."""
    try:
        await app.set_bot_commands([types.BotCommand(c, d) for c, d in USER_COMMANDS])
        log.info("Bot commands registered")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not register bot commands: %r", e)
    if C.OWNER_ID:
        try:
            await app.set_bot_commands(
                [types.BotCommand(c, d) for c, d in OWNER_COMMANDS],
                scope=types.BotCommandScopeChat(chat_id=C.OWNER_ID),
            )
        except Exception as e:  # noqa: BLE001 - owner has not started the bot yet
            log.info("Owner command menu not set (%r)", e)


# ------------------------------------------------------------------ handlers: basics
@app.on_message(filters.command(["start", "help", "h"]))
async def start_cmd(_, m):
    await reply(m, HELP)


@app.on_message(filters.command("ping"))
async def ping_cmd(_, m):
    t0 = time.time()
    msg = await reply(m, "🏓 Pinging...")
    await edit(msg, f"🏓 <b>Pong!</b> <code>{(time.time() - t0) * 1000:.0f} ms</code>")


@app.on_message(filters.command(["stats", "st"]))
async def stats_cmd(_, m):
    du = shutil.disk_usage(C.DOWNLOAD_DIR if os.path.isdir(C.DOWNLOAD_DIR) else "/")
    uploader = (
        f"Fast ({C.UPLOAD_SESSIONS}x{C.UPLOAD_WORKERS}) · done {fastupload.STATS['fast']} / fallback {fastupload.STATS['fallback']}"
        if C.FAST_UPLOAD
        else "Library default"
    )
    text = (
        stats_block()
        + "\n\n<b>⌬ System</b>\n"
        f"┟ <b>Disk</b> → {U.hsize(du.used)} / {U.hsize(du.total)}\n"
        f"┠ <b>Library</b> → {LIB_LABEL}\n"
        f"┠ <b>Python</b> → {sys.version.split()[0]}\n"
        f"┠ <b>Uploader</b> → {uploader}\n"
        f"┖ <b>Settings Storage</b> → {'Persistent ✅' if S.PERSISTENT else 'Not persistent ⚠️ (attach a Volume)'}"
    )
    await reply(m, text)


# ------------------------------------------------------------------ handlers: leech
async def notify_pm(m, link):
    """Send a short PM. Doubles as the check that the user has started the bot."""
    try:
        title = html.escape(m.chat.title or "the group")
        await send(
            app,
            m.from_user.id,
            f"📥 <b>Task started</b> in <b>{title}</b>\n<code>{html.escape(U.shorten(link, 80))}</code>\n\n"
            "Your files will be delivered here when the task is done.",
        )
        return True
    except Exception as e:  # noqa: BLE001 - user never started the bot / blocked it
        log.info("cannot PM %s: %r", m.from_user.id, e)
        return False


def pick_dests(m, s):
    if is_private(m):
        return [m.chat.id]
    if s["dest"] == "chat":
        return [m.chat.id]
    if s["dest"] == "both":
        return [m.from_user.id, m.chat.id]
    return [m.from_user.id]


@app.on_message(filters.command(["leech", "l"]))
async def leech_cmd(_, m):
    if not authorized(m):
        return await reply(m, "❌ You are not authorized to use this bot.")
    if not m.from_user:
        return await reply(m, "❌ Anonymous admins cannot use this command.")
    tokens = (m.text or "").split()[1:]
    if m.reply_to_message and (m.reply_to_message.text or m.reply_to_message.caption):
        tokens += (m.reply_to_message.text or m.reply_to_message.caption).split()
    link, z, e, name, thumb = parse(tokens)
    if not link:
        return await reply(m, HELP)
    if link.startswith("magnet:") or link.lower().split("?")[0].endswith(".torrent"):
        return await reply(m, "❌ Torrent / magnet links are not supported.")

    s = S.get(m.from_user.id)
    dests = pick_dests(m, s)
    if not is_private(m) and m.from_user.id in dests and not await notify_pm(m, link):
        rows = [[B("▶️ Start Bot", url=f"https://t.me/{BOT_USERNAME}?start=leech", style=GREEN)]]
        return await reply(
            m,
            f"⚠️ {m.from_user.mention}, start the bot in PM first so I can send your files, then run the command again.",
            rows,
        )

    t = Task(m, link, z, e, name, thumb, dests)
    TASKS[t.id] = t
    spawn(run_task(t))
    await ensure_status(m.chat.id, m)


# ------------------------------------------------------------------ handlers: status & cancel
@app.on_message(filters.command(["status", "s"]))
async def status_cmd(_, m):
    chat_id = m.chat.id
    view = status_view(chat_id, 0)
    if view is None:
        return await reply(m, "<b>No active tasks.</b>\n\n" + stats_block(0))
    async with STATUS_LOCK:
        old = STATUS.pop(chat_id, None)
        if old:
            try:
                await old["msg"].delete()
            except Exception:  # noqa: BLE001
                pass
        text, rows, _ = view
        msg = await reply(m, text, rows)
        STATUS[chat_id] = {"msg": msg, "page": 0, "text": text, "until": 0, "reply_to": m}


@app.on_callback_query(filters.regex(r"^st\|"))
async def status_cb(_, q):
    _, chat, page = q.data.split("|")
    chat_id, page = int(chat), int(page)
    view = status_view(chat_id, page)
    if view is None:
        STATUS.pop(chat_id, None)
        await q.answer("No active tasks.", show_alert=True)
        try:
            await q.message.delete()
        except Exception:  # noqa: BLE001
            pass
        return
    text, rows, page = view
    st = STATUS.get(chat_id)
    if st and st["msg"].id == q.message.id:
        st.update(page=page, text=text)
    await edit(q.message, text, rows)
    await q.answer("Refreshed")


async def do_cancel(uid, tid):
    t = TASKS.get(tid)
    if not t:
        return "Task not found (maybe it already finished)."
    if uid not in (t.uid, C.OWNER_ID):
        return "This is not your task!"
    t.cancel = True
    if t.gid:
        try:
            await rpc("aria2.remove", t.gid)
        except Exception:  # noqa: BLE001
            pass
    return f"🛑 Cancelling task <code>{t.id}</code> ..."


@app.on_message(filters.command(["cancel", "c"]))
async def cancel_cmd(_, m):
    if not m.from_user:
        return
    uid = m.from_user.id
    args = (m.text or "").split()[1:]
    if args:
        return await reply(m, await do_cancel(uid, args[0].lstrip("_")))
    mine = [t for t in TASKS.values() if t.uid == uid]
    if not mine:
        return await reply(m, "You have no running tasks.")
    if len(mine) == 1:
        return await reply(m, await do_cancel(uid, mine[0].id))
    lines = "\n".join(f"/c_{t.id} — <code>{html.escape(U.shorten(t.name or t.link, 40))}</code>" for t in mine)
    await reply(m, "You have several tasks. Tap the one to cancel:\n\n" + lines)


@app.on_message(filters.regex(r"^/(?:cancel|c)_(\w+)"))
async def cancel_msg(_, m):
    if not m.from_user:
        return
    await reply(m, await do_cancel(m.from_user.id, m.matches[0].group(1)))


@app.on_message(filters.command(["cancelall", "ca"]))
async def cancelall_cmd(_, m):
    if not m.from_user:
        return
    uid = m.from_user.id
    n = 0
    for t in list(TASKS.values()):
        if uid == C.OWNER_ID or t.uid == uid:
            t.cancel = True
            n += 1
            if t.gid:
                try:
                    await rpc("aria2.remove", t.gid)
                except Exception:  # noqa: BLE001
                    pass
    await reply(m, f"🛑 Cancelling <b>{n}</b> task(s)." if n else "No running tasks to cancel.")


# ------------------------------------------------------------------ handlers: authorization
def target_id(m):
    args = (m.text or "").split()[1:]
    if args:
        try:
            return int(args[0])
        except ValueError:
            return None
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id
    if not is_private(m):
        return m.chat.id
    return None


AUTH_USAGE = (
    "<b>Usage</b>\n"
    "<code>/a ID</code> — authorize a user or group ID\n"
    "Reply to a user's message with <code>/a</code> — authorize that user\n"
    "Send <code>/a</code> inside a group — authorize the group\n"
    "<code>/ua</code> works the same way to remove access"
)


@app.on_message(filters.command(["authorize", "a"]))
async def auth_cmd(_, m):
    if not m.from_user or m.from_user.id != C.OWNER_ID:
        return
    tid = target_id(m)
    if tid is None:
        return await reply(m, AUTH_USAGE)
    if tid in AUTH or tid in C.AUTHORIZED_CHATS or tid == C.OWNER_ID:
        return await reply(m, f"✅ <code>{tid}</code> is already authorized.")
    AUTH.add(tid)
    save_auth()
    await reply(m, f"✅ <code>{tid}</code> authorized.")


@app.on_message(filters.command(["unauthorize", "ua"]))
async def unauth_cmd(_, m):
    if not m.from_user or m.from_user.id != C.OWNER_ID:
        return
    tid = target_id(m)
    if tid is None:
        return await reply(m, AUTH_USAGE)
    if tid == C.OWNER_ID:
        return await reply(m, "❌ The owner cannot be unauthorized.")
    if tid in AUTH:
        AUTH.discard(tid)
        save_auth()
        return await reply(m, f"🚫 <code>{tid}</code> unauthorized.")
    if tid in C.AUTHORIZED_CHATS:
        return await reply(m, "⚠️ This ID comes from the AUTHORIZED_CHATS variable - remove it there.")
    await reply(m, f"<code>{tid}</code> was not authorized.")


# ------------------------------------------------------------------ handlers: /us settings
@app.on_message(filters.command(["usetting", "us"]))
async def usetting_cmd(_, m):
    if not authorized(m):
        return await reply(m, "❌ You are not authorized to use this bot.")
    if not m.from_user:
        return
    uid = m.from_user.id
    parts = (m.text or "").split(maxsplit=3)
    if len(parts) >= 2 and parts[1].lower() in ("-s", "-set"):
        if len(parts) < 3:
            return await reply(m, f"Usage: <code>/us -s key value</code>\nKeys: <code>{S.KEYS_HELP}</code>")
        key = parts[2]
        value = parts[3] if len(parts) > 3 else ""
        return await reply(m, S.set_direct(uid, key, value))
    text, rows = S.panel(uid, "main", m.from_user)
    await reply(m, text, rows)


@app.on_callback_query(filters.regex(r"^us\|"))
async def us_cb(_, q):
    p = q.data.split("|")
    try:
        uid = int(p[1])
    except (IndexError, ValueError):
        return await q.answer()
    act, args = p[2], p[3:]
    if q.from_user.id != uid:
        return await q.answer("This is not your settings menu!", show_alert=True)
    PENDING.pop(uid, None)
    msg = q.message
    target, note = "main", None

    if act == "close":
        try:
            await msg.delete()
        except Exception:  # noqa: BLE001
            pass
        return await q.answer()
    if act == "go":
        target = args[0]
    elif act == "tog":
        key, target = args
        S.put(uid, key, not S.get(uid)[key])
    elif act == "cyc":
        key, target = args
        cur = S.get(uid)[key]
        idx = S.SHOT_STEPS.index(cur) if cur in S.SHOT_STEPS else 0
        S.put(uid, key, S.SHOT_STEPS[(idx + 1) % len(S.SHOT_STEPS)])
    elif act == "set":
        key, value, target = args
        if key == "font" and value in U.FONTS:
            S.put(uid, "font", value)
        elif key == "dest" and value in S.DEST_LABELS:
            S.put(uid, "dest", value)
        elif key == "split" and value.isdigit():
            S.put(uid, "split", int(value))
        note = "Saved ✅"
    elif act == "delthumb":
        S.delete_thumb(uid)
        target, note = args[0], "Thumbnail deleted"
    elif act == "reset":
        S.reset(uid)
        note = "All settings were reset"
    elif act == "ask":
        kind, back = args
        PENDING[uid] = {"kind": kind, "back": back, "msg": msg, "chat": msg.chat.id, "exp": time.time() + 120}
        await edit(msg, S.prompt_text(kind), [[B("Cancel", S.cb(uid, "go", back), RED)]])
        return await q.answer()

    text, rows = S.panel(uid, target, q.from_user)
    await edit(msg, text, rows)
    await q.answer(note or "")


async def _pending(_, __, m):
    if not m.from_user:
        return False
    p = PENDING.get(m.from_user.id)
    if not p or p["chat"] != m.chat.id:
        return False
    if time.time() > p["exp"]:
        PENDING.pop(m.from_user.id, None)
        return False
    return True


@app.on_message(filters.create(_pending) & ~filters.regex(r"^/"))
async def pending_h(_, m):
    uid = m.from_user.id
    p = PENDING.pop(uid)
    kind = p["kind"]
    if kind == "thumb":
        is_img = m.photo or (m.document and (m.document.mime_type or "").startswith("image"))
        if is_img:
            raw = await m.download(file_name=str(S.THUMBS / f"{uid}_raw"))
            S.prep_thumb(raw, S.THUMBS / f"{uid}.jpg")
            os.remove(raw)
        elif (m.text or "").strip() == "-":
            S.delete_thumb(uid)
        else:
            PENDING[uid] = p
            return await reply(m, "❌ Please send a photo (or <code>-</code> to delete the thumbnail).")
    else:
        result = S.set_direct(uid, kind, (m.text or "").strip())
        if result.startswith("❌"):
            PENDING[uid] = p
            return await reply(m, result)
    try:
        await m.delete()
    except Exception:  # noqa: BLE001 - bot is not an admin in this group
        pass
    text, rows = S.panel(uid, p["back"], m.from_user)
    await edit(p["msg"], text, rows)


# ------------------------------------------------------------------ main
async def main():
    global SESSION, BOT_USERNAME
    shutil.rmtree(C.DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(C.DOWNLOAD_DIR, exist_ok=True)
    SESSION = aiohttp.ClientSession()
    await start_aria()
    await app.start()
    me = await app.get_me()
    BOT_USERNAME = me.username or ""
    await init_styles(app)
    fastupload.install(app)
    await setup_commands()
    psutil.cpu_percent()  # prime the counter so the first reading is meaningful
    spawn(status_loop())
    if S.PERSISTENT:
        log.info("Persistent volume mounted at %s", S.DATA)
    else:
        log.warning("No volume mounted at %s - settings and thumbnails are lost on redeploy!", S.DATA)
    log.info("Bot started as @%s using %s", BOT_USERNAME, LIB_LABEL)
    await tg.idle()
    await app.stop()
    if ARIA_PROC:
        ARIA_PROC.terminate()


if __name__ == "__main__":
    loop.run_until_complete(main())
