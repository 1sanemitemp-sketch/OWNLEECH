"""Per-user settings: storage and the /us (user settings) panels.

Panels: main -> Universal / Mirror / Leech. Every text option opens its own small menu
(thumbnail, caption, prefix, ...), exactly like WZML-X.
"""
import html
import json
import logging
import os
import time
from pathlib import Path

from PIL import Image

import config as C
import db
import limits
import rename_utils
from compat import GREEN, RED, B
from utils import FONTS, hsize, htime, parse_pairs, parse_remove, parse_replace, parse_size, parse_words

log = logging.getLogger("leechbot.settings")

# ----------------------------------------------------------------- storage
DATA = Path(C.DATA_DIR)
try:
    DATA.mkdir(parents=True, exist_ok=True)
except OSError:  # e.g. /data is not writable when running locally
    DATA = Path("./data")
    DATA.mkdir(parents=True, exist_ok=True)
THUMBS = DATA / "thumbs"
THUMBS.mkdir(parents=True, exist_ok=True)
RCLONE = DATA / "rclone"
RCLONE.mkdir(parents=True, exist_ok=True)
DB = DATA / "users.json"
PERSISTENT = os.path.ismount(str(DATA))

try:
    USERS = json.loads(DB.read_text())
except Exception:  # noqa: BLE001
    USERS = {}

MAX_UPLOAD = C.SPLIT_SIZE  # biggest file Telegram accepts for this bot (raised to 4 GB with a Premium string session)

if db.init():  # MongoDB is the source of truth when DATABASE_URL is set
    _remote = db.users_all()
    if _remote:
        USERS = _remote
        DB.write_text(json.dumps(USERS))
    else:  # first start with MongoDB: upload what we already have
        for _uid, _data in USERS.items():
            db.user_set(_uid, _data)
    for _uid, _blob in db.files_all("thumb").items():
        (THUMBS / f"{_uid}.jpg").write_bytes(_blob)
    for _uid, _blob in db.files_all("rclone").items():
        (RCLONE / f"{_uid}.conf").write_bytes(_blob)

DEFAULT = {
    "as_doc": False,  # False = Media, True = Document
    "prefix": "",
    "suffix": "",
    "remove": "",  # comma separated words
    "replace": "",  # comma separated  old:new  pairs
    "caption": "",  # template, empty = file name
    "font": "bold",
    "rename": "",  # auto rename template
    "shots": 0,  # screenshots per video (0 = off)
    "split": 0,  # split size in bytes (0 = auto / max)
    "equal": False,  # equal sized split parts
    "ytopts": "",  # yt-dlp options (JSON)
    "mediainfo": False,
}
INTERNAL = ("daily", "last_used")  # bookkeeping, never reset

SHOT_STEPS = [0, 3, 5, 7, 10]
SPLIT_OPTIONS = [
    ("Auto (Max)", 0),
    ("1.5GB", int(1.5 * 1024**3)),
    ("1GB", 1024**3),
    ("500MB", 500 * 1024**2),
]
BLOCKED_YT = {
    "outtmpl", "paths", "exec", "exec_cmd", "postprocessors", "cookiefile", "cachedir", "download_archive",
    "batchfile", "load_info_filename", "progress_hooks", "logger", "external_downloader",
    "external_downloader_args", "ffmpeg_location", "plugin_dirs",
}


def get(uid):
    return {**DEFAULT, **USERS.get(str(uid), {})}


def _save():
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(USERS))
    os.replace(tmp, DB)


def put(uid, key, value):
    USERS.setdefault(str(uid), {})[key] = value
    _save()
    db.user_set(uid, USERS[str(uid)])


def has_changes(uid):
    """True when the user changed anything (decides whether the Reset button is shown)."""
    cur = USERS.get(str(uid), {})
    if any(k in DEFAULT and cur[k] != DEFAULT[k] for k in cur):
        return True
    return bool(thumb_of(uid) or rclone_of(uid))


def reset(uid):
    keep = {k: v for k, v in USERS.get(str(uid), {}).items() if k in INTERNAL}
    if keep:
        USERS[str(uid)] = keep
        db.user_set(uid, keep)
    else:
        USERS.pop(str(uid), None)
        db.user_del(uid)
    _save()
    delete_thumb(uid)
    delete_rclone(uid)


