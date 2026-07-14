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
import queue
import shutil
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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

# Rolling in-memory log buffer (captured via a custom handler).
LOG_BUFFER: "queue.Queue" = queue.Queue(maxsize=2000)


class _LogCaptureHandler(logging.Handler):
    """Append formatted log records into LOG_BUFFER for the web UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            LOG_BUFFER.put_nowait(msg)
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
    items = list(LOG_BUFFER.queue)
    return items[-n:]


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
    # the post-roll delay elapses, so we only schedule the slice here.
    schedule_qso_slice(contact, audio_source, cfg, None)


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
    logger.info("QSOCapture started (mode=%s)", cfg.audio_mode)
    yield
    if n1mm:
        n1mm.stop()
    if audio_source:
        audio_source.stop()
    logger.info("QSOCapture stopped")


app = FastAPI(title="QSOCapture", version="1.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helper: scan recordings directory and build QSO records (legacy fallback)
# ---------------------------------------------------------------------------
def _scan_qsos(contest_filter: Optional[str] = None) -> List[dict]:
    """Walk the recordings dir and return metadata for each WAV file.

    The filename convention is ``YYYY-MM-DD_HHMM_CALL_BAND_LABEL.wav``.
    """
    results: List[dict] = []
    root = cfg.recordings_dir
    if not os.path.isdir(root):
        return results

    for contest in sorted(os.listdir(root)):
        cdir = os.path.join(root, contest)
        if not os.path.isdir(cdir):
            continue
        if contest_filter and contest != contest_filter:
            continue
        for fname in os.listdir(cdir):
            if not (fname.endswith(".wav") or fname.endswith(".mp3")):
                continue
            # Parse: 2026-07-13_2120_SQ3RX_20M_RX1.wav
            parts = fname[:-4].split("_")
            if len(parts) < 5:
                continue
            date_part, time_part, call, band, *rest = parts
            label = rest[0] if rest else "RX1"
            ts = f"{date_part} {time_part}"
            results.append({
                "contest": contest,
                "file": fname,
                "call": call,
                "band": band,
                "label": label,
                "timestamp": ts,
                "url": f"/audio/{contest}/{fname}",
            })
    # Sort by timestamp descending (newest first).
    results.sort(key=lambda r: r["timestamp"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Web / API routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve the Tailwind dashboard."""
    path = cfg.dashboard_file
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
    )
    return JSONResponse({"count": len(qsos), "qsos": qsos})


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
                    setattr(cfg, field, int(raw))
                elif ftype == "float":
                    setattr(cfg, field, float(raw))
                elif ftype == "bool":
                    setattr(cfg, field, str(raw).lower() in ("1", "true", "yes", "on"))
                else:
                    setattr(cfg, field, str(raw))
            except (ValueError, TypeError) as e:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid value for {field}: {e}")
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
        while not LOG_BUFFER.empty():
            try:
                LOG_BUFFER.get_nowait()
            except Exception:
                break
    _apply_and_restart()
    logger.info("Factory reset completed")
    return JSONResponse({"ok": True, "values": config_to_dict(cfg)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="info",
    )