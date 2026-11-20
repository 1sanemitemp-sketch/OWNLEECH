"""Central configuration. Everything is read from environment variables."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _bool(name, default=True):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# --- Telegram credentials -------------------------------------------------
API_ID = _int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID", 0)

# Comma separated user IDs / group IDs that may use the bot (owner is always allowed)
AUTHORIZED_CHATS = {
    int(x)
    for x in os.environ.get("AUTHORIZED_CHATS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
}

# --- Tasks ----------------------------------------------------------------
MAX_CONCURRENT = max(1, _int("MAX_CONCURRENT", 3))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
# Telegram bot upload limit is 2000 MiB per file; bigger files are split automatically.
SPLIT_SIZE = _int("SPLIT_SIZE", 2097152000)
ARIA_SECRET = os.environ.get("ARIA_SECRET", "leechsecret")

# --- Status message -------------------------------------------------------
STATUS_INTERVAL = max(3, _int("STATUS_INTERVAL", 6))  # seconds between live status edits
TASKS_PER_PAGE = max(1, _int("TASKS_PER_PAGE", 5))

# --- Upload speed ---------------------------------------------------------
# Multi-session parallel uploader. Falls back to the library uploader if anything goes wrong.
FAST_UPLOAD = _bool("FAST_UPLOAD", True)
UPLOAD_SESSIONS = max(1, min(_int("UPLOAD_SESSIONS", 8), 16))  # parallel MTProto connections per file
UPLOAD_WORKERS = max(1, min(_int("UPLOAD_WORKERS", 4), 8))  # in-flight requests per connection

# --- Persistent data (settings, thumbnails, auth list) --------------------
# On Railway: attach a Volume. RAILWAY_VOLUME_MOUNT_PATH is set automatically.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR") or "/data"