# ----------------------------------------------------------------- files
def thumb_of(uid):
    p = THUMBS / f"{uid}.jpg"
    return str(p) if p.exists() else None


def delete_thumb(uid):
    p = THUMBS / f"{uid}.jpg"
    if p.exists():
        p.unlink()
    db.file_del("thumb", uid)


def sync_thumb(uid):
    """Push the saved thumbnail of ``uid`` to MongoDB (no-op without DATABASE_URL)."""
    if db.ENABLED and thumb_of(uid):
        db.file_set("thumb", uid, Path(thumb_of(uid)).read_bytes())


def sync_rclone(uid):
    if db.ENABLED and rclone_of(uid):
        db.file_set("rclone", uid, Path(rclone_of(uid)).read_bytes())


def prep_thumb(src, dst, max_bytes=200 * 1024):
    """Save an image as a JPEG thumbnail: as big as possible (up to C.THUMB_MAX) but under 200 KB."""
    im = Image.open(src).convert("RGB")
    side = C.THUMB_MAX
    while True:
        small = im.copy()
        small.thumbnail((side, side))
        for quality in (92, 85, 78, 70, 62):
            small.save(dst, "JPEG", quality=quality, optimize=True)
            if os.path.getsize(dst) <= max_bytes:
                return
        if side <= 320:
            return
        side = max(320, int(side * 0.8))


def rclone_of(uid):
    p = RCLONE / f"{uid}.conf"
    return str(p) if p.exists() else None


def delete_rclone(uid):
    p = RCLONE / f"{uid}.conf"
    if p.exists():
        p.unlink()
    db.file_del("rclone", uid)


def split_limit(s):
    return min(s["split"] or MAX_UPLOAD, MAX_UPLOAD)


# ----------------------------------------------------------------- daily tasks / last used
def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def daily_used(uid):
    d = USERS.get(str(uid), {}).get("daily") or {}
    return int(d.get("n", 0)) if d.get("date") == _today() else 0


def bump_daily(uid):
    put(uid, "daily", {"date": _today(), "n": daily_used(uid) + 1})
    put(uid, "last_used", time.time())


def yt_opts(uid):
    """The user's yt-dlp options as a dict ({} when unset or invalid)."""
    opts, _ = parse_ytopts(USERS.get(str(uid), {}).get("ytopts", ""))
    return opts or {}


def parse_ytopts(text):
    text = (text or "").strip()
    if not text:
        return {}, None
    try:
        data = json.loads(text)
    except ValueError:
        return None, 'Send valid JSON, e.g. <code>{"format": "bv*[height&lt;=720]+ba/b"}</code>'
    if not isinstance(data, dict):
        return None, "The options must be a JSON object { ... }"
    bad = sorted(k for k in data if k in BLOCKED_YT)
    if bad:
        return None, f"These options are not allowed: <code>{', '.join(bad)}</code>"
    return data, None


# ----------------------------------------------------------------- text helpers
def _e(x):
    return html.escape(str(x))


def _val(x, limit=40):
    x = str(x or "").strip()
    if not x:
        return "<i>Not Exists</i>"
    x = x.replace("\n", " ")
    return f"<code>{_e(x if len(x) <= limit else x[: limit - 1] + '…')}</code>"


def _onoff(flag):
    return "Enabled" if flag else "Disabled"


def cb(uid, *parts):
    return "|".join(["us", str(uid), *map(str, parts)])


def _who(uid, user):
    if user is not None:
        full = " ".join(x for x in (user.first_name, getattr(user, "last_name", None)) if x)
        if full:
            return _e(full)
    return str(uid)


def _tail(uid, back):
    return [B("Back", cb(uid, "go", back), RED), B("Close", cb(uid, "close"), RED)]


# ----------------------------------------------------------------- generic option menus
RENAME_GUIDE = rename_utils.TEMPLATE_GUIDE_URL
CAPTION_GUIDE = "https://t.me/c/4291361651/33"
DEFAULT_TIMEOUT = 60


