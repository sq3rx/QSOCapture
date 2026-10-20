"""FastAPI application, web dashboard, and orchestration.

Entry point for QSOCapture. Parses config, instantiates audio source and N1MM
listener, serves a Tailwind dashboard and JSON API.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import logging.handlers
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

import db as qso_db
from audio_manager import create_audio_source
from config import BASE_DIR, CONFIG_SCHEMA, RECORDINGS_DIR, AppConfig, config_to_dict, load_config, save_config
from n1mm_listener import N1MMContact, N1MMListener
from version import APP_VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("QSOCapture.main")


def _setup_file_logging() -> None:
    """Add a rotating log file so diagnostics survive a restart/crash.

    The desktop EXE runs windowed (stderr is redirected to devnull), so the
    in-memory dashboard buffer alone is not enough to debug a contest issue.
    """
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        log_path = os.path.join(BASE_DIR, "QSOCapture.log")
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                return
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
    except Exception:
        pass


def _install_excepthooks() -> None:
    def _hook(exc_type, exc, tb):
        logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_hook(args):
        logger.critical("Unhandled exception in thread %s",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


_setup_file_logging()
_install_excepthooks()

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
    """Return (latest_tag, release_url) from GitHub, or (None, None) on failure.

    Uses the "latest release" endpoint first (prereleases excluded), then falls
    back to the tags API and picks the highest semantic version rather than
    trusting whatever tag happened to be created last.
    """
    import urllib.request

    now = time.time()
    cached = _version_cache["value"]
    if cached is not None and (now - _version_cache["ts"]) < _VERSION_CACHE_TTL:
        return cached

    headers = {
        "User-Agent": "QSOCapture/%s" % APP_VERSION,
        "Accept": "application/vnd.github+json",
    }
    result = None
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name", "") if isinstance(data, dict) else ""
        if tag:
            result = (tag, data.get("html_url") or
                      "https://github.com/%s/releases/tag/%s" % (GITHUB_REPO, tag))
    except Exception as e:
        logger.debug("latest release check failed: %s", e)

    if result is None:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/tags?per_page=20" % GITHUB_REPO,
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list) and data:
                tags = [d.get("name", "") for d in data if d.get("name")]
                tag = max(tags, key=parse_version)
                result = (tag, "https://github.com/%s/releases/tag/%s" % (GITHUB_REPO, tag))
        except Exception as e:
            logger.debug("version tag check failed: %s", e)

    if result is None:
        return (None, None)
    _version_cache["value"] = result
    _version_cache["ts"] = now
    return result


# Global state
cfg: AppConfig = load_config()
audio_source = None
n1mm = None
_config_lock = threading.Lock()


def _read_cfg() -> AppConfig:
    """Return the current config reference, consistent with concurrent writers.

    Writers mutate ``cfg`` (or replace it) while holding ``_config_lock``; reading
    the global under the same lock avoids observing a half-applied configuration.
    """
    with _config_lock:
        return cfg


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


def _apply_and_restart(restart_audio: bool = True, restart_n1mm: bool = True) -> None:
    """Restart audio source and/or N1MM listener after config changes."""
    global audio_source, n1mm
    cfg_local = _read_cfg()
    if restart_n1mm and n1mm:
        try:
            n1mm.stop()
        except Exception as exc:
            logger.debug("N1MM stop failed: %s", exc)
    if restart_audio and audio_source:
        try:
            audio_source.stop()
        except Exception as exc:
            logger.warning("Audio stop failed during restart: %s", exc)
    if restart_audio:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        try:
            audio_source = create_audio_source(cfg_local, label="RX1")
            audio_source.start()
        except Exception as exc:
            logger.error("Audio source failed to start: %s", exc)
            if audio_source is not None:
                audio_source._last_error = str(exc)
    if restart_n1mm:
        n1mm = N1MMListener(cfg_local, on_contact=on_contact)
        try:
            n1mm.start()
        except Exception as exc:
            # N1MMListener.start() is defensive itself; this is a last resort so a
            # settings save can never take the dashboard down.
            logger.error("N1MM listener failed to start: %s", exc)
            n1mm._bind_error = str(exc)
    logger.info("Configuration applied (audio_restart=%s, n1mm_restart=%s, mode=%s)",
                restart_audio, restart_n1mm, cfg_local.audio_mode)


def on_contact(contact: N1MMContact) -> None:
    """Callback for every decoded N1MM contact."""
    from n1mm_listener import schedule_qso_slice

    if audio_source is None:
        logger.warning("Ignoring QSO %s: audio source is not running", contact.call)
        return
    logger.info("Scheduling slice for %s", contact.call)
    cfg_local = _read_cfg()
    logger.debug("on_contact: call=%s contest=%s freq=%s recv_ts=%.1f pre=%.1f post=%.1f",
                 contact.call, contact.contest, contact.freq,
                 contact.receive_ts, cfg_local.pre_roll, cfg_local.post_roll)
    SLICE_POOL.submit(schedule_qso_slice, contact, audio_source, cfg_local)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global audio_source, n1mm
    qso_db.init_db()
    threading.Thread(
        target=qso_db.migrate_existing,
        args=(RECORDINGS_DIR,),
        daemon=True,
        name="migrate-existing",
    ).start()

    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    try:
        audio_source = create_audio_source(cfg, label="RX1")
        audio_source.start()
    except Exception as exc:
        logger.error("Audio source failed to start: %s", exc)
        audio_source = None
    n1mm = N1MMListener(cfg, on_contact=on_contact)
    try:
        n1mm.start()
    except Exception as exc:
        logger.error("N1MM listener failed to start: %s", exc)
        n1mm._bind_error = str(exc)
    dl_thread = threading.Thread(target=_disk_limit_loop, daemon=True,
                                 name="disk-limit")
    dl_thread.start()
    hb_thread = threading.Thread(target=_debug_heartbeat_loop, daemon=True,
                                 name="debug-heartbeat")
    hb_thread.start()
    logger.info("QSOCapture started (mode=%s)", cfg.audio_mode)
    try:
        yield
    except asyncio.CancelledError:
        pass
    finally:
        if n1mm:
            n1mm.stop()
        if audio_source:
            audio_source.stop()
        try:
            SLICE_POOL.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            SLICE_POOL.shutdown(wait=False)
        qso_db.close_db()
        logger.info("QSOCapture stopped")


app = FastAPI(title="QSOCapture", version=APP_VERSION, lifespan=lifespan)


async def _cancel_safe_app(scope, receive, send):
    """ASGI wrapper that catches CancelledError on shutdown for clean exit."""
    try:
        await app(scope, receive, send)
    except asyncio.CancelledError:
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
    contest = _safe_contest(contest)
    filename = os.path.basename(filename.replace("\\", "/"))
    if not filename or ":" in filename:
        raise HTTPException(status_code=404, detail="file not found")
    root = os.path.realpath(RECORDINGS_DIR)
    path = os.path.realpath(os.path.join(root, contest, filename))
    try:
        inside = os.path.commonpath(
            [os.path.normcase(root), os.path.normcase(path)]
        ) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside or not os.path.isfile(path):
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
            detail="Cannot start recording: audio source is not connected (check TCI/soundcard)",
        )
    audio_source.resume_continuous()
    return JSONResponse({"ok": True, "paused": False})


@app.get("/api/export")
def api_export(contest: Optional[str] = Query(None)) -> FileResponse:
    root = RECORDINGS_DIR
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail="no recordings")
    if contest:
        contest = _safe_contest(contest)
    base = root if not contest else os.path.join(root, contest)
    if contest and not os.path.isdir(base):
        raise HTTPException(status_code=404, detail="contest not found")

    # Do not include a chunk that is still being written.
    open_paths = set()
    if audio_source is not None and hasattr(audio_source, "get_open_files"):
        try:
            open_paths = {os.path.abspath(p) for p in audio_source.get_open_files()}
        except Exception:
            open_paths = set()

    fd, tmp_path = tempfile.mkstemp(prefix="qsocapture-export-", suffix=".zip")
    os.close(fd)
    written = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirs, files in os.walk(base):
                for fn in files:
                    if not (fn.endswith(".wav") or fn.endswith(".mp3")):
                        continue
                    full = os.path.join(dirpath, fn)
                    if os.path.abspath(full) in open_paths:
                        continue
                    arcname = os.path.relpath(full, root)
                    zf.write(full, arcname)
                    written += 1
        if written == 0:
            raise HTTPException(status_code=404, detail="no audio files to export")
    except HTTPException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"export failed: {exc}")

    name = (contest or "QSOCapture_all").replace(os.sep, "_")
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{name}.zip",
        background=BackgroundTask(lambda: os.path.exists(tmp_path) and os.remove(tmp_path)),
    )


@app.get("/api/audio_devices")
def api_audio_devices() -> JSONResponse:
    devices = []
    try:
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) <= 0:
                continue
            name = d.get("name", f"Device {i}")
            devices.append({
                "index": i,
                "name": name,
                "value": f"{i}|{name}",
                "hostapi": d.get("hostapi", -1),
                "max_input_channels": d.get("max_input_channels", 0),
            })
    except Exception as e:
        logger.debug("audio device enumeration failed: %s", e)
    return JSONResponse({"devices": devices})


@app.get("/api/paths")
def api_paths() -> JSONResponse:
    return JSONResponse({
        "recordings_abs": os.path.abspath(RECORDINGS_DIR),
        "config_abs": os.path.abspath(os.path.join(BASE_DIR, "config.cfg")),
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
    cfg_local = _read_cfg()
    audio_running = audio_source is not None and getattr(audio_source, "_running", False)
    audio_status = audio_source.get_status() if audio_source else {}
    n1mm_running = n1mm is not None and getattr(n1mm, "_running", False)
    tci_connected = bool(audio_status.get("connected", False)) if cfg_local.audio_mode == "tci" else False
    effective_rate = getattr(audio_source, "sample_rate", cfg_local.sample_rate)
    return JSONResponse({
        "station": cfg_local.station_name,
        "mode": cfg_local.audio_mode,
        "running": audio_running,
        "sample_rate": effective_rate,
        "channels": cfg_local.channels,
        "continuous": cfg_local.continuous_recording,
        "audio": {
            "connected": tci_connected,
            "frames_received": audio_status.get("frames_received", 0),
            "buffer_filled_sec": round(audio_status.get("buffer_filled_sec", 0.0), 1),
            "buffers": audio_status.get("buffers", []),
            "continuous_paused": bool(audio_status.get("continuous_paused", False)),
            "cont_queue_fill_pct": audio_status.get("cont_queue_fill_pct", 0.0),
            "cont_queue_dropped": audio_status.get("cont_queue_dropped", 0),
            "error": audio_status.get("error", ""),
        },
        "n1mm": {
            "running": n1mm_running,
            "bind": f"{cfg_local.n1mm_bind_ip}:{cfg_local.n1mm_udp_port}",
            "error": getattr(n1mm, "_bind_error", "") if n1mm else "",
        },
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
    cfg_local = _read_cfg()
    return JSONResponse({
        "version": APP_VERSION,
        "values": config_to_dict(cfg_local),
        "schema": [
            {"section": s, "field": f, "label": label, "type": t, "choices": ch, "help": h}
            for s, f, label, t, ch, h in CONFIG_SCHEMA
        ],
    })


@app.post("/api/config")
def api_set_config(payload: dict) -> JSONResponse:
    global cfg
    with _config_lock:
        new_cfg = copy.deepcopy(cfg)
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
        setattr(new_cfg, field, value)
    _validate_config(new_cfg)
    with _config_lock:
        old_cfg = cfg
    save_config(new_cfg, "config.cfg")
    with _config_lock:
        cfg = new_cfg
    restart_audio, restart_n1mm = _service_restart_flags(old_cfg, new_cfg)
    if restart_audio or restart_n1mm:
        _apply_and_restart(restart_audio, restart_n1mm)
    else:
        logger.info("Configuration saved (services unchanged)")
    return JSONResponse({"ok": True, "values": config_to_dict(new_cfg)})


def _service_restart_flags(old: AppConfig, new: AppConfig) -> tuple[bool, bool]:
    """Return (restart_audio, restart_n1mm) for changed config fields."""
    audio_fields = (
        "audio_mode", "audio_format", "sample_rate", "channels",
        "pre_roll", "post_roll", "so2r_mode", "soundcard_device",
        "soundcard_device2", "tci_host", "tci_port", "continuous_recording",
        "continuous_autostart", "continuous_chunk_minutes",
        "normalize_continuous",
    )
    n1mm_fields = ("n1mm_udp_port", "n1mm_bind_ip")
    return (
        any(getattr(old, field) != getattr(new, field) for field in audio_fields),
        any(getattr(old, field) != getattr(new, field) for field in n1mm_fields),
    )


def _validate_config(cfg_local: AppConfig) -> None:
    """Reject a complete config that is internally inconsistent."""
    if cfg_local.so2r_mode == "dual_card":
        if cfg_local.channels < 2:
            raise HTTPException(
                status_code=400,
                detail="dual_card SO2R mode requires channels=2 (two receivers)",
            )
        if not cfg_local.soundcard_device2:
            raise HTTPException(
                status_code=400,
                detail="dual_card SO2R mode requires a second soundcard device (RX2)",
            )
    if cfg_local.audio_mode not in ("tci", "soundcard"):
        raise HTTPException(status_code=400, detail="invalid audio_mode")
    if cfg_local.audio_format not in ("wav", "mp3"):
        raise HTTPException(status_code=400, detail="invalid audio_format")
    if cfg_local.sample_rate < 8000 or cfg_local.sample_rate > 192000:
        raise HTTPException(status_code=400, detail="sample_rate out of range")


@app.post("/api/reset_config")
def api_reset_config() -> JSONResponse:
    global cfg
    new_cfg = AppConfig()
    save_config(new_cfg, "config.cfg")
    with _config_lock:
        cfg = new_cfg
    LOG_BUFFER.clear()
    _apply_and_restart()
    logger.info("Configuration reset to defaults")
    return JSONResponse({"ok": True, "values": config_to_dict(new_cfg)})


def _pause_for_maintenance() -> tuple[object, bool]:
    """Pause continuous recording and wait for files to close.

    Returns ``(source, was_paused)`` so the caller can resume the previous
    state. Necessary on Windows: an open chunk file cannot be deleted.
    """
    src = audio_source
    if src is None:
        return None, True
    was_paused = bool(getattr(src, "_continuous_paused", False))
    if not was_paused and getattr(src.cfg, "continuous_recording", False):
        try:
            src.pause_continuous()
            if hasattr(src, "wait_continuous_idle"):
                src.wait_continuous_idle(timeout=10.0)
        except Exception as exc:
            logger.warning("Could not pause continuous recording: %s", exc)
    return src, was_paused


def _resume_after_maintenance(src, was_paused: bool) -> None:
    if src is None or was_paused:
        return
    try:
        if getattr(src.cfg, "continuous_recording", False):
            src.resume_continuous()
    except Exception as exc:
        logger.warning("Could not resume continuous recording: %s", exc)


def _backup_before_destructive(action: str) -> None:
    try:
        qso_db.checkpoint()
        qso_db.backup_database(os.path.join(BASE_DIR, "qsos.db.bak"))
    except Exception as exc:
        logger.warning("Pre-%s database backup failed: %s", action, exc)


def _remove_recordings_dir() -> int:
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
            except OSError as exc:
                logger.warning("Could not delete %s: %s", p, exc)
    return removed


@app.post("/api/clear_qsos")
def api_clear_qsos() -> JSONResponse:
    _backup_before_destructive("clear_qsos")
    qso_db.clear_all()
    logger.info("QSO database cleared")
    return JSONResponse({"ok": True})


@app.post("/api/delete_recordings")
def api_delete_recordings() -> JSONResponse:
    src, was_paused = _pause_for_maintenance()
    _backup_before_destructive("delete_recordings")
    qso_db.clear_all()
    removed = _remove_recordings_dir()
    _resume_after_maintenance(src, was_paused)
    logger.info("All recordings deleted (%d entries removed)", removed)
    return JSONResponse({"ok": True, "removed": removed})


@app.post("/api/delete_contest")
def api_delete_contest(payload: dict) -> JSONResponse:
    contest = _safe_contest(payload.get("contest"))
    contest_dir = os.path.join(RECORDINGS_DIR, contest)
    _backup_before_destructive("delete_contest")
    removed_files = 0
    if os.path.isdir(contest_dir):
        shutil.rmtree(contest_dir)
        removed_files = 1
    deleted_rows = qso_db.delete_contest(contest)
    logger.info("Contest '%s' deleted (%d DB rows, %d folders)", contest, deleted_rows, removed_files)
    return JSONResponse({"ok": True, "contest": contest, "deleted_rows": deleted_rows})


@app.post("/api/delete_contest_recordings")
def api_delete_contest_recordings(payload: dict) -> JSONResponse:
    contest = _safe_contest(payload.get("contest"))
    contest_dir = os.path.join(RECORDINGS_DIR, contest)
    _backup_before_destructive("delete_contest_recordings")
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
    src, was_paused = _pause_for_maintenance()
    _backup_before_destructive("delete_continuous")
    deleted_rows, file_paths = qso_db.delete_continuous_range(date_from, date_to)
    removed_files = 0
    failed_files = 0
    root = os.path.realpath(RECORDINGS_DIR)
    for rel_path in file_paths:
        fp = os.path.realpath(os.path.join(root, rel_path))
        try:
            if os.path.commonpath([os.path.normcase(root), os.path.normcase(fp)]) != os.path.normcase(root):
                logger.warning("Refusing to delete file outside recordings: %s", fp)
                failed_files += 1
                continue
            if os.path.isfile(fp):
                os.remove(fp)
            removed_files += 1
        except (OSError, ValueError) as e:
            logger.warning("Cannot delete continuous file %s: %s", fp, e)
            failed_files += 1
    _resume_after_maintenance(src, was_paused)
    logger.info("Continuous recordings deleted (%d files, %d DB rows, %d failed)",
                removed_files, deleted_rows, failed_files)
    return JSONResponse({"ok": True, "deleted_files": removed_files, "deleted_rows": deleted_rows})


@app.post("/api/factory_reset")
def api_factory_reset() -> JSONResponse:
    global cfg
    src, _was_paused = _pause_for_maintenance()
    _backup_before_destructive("factory_reset")
    qso_db.clear_all()
    _remove_recordings_dir()
    new_cfg = AppConfig()
    save_config(new_cfg, "config.cfg")
    with _config_lock:
        cfg = new_cfg
    LOG_BUFFER.clear()
    _apply_and_restart()
    logger.info("Factory reset completed")
    return JSONResponse({"ok": True, "values": config_to_dict(new_cfg)})


def _safe_contest(raw) -> str:
    """Sanitize a contest name to a single path-safe entry (rejects traversal).

    Rejects absolute paths, drive-relative paths (``C:foo``) and any name that
    still contains a path separator. Contest folder names created by the app are
    already sanitized, so only legacy/odd values are affected.
    """
    name = str(raw or "").strip()
    if (not name or name in (".", "..") or os.path.isabs(name)
            or name.startswith("/") or name.startswith("\\")
            or "/" in name or "\\" in name or ":" in name):
        raise HTTPException(status_code=400, detail="invalid contest name")
    base = os.path.basename(name)
    if not base or base in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid contest name")
    return base


def _validate_field_range(field: str, value) -> None:
    """Reject invalid config values before they are applied."""
    limits = {
        "sample_rate": (8000, 192000), "channels": (1, 2),
        "pre_roll": (0.0, 120.0), "post_roll": (0.0, 120.0),
        "continuous_chunk_minutes": (1, 1440),
        "tci_port": (1, 65535), "n1mm_udp_port": (1, 65535),
        "web_port": (1, 65535), "max_recordings_gb": (0.0, 100000.0),
    }
    choices = {
        "so2r_mode": ["stereo", "dual_card"],
        "audio_mode": ["tci", "soundcard"],
        "audio_format": ["wav", "mp3"],
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


def _continuous_chunk_timestamp(filename: str) -> float:
    """Return epoch from a continuous chunk filename, or mtime as fallback."""
    import re
    from datetime import datetime, timezone
    m = re.match(r"^(\d{8})_(\d{6})", filename)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y%m%d %H%M%S"
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        return os.path.getmtime(os.path.join(RECORDINGS_DIR, "_continuous", filename))
    except OSError:
        return 0.0


def enforce_disk_limit() -> int:
    """Delete oldest continuous chunks until under max_recordings_gb (no-op when 0/unlimited)."""
    cfg_local = _read_cfg()
    limit_gb = getattr(cfg_local, "max_recordings_gb", 0.0) or 0.0
    if limit_gb <= 0:
        return 0
    rec_dir = RECORDINGS_DIR
    cont_dir = os.path.join(rec_dir, "_continuous")
    if not os.path.isdir(cont_dir):
        return 0
    open_paths = set()
    if audio_source is not None and hasattr(audio_source, "get_open_files"):
        try:
            open_paths = {os.path.abspath(p) for p in audio_source.get_open_files()}
        except Exception:
            open_paths = set()
    removed = 0
    while True:
        total = 0.0
        files = []
        for fn in os.listdir(cont_dir):
            fp = os.path.join(cont_dir, fn)
            if not os.path.isfile(fp) or os.path.abspath(fp) in open_paths:
                continue
            try:
                total += os.path.getsize(fp) / (1024 ** 3)
                files.append((_continuous_chunk_timestamp(fn), fp, fn))
            except OSError:
                pass
        if total <= limit_gb or not files:
            break
        files.sort()
        _ts, fp, fn = files[0]
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
        push_event("continuous_chunk_saved")
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
                n1mm_running = n1mm is not None and getattr(n1mm, "_running", False)
                cfg_local = _read_cfg()
                tci_connected = bool(status.get("connected")) if status and cfg_local.audio_mode == "tci" else False
                logger.debug(
                    "heartbeat: audio_running=%s buf=[%s] tci_connected=%s "
                    "n1mm_running=%s continuous_paused=%s",
                    running, buf_txt,
                    tci_connected,
                    n1mm_running,
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
