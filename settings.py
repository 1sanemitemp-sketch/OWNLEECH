"""Per-user settings: storage and the /us (user settings) panels."""
import html
import json
import logging
import os
from pathlib import Path

from PIL import Image

import config as C
from compat import GREEN, RED, B
from utils import FONTS, hsize, parse_pairs, parse_size, parse_words

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
    "auto_thumb": True,
    "shots": 0,  # screenshots per video (0 = off)
    "split": 0,  # split size in bytes (0 = auto / max)
    "dest": "pm",  # pm | chat | both   (only matters when leeching inside a group)
}

DEST_LABELS = {"pm": "PM Only", "chat": "Group Only", "both": "PM + Group"}
SHOT_STEPS = [0, 3, 5, 7, 10]
SPLIT_OPTIONS = [
    ("Auto (Max)", 0),
    ("1.5GB", int(1.5 * 1024**3)),
    ("1GB", 1024**3),
    ("500MB", 500 * 1024**2),
]


def get(uid):
    return {**DEFAULT, **USERS.get(str(uid), {})}


def _save():
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(USERS))
    os.replace(tmp, DB)


def put(uid, key, value):
    USERS.setdefault(str(uid), {})[key] = value
    _save()


def reset(uid):
    USERS.pop(str(uid), None)
    _save()
    delete_thumb(uid)


def thumb_of(uid):
    p = THUMBS / f"{uid}.jpg"
    return str(p) if p.exists() else None


def delete_thumb(uid):
    p = THUMBS / f"{uid}.jpg"
    if p.exists():
        p.unlink()


def prep_thumb(src, dst):
    im = Image.open(src).convert("RGB")
    im.thumbnail((320, 320))
    im.save(dst, "JPEG", quality=85)


def split_limit(s):
    return min(s["split"] or C.SPLIT_SIZE, C.SPLIT_SIZE)


# ----------------------------------------------------------------- text helpers
def _e(x):
    return html.escape(str(x))


def _val(x, limit=40):
    x = str(x or "").strip()
    if not x:
        return "<i>Not Set</i>"
    x = x.replace("\n", " ")
    return f"<code>{_e(x if len(x) <= limit else x[: limit - 1] + '…')}</code>"


def _onoff(flag):
    return "Enabled" if flag else "Disabled"


def _mk(label, on=False):
    return ("✅ " if on else "") + label


def cb(uid, *parts):
    return "|".join(["us", str(uid), *map(str, parts)])


# ----------------------------------------------------------------- panels
def panel(uid, name, user=None):
    """Return (text, rows) for panel ``name``: main | uni | leech | font | dest | split."""
    s = get(uid)
    return {
        "main": _main,
        "uni": _universal,
        "leech": _leech,
        "font": _font,
        "dest": _dest,
        "split": _split,
    }.get(name, _main)(uid, s, user)


def _main(uid, s, user):
    if user is not None:
        full = " ".join(x for x in (user.first_name, getattr(user, "last_name", None)) if x)
        uname = f"@{user.username}" if getattr(user, "username", None) else "N/A"
        dc = getattr(user, "dc_id", None) or "N/A"
    else:
        full, uname, dc = "N/A", "N/A", "N/A"
    text = (
        "<b><u>⚙️ User Settings :</u></b>\n\n"
        f"┌ <b>Name</b> : {_e(full)}\n"
        f"├ <b>ID</b> : <code>{uid}</code>\n"
        f"├ <b>Username</b> : {_e(uname)}\n"
        f"├ <b>Telegram DC</b> : {_e(dc)}\n"
        "└ <b>Language</b> : English\n\n"
        "<b>➲ <u>Available Args :</u></b>\n"
        "• <code>-s</code> or <code>-set</code> : Set directly via arg\n"
        "   <code>/us -s font bold</code>\n"
        "   <code>/us -s prefix [GRP]</code>\n"
        "• Keys : <code>prefix suffix remove replace caption font dest shots split mode autothumb reset</code>"
    )
    rows = [
        [B("Universal Settings", cb(uid, "go", "uni"), GREEN)],
        [B("Leech Settings", cb(uid, "go", "leech"), GREEN)],
        [B("Reset Setting", cb(uid, "reset"), RED)],
        [B("Close", cb(uid, "close"), RED)],
    ]
    return text, rows