def timeout_of(kind):
    return MENUS[kind].get("timeout", DEFAULT_TIMEOUT)


MENUS = {
    "thumb": {
        "title": "Thumbnail Settings", "label": "Custom Thumbnail", "set": "Set Thumbnail", "back": "leech",
        "desc": "Custom Thumbnail to appear on the Leeched files uploaded by the bot",
        "ask": "Send a photo to save it as custom thumbnail.\nAlternatively: <code>/us [photo] -s thumb</code>",
    },
    "caption": {
        "title": "Caption Settings", "label": "Leech Filename Caption", "set": "Set Caption", "back": "leech",
        "desc": "Leech Caption is the Custom Caption on the Leech Files Uploaded by the bot",
        "ask": "Send Leech Caption. You can add HTML tags.\n"
        f'Documentation Here : <a href="{CAPTION_GUIDE}">Click Here</a>',
    },
    "prefix": {
        "title": "Prefix Settings", "label": "Leech Prefix", "set": "Set Prefix", "back": "leech",
        "desc": "Text added before the name of every leeched file",
        "ask": "Send the prefix. It supports HTML tags.\nUse <code>\\s</code> to add a space after it.\n"
        "Example: <code>@channel_name\\s</code>",
    },
    "suffix": {
        "title": "Suffix Settings", "label": "Leech Suffix", "set": "Set Suffix", "back": "leech",
        "desc": "Text added after the name of every leeched file (before the extension)",
        "ask": "Send the suffix. It supports HTML tags.\nUse <code>\\s</code> to add a space before it.\n"
        "Example: <code>\\s@channel_name</code>",
    },
    "remove": {
        "title": "Remove Settings", "label": "Remove", "set": "Set Remove", "back": "leech", "timeout": 120,
        "desc": "Remove exact words or regex patterns from leeched filenames. Rules are case-sensitive "
        "and the file extension is protected.",
        "ask": "Send Remove rules separated by <code>|</code>.\n"
        "Example: <code>Love|Power|R:www\\.example\\.com[\\s_-]*</code>\n"
        "Regex rules must start with <code>R:</code>. Use <code>\\|</code> for regex alternation.",
    },
    "replace": {
        "title": "Replace Settings", "label": "Replace", "set": "Set Replace", "back": "leech", "timeout": 120,
        "desc": "Find, replace, or delete text in leeched filenames. Rules run top to bottom and the file "
        "extension is protected.",
        "ask": "Send one Replace rule per line.\n<code>OLD|NEW|CASE|TIMES</code>\n\n"
        "<b>CASE:</b> 0 = insensitive (default), 1/I = sensitive\n"
        "<b>TIMES:</b> 0 = all (default), or a positive number\n\n<b>Examples:</b>\n"
        "<code>.| |0\nCinemas|\nHD|FullHD|1|1</code>",
    },
    "rename": {
        "title": "Auto Rename Settings", "label": "Leech Auto Rename", "set": "Set Auto Rename", "back": "leech",
        "desc": "Rename every leeched file with a format built from the info found in the file name "
        "and in the file itself",
        "ask": f'☆ Send your Auto Rename format.\nGuide: <a href="{RENAME_GUIDE}">Click Here</a>',
    },
    "ytopts": {
        "title": "YT-DLP Options", "label": "YT-DLP Options", "set": "Set YT-DLP Options", "back": "uni",
        "desc": "Extra yt-dlp options (JSON) used when the link is downloaded with yt-dlp",
        "ask": 'Send the options as JSON.\n<code>{"format": "bv*[height&lt;=1080]+ba/b"}</code>',
    },
    "rclone": {
        "title": "RClone Settings", "label": "RClone.Conf File", "set": "Set RClone", "back": "mirror",
        "desc": "RClone is a command-line program to sync files and directories to and from different "
        "cloud storage providers like GDrive, OneDrive...",
        "ask": "Send rclone.conf.",
    },
}


def _menu_value(uid, kind, s):
    if kind == "thumb":
        return "Exists" if thumb_of(uid) else "Not Exists"
    if kind == "rclone":
        return "Exists" if rclone_of(uid) else "Not Exists"
    return _val(s[kind])


