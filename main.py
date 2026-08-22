"""FastAPI application, web dashboard, and orchestration.

Entry point for QSOCapture. Parses config, instantiates audio source and N1MM
listener, serves a Tailwind dashboard and JSON API.
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
import subprocess
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import config as config_module
from config import AppConfig, load_config, config_to_dict, save_config, CONFIG_SCHEMA, RECORDINGS_DIR
from audio_manager import create_audio_source
from n1mm_listener import N1MMListener, schedule_qso_slice
from n3fjp_listener import N3FJPListener
import db as qso_db
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("QSOCapture.main")

APP_VERSION = "0.7.0beta"
GITHUB_REPO = "sq3rx/QSOCapture"

# Version comparison helpers
_SUFFIX_ORDER = {"": 3, "rc": 2, "beta": 1, "alpha": 0}


def parse_version(v):
    """Parse "x.y.z<tag>" into a comparable tuple ``([x,y,z], rank)``."""
    v = (v or "").strip().lower().lstrip("v")
    nums = [0, 0, 0]
    suffix = ""
    head = v
    for tag in ("alpha", "beta", "rc"):
        if tag in v:
            idx = v.index(tag)
            head = v[:idx]
            suffix = tag
            break
    parts = [p for p in head.replace("-", ".").split(".") if p != ""]
    for i, p in enumerate(parts[:3]):
        try:
            nums[i] = int(p)
        except ValueError:
            try:
                nums[i] = int(float(p))
            except ValueError:
                nums[i] = 0
    return (nums, _SUFFIX_ORDER.get(suffix, 3))


def compare_versions(a, b):
    """Return -1 / 0 / 1 if a < / == / > b."""
    pa, pb = parse_version(a), parse_version(b)
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


_VERSION_CACHE_TTL = 3600.0
_version_cache = {"value": None, "ts": 0.0}


def get_latest_version():
    """Return (latest_tag, release_url) from GitHub tags API, or (None, None) on failure."""
    import urllib.request

    now = time.time()
    cached = _version_cache["value"]
    if cached is not None and (now - _version_cache["ts"]) < _VERSION_CACHE_TTL:
        return cached
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/tags?per_page=1" % GITHUB_REPO,
            headers={
                "User-Agent": "QSOCapture/%s" % APP_VERSION,
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, list) or not data:
            return (None, None)
        tag = data[0].get("name", "")
        if not tag:
            return (None, None)
        result = (tag, "https://github.com/%s/releases/tag/%s" % (GITHUB_REPO, tag))
        _version_cache["value"] = result
        _version_cache["ts"] = now
        return result
    except Exception:
        return (None, None)


# Global state
cfg: AppConfig = load_config()
audio_source = None
logger_listener = None
_config_lock = threading.Lock()

DEBUG_LOGGING = False


def set_debug_logging(on: bool) -> None:
    """Enable/disable DEBUG emission for QSOCapture loggers at runtime."""
    global DEBUG_LOGGING
    DEBUG_LOGGING = bool(on)
    level = logging.DEBUG if DEBUG_LOGGING else logging.INFO
    logging.getLogger("QSOCapture").setLevel(level)
    logger.info("Debug logging %s", "enabled" if DEBUG_LOGGING else "disabled")


INDEX_HTML_OVERRIDE: Optional[str] = None

LOG_BUFFER: "deque" = deque(maxlen=2000)

SLICE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qso-slice")


class _LogCaptureHandler(logging.Handler):
    """Append formatted log records to LOG_BUFFER for the web UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
        except Exception:
            pass


_log_handler = _LogCaptureHandler()
_log_handler.setLevel(logging.DEBUG)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(_log_handler)


def get_recent_logs(n: int = 200) -> List[str]:
    """Return the most recent n log lines (oldest first)."""
    items = list(LOG_BUFFER)
    return items[-n:]


EVENT_QUEUE: "queue.Queue" = queue.Queue(maxsize=500)


def push_event(name: str, data: Optional[dict] = None) -> None:
    """Enqueue a dashboard event (best-effort, never blocks)."""
    try:
        EVENT_QUEUE.put_nowait({"event": name, "data": data or {}})
    except queue.Full:
        pass


