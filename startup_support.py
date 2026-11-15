"""Pure-stdlib helpers shared by the desktop launcher and the FastAPI app.

Kept free of Qt/uvicorn imports so the launcher can configure logging and the
WebEngine environment *before* the Qt DLLs are loaded, and so the logic can be
unit-tested without a GUI.
"""

from __future__ import annotations

import os
import time

MARKER_FILENAME = "launch.pending"
SAFE_MODE_ENV = "QSOCAPTURE_SAFE_MODE"
DISABLE_AUDIO_ENV = "QSOCAPTURE_DISABLE_AUDIO"

# Keep the dashboard's JavaScript and EventSource stream alive while the
# window is minimised to the tray or covered by the logging program.
BASE_CHROMIUM_FLAGS = (
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)

# Conservative rendering used after an unclean previous launch (the reported
# heap-corruption / access-violation crashes happen inside Qt WebEngine).
SAFE_MODE_CHROMIUM_FLAGS = (
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
)

_TRUTHY = ("1", "true", "yes", "on")


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def launch_marker_path(app_dir: str) -> str:
    return os.path.join(app_dir, MARKER_FILENAME)


def launch_pending(app_dir: str) -> bool:
    """Return True when the previous launch did not reach the dashboard."""
    return os.path.isfile(launch_marker_path(app_dir))


def mark_launch_pending(app_dir: str) -> None:
    """Record that a launch is in progress (best effort)."""
    try:
        os.makedirs(app_dir, exist_ok=True)
        with open(launch_marker_path(app_dir), "w", encoding="utf-8") as fh:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fh.write(f"{stamp} pid={os.getpid()}\n")
    except OSError:
        pass


def clear_launch_marker(app_dir: str) -> None:
    try:
        os.remove(launch_marker_path(app_dir))
    except OSError:
        pass


def build_chromium_flags(existing: str, safe_mode: bool) -> str:
    """Merge the required Chromium flags into *existing* without duplicates."""
    flags = (existing or "").split()
    wanted = BASE_CHROMIUM_FLAGS + (SAFE_MODE_CHROMIUM_FLAGS if safe_mode else ())
    for flag in wanted:
        if flag not in flags:
            flags.append(flag)
    return " ".join(flags)


def safe_mode_requested(app_dir: str) -> bool:
    """True if safe mode was forced via the environment or a crash left a marker."""
    if _env_truthy(os.environ.get(SAFE_MODE_ENV)):
        return True
    return launch_pending(app_dir)


def audio_disabled() -> bool:
    """True when audio capture must stay off (safe mode / explicit request)."""
    return _env_truthy(os.environ.get(DISABLE_AUDIO_ENV))