def _menu_exists(uid, kind, s):
    if kind == "thumb":
        return bool(thumb_of(uid))
    if kind == "rclone":
        return bool(rclone_of(uid))
    return bool(s[kind])


def _menu(uid, kind, s, user):
    m = MENUS[kind]
    text = (
        f"<b><u>☰ {m['title']} :</u></b>\n\n"
        f"➲ <b>{m['label']}</b> : {_menu_value(uid, kind, s)}\n\n"
        f"➲ <b>Description</b> : <i>{m['desc']}</i>"
    )
    rows = [[B(m["set"], cb(uid, "ask", kind), GREEN)]]
    if _menu_exists(uid, kind, s):
        rows.append([B("Remove", cb(uid, "clr", kind), RED)])
    rows.append(_tail(uid, m["back"]))
    return text, rows


def prompt_text(uid, kind):
    m = MENUS[kind]
    s = get(uid)
    return (
        f"<b><u>☰ {m['title']} :</u></b>\n\n"
        f"➲ <b>{m['label']}</b> : {_menu_value(uid, kind, s)}\n\n"
        f"➲ <b>Description</b> : <i>{m['desc']}</i>\n\n"
        f"{m['ask']}\n<b>Timeout:</b> <code>{timeout_of(kind)} sec</code>\n"
        "<i>(In groups, reply to this message.)</i>"
    )


def prompt_rows(uid, kind):
    return [[B("Stop Change", cb(uid, "go", "m_" + kind), RED)], _tail(uid, MENUS[kind]["back"])]


def clear_value(uid, kind):
    if kind == "thumb":
        delete_thumb(uid)
    elif kind == "rclone":
        delete_rclone(uid)
    else:
        put(uid, kind, "")


# ----------------------------------------------------------------- panels
def panel(uid, name, user=None):
    """Return (text, rows) for panel ``name``: main | uni | mirror | leech | font | split | confirm | m_<kind>."""
    s = get(uid)
    if name.startswith("m_") and name[2:] in MENUS:
        return _menu(uid, name[2:], s, user)
    return {
        "main": _main,
        "uni": _universal,
        "mirror": _mirror,
        "leech": _leech,
        "font": _font,
        "split": _split,
        "confirm": _confirm,
    }.get(name, _main)(uid, s, user)


def _main(uid, s, user):
    if user is not None:
        full = " ".join(x for x in (user.first_name, getattr(user, "last_name", None)) if x)
        uname = f"@{user.username}" if getattr(user, "username", None) else "N/A"
        dc = getattr(user, "dc_id", None) or "N/A"
    else:
        full, uname, dc = "N/A", "N/A", "N/A"
    text = (
        "<b><u>☰ User Settings :</u></b>\n\n"
        f"┌ <b>Name</b> : {_e(full)} ( <code>{uid}</code> )\n"
        f"├ <b>Username</b> : {_e(uname)}\n"
        f"├ <b>Telegram DC</b> : {_e(dc)}\n"
        "└ <b>Language</b> : English\n\n"
        "<b>➲ <u>Available Args :</u></b>\n"
        "• <code>-s</code> or <code>-set</code> : Set Directly via Arg\n"
        "   <code>/us -s font bold</code>\n"
        "   <code>/us -s prefix [GRP]\\s</code>\n"
        "• Keys : <code>thumb prefix suffix remove replace caption font rename mode split equal shots "
        "mediainfo ytopts reset</code>"
    )
    rows = [
        [B("Universal Settings", cb(uid, "go", "uni"), GREEN)],
        [B("Mirror Settings", cb(uid, "go", "mirror"), GREEN)],
        [B("Leech Settings", cb(uid, "go", "leech"), GREEN)],
    ]
    if has_changes(uid):
        rows.append([B("Reset Setting", cb(uid, "reset"), RED)])
    rows.append([B("Close", cb(uid, "close"), RED)])
    return text, rows


def _confirm(uid, s, user):
    text = "<b>Do you want to Reset Settings ?</b>"
    rows = [
        [B("Yes", cb(uid, "resetyes"), GREEN), B("No", cb(uid, "go", "main"), RED)],
        [B("Close", cb(uid, "close"), RED)],
    ]
    return text, rows


