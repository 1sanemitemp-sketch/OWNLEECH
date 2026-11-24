"""Leech Bot - WZML-X style Telegram leech bot (direct links + yt-dlp).

Features: Aria2 / yt-dlp downloads, zip / extract / rename, auto split for the 2GB bot limit,
multi-session fast uploader, WZML-X style live status, per-user settings panel (/us) with
caption fonts, group leech with delivery to the user's PM, auto command menu.
"""
import asyncio
import html
import inspect
import json
import hashlib
import logging
import math
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
import db
import fastupload
import limits
import rename_utils as R
import settings as S
import statsui
import utils as U
from compat import (
    BLUE,
    GREEN,
    KB,
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
    db.init()
    bot_id = (C.BOT_TOKEN.split(":")[0] or "0") if C.BOT_TOKEN else "0"
    saved_session = db.kv_get(f"session_{bot_id}") if db.ENABLED else None
    wanted = {
        "api_id": C.API_ID,
        "api_hash": C.API_HASH,
        "bot_token": C.BOT_TOKEN,
        "parse_mode": enums.ParseMode.HTML,
        "in_memory": False,  # keep the auth key: a fresh key on every restart makes Telegram answer 500
        "workdir": str(limits.DATA),
        "workers": 16,
        "max_concurrent_transmissions": 8,
        "sleep_threshold": 60,
    }
    if saved_session:  # login stored in MongoDB: no new auth key on every restart, no volume needed
        wanted["session_string"] = saved_session
        wanted["in_memory"] = True
    accepted = set(inspect.signature(Client.__init__).parameters)
    return Client(f"leechbot_{bot_id}", **{k: v for k, v in wanted.items() if k in accepted})


def make_user_client():
    """Premium account (STRING_SESSION) used only for uploads bigger than 2 GB."""
    if not C.STRING_SESSION:
        return None
    wanted = {
        "api_id": C.API_ID,
        "api_hash": C.API_HASH,
        "session_string": C.STRING_SESSION,
        "parse_mode": enums.ParseMode.HTML,
        "in_memory": True,
        "no_updates": True,
        "workers": 4,
        "max_concurrent_transmissions": 8,
        "sleep_threshold": 60,
    }
    accepted = set(inspect.signature(Client.__init__).parameters)
    return Client("leechuser", **{k: v for k, v in wanted.items() if k in accepted})


app = make_client()
USER = make_user_client()  # started in main() only when the account is Premium
USER_ID = 0
STAGE = {}  # token -> future resolved when the bot receives the staged upload of the premium account

BOOT = time.time()
SESSION: aiohttp.ClientSession = None
BOT_USERNAME = ""
SUF = C.COMMAND_SUFFIX


def cmds(*names):
    """Command names with COMMAND_SUFFIX appended (/start is never suffixed)."""
    return [n + SUF for n in names]


TASKS = {}
PENDING = {}
STATUS = {}  # chat_id -> {"msg", "page", "text", "until", "reply_to"}
STATUS_LOCK = asyncio.Lock()
BG = set()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
ARIA_PROC = None

AUTH_DB = S.DATA / "auth.json"
try:
    _saved_auth = db.kv_get("auth") if db.ENABLED else None
    AUTH = set(_saved_auth if _saved_auth is not None else json.loads(AUTH_DB.read_text()))
except Exception:  # noqa: BLE001
    AUTH = set()


def save_auth():
    tmp = AUTH_DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(AUTH)))
    os.replace(tmp, AUTH_DB)
    db.kv_set("auth", sorted(AUTH))


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


def can_use(uid, chat_id):
    ids = C.AUTHORIZED_CHATS | AUTH
    return uid == C.OWNER_ID or uid in ids or chat_id in ids


def is_private(m):
    return m.chat.type == enums.ChatType.PRIVATE


# ------------------------------------------------------------------ task model
class Cancelled(Exception):
    pass


class LimitError(RuntimeError):
    pass


def over_limit(t, total, key):
    """Raise LimitError when a (non-owner) task is bigger than the engine limit or the leech limit."""
    if t.owner or not total:
        return
    for k in (key, "leech"):
        cap = limits.gb(k)
        if cap and total > cap:
            raise LimitError(
                f"Size limit reached: {U.hsize(total)} is bigger than the {limits.DEFS[k][0]} ({limits.text(k)})."
            )


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
        self.status = "Download"
        self.capnames = {}
        self.owner = user.id == C.OWNER_ID
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
        f"┖ <b>Stop</b> → /c{SUF}_{t.id}"
    )


def stats_block(n=None):
    n = len(TASKS) if n is None else n
    running = len(TASKS)
    cap = limits.get("bot_tasks")
    avail = max(cap - running, 0) if cap else "∞"
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
    """A new task always gets a fresh status message at the bottom (the old one is deleted)."""
    async with STATUS_LOCK:
        old = STATUS.pop(chat_id, None)
        if old:
            try:
                await old["msg"].delete()
            except Exception:  # noqa: BLE001
                pass
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
        try:
            over_limit(t, t.total, "direct")
        except LimitError:
            try:
                await rpc("aria2.remove", gid)
            except Exception:  # noqa: BLE001
                pass
            raise
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
            over_limit(t, t.total, "ytdlp")

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
    opts.update(S.yt_opts(t.uid))
    opts["progress_hooks"] = [hook]
    opts["outtmpl"] = f"{t.dir}/%(title).150B.%(ext)s"
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
        except (Cancelled, LimitError):
            raise
        except Exception as e1:  # noqa: BLE001
            try:
                await dl_ytdlp(t)
            except (Cancelled, LimitError):
                raise
            except Exception:  # noqa: BLE001
                raise RuntimeError(str(e1))
    else:
        await dl_ytdlp(t)