def _make_logger_listener(cfg):
    """Build the logger listener for the configured source."""
    if cfg.logger_source == "n3fjp":
        return N3FJPListener(cfg, on_contact=on_contact)
    return N1MMListener(cfg, on_contact=on_contact)


def _apply_and_restart() -> None:
    """Restart audio source and logger listener after config changes."""
    global audio_source, logger_listener
    if logger_listener:
        logger_listener.stop()
    if audio_source:
        audio_source.stop()
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    audio_source = create_audio_source(cfg, label="RX1")
    audio_source.start()
    logger_listener = _make_logger_listener(cfg)
    logger_listener.start()
    logger.info("Configuration applied and services restarted (mode=%s, logger_source=%s)",
                cfg.audio_mode, cfg.logger_source)


def on_contact(contact) -> None:
    """Callback for every decoded contact (N1MM or N3FJP)."""
    logger.info("Scheduling slice for %s", getattr(contact, "call", "?"))
    logger.debug("on_contact: call=%s contest=%s freq=%s recv_ts=%.1f pre=%.1f post=%.1f",
                 getattr(contact, "call", ""), getattr(contact, "contest", ""),
                 getattr(contact, "freq", ""),
                 getattr(contact, "receive_ts", 0.0), cfg.pre_roll, cfg.post_roll)
    SLICE_POOL.submit(schedule_qso_slice, contact, audio_source, cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global audio_source, logger_listener
    qso_db.init_db()
    threading.Thread(
        target=qso_db.migrate_existing,
        args=(RECORDINGS_DIR,),
        daemon=True,
        name="migrate-existing",
    ).start()

    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    audio_source = create_audio_source(cfg, label="RX1")
    audio_source.start()
    logger_listener = _make_logger_listener(cfg)
    logger_listener.start()
    dl_thread = threading.Thread(target=_disk_limit_loop, daemon=True,
                                 name="disk-limit")
    dl_thread.start()
    hb_thread = threading.Thread(target=_debug_heartbeat_loop, daemon=True,
                                 name="debug-heartbeat")
    hb_thread.start()
    logger.info("QSOCapture started (mode=%s, logger_source=%s)",
                cfg.audio_mode, cfg.logger_source)
    try:
        yield
    except _asyncio.CancelledError:
        pass
    finally:
        if logger_listener:
            logger_listener.stop()
        if audio_source:
            audio_source.stop()
        try:
            SLICE_POOL.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            SLICE_POOL.shutdown(wait=False)
        logger.info("QSOCapture stopped")


app = FastAPI(title="QSOCapture", version=APP_VERSION, lifespan=lifespan)

import asyncio as _asyncio


async def _cancel_safe_app(scope, receive, send):
    """ASGI wrapper that catches CancelledError on shutdown for clean exit."""
    try:
        await app(scope, receive, send)
    except _asyncio.CancelledError:
        return


application = _cancel_safe_app


# Web / API routes
@app.get("/icon.svg")
def icon_svg() -> FileResponse:
    candidates = []
    if INDEX_HTML_OVERRIDE:
        candidates.append(os.path.join(os.path.dirname(INDEX_HTML_OVERRIDE), "icon.svg"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg"))
    for path in candidates:
        if os.path.isfile(path):
            return FileResponse(path, media_type="image/svg+xml", filename="icon.svg")
    raise HTTPException(status_code=404, detail="icon.svg not found")


@app.get("/icon.ico")
def favicon() -> FileResponse:
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
    return favicon()


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    path = INDEX_HTML_OVERRIDE or cfg.dashboard_file
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="dashboard file not found")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/contests")
def api_contests() -> JSONResponse:
    contests = qso_db.list_contests()
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
    contest = os.path.basename(contest)
    filename = os.path.basename(filename)
    rel = os.path.normpath(os.path.join(contest, filename)).lstrip("./\\")
    path = os.path.join(RECORDINGS_DIR, rel)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    media = "audio/wav" if filename.endswith(".wav") else "audio/mpeg"
    return FileResponse(path, media_type=media, filename=filename)


@app.post("/api/continuous/pause")
def api_continuous_pause() -> JSONResponse:
    if audio_source is None:
        raise HTTPException(status_code=503, detail="audio not running")
    audio_source.pause_continuous()
    return JSONResponse({"ok": True, "paused": True})


@app.post("/api/continuous/resume")
def api_continuous_resume() -> JSONResponse:
    if audio_source is None:
        raise HTTPException(status_code=503, detail="audio not running")
    if not audio_source.is_connected():
        raise HTTPException(
            status_code=409,
            detail="Cannot start recording: audio source not connected (TCI not linked)",
        )
    audio_source.resume_continuous()
    return JSONResponse({"ok": True, "paused": False})


@app.get("/api/export")
def api_export(contest: Optional[str] = Query(None)) -> FileResponse:
    root = RECORDINGS_DIR
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
    return JSONResponse({
        "recordings_abs": os.path.abspath(RECORDINGS_DIR),
        "config_abs": os.path.abspath("config.cfg"),
    })


@app.post("/api/open_folder")
def api_open_folder() -> JSONResponse:
    abs_path = os.path.abspath(RECORDINGS_DIR)
    try:
        os.makedirs(abs_path, exist_ok=True)
        if os.name == "nt":
            os.startfile(abs_path)
        elif os.name == "posix":
            subprocess.Popen(["xdg-open", abs_path])
        else:
            subprocess.Popen(["open", abs_path])
        return JSONResponse({"ok": True, "path": abs_path})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot open folder: {e}")


@app.get("/api/status")
def api_status() -> JSONResponse:
    audio_running = audio_source is not None and getattr(audio_source, "_running", False)
    audio_status = audio_source.get_status() if audio_source else {}
    ll = logger_listener
    logger_running = ll is not None and getattr(ll, "_running", False)
    logger_status = ll.get_status() if ll else {}
    tci_connected = bool(audio_status.get("connected", False)) if cfg.audio_mode == "tci" else False
    logger_detail = {"type": cfg.logger_source, "running": logger_running}
    if cfg.logger_source == "n3fjp":
        logger_detail.update({
            "connected": bool(logger_status.get("connected", False)),
            "addr": logger_status.get("host", f"{cfg.n3fjp_host}:{cfg.n3fjp_port}"),
            "last_reconcile": logger_status.get("last_reconcile", 0.0),
        })
    else:
        logger_detail.update({
            "bind": logger_status.get("bind", f"{cfg.n1mm_bind_ip}:{cfg.n1mm_udp_port}"),
        })
    return JSONResponse({
        "station": cfg.station_name,
        "mode": cfg.audio_mode,
        "running": audio_running,
        "sample_rate": cfg.sample_rate,
        "channels": cfg.channels,
        "continuous": cfg.continuous_recording,
        "audio": {
            "connected": tci_connected,
            "frames_received": audio_status.get("frames_received", 0),
            "buffer_filled_sec": round(audio_status.get("buffer_filled_sec", 0.0), 1),
            "buffers": audio_status.get("buffers", []),
            "continuous_paused": bool(audio_status.get("continuous_paused", False)),
            "cont_queue_fill_pct": audio_status.get("cont_queue_fill_pct", 0.0),
            "cont_queue_dropped": audio_status.get("cont_queue_dropped", 0),
        },
        "logger": logger_detail,
    })


@app.get("/api/log")
def api_log(
    n: int = Query(200, ge=1, le=2000),
    debug: bool = Query(False),
) -> JSONResponse:
    lines = get_recent_logs(n if not debug else 2000)
    if not debug:
        lines = [ln for ln in lines if "[DEBUG]" not in ln]
    return JSONResponse({"lines": lines, "debug": debug})


@app.post("/api/debug")
def api_set_debug(payload: dict) -> JSONResponse:
    enabled = bool(payload.get("enabled", False))
    set_debug_logging(enabled)
    return JSONResponse({"ok": True, "debug": DEBUG_LOGGING})


@app.get("/api/events")
async def api_events(request: Request):
    """Server-Sent Events stream of dashboard notifications."""

    async def event_stream():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    item = EVENT_QUEUE.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.5)
                    continue
                payload = json.dumps(item)
                yield f"event: {item['event']}\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/version_check")
