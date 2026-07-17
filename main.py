"""main.py - FastAPI application, web dashboard, and orchestration.

This is the entry point for QSOCapture. Responsibilities:

* Parse ``config.cfg`` via :mod:`config`.
* Instantiate the audio source(s) (TCI or soundcard) and start capture +
  optional continuous recording threads.
* Start the N1MM UDP listener; on each contact, schedule a post-roll delayed
  QSO slice and persist full contact metadata to the SQLite database.
* Serve a responsive Tailwind dashboard (``index.html``) and a small JSON API
  used by the frontend: list contests, list QSOs (with extended
  search/filter), live status (TCI + N1MM) and a rolling log stream.
"""

from __future__ import annotations

import logging
import os
import time
import shutil
import zipfile
import io
import queue
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import config as config_module
from config import AppConfig, load_config, config_to_dict, save_config, CONFIG_SCHEMA
from audio_manager import create_audio_source
from n1mm_listener import N1MMListener, N1MMContact
import db as qso_db
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("QSOCapture.main")

# ---------------------------------------------------------------------------
# Global application state (populated in lifespan startup)
# ---------------------------------------------------------------------------
cfg: AppConfig = load_config()
audio_source = None
n1mm = None
_config_lock = threading.Lock()

# Optional override for the dashboard HTML file path. The launcher sets this
# when it cannot copy ``index.html`` next to the executable (e.g. a read-only
# ``Program Files`` install) and instead placed a copy in ``%LOCALAPPDATA%``.
INDEX_HTML_OVERRIDE: Optional[str] = None

# Rolling in-memory log buffer (captured via a custom handler). A bounded
# deque is cheaper than a Queue: append/popleft are O(1) and get_recent_logs
# does not need to copy the underlying queue object.
LOG_BUFFER: "deque" = deque(maxlen=2000)

# Pool used to run the post-roll delayed QSO slicing tasks. Bounding it
# prevents unbounded thread creation during a contest pile-up.
#
# The worker threads MUST be daemon threads. Otherwise, when the app is
# stopped with Ctrl+C, Python waits for every non-daemon worker to finish
# (each may be sleeping up to ``post_roll`` seconds and then writing a file),
# which makes shutdown hang for a long time. Making them daemon lets the
# interpreter exit immediately; we also shut the pool down (without waiting)
# in the lifespan ``finally`` so in-flight tasks are dropped cleanly.
import concurrent.futures.thread as _cf_thread
import weakref as _weakref


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are daemon threads.

    Python 3.14 creates the worker threads directly inside ``_adjust_thread_count``
    (there is no ``_make_worker_thread`` hook), so we override that method and
    mirror its body but set ``daemon=True`` on the ``threading.Thread`` *before*
    it is started (setting daemon after start() raises RuntimeError).

    With daemon workers the interpreter can exit immediately on Ctrl+C instead
    of blocking until every in-flight post-roll QSO slice finishes.
    """

    def _adjust_thread_count(self):
        # if idle threads are available, don't spin new threads
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_cf_thread._worker,
                daemon=True,
                args=(
                    _weakref.ref(self, weakref_cb),
                    self._create_worker_context(),
                    self._work_queue,
                ),
            )
            t.start()
            self._threads.add(t)
            _cf_thread._threads_queues[t] = self._work_queue


SLICE_POOL = _DaemonThreadPoolExecutor(max_workers=4, thread_name_prefix="qso-slice")


class _LogCaptureHandler(logging.Handler):
    """Append formatted log records into LOG_BUFFER for the web UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
        except Exception:
            pass


_log_handler = _LogCaptureHandler()
_log_handler.setLevel(logging.INFO)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(_log_handler)


def get_recent_logs(n: int = 200) -> List[str]:
    """Return the most recent *n* log lines (oldest first)."""
    items = list(LOG_BUFFER)
    return items[-n:]


# Server-Sent Events (SSE) queue. The audio backend pushes lightweight
# notifications (e.g. "continuous started", "qso saved", "chunk finalised")
# here so the dashboard can refresh the list on *events* instead of polling
# every few seconds (which caused distracting flicker). A bounded queue keeps
# memory flat; if the UI falls behind, oldest notifications are dropped.
EVENT_QUEUE: "queue.Queue" = queue.Queue(maxsize=500)


def push_event(name: str, data: Optional[dict] = None) -> None:
    """Enqueue a dashboard event (best-effort, never blocks)."""
    try:
        EVENT_QUEUE.put_nowait({"event": name, "data": data or {}})
    except queue.Full:
        pass