# ------------------------------------------------------------------ processing
ARCH = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso")


def tick(t, done):
    """Update processed bytes and a smoothed speed (used by extract / archive / split)."""
    t.done = done
    now = time.time()
    if now - t._t >= 1:
        t.speed = max((done - t._c) / (now - t._t), 0)
        t._c, t._t = done, now


async def run_7z(t, cmd, size, base=0, cwd=None):
    """Run 7z with -bsp1 and turn its percentage output into task progress."""
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd
    )
    tail = b""
    while True:
        chunk = await p.stdout.read(4096)
        if not chunk:
            break
        tail = (tail + chunk)[-2000:]
        found = re.findall(rb"(\d{1,3})%", chunk)
        if found:
            tick(t, base + int(size * min(int(found[-1]), 100) / 100))
        if t.cancel:
            p.kill()
            await p.wait()
            raise Cancelled
    rc = await p.wait()
    return rc, tail.decode(errors="ignore")


async def extract(t):
    t.status = "Extract"
    for _ in range(2):  # 2 passes handle .tar.gz
        archives = [f for f in list_files(t.dir) if f.lower().endswith(ARCH)]
        if not archives:
            break
        t.total = sum(os.path.getsize(f) for f in archives)
        t.done = t.speed = t._c = 0
        t._t = time.time()
        base = 0
        for f in archives:
            if t.cancel:
                raise Cancelled
            size = os.path.getsize(f)
            t.name = os.path.basename(f)
            out = os.path.join(t.dir, "extracted")
            rc, tail = await run_7z(t, ["7z", "x", f, f"-o{out}", "-y", "-bsp1"], size, base)
            if rc != 0:
                raise RuntimeError("Extract failed: " + tail[-200:])
            os.remove(f)
            base += size
            t.done = base
    return list_files(t.dir)


async def make_zip(t, files):
    t.status = "Archive"
    base = t.new_name or (os.path.splitext(os.path.basename(files[0]))[0] if len(files) == 1 else (t.name or t.id))
    base = re.sub(r"\.zip$", "", base, flags=re.I)
    out_dir = t.dir + "_z"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, base + ".zip")
    total = sum(os.path.getsize(f) for f in files)
    t.name, t.total = base + ".zip", total
    t.done = t.speed = t._c = 0
    t._t = time.time()
    rc, tail = await run_7z(t, ["7z", "a", "-tzip", "-mx=1", "-bsp1", out, "."], total, 0, cwd=t.dir)
    if rc != 0:
        raise RuntimeError("Zip failed: " + tail[-200:])
    shutil.rmtree(t.dir, ignore_errors=True)
    t.dir = out_dir
    return [out]


def rename_one(t, f):
    ext = os.path.splitext(f)[1]
    n = t.new_name if os.path.splitext(t.new_name)[1] else t.new_name + ext
    new = os.path.join(os.path.dirname(f), U.safe_filename(n))
    os.rename(f, new)
    return new


async def smart_names(t, files, s):
    """Auto rename -> underscores to spaces -> remove / replace -> prefix / suffix (+ caption name)."""
    out = []
    for f in files:
        d, n = os.path.split(f)
        core = n
        if s["rename"]:
            core = await R.auto_renamed(s["rename"], n, f)
        core = U.tidy_name(core)
        core = U.clean_name(core, s)
        final = U.affix_name(core, s)
        if final != n:
            dst = os.path.join(d, final)
            if not os.path.exists(dst):
                os.rename(f, dst)
                f = dst
            else:
                final = n
        t.capnames[final] = U.caption_name(core, s)
        out.append(f)
    return out


def cap_name_of(t, name):
    """Caption version of a file name (keeps HTML from prefix / suffix). Split parts reuse their parent."""
    if name in t.capnames:
        return t.capnames[name]
    for key, val in t.capnames.items():
        if name.startswith(key):
            return val + html.escape(name[len(key):])
    return html.escape(name)


def glob_escape(p):
    return re.sub(r"([\[\]\*\?])", r"[\1]", p)