def api_version_check() -> JSONResponse:
    latest, release_url = get_latest_version()
    if not latest:
        return JSONResponse({
            "current": APP_VERSION, "latest": None,
            "update_available": False, "release_url": None, "error": "offline",
        })
    update_available = compare_versions(latest, APP_VERSION) > 0
    return JSONResponse({
        "current": APP_VERSION, "latest": latest,
        "update_available": update_available, "release_url": release_url, "error": None,
    })


@app.get("/api/config")
def api_get_config() -> JSONResponse:
    return JSONResponse({
        "version": APP_VERSION,
        "values": config_to_dict(cfg),
        "schema": [
            {"section": s, "field": f, "label": l, "type": t, "choices": ch, "help": h}
            for s, f, l, t, ch, h in CONFIG_SCHEMA
        ],
    })


@app.post("/api/config")
def api_set_config(payload: dict) -> JSONResponse:
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
            _validate_field_range(field, value)
            setattr(cfg, field, value)
    save_config(cfg, "config.cfg")
    _apply_and_restart()
    return JSONResponse({"ok": True, "values": config_to_dict(cfg)})


@app.post("/api/reset_config")
def api_reset_config() -> JSONResponse:
    global cfg
    with _config_lock:
        cfg = AppConfig()
        save_config(cfg, "config.cfg")
        LOG_BUFFER.clear()
    _apply_and_restart()
    logger.info("Configuration reset to defaults")
    return JSONResponse({"ok": True, "values": config_to_dict(cfg)})


