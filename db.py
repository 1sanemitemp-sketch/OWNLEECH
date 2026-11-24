"""Optional MongoDB storage (DATABASE_URL). Everything also works without it (local files / volume).

Reads happen once at start-up; writes go through a background thread so the bot never waits for the database.
"""
import logging
import queue
import threading

import config as C

log = logging.getLogger("leechbot.db")
ENABLED = False
_db = None
_q = queue.Queue()
_started = False


def init():
    """Connect once. Returns True when MongoDB is ready."""
    global _db, ENABLED, _started
    if _started or not C.DATABASE_URL:
        return ENABLED
    _started = True
    try:
        import pymongo

        client = pymongo.MongoClient(C.DATABASE_URL, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        _db = client.get_default_database(default="leechbot")
        ENABLED = True
        threading.Thread(target=_worker, daemon=True, name="mongo-writer").start()
        log.info("MongoDB connected (database: %s)", _db.name)
    except Exception as e:  # noqa: BLE001
        log.error("MongoDB connection failed - using local files instead: %r", e)
    return ENABLED


def _worker():
    while True:
        fn, args = _q.get()
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("MongoDB write failed: %r", e)
        finally:
            _q.task_done()


def flush(timeout=8):
    """Wait (a little) for pending writes - called on shutdown."""
    if ENABLED:
        t = threading.Thread(target=_q.join, daemon=True)
        t.start()
        t.join(timeout)


# ---- key / value (auth list, bot limits, telegram session ...)
def kv_get(key, default=None):
    if not ENABLED:
        return default
    try:
        doc = _db.kv.find_one({"_id": key})
        return doc["v"] if doc else default
    except Exception as e:  # noqa: BLE001
        log.warning("MongoDB read failed: %r", e)
        return default


def kv_set(key, value):
    if ENABLED:
        _q.put((lambda: _db.kv.replace_one({"_id": key}, {"_id": key, "v": value}, upsert=True), ()))


# ---- users (per-user settings)
def users_all():
    try:
        return {d["_id"]: d["d"] for d in _db.users.find()} if ENABLED else {}
    except Exception as e:  # noqa: BLE001
        log.warning("MongoDB read failed: %r", e)
        return {}


def user_set(uid, data):
    if ENABLED:
        _q.put((lambda: _db.users.replace_one({"_id": str(uid)}, {"_id": str(uid), "d": data}, upsert=True), ()))


def user_del(uid):
    if ENABLED:
        _q.put((lambda: _db.users.delete_one({"_id": str(uid)}), ()))


# ---- small binary files (thumbnails, rclone.conf)
def files_all(kind):
    try:
        return {d["uid"]: bytes(d["b"]) for d in _db.files.find({"kind": kind})} if ENABLED else {}
    except Exception as e:  # noqa: BLE001
        log.warning("MongoDB read failed: %r", e)
        return {}


def file_set(kind, uid, blob):
    if ENABLED:
        _id = f"{kind}:{uid}"
        _q.put((lambda: _db.files.replace_one({"_id": _id}, {"_id": _id, "kind": kind, "uid": str(uid), "b": blob}, upsert=True), ()))


def file_del(kind, uid):
    if ENABLED:
        _q.put((lambda: _db.files.delete_one({"_id": f"{kind}:{uid}"}), ()))