async def split_big(path, limit, equal=False, t=None):
    size = os.path.getsize(path)
    if size <= limit:
        return [path]
    chunk = limit
    if equal:  # equal sized parts instead of full parts + one small leftover
        chunk = math.ceil(size / math.ceil(size / limit))
    pattern = glob_escape(path) + ".[0-9][0-9][0-9]"
    if t:
        t.status, t.name, t.total = "Split", os.path.basename(path), size
        t.done = t.speed = t._c = 0
        t._t = time.time()

    async def poll():
        while True:
            await asyncio.sleep(1)
            tick(t, sum(os.path.getsize(x) for x in glob(pattern)))

    poller = asyncio.ensure_future(poll()) if t else None
    try:
        p = await asyncio.create_subprocess_exec(
            "split", "-b", str(chunk), "-d", "--numeric-suffixes=1", "-a", "3", path, path + ".",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await p.communicate()
    finally:
        if poller:
            poller.cancel()
    if p.returncode != 0:
        raise RuntimeError("Split failed: " + err.decode(errors="ignore")[-200:])
    os.remove(path)
    return sorted(glob(pattern))


async def make_covers(path, dur, tag):
    """HD frame from the middle of a video -> (thumbnail under 200 KB, big HD cover)."""
    cover = os.path.join(C.DOWNLOAD_DIR, f"{tag}_cv.jpg")
    m = C.THUMB_MAX
    vf = f"scale='if(gt(iw,ih),min({m},iw),-2)':'if(gt(iw,ih),-2,min({m},ih))'"
    rc, _, _ = await run(
        "ffmpeg", "-y", "-ss", str(max(dur // 2, 0)), "-i", path, "-frames:v", "1", "-vf", vf, "-q:v", "3", cover
    )
    if rc != 0 or not os.path.exists(cover):
        return None, None
    thumb = os.path.join(C.DOWNLOAD_DIR, f"{tag}_th.jpg")
    try:
        await asyncio.to_thread(S.prep_thumb, cover, thumb)
    except Exception as e:  # noqa: BLE001
        log.warning("thumbnail processing failed: %r", e)
        return cover, cover
    return thumb, cover


async def make_shots(t, path, dur, n, tag):
    outs = []
    if n <= 0 or dur < 10:
        return outs
    for i in range(1, n + 1):
        out = os.path.join(C.DOWNLOAD_DIR, f"{t.id}_{tag}_ss{i}.jpg")
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
async def copy_to(sent, chat_id, rk):
    try:
        return await sent.copy(chat_id, **rk)
    except TypeError:
        return await sent.copy(chat_id)


def with_name(method, name):
    """Pass the exact file name to Telegram when this library supports it."""
    try:
        return {"file_name": name} if "file_name" in inspect.signature(method).parameters else {}
    except (TypeError, ValueError):
        return {}


TELEGRAPH = {"token": None}


async def telegraph_page(title, text):
    """Publish text on telegra.ph and return the page URL (None on any failure)."""
    try:
        if not TELEGRAPH["token"]:
            tok_file = S.DATA / "telegraph.json"
            try:
                TELEGRAPH["token"] = json.loads(tok_file.read_text())["token"]
            except Exception:  # noqa: BLE001
                async with SESSION.post(
                    "https://api.telegra.ph/createAccount", json={"short_name": "MediaInfo", "author_name": "MediaInfo"}
                ) as r:
                    TELEGRAPH["token"] = (await r.json())["result"]["access_token"]
                tok_file.write_text(json.dumps({"token": TELEGRAPH["token"]}))
        payload = {
            "access_token": TELEGRAPH["token"],
            "title": title[:200] or "MediaInfo",
            "author_name": "MediaInfo",
            "content": [{"tag": "pre", "children": [text[:50000]]}],
        }
        async with SESSION.post("https://api.telegra.ph/createPage", json=payload) as r:
            data = await r.json()
        return data["result"]["url"] if data.get("ok") else None
    except Exception as e:  # noqa: BLE001
        log.warning("mediainfo page failed: %r", e)
        return None


async def mediainfo_markup(path):
    """Inline button that opens the MediaInfo page of ``path`` (None when unavailable)."""
    rc, out, _ = await run("mediainfo", path)
    if rc != 0 or not out.strip():
        return None
    name = os.path.basename(path)
    link = await telegraph_page(name, out.replace(path, name))
    return KB([[B("📄 MediaInfo", url=link)]]) if link else None


async def log_send(kind, text):
    """Post a text log (never raises - a missing channel or permission must not break a task)."""
    chat = C.log_id(kind)
    if not chat:
        return
    try:
        await send(app, chat, text)
    except Exception as e:  # noqa: BLE001
        log.warning("log channel %s (%s) failed: %r - is the bot an admin there?", kind, chat, e)


async def log_copy(kind, sent):
    chat = C.log_id(kind)
    if not chat:
        return
    try:
        await app.copy_message(chat, sent.chat.id, sent.id)
    except Exception as e:  # noqa: BLE001
        log.warning("log channel %s (%s) copy failed: %r - is the bot an admin there?", kind, chat, e)


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


VIDEO_PARAMS = set(inspect.signature(app.send_video).parameters)
COVER_OK = C.USE_COVER and "cover" in VIDEO_PARAMS


async def send_file(t, f, s, thumb, prog):
    """Upload one file to the first destination, then copy it (instantly) to the others."""
    name = os.path.basename(f)
    size = os.path.getsize(f)
    kind = U.media_kind(f)
    tag = secrets.token_hex(3)
    primary = t.dests[0]
    in_group = lambda cid: cid == t.chat_id and not t.private  # noqa: E731
    rk = reply_kwargs(t.cmd.id) if in_group(primary) else {}
    is_video = kind == "video"
    info = await R.probe_info(f) if kind in ("video", "audio") else {}
    dur, w, h = info.get("duration", 0), info.get("width", 0), info.get("height", 0)
    md5 = ""
    if re.search(r"\{md5_hash\}", s["caption"] or "", re.I):
        md5 = await asyncio.to_thread(md5_of, f)
    caption = U.render_caption(s, cap_name_of(t, name), size, info, md5)
    kw = dict(chat_id=primary, caption=caption, progress=prog, **rk)
    if s["mediainfo"] and kind in ("video", "audio"):
        markup = await mediainfo_markup(f)
        if markup:
            kw["reply_markup"] = markup

    # HD cover: -t link / your thumbnail first, otherwise a frame taken from the video itself
    th = cv = thumb
    if is_video and not thumb:
        th, cv = await make_covers(f, dur, f"{t.id}_{tag}")
    if USER and size > C.SPLIT_SIZE:  # bigger than a bot can upload -> Premium account, then the bot copies it
        sent = await send_via_user(t, f, kind, s, caption, kw.get("reply_markup"), th, cv, info, name, prog)
    elif not s["as_doc"] and is_video:
        extra_kw = {"cover": cv} if (COVER_OK and cv) else {}
        sent = await app.send_video(
            video=f, duration=dur, width=w, height=h, thumb=th, supports_streaming=True,
            **extra_kw, **with_name(app.send_video, name), **kw,
        )
    elif not s["as_doc"] and kind == "audio":
        sent = await app.send_audio(
            audio=f, duration=dur, thumb=thumb, **with_name(app.send_audio, name), **kw
        )
    elif not s["as_doc"] and kind == "image" and size < 10 * 1024 * 1024:
        sent = await app.send_photo(photo=f, **kw)
    else:
        sent = await app.send_document(
            document=f, thumb=th, force_document=True, **with_name(app.send_document, name), **kw
        )

    delivered = [(name, sent)]
    spawn(log_copy("leech", sent))
    for extra in t.dests[1:]:
        try:
            delivered.append((name, await copy_to(sent, extra, reply_kwargs(t.cmd.id) if in_group(extra) else {})))
        except Exception as e:  # noqa: BLE001
            log.warning("copy to %s failed: %r", extra, e)

    if is_video and s["shots"] and not t.cancel:
        await send_shots(t, f, dur, s["shots"], rk, tag)
    return delivered


async def send_via_user(t, f, kind, s, caption, markup, th, cv, info, name, prog):
    """Upload with the Premium string session (up to 4 GB) into the bot's own PM, then let the bot copy it.

    The Premium account never has to join or administer any channel: the bot simply receives the file
    in its private chat with that account and copies it to the real destination.
    """
    token = secrets.token_hex(4)
    fut = asyncio.get_running_loop().create_future()
    STAGE[token] = fut
    common = dict(chat_id=BOT_USERNAME, caption=f"#L{token}", progress=prog)
    dur, w, h = info.get("duration", 0), info.get("width", 0), info.get("height", 0)
    staged_by_user = None
    try:
        if not s["as_doc"] and kind == "video":
            extra_kw = {"cover": cv} if (COVER_OK and cv) else {}
            staged_by_user = await USER.send_video(
                video=f, duration=dur, width=w, height=h, thumb=th, supports_streaming=True,
                **extra_kw, **with_name(USER.send_video, name), **common,
            )
        elif not s["as_doc"] and kind == "audio":
            staged_by_user = await USER.send_audio(
                audio=f, duration=dur, thumb=th, **with_name(USER.send_audio, name), **common
            )
        else:
            staged_by_user = await USER.send_document(
                document=f, thumb=th, force_document=True, **with_name(USER.send_document, name), **common
            )
        staged = await asyncio.wait_for(fut, 120)  # the same message, as received by the bot
    except asyncio.TimeoutError as e:
        raise RuntimeError(
            "The bot did not receive the file from the Premium account. Open the bot with that account "
            "and press Start once, then try again."
        ) from e
    finally:
        STAGE.pop(token, None)
    extra = {"reply_markup": markup} if markup else {}
    try:
        return await app.copy_message(t.dests[0], staged.chat.id, staged.id, caption=caption, **extra)
    finally:
        try:
            await USER.delete_messages(staged_by_user.chat.id, staged_by_user.id)
        except Exception:  # noqa: BLE001
            pass


@app.on_message(filters.private & (filters.document | filters.video | filters.audio), group=-1)
async def stage_handler(_, m):
    """Receives the staged upload sent by the Premium account (see send_via_user)."""
    if not USER_ID or not m.from_user or m.from_user.id != USER_ID:
        return
    token = re.match(r"#L([0-9a-f]{8})$", m.caption or "")
    fut = STAGE.get(token.group(1)) if token else None
    if fut and not fut.done():
        fut.set_result(m)


async def send_shots(t, f, dur, n, rk, tag):
    shots = await make_shots(t, f, dur, n, tag)
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
    """Upload every file. The status shows ONE overall progress for all files together."""
    thumb = t.thumb or S.thumb_of(t.uid)
    sizes = [os.path.getsize(f) for f in files]
    n = len(files)
    t.total, t.done, t.speed, t._c = sum(sizes), 0, 0, 0
    t._t = time.time()
    t.status = f"Upload (0/{n})" if n > 1 else "Upload"
    part = {}
    finished = 0
    sem = asyncio.Semaphore(max(1, limits.get("parallel")))  # 1 = strict order (episodes stay in order)

    def make_prog(i):
        async def prog(cur, total):
            if t.cancel:
                app.stop_transmission()
            part[i] = cur
            tick(t, sum(part.values()))

        return prog

    async def one(i, f):
        nonlocal finished
        async with sem:
            if t.cancel:
                raise Cancelled
            t.name = os.path.basename(f)
            res = await send_file(t, f, s, thumb, make_prog(i))
            part[i] = sizes[i]
            finished += 1
            if n > 1:
                t.status = f"Upload ({finished}/{n})"
            tick(t, sum(part.values()))
            return res

    jobs = [asyncio.ensure_future(one(i, f)) for i, f in enumerate(files)]
    try:
        results = await asyncio.gather(*jobs)
    except BaseException:
        for j in jobs:
            j.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise
    return [d for r in results for d in r]


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
        over_limit(t, sum(os.path.getsize(f) for f in files), "leech")
        if not t.name:
            t.name = os.path.basename(files[0])
        if t.extract:
            files = await extract(t)
        if t.zip:
            files = await make_zip(t, files)
        elif t.new_name and len(files) == 1:
            files = [rename_one(t, files[0])]
        if not t.new_name:
            files = await smart_names(t, files, s)
        limit = S.split_limit(s)
        final = []
        for f in files:
            final += await split_big(f, limit, s["equal"], t)
        t.final_name = os.path.basename(final[0]) if len(final) == 1 else t.name
        delivered = await upload_all(t, final, s)
        await finish_ok(t, final, delivered)
    except Cancelled:
        await notify(t, f"❌ <b>Task cancelled.</b> ({t.mention})")
    except Exception as e:  # noqa: BLE001
        if t.cancel:
            await notify(t, f"❌ <b>Task cancelled.</b> ({t.mention})")
        else:
            if not isinstance(e, LimitError):
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


def _c(name, short=None):
    return f"/{name}{SUF}" + (f" or /{short}{SUF}" if short else "")


HELP = (
    "<b>📥 Leech Bot — Commands</b>\n"
    "<i>Every command has a short form.</i>\n\n"
    f"┌ {_c('leech', 'l')} — leech a link to Telegram\n"
    f"├ {_c('usetting', 'us')} — your personal settings\n"
    f"├ {_c('status', 's')} — live status of running tasks\n"
    f"├ {_c('cancel', 'c')} — cancel a task (<code>/c{SUF}_ID</code> works too)\n"
    f"├ {_c('cancelall', 'ca')} — cancel all your tasks\n"
    f"├ {_c('stats', 'st')} — bot &amp; server stats\n"
    f"├ /ping{SUF} — check response time\n"
    f"└ {_c('help', 'h')} — this message\n\n"
    "<b>Owner only</b>\n"
    f"┌ {_c('botsettings', 'bs')} — bot limits (user tasks, bot tasks, leech limit ...)\n"
    f"├ {_c('authorize', 'a')} — authorize a user / group\n"
    f"└ {_c('unauthorize', 'ua')} — remove authorization\n\n"
    "<b>Leech arguments</b>\n"
    "┌ <code>-z</code> zip the files\n"
    "├ <code>-e</code> extract archives\n"
    "├ <code>-n</code> new file name\n"
    "└ <code>-t</code> thumbnail link\n\n"
    "<b>Examples</b>\n"
    f"<code>/l{SUF} https://site.com/movie.mkv</code>\n"
    f"<code>/l{SUF} https://site.com/movie.mkv -n My Movie.mkv -t https://site.com/t.jpg</code>\n"
    f"<code>/l{SUF} https://site.com/a.zip -e</code>\n\n"
    "<i>Leech inside a group and the files always arrive in your bot PM.</i>"
)
USER_COMMANDS = [
    ("leech", "Leech a link to Telegram • short: /l"),
    ("usetting", "Open your settings • short: /us"),
    ("status", "Live task status • short: /s"),
    ("cancel", "Cancel a task • short: /c"),
    ("cancelall", "Cancel all your tasks • short: /ca"),
    ("stats", "Bot & server stats • short: /st"),
    ("ping", "Check bot response time"),
    ("help", "Show all commands • short: /h"),
]
OWNER_COMMANDS = USER_COMMANDS + [
    ("botsettings", "Bot limits (owner) • short: /bs"),
    ("authorize", "Authorize a user or group • short: /a"),
    ("unauthorize", "Remove authorization • short: /ua"),
]


async def setup_commands():
    """Register the command menu automatically on every start - no BotFather setup needed."""
    def menu(items):
        return [types.BotCommand(c + SUF, d) for c, d in items]

    try:
        await app.set_bot_commands([types.BotCommand("start", "Start the bot")] + menu(USER_COMMANDS))
        log.info("Bot commands registered")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not register bot commands: %r", e)
    if C.OWNER_ID:
        try:
            await app.set_bot_commands(
                [types.BotCommand("start", "Start the bot")] + menu(OWNER_COMMANDS),
                scope=types.BotCommandScopeChat(chat_id=C.OWNER_ID),
            )
        except Exception as e:  # noqa: BLE001 - owner has not started the bot yet
            log.info("Owner command menu not set (%r)", e)


# ------------------------------------------------------------------ handlers: basics
def start_rows():
    """Buttons from ST_BUTTON_1 / ST_BUTTON_2 ("TEXT:URL")."""
    row = []
    for raw in (C.ST_BUTTON_1, C.ST_BUTTON_2):
        label, sep, url = (raw or "").partition(":")
        label, url = label.strip(), url.strip()
        if sep and label and url.startswith(("http://", "https://", "tg://")):
            row.append(B(label, url=url, style=GREEN))
    return [row] if row else None


@app.on_message(filters.command("start"))
async def start_cmd(_, m):
    """The only command everybody can use."""
    await reply(m, C.ST_TEXT, start_rows())


@app.on_message(filters.command(cmds("help", "h")))
async def help_cmd(_, m):
    if authorized(m):
        await reply(m, HELP)


@app.on_message(filters.command(cmds("ping")))
async def ping_cmd(_, m):
    if not authorized(m):
        return
    t0 = time.time()
    msg = await reply(m, "🏓 Pinging...")
    await edit(msg, f"🏓 <b>Pong!</b> <code>{(time.time() - t0) * 1000:.0f} ms</code>")


# ------------------------------------------------------------------ handlers: /stats
def uploader_line():
    extra = " · 4GB ✅" if USER else ""
    if not C.FAST_UPLOAD:
        return "Library default" + extra
    return (
        f"Fast ({C.UPLOAD_SESSIONS}x{C.UPLOAD_WORKERS}) · done {fastupload.STATS['fast']}"
        f" / fallback {fastupload.STATS['fallback']}" + extra
    )


STATS_HOME = "<b><i>⌬ Bot &amp; OS Statistics!</i></b>"


def stats_rows(view):
    if view == "main":
        return [
            [B("Bot Stats", "sx|bot", GREEN), B("OS Stats", "sx|os", GREEN)],
            [B("Repo Stats", "sx|repo", GREEN), B("Bot Limits", "sx|limits", GREEN)],
            [B("Close", "sx|close", RED)],
        ]
    return [[B("Back", "sx|main", RED), B("Close", "sx|close", RED)]]


@app.on_message(filters.command(cmds("stats", "st")))
async def stats_cmd(_, m):
    if authorized(m):
        await reply(m, STATS_HOME, stats_rows("main"))


@app.on_callback_query(filters.regex(r"^sx\|"))
async def stats_cb(_, q):
    if not can_use(q.from_user.id, q.message.chat.id):
        return await q.answer("You are not allowed to use this.", show_alert=True)
    view = q.data.split("|")[1]
    if view == "close":
        try:
            await q.message.delete()
        except Exception:  # noqa: BLE001
            pass
        return await q.answer()
    if view == "bot":
        text = statsui.bot_stats(BOOT, uploader_line())
    elif view == "os":
        text = statsui.os_stats()
    elif view == "repo":
        text = await statsui.repo_stats(SESSION)
    elif view == "limits":
        text = statsui.limits_text(len(TASKS))
    else:
        view, text = "main", STATS_HOME
    await edit(q.message, text, stats_rows(view))
    await q.answer()


# ------------------------------------------------------------------ handlers: /botsettings (owner only)
def bs_panel():
    keys = list(limits.DEFS)
    lines = ["<b><u>☰ Bot Settings :</u></b>\n"]
    for i, k in enumerate(keys):
        sym = "┌" if i == 0 else ("└" if i == len(keys) - 1 else "├")
        lines.append(f"{sym} <b>{limits.DEFS[k][0]}</b> : {limits.text(k)}")
    lines.append("\n<i>0 = unlimited. The owner is never limited. Changes apply instantly.</i>")
    rows = []
    for i in range(0, len(keys), 2):
        rows.append([B(limits.DEFS[k][0], f"bs|ask|{k}", GREEN) for k in keys[i : i + 2]])
    rows.append([B("Close", "bs|close", RED)])
    return "\n".join(lines), rows


@app.on_message(filters.command(cmds("botsettings", "bs")))
async def botsettings_cmd(_, m):
    if m.from_user and m.from_user.id == C.OWNER_ID:
        text, rows = bs_panel()
        await reply(m, text, rows)


@app.on_callback_query(filters.regex(r"^bs\|"))
async def bs_cb(_, q):
    if q.from_user.id != C.OWNER_ID:
        return await q.answer("Only the owner can use this.", show_alert=True)
    PENDING.pop(q.from_user.id, None)
    p = q.data.split("|")
    if p[1] == "close":
        try:
            await q.message.delete()
        except Exception:  # noqa: BLE001
            pass
        return await q.answer()
    if p[1] == "ask":
        key = p[2]
        pend = {"kind": "bs:" + key, "msg": q.message, "chat": q.message.chat.id, "timeout": 60,
                "exp": time.time() + 60, "user": q.from_user}
        PENDING[q.from_user.id] = pend
        label = limits.DEFS[key][0]
        await edit(
            q.message,
            f"<b><u>☰ {label} :</u></b>\n\n➲ <b>Current</b> : {limits.text(key)}\n\n"
            "Send a number (<code>0</code> = unlimited).\n<b>Timeout:</b> <code>60 sec</code>",
            [[B("Stop Change", "bs|go", RED)], [B("Close", "bs|close", RED)]],
        )
        spawn(expire_prompt(q.from_user.id, pend))
        return await q.answer()
    text, rows = bs_panel()
    await edit(q.message, text, rows)
    await q.answer()


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
    """Files always go to the user's bot PM when the command is used in a group (not changeable)."""
    return [m.chat.id] if is_private(m) else [m.from_user.id]


@app.on_message(filters.command(cmds("leech", "l")))
async def leech_cmd(_, m):
    if not authorized(m):
        return
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

    uid = m.from_user.id
    if uid != C.OWNER_ID:  # the owner has no limits at all
        daily, per_user, per_bot = limits.get("daily"), limits.get("user_tasks"), limits.get("bot_tasks")
        if daily and S.daily_used(uid) >= daily:
            return await reply(m, f"❌ Daily task limit reached ({daily} per day). Try again tomorrow.")
        if per_user and sum(1 for x in TASKS.values() if x.uid == uid) >= per_user:
            return await reply(m, f"❌ You already have {per_user} running task(s). Wait for one to finish.")
        if per_bot and len(TASKS) >= per_bot:
            return await reply(m, f"❌ The bot is busy ({per_bot} tasks running). Try again in a moment.")

    s = S.get(uid)
    dests = pick_dests(m, s)
    if not is_private(m) and m.from_user.id in dests and not await notify_pm(m, link):
        rows = [[B("▶️ Start Bot", url=f"https://t.me/{BOT_USERNAME}?start=leech", style=GREEN)]]
        return await reply(
            m,
            f"⚠️ {m.from_user.mention}, start the bot in PM first so I can send your files, then run the command again.",
            rows,
        )

    S.bump_daily(uid)
    t = Task(m, link, z, e, name, thumb, dests)
    TASKS[t.id] = t
    spawn(
        log_send(
            "links",
            f"🔗 <b>New leech</b>\n┌ <b>User</b> : {m.from_user.mention} (<code>{uid}</code>)\n"
            f"├ <b>Chat</b> : <code>{m.chat.id}</code>\n"
            f"├ <b>Task</b> : <code>{t.id}</code>\n"
            f"└ <b>Link</b> : <code>{html.escape(U.shorten(link, 300))}</code>",
        )
    )
    spawn(run_task(t))
    await ensure_status(m.chat.id, m)


# ------------------------------------------------------------------ handlers: status & cancel
@app.on_message(filters.command(cmds("status", "s")))
async def status_cmd(_, m):
    if not authorized(m):
        return
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


@app.on_message(filters.command(cmds("cancel", "c")))
async def cancel_cmd(_, m):
    if not m.from_user or not authorized(m):
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
    lines = "\n".join(f"/c{SUF}_{t.id} — <code>{html.escape(U.shorten(t.name or t.link, 40))}</code>" for t in mine)
    await reply(m, "You have several tasks. Tap the one to cancel:\n\n" + lines)


@app.on_message(filters.regex(rf"^/(?:cancel|c){SUF}_(\w+)"))
async def cancel_msg(_, m):
    if not m.from_user or not authorized(m):
        return
    await reply(m, await do_cancel(m.from_user.id, m.matches[0].group(1)))


@app.on_message(filters.command(cmds("cancelall", "ca")))
async def cancelall_cmd(_, m):
    if not m.from_user or not authorized(m):
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


@app.on_message(filters.command(cmds("authorize", "a")))
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


@app.on_message(filters.command(cmds("unauthorize", "ua")))
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
def is_image_msg(m):
    return bool(m and (m.photo or (m.document and (m.document.mime_type or "").startswith("image"))))


async def save_thumb_from(m, uid):
    raw = await m.download(file_name=str(S.THUMBS / f"{uid}_raw"))
    try:
        S.prep_thumb(raw, S.THUMBS / f"{uid}.jpg")
    finally:
        if os.path.exists(raw):
            os.remove(raw)
    S.sync_thumb(uid)


@app.on_message(filters.command(cmds("usetting", "us")))
async def usetting_cmd(_, m):
    if not authorized(m):
        return
    if not m.from_user:
        return
    uid = m.from_user.id
    parts = (m.text or m.caption or "").split(maxsplit=3)
    if len(parts) >= 2 and parts[1].lower() in ("-s", "-set"):
        if len(parts) < 3:
            return await reply(m, f"Usage: <code>/us -s key value</code>\nKeys: <code>{S.KEYS_HELP}</code>")
        key = parts[2]
        value = parts[3] if len(parts) > 3 else ""
        if key.lower() == "thumb":
            src = m if is_image_msg(m) else m.reply_to_message
            if is_image_msg(src):
                await save_thumb_from(src, uid)
                return await reply(m, "✅ <b>Thumbnail</b> saved. Open /us to review.")
            if value.strip() in ("-", "none", "None"):
                S.delete_thumb(uid)
                return await reply(m, "✅ <b>Thumbnail</b> removed.")
            return await reply(m, "❌ Reply to a photo with <code>/us -s thumb</code> (or send <code>-</code> to remove).")
        return await reply(m, S.set_direct(uid, key, value))
    text, rows = S.panel(uid, "main", m.from_user)
    await reply(m, text, rows)


async def expire_prompt(uid, p):
    """After the timeout the prompt turns back into the menu (like WZML-X)."""
    await asyncio.sleep(p["timeout"])
    if PENDING.get(uid) is p:
        PENDING.pop(uid, None)
        try:
            if p["kind"].startswith("bs:"):
                text, rows = bs_panel()
            else:
                text, rows = S.panel(uid, "m_" + p["kind"], p["user"])
            await edit(p["msg"], text, rows)
        except Exception:  # noqa: BLE001
            pass


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
        elif key == "split" and value.isdigit():
            S.put(uid, "split", int(value))
        note = "Saved ✅"
    elif act == "clr":
        kind = args[0]
        S.clear_value(uid, kind)
        target, note = "m_" + kind, "Removed"
    elif act == "reset":
        target = "confirm"
    elif act == "resetyes":
        S.reset(uid)
        note = "All settings were reset"
    elif act == "ask":
        kind = args[0]
        wait = S.timeout_of(kind)
        pend = {"kind": kind, "msg": msg, "chat": msg.chat.id, "timeout": wait, "exp": time.time() + wait,
                "user": q.from_user}
        PENDING[uid] = pend
        await edit(msg, S.prompt_text(uid, kind), S.prompt_rows(uid, kind))
        spawn(expire_prompt(uid, pend))
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
    if kind.startswith("bs:"):  # owner: /botsettings value
        key = kind[3:]
        raw = (m.text or "").strip().replace(",", ".")
        try:
            value = float(raw)
            if value < 0:
                raise ValueError
        except ValueError:
            PENDING[uid] = p
            return await reply(m, "❌ Send a number, e.g. <code>2</code> (0 = unlimited).")
        limits.put(key, value)
        try:
            await m.delete()
        except Exception:  # noqa: BLE001
            pass
        text, rows = bs_panel()
        return await edit(p["msg"], text, rows)
    if kind == "thumb":
        if is_image_msg(m):
            await save_thumb_from(m, uid)
        elif (m.text or "").strip() == "-":
            S.delete_thumb(uid)
        else:
            PENDING[uid] = p
            return await reply(m, "❌ Please send a photo (or <code>-</code> to delete the thumbnail).")
    elif kind == "rclone":
        if not m.document:
            PENDING[uid] = p
            return await reply(m, "❌ Please send the <code>rclone.conf</code> file.")
        path = str(S.RCLONE / f"{uid}.conf")
        await m.download(file_name=path)
        try:
            ok = "[" in open(path, encoding="utf-8", errors="ignore").read(200000)
        except OSError:
            ok = False
        if ok:
            S.sync_rclone(uid)
        if not ok:
            S.delete_rclone(uid)
            PENDING[uid] = p
            return await reply(m, "❌ That does not look like an rclone.conf (no <code>[remote]</code> section).")
    else:
        result = S.set_direct(uid, kind, (m.text or "").strip())
        if result.startswith("❌"):
            PENDING[uid] = p
            return await reply(m, result)
    try:
        await m.delete()
    except Exception:  # noqa: BLE001 - bot is not an admin in this group
        pass
    text, rows = S.panel(uid, "m_" + kind, p["user"])
    await edit(p["msg"], text, rows)


# ------------------------------------------------------------------ main
async def main():
    global SESSION, BOT_USERNAME, USER, USER_ID
    shutil.rmtree(C.DOWNLOAD_DIR, ignore_errors=True)
    os.makedirs(C.DOWNLOAD_DIR, exist_ok=True)
    if not (C.API_ID and C.API_HASH and C.BOT_TOKEN):
        raise SystemExit("API_ID, API_HASH and BOT_TOKEN must be set in config.env (or as variables).")
    SESSION = aiohttp.ClientSession()
    await start_aria()
    try:
        await app.start()
    except Exception as e:  # noqa: BLE001
        log.error(
            "Telegram login failed: %r\nCheck API_ID / API_HASH (my.telegram.org) and BOT_TOKEN (@BotFather): "
            "no spaces or quotes, token must be the newest one.", e,
        )
        raise
    me = await app.get_me()
    BOT_USERNAME = me.username or ""
    if db.ENABLED:  # remember the bot login so the next start (even on a fresh container) reuses it
        key = f"session_{me.id}"
        try:
            if db.kv_get(key) is None:
                db.kv_set(key, await app.export_session_string())
        except Exception as e:  # noqa: BLE001
            log.debug("could not store the bot session: %r", e)
    if USER:
        try:
            await USER.start()
            user_me = await USER.get_me()
            if getattr(user_me, "is_premium", False):
                USER_ID = user_me.id
                S.MAX_UPLOAD = C.PREMIUM_SPLIT
                fastupload.install(USER)
                log.info("Premium string session active (%s): uploads up to 4GB", user_me.first_name)
            else:
                log.warning("STRING_SESSION account is not Premium - staying on the 2GB bot limit")
                await USER.stop()
                USER = None
        except Exception as e:  # noqa: BLE001
            log.error("STRING_SESSION could not start (%r) - staying on the 2GB bot limit", e)
            USER = None
    await init_styles(app)
    fastupload.install(app)
    await setup_commands()
    psutil.cpu_percent()  # prime the counter so the first reading is meaningful
    spawn(status_loop())
    if db.ENABLED:
        log.info("Data is stored in MongoDB")
    elif S.PERSISTENT:
        log.info("Persistent volume mounted at %s", S.DATA)
    else:
        log.warning("No volume mounted at %s - settings and thumbnails are lost on redeploy!", S.DATA)
    log.info("Bot started as @%s using %s", BOT_USERNAME, LIB_LABEL)
    await tg.idle()
    db.flush()
    if USER:
        try:
            await USER.stop()
        except Exception:  # noqa: BLE001
            pass
    await app.stop()
    if ARIA_PROC:
        ARIA_PROC.terminate()


if __name__ == "__main__":
    loop.run_until_complete(main())