@app.post("/api/clear_qsos")
def api_clear_qsos() -> JSONResponse:
    qso_db.clear_all()
    logger.info("QSO database cleared")
    return JSONResponse({"ok": True})


@app.post("/api/delete_recordings")
def api_delete_recordings() -> JSONResponse:
    qso_db.clear_all()
    rec_dir = RECORDINGS_DIR
    removed = 0
    if os.path.isdir(rec_dir):
        for entry in os.listdir(rec_dir):
            p = os.path.join(rec_dir, entry)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                removed += 1
            except OSError:
                pass
    logger.info("All recordings deleted (%d entries removed)", removed)
    return JSONResponse({"ok": True, "removed": removed})


@app.post("/api/delete_contest")
def api_delete_contest(payload: dict) -> JSONResponse:
    contest = (payload.get("contest") or "").strip()
    if not contest:
        raise HTTPException(status_code=400, detail="contest name is required")
    contest_dir = os.path.join(RECORDINGS_DIR, contest)
    removed_files = 0
    if os.path.isdir(contest_dir):
        shutil.rmtree(contest_dir)
        removed_files = 1
    deleted_rows = qso_db.delete_contest(contest)
    logger.info("Contest '%s' deleted (%d DB rows, %d folders)", contest, deleted_rows, removed_files)
    return JSONResponse({"ok": True, "contest": contest, "deleted_rows": deleted_rows})


@app.post("/api/delete_contest_recordings")
def api_delete_contest_recordings(payload: dict) -> JSONResponse:
    contest = (payload.get("contest") or "").strip()
    if not contest:
        raise HTTPException(status_code=400, detail="contest name is required")
    contest_dir = os.path.join(RECORDINGS_DIR, contest)
    removed_files = 0
    if os.path.isdir(contest_dir):
        shutil.rmtree(contest_dir)
        removed_files = 1
    cleared_rows = qso_db.clear_contest_file_paths(contest)
    logger.info("Contest '%s' recordings deleted (%d folders), file paths cleared (%d rows)", contest, removed_files, cleared_rows)
    return JSONResponse({"ok": True, "contest": contest, "deleted_folders": removed_files, "cleared_rows": cleared_rows})


