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
import rename_utils
from compat import GREEN, RED, B
from utils import FONTS, hsize, htime, parse_pairs, parse_size, parse_words

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
    else:
        USERS.pop(str(uid), None)
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


def prep_thumb(src, dst):
    """Save an image as an HD JPEG thumbnail (longest side C.THUMB_MAX)."""
    im = Image.open(src).convert("RGB")
    im.thumbnail((C.THUMB_MAX, C.THUMB_MAX))
    quality = 92
    while True:
        im.save(dst, "JPEG", quality=quality, optimize=True)
        if quality <= 70 or os.path.getsize(dst) <= 300 * 1024:
            break
        quality -= 8


def rclone_of(uid):
    p = RCLONE / f"{uid}.conf"
    return str(p) if p.exists() else None


def delete_rclone(uid):
    p = RCLONE / f"{uid}.conf"
    if p.exists():
        p.unlink()


def split_limit(s):
    return min(s["split"] or C.SPLIT_SIZE, C.SPLIT_SIZE)


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
        "Variables : <code>{filename} {size} {duration} {resolution} {ext}</code>",
    },
    "prefix": {
        "title": "Prefix Settings", "label": "Leech Prefix", "set": "Set Prefix", "back": "leech",
        "desc": "Text added before the name of every leeched file",
        "ask": "Send the prefix. Use <code>\\s</code> for a space at the end.\nExample: <code>[MySite]\\s</code>",
    },
    "suffix": {
        "title": "Suffix Settings", "label": "Leech Suffix", "set": "Set Suffix", "back": "leech",
        "desc": "Text added after the name of every leeched file (before the extension)",
        "ask": "Send the suffix. Use <code>\\s</code> for a space at the start.\nExample: <code>\\s@MyChannel</code>",
    },
    "remove": {
        "title": "Remove Settings", "label": "Leech Remove", "set": "Set Remove Words", "back": "leech",
        "desc": "Words that are removed from the name of every leeched file",
        "ask": "Send words to remove, separated by commas.\n<code>www.site.com, [Group], 1080p</code>",
    },
    "replace": {
        "title": "Replace Settings", "label": "Leech Replace", "set": "Set Replace Pairs", "back": "leech",
        "desc": "Words that are replaced in the name of every leeched file",
        "ask": "Send <code>old:new</code> pairs separated by commas.\n<code>WEB-DL:WEBDL, Ep:E</code>",
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
        f"{m['ask']}\n<b>Timeout:</b> <code>60 sec</code>\n"
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
    limit = C.DAILY_TASK_LIMIT
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
        f"➲ <b>Telegram limit</b> : <i>{hsize(C.SPLIT_SIZE)} per file</i>\n\n"
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
    elif key == "remove":
        put(uid, key, "" if clear else ", ".join(parse_words(value)))
    elif key == "replace":
        pairs = parse_pairs(value)
        if not clear and not pairs:
            return "❌ Use <code>old:new</code> pairs, e.g. <code>/us -s replace WEB-DL:WEBDL</code>"
        put(uid, key, "" if clear else ", ".join(f"{a}:{b}" for a, b in pairs))
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
            put(uid, "split", min(size, C.SPLIT_SIZE))
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
