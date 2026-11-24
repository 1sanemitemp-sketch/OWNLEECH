"""Text builders for /stats (Bot Stats, OS Stats, Repo Stats, Bot Limits)."""
import asyncio
import os
import platform
import shutil
import time
from datetime import datetime, timezone

import psutil

import config as C
import limits
from utils import hsize

_REPO_CACHE = {"at": 0, "text": ""}


def bar(pct, width=10):
    pct = min(max(float(pct), 0.0), 100.0)
    step = 100 / width
    full = int(pct // step)
    half = (pct - full * step) >= step / 2 and full < width
    s = "●" * full + ("◑" if half else "")
    return "[" + s + "○" * (width - len(s)) + "]"


def uptime(sec):
    sec = int(max(sec, 0))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return (f"{d}d" if d else "") + (f"{h}h" if d or h else "") + (f"{m}m" if d or h or m else "") + f"{s}s"


def _title(text):
    return f"<b><i>⌬ {text}</i></b>"


def bot_stats(boot, uploader_line):
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    os.makedirs(C.DOWNLOAD_DIR, exist_ok=True)
    du = shutil.disk_usage(C.DOWNLOAD_DIR)
    try:
        io = psutil.disk_io_counters()
        read, write = hsize(io.read_bytes), hsize(io.write_bytes)
    except Exception:  # noqa: BLE001
        read = write = "Access Denied"
    return (
        f"{_title('BOT STATISTICS :')}\n"
        f"┌ <b>Bot Uptime</b> : {uptime(time.time() - boot)}\n"
        f"└ <b>Uploader</b> : {uploader_line}\n\n"
        f"{_title('RAM ( MEMORY ) :')}\n"
        f"┌ {bar(vm.percent)} {vm.percent}%\n"
        f"└ <b>U</b> : {hsize(vm.used)} | <b>F</b> : {hsize(vm.available)} | <b>T</b> : {hsize(vm.total)}\n\n"
        f"{_title('SWAP MEMORY :')}\n"
        f"┌ {bar(sw.percent)} {sw.percent}%\n"
        f"└ <b>U</b> : {hsize(sw.used)} | <b>F</b> : {hsize(sw.free)} | <b>T</b> : {hsize(sw.total)}\n\n"
        f"{_title('DISK :')}\n"
        f"┌ {bar(du.used / du.total * 100)} {du.used / du.total * 100:.1f}%\n"
        f"├ <b>Total Disk Read</b> : {read}\n"
        f"├ <b>Total Disk Write</b> : {write}\n"
        f"└ <b>U</b> : {hsize(du.used)} | <b>F</b> : {hsize(du.free)} | <b>T</b> : {hsize(du.total)}"
    )


def os_stats():
    net = psutil.net_io_counters()
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    try:
        usable = len(os.sched_getaffinity(0))
    except AttributeError:
        usable = logical
    try:
        load = ", ".join(f"{x / logical * 100:.2f}%" for x in os.getloadavg())
    except (OSError, AttributeError):
        load = "N/A"
    try:
        freq = f"{psutil.cpu_freq().current / 1000:.2f} GHz"
    except Exception:  # noqa: BLE001
        freq = "N/A"
    cpu = psutil.cpu_percent()
    return (
        f"{_title('OS SYSTEM :')}\n"
        f"├ <b>OS Uptime</b> : {uptime(time.time() - psutil.boot_time())}\n"
        f"├ <b>OS Version</b> : {platform.version()}\n"
        f"└ <b>OS Arch</b> : {platform.platform()}\n\n"
        f"{_title('NETWORK STATS :')}\n"
        f"├ <b>Upload Data</b> : {hsize(net.bytes_sent)}\n"
        f"├ <b>Download Data</b> : {hsize(net.bytes_recv)}\n"
        f"├ <b>Pkts Sent</b> : {net.packets_sent // 1000}k\n"
        f"├ <b>Pkts Received</b> : {net.packets_recv // 1000}k\n"
        f"└ <b>Total I/O Data</b> : {hsize(net.bytes_sent + net.bytes_recv)}\n\n"
        f"{_title('CPU :')}\n"
        f"├ {bar(cpu)} {cpu}%\n"
        f"├ <b>CPU Frequency</b> : {freq}\n"
        f"├ <b>System Avg Load</b> : {load} (1m, 5m, 15m)\n"
        f"├ <b>P-Core(s)</b> : {physical} | <b>V-Core(s)</b> : {max(logical - physical, 0)}\n"
        f"├ <b>Total Core(s)</b> : {logical}\n"
        f"└ <b>Usable CPU(s)</b> : {usable}"
    )


def limits_text(running_total=0):
    return (
        f"{_title('BOT LIMITATIONS :')}\n"
        f"┌ <b>Direct Limit</b> : {limits.text('direct')}\n"
        f"├ <b>YT-DLP Limit</b> : {limits.text('ytdlp')}\n"
        f"├ <b>Leech Limit</b> : {limits.text('leech')}\n"
        f"├ <b>Daily Tasks</b> : {limits.text('daily')}\n"
        f"└ <b>Parallel Uploads</b> : {limits.text('parallel')}\n\n"
        f"┌ <b>User Parallel Tasks</b> : {limits.text('user_tasks')}\n"
        f"└ <b>Bot Parallel Tasks</b> : {limits.text('bot_tasks')} (running {running_total})"
    )


async def repo_stats(session):
    if time.time() - _REPO_CACHE["at"] < 300 and _REPO_CACHE["text"]:
        return _REPO_CACHE["text"]
    updated = changelog = latest = ""
    remarks = "Version information unavailable"
    if C.UPSTREAM_REPO:
        base = f"https://api.github.com/repos/{C.UPSTREAM_REPO}"
        try:
            async with session.get(
                f"{base}/commits/{C.UPSTREAM_BRANCH}", timeout=_timeout(10)
            ) as r:
                data = await r.json()
            commit = data["commit"]
            when = datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
            ago = (datetime.now(timezone.utc) - when).total_seconds()
            updated = f"{when.astimezone().strftime('%d/%m/%Y')} ( {_ago(ago)} )"
            changelog = commit["message"].splitlines()[0][:200]
        except Exception:  # noqa: BLE001
            pass
        try:
            async with session.get(f"{base}/releases/latest", timeout=_timeout(10)) as r:
                latest = (await r.json()).get("tag_name", "") or ""
        except Exception:  # noqa: BLE001
            pass
        if latest:
            remarks = "Bot is up to date" if latest == C.BOT_VERSION else "A newer version is available"
    else:
        remarks = "Set UPSTREAM_REPO (owner/repo) to see repo info"
    text = (
        f"{_title('REPO STATISTICS :')}\n"
        f"├ <b>Bot Updated</b> : {updated or 'N/A'}\n"
        f"├ <b>Current Version</b> : {C.BOT_VERSION}\n"
        f"├ <b>Latest Version</b> : {latest or 'N/A'}\n"
        f"└ <b>Last ChangeLog</b> : <code>{_esc(changelog or 'N/A')}</code>\n\n"
        f"{_title('REMARKS :')} <code>{_esc(remarks)}</code>"
    )
    _REPO_CACHE.update(at=time.time(), text=text)
    return text


def _ago(sec):
    sec = int(sec)
    if sec < 3600:
        return f"{max(sec // 60, 1)} minutes ago"
    if sec < 86400:
        return f"{sec // 3600} hours ago"
    return f"{sec // 86400} days ago"


def _timeout(sec):
    import aiohttp

    return aiohttp.ClientTimeout(total=sec)


def _esc(x):
    import html

    return html.escape(str(x))