@app.post("/api/delete_continuous")
def api_delete_continuous(payload: dict) -> JSONResponse:
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    if date_from is not None:
        try:
            date_from = float(date_from)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid date_from")
    if date_to is not None:
        try:
            date_to = float(date_to)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid date_to")
    if audio_source:
        audio_source._close_cont_files()
    deleted_rows, file_paths = qso_db.delete_continuous_range(date_from, date_to)
    removed_files = 0
    failed_files = 0
    for rel_path in file_paths:
        fp = os.path.join(RECORDINGS_DIR, rel_path)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
                removed_files += 1
            else:
                removed_files += 1
        except OSError as e:
            logger.warning("Cannot delete continuous file %s: %s", fp, e)
            failed_files += 1
    if audio_source and not audio_source._continuous_paused:
        audio_source._open_cont_files()
        audio_source._cont_start = time.time()
    logger.info("Continuous recordings deleted (%d files, %d DB rows, %d failed)",
                removed_files, deleted_rows, failed_files)
    return JSONResponse({"ok": True, "deleted_files": removed_files, "deleted_rows": deleted_rows})


@app.post("/api/factory_reset")
def api_factory_reset() -> JSONResponse:
    global cfg
    with _config_lock:
        qso_db.clear_all()
        rec_dir = RECORDINGS_DIR
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
        cfg = AppConfig()
        save_config(cfg, "config.cfg")
        LOG_BUFFER.clear()
    _apply_and_restart()
    logger.info("Factory reset completed")
    return JSONResponse({"ok": True, "values": config_to_dict(cfg)})


def _validate_field_range(field: str, value) -> None:
    """Reject invalid config values before they are applied."""
    limits = {
        "sample_rate": (8000, 192000), "channels": (1, 2),
        "pre_roll": (0.0, 120.0), "post_roll": (0.0, 120.0),
        "continuous_chunk_minutes": (1, 1440),
        "tci_port": (1, 65535), "n1mm_udp_port": (1, 65535),
        "web_port": (1, 65535), "sample_width": (1, 4),
        "max_recordings_gb": (0.0, 100000.0),
        "n3fjp_port": (1, 65535),
        "n3fjp_reconcile_interval_s": (0, 86400),
        "n3fjp_list_window": (5, 500),
    }
    choices = {
        "so2r_mode": ["stereo", "dual_card"],
        "logger_source": ["n1mm", "n3fjp"],
    }
    if field in choices and value not in choices[field]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid value for {field}: '{value}'. Allowed: {choices[field]}",
        )
    if field in limits:
        lo, hi = limits[field]
        if value < lo or value > hi:
            raise HTTPException(
                status_code=400,
                detail=f"Value for {field} out of range ({lo}..{hi}): {value}",
            )


def enforce_disk_limit() -> int:
    """Delete oldest continuous chunks until under max_recordings_gb (no-op when 0/unlimited)."""
    limit_gb = getattr(cfg, "max_recordings_gb", 0.0) or 0.0
    if limit_gb <= 0:
        return 0
    rec_dir = RECORDINGS_DIR
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


def _disk_limit_loop() -> None:
    while True:
        try:
            enforce_disk_limit()
        except Exception as e:
            logger.debug("disk limit check error: %s", e)
        time.sleep(300)


def _debug_heartbeat_loop() -> None:
    """Periodic DEBUG status dump (only when DEBUG_LOGGING is on)."""
    while True:
        if DEBUG_LOGGING:
            try:
                running = audio_source is not None and getattr(audio_source, "_running", False)
                status = audio_source.get_status() if audio_source else {}
                buffers = (status.get("buffers") or []) if status else []
                buf_txt = ", ".join(
                    f"{b.get('label','?')}={b.get('filled_sec',0)}s" for b in buffers
                ) or "—"
                n1mm_running = logger_listener is not None and getattr(logger_listener, "_running", False)
                tci_connected = bool(status.get("connected")) if status and cfg.audio_mode == "tci" else False
                logger.debug(
                    "heartbeat: audio_running=%s buf=[%s] tci_connected=%s "
                    "logger_running=%s (src=%s) continuous_paused=%s",
                    running, buf_txt,
                    tci_connected,
                    n1mm_running,
                    cfg.logger_source,
                    bool(status.get("continuous_paused")) if status else False,
                )
            except Exception as e:
                logger.debug("heartbeat error: %s", e)
        time.sleep(5)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:application",
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="info",
    )