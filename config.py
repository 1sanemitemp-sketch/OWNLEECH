import os

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
AUTHORIZED_CHATS = {
    int(x) for x in os.environ.get("AUTHORIZED_CHATS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()
}
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "3"))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/app/downloads")
# Telegram bot upload limit 2GB; er beshi hole auto split hobe
SPLIT_SIZE = int(os.environ.get("SPLIT_SIZE", str(2097152000)))
ARIA_SECRET = os.environ.get("ARIA_SECRET", "leechsecret")

# Persistent data (settings, thumbnails). Railway volume attach korle RAILWAY_VOLUME_MOUNT_PATH auto set hoy,
# na thakle /data use hobe. Kichu set korte hobe na.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR") or "/data"