def _universal(uid, s, user):
    limit = limits.get("daily")
    used = daily_used(uid)
    last = USERS.get(str(uid), {}).get("last_used")
    text = (
        f"<b><u>☰ Universal Settings</u> : {_who(uid, user)}</b>\n\n"
        f"┌ <b>YT-DLP Options</b> : {_val(s['ytopts'])}\n"
        f"├ <b>Daily Tasks</b> : {used if limit else '∞'} / {limit or '∞'} per day\n"
        f"├ <b>Last Bot Used</b> : {htime(time.time() - last) + ' ago' if last else 'Never'}\n"
        f"├ <b>MediaInfo Mode</b> : {_onoff(s['mediainfo'])}\n"
        "└ <b>User Bot PM</b> : Force Enabled"
    )
    rows = [
        [
            B("YT-DLP Options", cb(uid, "go", "m_ytopts"), GREEN),
            B(
                "Disable MediaInfo" if s["mediainfo"] else "Enable MediaInfo",
                cb(uid, "tog", "mediainfo", "uni"),
                RED if s["mediainfo"] else GREEN,
            ),
        ],
        _tail(uid, "main"),
    ]
    return text, rows


def _mirror(uid, s, user):
    text = (
        f"<b><u>☰ Mirror/Clone Settings</u> : {_who(uid, user)}</b>\n\n"
        f"└ <b>RClone Config</b> : {'Exists' if rclone_of(uid) else 'Not Exists'}"
    )
    rows = [[B("RClone", cb(uid, "go", "m_rclone"), GREEN)], _tail(uid, "main")]
    return text, rows


def _leech(uid, s, user):
    shots = s["shots"]
    text = (
        f"<b><u>☰ Leech Settings</u> : {_who(uid, user)}</b>\n\n"
        f"┌ <b>Leech Type</b> : {'Document' if s['as_doc'] else 'Media'}\n"
        f"├ <b>Leech Thumbnail</b> : {'Exists' if thumb_of(uid) else 'Not Exists'}\n"
        f"├ <b>Leech Split Size</b> : {'Auto' if not s['split'] else hsize(s['split'])} "
        f"(max {hsize(split_limit(s))})\n"
        f"├ <b>Equal Splits</b> : {_onoff(s['equal'])}\n"
        f"├ <b>Leech Prefix</b> : {_val(s['prefix'])}\n"
        f"├ <b>Leech Suffix</b> : {_val(s['suffix'])}\n"
        f"├ <b>Leech Remove</b> : {_val(s['remove'])}\n"
        f"├ <b>Leech Replace</b> : {_val(s['replace'])}\n"
        f"├ <b>Leech Caption</b> : {_val(s['caption'])}\n"
        f"├ <b>Caption Font</b> : {FONTS[s['font']][0]}\n"
        f"├ <b>Leech Auto Rename</b> : {_val(s['rename'])}\n"
        f"└ <b>Screenshots</b> : {shots if shots else 'Disabled'}"
    )
    rows = [
        [
            B("Send As Media" if s["as_doc"] else "Send As Document", cb(uid, "tog", "as_doc", "leech"), GREEN),
            B("Thumbnail", cb(uid, "go", "m_thumb"), GREEN),
        ],
        [
            B("Leech Split Size", cb(uid, "go", "split"), GREEN),
            B(
                "Disable Equal Splits" if s["equal"] else "Enable Equal Splits",
                cb(uid, "tog", "equal", "leech"),
                RED if s["equal"] else GREEN,
            ),
        ],
        [B("Leech Prefix", cb(uid, "go", "m_prefix"), GREEN), B("Leech Suffix", cb(uid, "go", "m_suffix"), GREEN)],
        [B("Leech Remove", cb(uid, "go", "m_remove"), GREEN), B("Leech Replace", cb(uid, "go", "m_replace"), GREEN)],
        [B("Leech Caption", cb(uid, "go", "m_caption"), GREEN), B("Caption Font", cb(uid, "go", "font"), GREEN)],
        [
            B("Leech Auto Rename", cb(uid, "go", "m_rename"), GREEN),
            B(f"Screenshot: {shots if shots else 'OFF'}", cb(uid, "cyc", "shots", "leech"), GREEN),
        ],
        _tail(uid, "main"),
    ]
    return text, rows