def _universal(uid, s, user):
    text = (
        "<b><u>⚙️ Universal Settings :</u></b>\n\n"
        f"┌ <b>Leech Destination</b> : {DEST_LABELS[s['dest']]}\n"
        f"├ <b>Prefix</b> : {_val(s['prefix'])}\n"
        f"├ <b>Suffix</b> : {_val(s['suffix'])}\n"
        f"├ <b>Remove</b> : {_val(s['remove'])}\n"
        f"└ <b>Replace</b> : {_val(s['replace'])}\n\n"
        "<i>Destination applies when you leech inside a group: files are sent to your bot PM "
        "(default), to the group, or both.</i>"
    )
    rows = [
        [B(_mk("Leech Destination", s["dest"] != "pm"), cb(uid, "go", "dest"), GREEN)],
        [
            B(_mk("Prefix", bool(s["prefix"])), cb(uid, "ask", "prefix", "uni"), GREEN),
            B(_mk("Suffix", bool(s["suffix"])), cb(uid, "ask", "suffix", "uni"), GREEN),
        ],
        [
            B(_mk("Remove", bool(s["remove"])), cb(uid, "ask", "remove", "uni"), GREEN),
            B(_mk("Replace", bool(s["replace"])), cb(uid, "ask", "replace", "uni"), GREEN),
        ],
        [B("Back", cb(uid, "go", "main"), RED), B("Close", cb(uid, "close"), RED)],
    ]
    return text, rows


def _leech(uid, s, user):
    has_thumb = bool(thumb_of(uid))
    shots = s["shots"]
    text = (
        "<b><u>⚙️ Leech Settings :</u></b>\n\n"
        f"┌ <b>Send As</b> : {'Document' if s['as_doc'] else 'Media'}\n"
        f"├ <b>Thumbnail</b> : {'Exists' if has_thumb else 'Not Exists'}\n"
        f"├ <b>Auto Thumbnail</b> : {_onoff(s['auto_thumb'])}\n"
        f"├ <b>Leech Caption</b> : {_val(s['caption'])}\n"
        f"├ <b>Caption Font</b> : {FONTS[s['font']][0]}\n"
        f"├ <b>Screenshots</b> : {shots if shots else 'Disabled'}\n"
        f"└ <b>Leech Splits</b> : {'Auto' if not s['split'] else hsize(s['split'])} "
        f"(max {hsize(split_limit(s))})\n\n"
        "<b>Caption variables :</b> <code>{filename} {size} {duration} {resolution} {ext}</code>"
    )
    rows = [
        [
            B(f"Send As {'Document' if s['as_doc'] else 'Media'}", cb(uid, "tog", "as_doc", "leech"), GREEN),
            B(_mk("Thumbnail", has_thumb), cb(uid, "ask", "thumb", "leech"), GREEN),
        ],
        [
            B(_mk("Leech Caption", bool(s["caption"])), cb(uid, "ask", "caption", "leech"), GREEN),
            B(_mk("Caption Font", s["font"] != "default"), cb(uid, "go", "font"), GREEN),
        ],
        [
            B(f"Auto Thumbnail: {'ON' if s['auto_thumb'] else 'OFF'}", cb(uid, "tog", "auto_thumb", "leech"), GREEN),
            B(f"Screenshot: {shots if shots else 'OFF'}", cb(uid, "cyc", "shots", "leech"), GREEN),
        ],
        [B(_mk("Leech Splits", bool(s["split"])), cb(uid, "go", "split"), GREEN)],
    ]
    if has_thumb:
        rows.append([B("Delete Thumbnail", cb(uid, "delthumb", "leech"), RED)])
    rows.append([B("Back", cb(uid, "go", "main"), RED), B("Close", cb(uid, "close"), RED)])
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
                B(_mk(FONTS[k][0], s["font"] == k), cb(uid, "set", "font", k, "font"), GREEN)
                for k in keys[i : i + 2]
            ]
        )
    rows.append([B("Back", cb(uid, "go", "leech"), RED), B("Close", cb(uid, "close"), RED)])
    return text, rows


def _dest(uid, s, user):
    text = (
        "<b><u>⚙️ Leech Destination :</u></b>\n\n"
        f"➲ <b>Current</b> : <i>{DEST_LABELS[s['dest']]}</i>\n\n"
        "• <b>PM Only</b> - files go to your bot PM, the group only gets a short done message\n"
        "• <b>Group Only</b> - files are uploaded in the group\n"
        "• <b>PM + Group</b> - PM first, then an instant copy in the group\n\n"
        "<i>Start the bot in PM once, otherwise it cannot message you.</i>"
    )
    rows = [
        [B(_mk(label, s["dest"] == key), cb(uid, "set", "dest", key, "dest"), GREEN)]
        for key, label in DEST_LABELS.items()
    ]
    rows.append([B("Back", cb(uid, "go", "uni"), RED), B("Close", cb(uid, "close"), RED)])
    return text, rows