def _apply_and_restart() -> None:
    """Restart audio source and N1MM listener so config changes take effect.

    Called after the live ``cfg`` object has been mutated. Hot-reloadable
    settings (e.g. paths, N1MM port, audio mode) are re-read by the freshly
    created threads.
    """
    global audio_source, n1mm
    # Stop old components.
    if n1mm:
        n1mm.stop()
    if audio_source:
        audio_source.stop()
    # Recreate with updated cfg.
    os.makedirs(cfg.recordings_dir, exist_ok=True)
    audio_source = create_audio_source(cfg, label="RX1")
    audio_source.start()
    n1mm = N1MMListener(cfg, on_contact=on_contact)
    n1mm.start()
    logger.info("Configuration applied and services restarted (mode=%s)", cfg.audio_mode)


def on_contact(contact: N1MMContact) -> None:
    """Callback triggered for every decoded N1MM contact."""
    from n1mm_listener import schedule_qso_slice

    logger.info("Scheduling slice for %s", contact.call)
    # NOTE: do NOT year-prefix contact.contest here. The audio backend's
    # slice_qso() already prefixes the contest with the year (e.g.
    # "2026_CQWWCW") when building the directory and DB record. Prefixing
    # here too produced a doubled prefix like "2026_2026_CQWWCW".
    # The QSO slice (and its DB record) is created by the audio backend once
    # the post-roll delay elapses, so we only schedule the slice here. The
    # task runs in the bounded SLICE_POOL instead of spawning a new thread
    # per contact (which could grow unbounded during a contest pile-up).
    SLICE_POOL.submit(schedule_qso_slice, contact, audio_source, cfg)


# ---------------------------------------------------------------------------
# Lifespan (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global audio_source, n1mm
    # Initialize database and seed with any pre-existing recordings.
    qso_db.init_db()
    qso_db.migrate_existing(cfg.recordings_dir)

    os.makedirs(cfg.recordings_dir, exist_ok=True)
    audio_source = create_audio_source(cfg, label="RX1")
    audio_source.start()
    n1mm = N1MMListener(cfg, on_contact=on_contact)
    n1mm.start()
    # Periodic disk-limit enforcement (best-effort, daemon thread).
    dl_thread = threading.Thread(target=_disk_limit_loop, daemon=True,
                                 name="disk-limit")
    dl_thread.start()
    logger.info("QSOCapture started (mode=%s)", cfg.audio_mode)
    try:
        yield
    except _asyncio.CancelledError:
        # Server is shutting down (Ctrl+C): the lifespan coroutine is cancelled
        # while parked on the inner receive(). Swallow it so it does not surface
        # as an "Exception in ASGI application" traceback on shutdown.
        pass
    finally:
        # Cleanup always runs (normal exit or cancelled shutdown).
        if n1mm:
            n1mm.stop()
        if audio_source:
            audio_source.stop()
        # Discard any in-flight post-roll QSO slicing tasks. The pool workers
        # are daemon threads (see SLICE_POOL definition), so this lets the
        # interpreter exit immediately instead of blocking on sleep(s) /
        # file writes during shutdown. cancel_futures=True (3.9+) drops tasks
        # that have not started yet; already-running daemon workers are simply
        # abandoned when the process exits.
        try:
            SLICE_POOL.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Older Python without cancel_futures: shutdown without waiting.
            SLICE_POOL.shutdown(wait=False)
        logger.info("QSOCapture stopped")