def _font(uid, s, user):
    text = (
        "<b><u>☰ Caption Font Settings :</u></b>\n\n"
        f"➲ <b>Current Font</b> : <i>{FONTS[s['font']][0]}</i>\n\n"
        "The selected Telegram font is applied to the full upload caption."
    )
    keys = list(FONTS)
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                B(("✅ " if s["font"] == k else "") + FONTS[k][0], cb(uid, "set", "font", k, "font"), GREEN)
                for k in keys[i : i + 2]
            ]
        )
    rows.append(_tail(uid, "leech"))
    return text, rows


def _split(uid, s, user):
    text = (
        "<b><u>☰ Leech Split Size :</u></b>\n\n"
        f"➲ <b>Current</b> : <i>{'Auto' if not s['split'] else hsize(s['split'])}</i>\n"
        f"➲ <b>Telegram limit</b> : <i>{hsize(MAX_UPLOAD)} per file</i>\n\n"
        "Files bigger than the selected size are split into parts."
    )
    rows, row = [], []
    for label, size in SPLIT_OPTIONS:
        row.append(B(("✅ " if s["split"] == size else "") + label, cb(uid, "set", "split", size, "split"), GREEN))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_tail(uid, "leech"))
    return text, rows


# ----------------------------------------------------------------- direct set: /us -s key value
KEYS_HELP = (
    "thumb, prefix, suffix, remove, replace, caption, font, rename, mode, split, equal, shots, "
    "mediainfo, ytopts, reset"
)


def set_direct(uid, key, value):
    """Apply ``/us -s key value``. Returns a user-facing HTML message (starts with ❌ on error)."""
    key = (key or "").lower()
    value = (value or "").strip()
    clear = value in ("-", "none", "None", "")
    if key == "reset":
        reset(uid)
        return "✅ All settings were reset."
    if key in ("prefix", "suffix", "caption", "rename"):
        put(uid, key, "" if clear else value)
    elif key in ("remove", "replace"):
        if not clear:
            try:
                (parse_remove if key == "remove" else parse_replace)(value)
            except ValueError as e:
                return f"❌ {e}"
        put(uid, key, "" if clear else value)
    elif key == "ytopts":
        if not clear:
            _, err = parse_ytopts(value)
            if err:
                return "❌ " + err
        put(uid, key, "" if clear else value)
    elif key == "font":
        alias = {
            "strikethrough": "strike", "code": "mono", "mono/code": "mono", "monospace": "mono",
            "preformatted": "pre",
        }
        k = alias.get(value.lower(), value.lower()) if not clear else "default"
        if k not in FONTS:
            return f"❌ Font must be one of: <code>{', '.join(FONTS)}</code>"
        put(uid, "font", k)
    elif key in ("shots", "screenshot", "screenshots"):
        if not value.isdigit() or not 0 <= int(value) <= 10:
            return "❌ Screenshots must be a number from 0 to 10."
        put(uid, "shots", int(value))
    elif key == "split":
        if value.lower() in ("auto", "max", "0") or clear:
            put(uid, "split", 0)
        else:
            size = parse_size(value)
            if not size or size < 50 * 1024**2:
                return "❌ Use a size like <code>1gb</code>, <code>500mb</code> or <code>auto</code> (min 50MB)."
            put(uid, "split", min(size, MAX_UPLOAD))
    elif key == "mode":
        if value.lower() not in ("media", "document", "doc"):
            return "❌ Mode must be <code>media</code> or <code>document</code>"
        put(uid, "as_doc", value.lower() != "media")
    elif key in ("equal", "equalsplits", "mediainfo"):
        if value.lower() not in ("on", "off", "true", "false", "1", "0"):
            return "❌ Use <code>on</code> or <code>off</code>"
        put(uid, "mediainfo" if key == "mediainfo" else "equal", value.lower() in ("on", "true", "1"))
    else:
        return f"❌ Unknown key. Available: <code>{KEYS_HELP}</code>"
    return f"✅ <b>{_e(key)}</b> updated. Open /us to review."
