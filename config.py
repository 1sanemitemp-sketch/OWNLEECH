"""Central configuration.

Values come from (highest priority first):
  1. Railway / Docker environment variables
  2. config.env  (next to this file; copy config.envx -> config.env and edit it)
  3. the defaults below
"""
import os
from pathlib import Path


def _load_env_file():
    for name in ("config.env",):
        path = Path(__file__).resolve().with_name(name)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:  # real environment variables always win
                os.environ[key] = value


_load_env_file()


def _int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _bool(name, default=True):
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _str(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


# --- Telegram credentials -------------------------------------------------
API_ID = _int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID", 0)

# Premium account string session -> uploads up to 4 GB (a normal bot is limited to 2 GB).
# The premium account does NOT need to join / administer any channel.
STRING_SESSION = _str("STRING_SESSION")

# MongoDB connection string. Keeps settings, thumbnails, authorized users, limits and the bot login
# between restarts without any volume (handy on Railway). Without it /data (volume) is used.
DATABASE_URL = _str("DATABASE_URL")

# Comma separated user IDs / group IDs that may use the bot (owner is always allowed)
AUTHORIZED_CHATS = {
    int(x)
    for x in os.environ.get("AUTHORIZED_CHATS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

# --- Log channels (the bot must be admin there; nothing else is needed) ----
# ALL_LOG_ID is the default for the three below. Set a specific one to send that log somewhere else.
ALL_LOG_ID = _int("ALL_LOG_ID", 0)
LINKS_LOG_ID = _int("LINKS_LOG_ID", 0)  # every leech link + who started it
LEECH_LOG_ID = _int("LEECH_LOG_ID", 0)  # a copy of every leeched file
MIRROR_LOG_ID = _int("MIRROR_LOG_ID", 0)  # reserved for mirror tasks (this bot only leeches)


def log_id(kind):
    """Chat id for log ``kind`` ("links" | "leech" | "mirror"), 0 when logging is off."""
    return {"links": LINKS_LOG_ID, "leech": LEECH_LOG_ID, "mirror": MIRROR_LOG_ID}[kind] or ALL_LOG_ID


# --- Commands / start message ----------------------------------------------
# COMMAND_SUFFIX=1  ->  /leech1 /l1 /usetting1 /us1 /status1 ... (/start never changes)
COMMAND_SUFFIX = _str("COMMAND_SUFFIX", "")
ST_TEXT = _str("ST_TEXT", "Now, This bot will send all your files and links here. Start Using ...")
ST_BUTTON_1 = _str("ST_BUTTON_1", "OWNER:https://t.me/sanemi111")
ST_BUTTON_2 = _str("ST_BUTTON_2", "UPDATES:https://t.me/+RubcD9bIv8tlY2I1")

# --- Tasks ----------------------------------------------------------------
# Limits below can be changed live by the owner with /botsettings. 0 = unlimited.
# The owner is never limited.
BOT_MAX_TASKS = _int("BOT_MAX_TASKS", _int("MAX_CONCURRENT", 8))
USER_MAX_TASKS = _int("USER_MAX_TASKS", 2)
LEECH_LIMIT = _float("LEECH_LIMIT", 0)  # GB per task
DIRECT_LIMIT = _float("DIRECT_LIMIT", 0)  # GB, direct links (aria2)
YTDLP_LIMIT = _float("YTDLP_LIMIT", 0)  # GB, yt-dlp links
DAILY_TASK_LIMIT = _int("DAILY_TASK_LIMIT", 0)  # tasks per user per day
UPLOAD_PARALLEL = max(1, _int("UPLOAD_PARALLEL", 1))  # files of one task uploaded at the same time

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
# Telegram bot upload limit is 2000 MiB per file; bigger files are split automatically.
SPLIT_SIZE = _int("SPLIT_SIZE", 2097152000)
PREMIUM_SPLIT = 4000 * 1024 * 1024  # used automatically when STRING_SESSION is a Premium account
ARIA_SECRET = os.environ.get("ARIA_SECRET", "leechsecret")

# --- Thumbnails ------------------------------------------------------------
# Longest side of the HD thumbnail in pixels (it is also kept under 200 KB).
THUMB_MAX = max(160, min(_int("THUMB_MAX", 1280), 2000))
# Send a full-size HD cover too when the Telegram library supports it.
USE_COVER = _bool("USE_COVER", True)

# --- Status message -------------------------------------------------------
STATUS_INTERVAL = max(3, _int("STATUS_INTERVAL", 6))  # seconds between live status edits
TASKS_PER_PAGE = max(1, _int("TASKS_PER_PAGE", 5))

# --- Upload speed ---------------------------------------------------------
# Multi-session parallel uploader. Falls back to the library uploader if anything goes wrong.
FAST_UPLOAD = _bool("FAST_UPLOAD", True)
UPLOAD_SESSIONS = max(1, min(_int("UPLOAD_SESSIONS", 12), 20))  # parallel MTProto connections per file
UPLOAD_WORKERS = max(1, min(_int("UPLOAD_WORKERS", 6), 8))  # in-flight requests per connection

# --- Repo stats (/stats -> Repo Stats) -------------------------------------
BOT_VERSION = _str("BOT_VERSION", "v1.5.0")
UPSTREAM_REPO = (
    _str("UPSTREAM_REPO", "")  # "owner/repo" or the full GitHub URL
    .replace("https://github.com/", "").replace("http://github.com/", "").removesuffix("/").removesuffix(".git")
)
UPSTREAM_BRANCH = _str("UPSTREAM_BRANCH", "main")

# --- Persistent data (settings, thumbnails, auth list) --------------------
# On Railway: attach a Volume. RAILWAY_VOLUME_MOUNT_PATH is set automatically.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR") or "/data"