app = FastAPI(title="QSOCapture", version="0.1.0beta", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Shutdown-safe ASGI wrapper
# ---------------------------------------------------------------------------
# When the server is stopped (Ctrl+C) the lifespan coroutine and any in-flight
# request/SSE tasks are cancelled with asyncio.CancelledError. Starlette/uvicorn
# re-raise these to the top-level runner, which prints an
# "Exception in ASGI application" traceback. This thin wrapper catches
# CancelledError at the ASGI boundary and returns cleanly so shutdown is silent.
import asyncio as _asyncio


async def _cancel_safe_app(scope, receive, send):
    try:
        await app(scope, receive, send)
    except _asyncio.CancelledError:
        # Normal shutdown cancellation: do not surface as a traceback.
        return


# uvicorn imports this name (see __main__ below).
application = _cancel_safe_app


# ---------------------------------------------------------------------------
# Web / API routes
# ---------------------------------------------------------------------------
@app.get("/icon.ico")
def favicon() -> FileResponse:
    """Serve the application icon (used as the dashboard favicon)."""
    candidates = []
    if INDEX_HTML_OVERRIDE:
        candidates.append(os.path.join(os.path.dirname(INDEX_HTML_OVERRIDE), "icon.ico"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico"))
    for path in candidates:
        if os.path.isfile(path):
            return FileResponse(path, media_type="image/x-icon", filename="icon.ico")
    raise HTTPException(status_code=404, detail="icon not found")


@app.get("/favicon.ico")
def favicon_root() -> FileResponse:
    """Serve the application icon at the default ``/favicon.ico`` path.

    Browsers automatically request ``/favicon.ico`` for the tab icon. This
    route serves the same multi-size icon (generated from ``icon.svg``) so the
    dashboard tab consistently shows the QSOCapture logo in both the N1MM and
    Continuous views.
    """
    return favicon()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve the Tailwind dashboard."""
    path = INDEX_HTML_OVERRIDE or cfg.dashboard_file
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="dashboard file not found")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/contests")
def api_contests() -> JSONResponse:
    """Return the list of available contest groupings."""
    root = cfg.recordings_dir
    contests = []
    if os.path.isdir(root):
        contests = sorted(
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and not d.startswith("_")
        )
    return JSONResponse({"contests": contests})


@app.get("/api/qsos")
def api_qsos(
    contest: Optional[str] = Query(None),
    call: Optional[str] = Query(None),
    band: Optional[str] = Query(None),
    mode: Optional[str] = Query(None),
    date_from: Optional[float] = Query(None),
    date_to: Optional[float] = Query(None),
    continuous: Optional[bool] = Query(None),
    rx: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    sort_by: str = Query("timestamp"),
    sort_dir: str = Query("DESC"),
) -> JSONResponse:
    """Return filtered QSO records backed by the SQLite database.

    * ``contest``  exact contest directory name.
    * ``call``     case-insensitive substring or regex match on callsign
      (e.g. ``SQ3RX`` or ``^SQ``).
    * ``band``     exact band label (e.g. 20M).
    * ``mode``     exact mode (e.g. CW, SSB, RTTY).
    * ``date_from`` / ``date_to`` epoch seconds time window.
    * ``continuous`` ``true`` = only continuous chunks, ``false`` = only
      N1MM QSOs, omitted = both.
    """
    qsos = qso_db.query_contacts(
        contest=contest, call=call, band=band,
        mode=mode, date_from=date_from, date_to=date_to,
        continuous=continuous, rx=rx,
        offset=offset, limit=limit,
        sort_by=sort_by, sort_dir=sort_dir,
    )
    return JSONResponse({"count": qsos["total"], "qsos": qsos["qsos"]})


@app.get("/audio/{contest}/{filename}")
def serve_audio(contest: str, filename: str) -> FileResponse:
    """Stream a recorded QSO / continuous audio file."""
    # Prevent path traversal.
    contest = os.path.basename(contest)
    filename = os.path.basename(filename)
    # ``contest`` may itself contain subpaths (e.g. "_continuous") – re-join
    # safely after stripping any traversal components.
    rel = os.path.normpath(os.path.join(contest, filename)).lstrip("./\\")
    path = os.path.join(cfg.recordings_dir, rel)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    media = "audio/wav" if filename.endswith(".wav") else "audio/mpeg"
    return FileResponse(path, media_type=media, filename=filename)


@app.post("/api/continuous/pause")
def api_continuous_pause() -> JSONResponse:
    """Finalise the current continuous chunk and stop recording new audio."""
    if audio_source is None:
        raise HTTPException(status_code=503, detail="audio not running")
    audio_source.pause_continuous()
    return JSONResponse({"ok": True, "paused": True})


@app.post("/api/continuous/resume")
def api_continuous_resume() -> JSONResponse:
    """Resume continuous recording with a fresh chunk."""
    if audio_source is None:
        raise HTTPException(status_code=503, detail="audio not running")
    audio_source.resume_continuous()
    return JSONResponse({"ok": True, "paused": False})


@app.get("/api/export")
def api_export(contest: Optional[str] = Query(None)) -> FileResponse:
    """Export all recordings (or a single contest folder) as a ZIP archive.

    The archive is built in memory and streamed back, so the dashboard can
    download the whole log for backup or off-machine analysis.
    """
    root = cfg.recordings_dir
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail="no recordings")
    base = root if not contest else os.path.join(root, contest)
    if contest and not os.path.isdir(base):
        raise HTTPException(status_code=404, detail="contest not found")
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not (fn.endswith(".wav") or fn.endswith(".mp3")):
                    continue
                full = os.path.join(dirpath, fn)
                # Store with a path relative to the recordings dir.
                arcname = os.path.relpath(full, root)
                zf.write(full, arcname)
                written += 1
    if written == 0:
        raise HTTPException(status_code=404, detail="no audio files to export")
    buf.seek(0)
    name = (contest or "QSOCapture_all").replace(os.sep, "_")
    return FileResponse(buf, media_type="application/zip",
                        filename=f"{name}.zip")


@app.get("/api/audio_devices")
def api_audio_devices() -> JSONResponse:
    """Return the list of available soundcard input devices."""
    devices = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            name = d.get("name", f"Device {i}")
            devices.append({"index": i, "name": name})
    except Exception:
        pass
    return JSONResponse({"devices": devices})


@app.get("/api/paths")
def api_paths() -> JSONResponse:
    """Return absolute filesystem paths used by the app (for the UI)."""
    return JSONResponse({
        "recordings_abs": os.path.abspath(cfg.recordings_dir),
        "config_abs": os.path.abspath("config.cfg"),
    })


@app.get("/api/status")
def api_status() -> JSONResponse:
    """Lightweight health/status endpoint with TCI and N1MM state."""
    audio_running = audio_source is not None and getattr(audio_source, "_running", False)
    audio_status = audio_source.get_status() if audio_source else {}
    n1mm_running = n1mm is not None and getattr(n1mm, "_running", False)
    return JSONResponse({
        "station": cfg.station_name,
        "mode": cfg.audio_mode,
        "running": audio_running,
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "continuous": cfg.continuous_recording,
        "audio": {
            "connected": bool(audio_status.get("connected", False)),
            "frames_received": audio_status.get("frames_received", 0),
            "buffer_filled_sec": round(audio_status.get("buffer_filled_sec", 0.0), 1),
            "buffers": audio_status.get("buffers", []),
            "continuous_paused": bool(audio_status.get("continuous_paused", False)),
        },
        "n1mm": {
            "running": n1mm_running,
            "bind": f"{cfg.n1mm_bind_ip}:{cfg.n1mm_udp_port}",
        },
    })


@app.get("/api/log")
def api_log(n: int = Query(200, ge=1, le=1000)) -> JSONResponse:
    """Return the most recent *n* application log lines."""
    return JSONResponse({"lines": get_recent_logs(n)})


@app.get("/api/events")
async def api_events(request: Request):
    """Server-Sent Events stream of dashboard notifications.

    Emits lightweight JSON "event: <name>\\ndata: <json>" frames whenever the
    audio backend starts/stops recording, saves a QSO, or finalises a
    continuous chunk. The frontend uses these to refresh the list on *events*
    instead of polling on a timer (which caused flicker). The generator exits
    cleanly when the client disconnects.
    """
    import asyncio

    async def event_stream():
        # Prime the client so the connection is immediately usable.
        yield ": connected\n\n"
        try:
            while True:
                try:
                    item = EVENT_QUEUE.get_nowait()
                except queue.Empty:
                    # No pending event: small sleep so the loop stays responsive
                    # to cancellation (server shutdown / client disconnect).
                    await asyncio.sleep(0.5)
                    continue
                payload = json.dumps(item)
                yield f"event: {item['event']}\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            # Server is shutting down (or client gone): terminate the SSE
            # stream silently instead of surfacing a CancelledError as an
            # "Exception in ASGI application" traceback. Avoiding
            # run_in_executor / is_disconnected here prevents a hang on
            # shutdown (no pending thread-pool task to wait for).
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Configuration API (web-driven settings)
# ---------------------------------------------------------------------------
@app.get("/api/config")
def api_get_config() -> JSONResponse:
    """Return the current configuration (values + UI schema)."""
    return JSONResponse({
        "values": config_to_dict(cfg),
        "schema": [
            {"section": s, "field": f, "label": l, "type": t, "choices": ch, "help": h}
            for s, f, l, t, ch, h in CONFIG_SCHEMA
        ],
    })


@app.post("/api/config")
def api_set_config(payload: dict) -> JSONResponse:
    """Update configuration live and persist to ``config.cfg``.

    Accepts a flat dict of field -> value. Values are coerced to the type
    declared by :data:`CONFIG_SCHEMA`. After applying, the audio source and
    N1MM listener are restarted so the new settings take effect immediately.
    """
    global cfg
    with _config_lock:
        for section, field, _label, ftype, _choices, _help in CONFIG_SCHEMA:
            if field not in payload:
                continue
            raw = payload[field]
            try:
                if ftype == "int":
                    value = int(raw)
                elif ftype == "float":
                    value = float(raw)
                elif ftype == "bool":
                    value = str(raw).lower() in ("1", "true", "yes", "on")
                else:
                    value = str(raw)
            except (ValueError, TypeError) as e:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid value for {field}: {e}")
            # Sanity-check ranges so a bad value does not crash the audio
            # source (or bind to an invalid port) when it is restarted below.
            _validate_field_range(field, value)
            setattr(cfg, field, value)
    save_config(cfg, "config.cfg")
    _apply_and_restart()
    return JSONResponse({"ok": True, "values": config_to_dict(cfg)})


@app.post("/api/factory_reset")
def api_factory_reset() -> JSONResponse:
    """Reset the application to factory defaults.

    Clears the entire QSO log database, wipes all recorded audio files,
    restores the default configuration and restarts all services.
    """
    global cfg
    with _config_lock:
        # 1. Clear the QSO log database.
        qso_db.clear_all()
        # 2. Remove all recorded audio files.
        rec_dir = cfg.recordings_dir
        if os.path.isdir(rec_dir):
            for entry in os.listdir(rec_dir):
                p = os.path.join(rec_dir, entry)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                except OSError:
                    pass
        # 3. Restore default configuration and persist it.
        cfg = AppConfig()
        save_config(cfg, "config.cfg")
        # 4. Clear the in-memory log buffer.
        LOG_BUFFER.clear()
    _apply_and_restart()
    logger.info("Factory reset completed")
    return JSONResponse({"ok": True, "values": config_to_dict(cfg)})


def _validate_field_range(field: str, value) -> None:
    """Reject obviously invalid configuration values before they are applied.

    Prevents e.g. ``channels=5`` or ``sample_rate=1`` from crashing the audio
    backend at restart, or binding to an illegal port.
    """
    limits = {
        "sample_rate": (8000, 192000),
        "channels": (1, 2),
        "pre_roll": (0.0, 120.0),
        "post_roll": (0.0, 120.0),
        "continuous_chunk_minutes": (1, 1440),
        "tci_port": (1, 65535),
        "tci_receiver": (0, 3),
        "n1mm_udp_port": (1, 65535),
        "web_port": (1, 65535),
        "sample_width": (1, 4),
        "max_recordings_gb": (0.0, 100000.0),
    }
    if field in limits:
        lo, hi = limits[field]
        if value < lo or value > hi:
            raise HTTPException(
                status_code=400,
                detail=f"Value for {field} out of range ({lo}..{hi}): {value}",
            )


def enforce_disk_limit() -> int:
    """Delete oldest continuous chunks until ``recordings_dir`` is under
    ``max_recordings_gb`` (no-op when the limit is 0 / unlimited).

    Only continuous chunks are pruned (they are the bulk, regenerable
    material); individual N1MM QSO slices are preserved. Returns the number
    of files removed.
    """
    limit_gb = getattr(cfg, "max_recordings_gb", 0.0) or 0.0
    if limit_gb <= 0:
        return 0
    rec_dir = cfg.recordings_dir
    cont_dir = os.path.join(rec_dir, "_continuous")
    if not os.path.isdir(cont_dir):
        return 0
    removed = 0
    while True:
        total = 0.0
        files = []
        for fn in os.listdir(cont_dir):
            fp = os.path.join(cont_dir, fn)
            if os.path.isfile(fp):
                try:
                    total += os.path.getsize(fp) / (1024 ** 3)
                    files.append((os.path.getmtime(fp), fp, fn))
                except OSError:
                    pass
        if total <= limit_gb or not files:
            break
        # Remove the oldest chunk.
        files.sort()
        _mt, fp, fn = files[0]
        try:
            os.remove(fp)
            rel = "_continuous/" + fn
            try:
                qso_db.delete_qso(rel)
            except Exception:
                pass
            removed += 1
        except OSError:
            break
    if removed:
        logger.info("Disk limit enforced: removed %d old continuous chunk(s)", removed)
    return removed


# Background scheduler that periodically enforces the recordings disk cap.
def _disk_limit_loop() -> None:
    while True:
        try:
            enforce_disk_limit()
        except Exception as e:
            logger.debug("disk limit check error: %s", e)
        time.sleep(300)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:application",
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="info",
    )
