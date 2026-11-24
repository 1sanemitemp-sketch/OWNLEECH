"""Live bot limits: config.env / Railway value, optionally overridden by the owner via /botsettings."""
import json
import os
from pathlib import Path

import config as C
import db

DATA = Path(C.DATA_DIR)
try:
    DATA.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA = Path("./data")
    DATA.mkdir(parents=True, exist_ok=True)
FILE = DATA / "botsettings.json"

# key -> (label, config attribute, type, unit shown after the value)
DEFS = {
    "user_tasks": ("User Max Tasks", "USER_MAX_TASKS", int, ""),
    "bot_tasks": ("Bot Max Tasks", "BOT_MAX_TASKS", int, ""),
    "leech": ("Leech Limit", "LEECH_LIMIT", float, "GB"),
    "direct": ("Direct Limit", "DIRECT_LIMIT", float, "GB"),
    "ytdlp": ("YT-DLP Limit", "YTDLP_LIMIT", float, "GB"),
    "daily": ("Daily Task Limit", "DAILY_TASK_LIMIT", int, "per user"),
    "parallel": ("Parallel Uploads", "UPLOAD_PARALLEL", int, "files"),
}

db.init()
try:
    _OVERRIDES = db.kv_get("botsettings") if db.ENABLED else None
    if _OVERRIDES is None:
        _OVERRIDES = json.loads(FILE.read_text())
except Exception:  # noqa: BLE001
    _OVERRIDES = {}


def get(key):
    label, attr, typ, _ = DEFS[key]
    try:
        return typ(_OVERRIDES[key])
    except (KeyError, TypeError, ValueError):
        return typ(getattr(C, attr))


def put(key, value):
    typ = DEFS[key][2]
    _OVERRIDES[key] = typ(value)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_OVERRIDES))
    os.replace(tmp, FILE)
    db.kv_set("botsettings", dict(_OVERRIDES))


def text(key):
    """Human readable value: 'inf' for 0 / 'unit'."""
    v = get(key)
    unit = DEFS[key][3]
    shown = "∞" if not v else (f"{v:g}" if isinstance(v, float) else str(v))
    if key == "parallel":
        shown = str(max(v, 1))
    return f"{shown} {unit}".strip()


def gb(key):
    """Limit in bytes (0 = unlimited)."""
    return int(get(key) * 1024**3)