def _split(uid, s, user):
    text = (
        "<b><u>⚙️ Leech Splits :</u></b>\n\n"
        f"➲ <b>Current</b> : <i>{'Auto' if not s['split'] else hsize(s['split'])}</i>\n"
        f"➲ <b>Telegram limit</b> : <i>{hsize(C.SPLIT_SIZE)} per file</i>\n\n"
        "Files bigger than the selected size are split into parts."
    )
    rows = []
    row = []
    for label, size in SPLIT_OPTIONS:
        row.append(B(_mk(label, s["split"] == size), cb(uid, "set", "split", size, "split"), GREEN))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("Back", cb(uid, "go", "leech"), RED), B("Close", cb(uid, "close"), RED)])
    return text, rows


# ----------------------------------------------------------------- input prompts
PROMPTS = {
    "prefix": ("Prefix", "Send the text to add <b>before</b> every file name.", "uni"),
    "suffix": ("Suffix", "Send the text to add <b>after</b> every file name (before the extension).", "uni"),
    "remove": (
        "Remove",
        "Send words to remove from file names, separated by commas.\n<code>www.site.com, [Group], 1080p</code>",
        "uni",
    ),
    "replace": (
        "Replace",
        "Send <code>old:new</code> pairs separated by commas.\n<code>WEB-DL:WEBDL, Ep:E</code>",
        "uni",
    ),
    "caption": (
        "Leech Caption",
        "Send the caption template.\nVariables: <code>{filename} {size} {duration} {resolution} {ext}</code>",
        "leech",
    ),
    "thumb": ("Thumbnail", "Send a <b>photo</b> to use as your thumbnail.", "leech"),
}


def prompt_text(kind):
    title, body, _ = PROMPTS[kind]
    return (
        f"<b><u>⚙️ {title} :</u></b>\n\n{body}\n\n"
        "Send <code>-</code> to clear it. <i>(In groups, reply to this message. Timeout: 2 min)</i>"
    )


# ----------------------------------------------------------------- direct set: /us -s key value
KEYS_HELP = "prefix, suffix, remove, replace, caption, font, dest, shots, split, mode, autothumb, reset"


def set_direct(uid, key, value):
    """Apply ``/us -s key value``. Returns a user-facing HTML message."""
    key = (key or "").lower()
    value = (value or "").strip()
    clear = value in ("-", "none", "None", "")
    if key == "reset":
        reset(uid)
        return "✅ All settings were reset."
    if key in ("prefix", "suffix", "caption"):
        put(uid, key, "" if clear else value)
    elif key == "remove":
        put(uid, key, "" if clear else ", ".join(parse_words(value)))
    elif key == "replace":
        pairs = parse_pairs(value)
        if not clear and not pairs:
            return "❌ Use <code>old:new</code> pairs, e.g. <code>/us -s replace WEB-DL:WEBDL</code>"
        put(uid, key, "" if clear else ", ".join(f"{a}:{b}" for a, b in pairs))
    elif key == "font":
        alias = {
            "strikethrough": "strike",
            "code": "mono",
            "mono/code": "mono",
            "monospace": "mono",
            "preformatted": "pre",
        }
        k = alias.get(value.lower(), value.lower()) if not clear else "default"
        if k not in FONTS:
            return f"❌ Font must be one of: <code>{', '.join(FONTS)}</code>"
        put(uid, "font", k)
    elif key == "dest":
        k = {"pm": "pm", "group": "chat", "chat": "chat", "both": "both"}.get(value.lower())
        if not k:
            return "❌ Destination must be <code>pm</code>, <code>group</code> or <code>both</code>"
        put(uid, "dest", k)
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
    elif key in ("autothumb", "auto_thumb"):
        if value.lower() not in ("on", "off", "true", "false", "1", "0"):
            return "❌ Use <code>on</code> or <code>off</code>"
        put(uid, "auto_thumb", value.lower() in ("on", "true", "1"))
    else:
        return f"❌ Unknown key. Available: <code>{KEYS_HELP}</code>"
    return f"✅ <b>{_e(key)}</b> updated. Open /us to review."
