"""Multi-session parallel uploader.

The stock Pyrogram uploader pushes a file through 3 MTProto connections. This module replaces
``Client.save_file`` for big files with a version that opens several more connections (default 8)
and keeps many 512 KB parts in flight at the same time. Everything else (send_video,
send_document, captions, thumbnails ...) keeps using the normal library methods.

Safety net: if the fast path fails for any reason other than a user cancel, the original library
uploader is used for that file, so an upload never fails just because of this optimisation.
"""
import asyncio
import contextlib
import logging
import math
import os
import random
import time
from importlib import import_module
from pathlib import PurePath

import config as C
from compat import StopTransmission, errors, raw, tg

log = logging.getLogger("leechbot.fastupload")

PART_SIZE = 512 * 1024
BIG_FILE = 10 * 1024 * 1024
MAX_FILE = 4000 * 1024 * 1024
RETRIES = 5

STATS = {"fast": 0, "fallback": 0}


def install(client):
    """Patch ``client.save_file``. Returns True when the fast uploader is active."""
    if not C.FAST_UPLOAD:
        log.info("Fast uploader disabled (FAST_UPLOAD=0)")
        return False
    try:
        session_cls = import_module(f"{tg.__name__}.session").Session
        original = client.save_file
    except Exception as e:  # noqa: BLE001
        log.warning("Fast uploader not installed (%r) - using library uploader", e)
        return False

    state = {"failures": 0}

    async def save_file(path, file_id=None, file_part=0, progress=None, progress_args=(), *args, **kwargs):
        eligible = (
            state["failures"] < 3
            and file_id is None
            and file_part == 0
            and not args
            and not kwargs
            and isinstance(path, (str, PurePath))
            and os.path.isfile(path)
            and BIG_FILE < os.path.getsize(path) <= MAX_FILE
        )
        if not eligible:
            return await original(path, file_id, file_part, progress, progress_args, *args, **kwargs)
        semaphore = getattr(client, "save_file_semaphore", None) or contextlib.nullcontext()
        try:
            async with semaphore:
                result = await _upload(client, session_cls, str(path), progress, progress_args)
            STATS["fast"] += 1
            return result
        except (StopTransmission, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001
            state["failures"] += 1
            STATS["fallback"] += 1
            log.warning("Fast upload failed (%r) - using library uploader for this file", e)
            return await original(path, file_id, file_part, progress, progress_args)

    client.save_file = save_file
    log.info("Fast uploader active: %d connections x %d workers", C.UPLOAD_SESSIONS, C.UPLOAD_WORKERS)
    return True


async def _call_progress(progress, args, done, total):
    res = progress(done, total, *args)
    if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
        await res


async def _upload(client, session_cls, path, progress, progress_args):
    size = os.path.getsize(path)
    total_parts = int(math.ceil(size / PART_SIZE))
    rnd = getattr(client, "rnd_id", None)
    file_id = rnd() if callable(rnd) else random.getrandbits(62)

    dc_id = await client.storage.dc_id()
    auth_key = await client.storage.auth_key()
    test_mode = await client.storage.test_mode()

    n_sessions = max(1, min(C.UPLOAD_SESSIONS, total_parts))
    pool = [session_cls(client, dc_id, auth_key, test_mode, is_media=True) for _ in range(n_sessions)]
    queue = asyncio.Queue(maxsize=n_sessions * C.UPLOAD_WORKERS * 2)
    st = {"done": 0, "error": None, "last": 0.0}
    abort = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def report(force=False):
        if progress is None:
            return
        now = time.monotonic()
        if not force and now - st["last"] < 0.2:
            return
        st["last"] = now
        try:
            await _call_progress(progress, progress_args, min(st["done"], size), size)
        except Exception as e:  # noqa: BLE001 - includes StopTransmission (user cancel)
            if st["error"] is None:
                st["error"] = e
            abort.set()

    async def worker(session):
        while True:
            item = await queue.get()
            if item is None:
                return
            if abort.is_set():
                continue  # keep draining so the producer never blocks
            rpc, nbytes = item
            for attempt in range(1, RETRIES + 1):
                try:
                    await session.invoke(rpc)
                    break
                except errors.FloodWait as e:
                    await asyncio.sleep(min(getattr(e, "value", 1) + 1, 60))
                except Exception as e:  # noqa: BLE001
                    if attempt == RETRIES:
                        if st["error"] is None:
                            st["error"] = e
                        abort.set()
                        break
                    await asyncio.sleep(min(attempt, 3))
            else:  # all attempts were flood waits
                if st["error"] is None:
                    st["error"] = RuntimeError("Telegram flood wait while uploading")
                abort.set()
            if not abort.is_set():
                st["done"] += nbytes
                await report()

    workers = []
    started = []
    try:
        results = await asyncio.gather(*(s.start() for s in pool), return_exceptions=True)
        started = [s for s, r in zip(pool, results) if not isinstance(r, BaseException)]
        failed = [r for r in results if isinstance(r, BaseException)]
        if failed:
            raise failed[0]

        workers = [loop.create_task(worker(s)) for s in pool for _ in range(C.UPLOAD_WORKERS)]

        with open(path, "rb") as fp:
            for part in range(total_parts):
                if abort.is_set():
                    break
                chunk = await loop.run_in_executor(None, fp.read, PART_SIZE)
                if not chunk:
                    break
                rpc = raw.functions.upload.SaveBigFilePart(
                    file_id=file_id, file_part=part, file_total_parts=total_parts, bytes=chunk
                )
                await queue.put((rpc, len(chunk)))
    finally:
        for _ in workers:
            await queue.put(None)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(*(s.stop() for s in started), return_exceptions=True)

    if st["error"] is not None:
        raise st["error"]
    if st["done"] < size:
        raise RuntimeError(f"Upload incomplete ({st['done']}/{size} bytes)")
    await report(force=True)
    if st["error"] is not None:
        raise st["error"]
    return raw.types.InputFileBig(id=file_id, parts=total_parts, name=os.path.basename(path))
