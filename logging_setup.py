"""Centralized logging configuration for QSOCapture.

Installs three sinks for log records:

* ``stderr`` - useful for ``python main.py``; a windowed EXE has ``stderr``
  redirected to ``devnull`` so this is effectively a no-op there,
* a rotating file ``QSOCapture.log`` in the application data directory so
  diagnostics survive a restart/crash,
* an in-memory ring buffer served to the web dashboard via ``/api/log``.

All timestamps are emitted in **UTC** with a trailing ``Z`` so they line up
with N1MM-logged QSO times and the dashboard (both UTC), regardless of the
host's local timezone.

The module is intentionally free of application imports (only ``config``) so
it can be used from the launcher, the FastAPI app and tests alike.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
from collections import deque
from typing import List, Optional

from config import BASE_DIR

LOG_FILENAME = "QSOCapture.log"
LOG_FORMAT = "%(asctime)s.%(msecs)03dZ [%(levelname)s] %(name)s [%(threadName)s]: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

# In-memory ring buffer consumed by the web dashboard. Kept at DEBUG always so
# the dashboard can retroactively show DEBUG history via ``/api/log?debug=1``
# even when file DEBUG emission is switched off.
LOG_BUFFER: "deque[str]" = deque(maxlen=2000)

_CONFIGURED = False
_file_handler: Optional[logging.handlers.RotatingFileHandler] = None
_buffer_handler: Optional["BufferHandler"] = None
_debug_enabled = False


class UTCFormatter(logging.Formatter):
    """Formatter that renders ``%(asctime)s`` in UTC instead of local time."""

    converter = time.gmtime


class BufferHandler(logging.Handler):
    """Append formatted records to :data:`LOG_BUFFER` for the web dashboard."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            # A formatting error must never break the calling thread; logging
            # is best-effort by definition.
            pass


def build_formatter() -> logging.Formatter:
    """Return the shared log formatter (UTC timestamps, level, logger, thread)."""
    return UTCFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def setup_logging(level: int = logging.INFO) -> None:
    """Install the stderr, rotating-file and in-memory handlers (idempotent).

    Safe to call multiple times (e.g. from the launcher and again from the
    FastAPI app); only the first call configures handlers.
    """
    global _CONFIGURED, _file_handler, _buffer_handler
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger()
    root.setLevel(level)
    formatter = build_formatter()

    # stderr - skipped when there is no usable stream (windowed EXE).
    if getattr(sys, "stderr", None) is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    _buffer_handler = BufferHandler()
    _buffer_handler.setLevel(logging.DEBUG)
    _buffer_handler.setFormatter(formatter)
    root.addHandler(_buffer_handler)

    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        log_path = os.path.join(BASE_DIR, LOG_FILENAME)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root.addHandler(handler)
        _file_handler = handler
    except Exception as exc:  # pragma: no cover - disk/permission failures
        root.warning("File logging unavailable: %s", exc)


def set_debug(enabled: bool) -> bool:
    """Enable/disable DEBUG emission at runtime.

    Only the ``QSOCapture`` logger hierarchy, the rotating file handler and
    ``uvicorn.error`` are touched. Third-party loggers (``websockets``,
    ``asyncio``, ``sounddevice``) deliberately keep their own levels so debug
    mode cannot flood the log with library noise. The in-memory buffer always
    records DEBUG; the dashboard filters it out unless requested.
    """
    global _debug_enabled
    _debug_enabled = bool(enabled)
    level = logging.DEBUG if _debug_enabled else logging.INFO
    logging.getLogger("QSOCapture").setLevel(level)
    if _file_handler is not None:
        _file_handler.setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
    logging.getLogger("QSOCapture.main").info(
        "Debug logging %s", "enabled" if _debug_enabled else "disabled")
    return _debug_enabled


def is_debug() -> bool:
    """Return whether runtime DEBUG emission is currently enabled."""
    return _debug_enabled


def get_recent_logs(n: int = 200) -> List[str]:
    """Return the most recent *n* log lines (oldest first)."""
    items = list(LOG_BUFFER)
    return items[-n:]


def clear_buffer() -> None:
    """Drop every buffered log line (used by config/factory reset)."""
    LOG_BUFFER.clear()


def describe_exception(exc: BaseException) -> str:
    """Return a stable, language-independent description of an exception.

    For ``OSError`` (including Windows ``WinError``) the numeric ``errno`` /
    ``winerror`` codes are used instead of the OS-localized ``strerror`` text,
    which differs between Windows display languages and makes logs hard to
    compare or search.
    """
    parts = [type(exc).__name__]
    winerror = getattr(exc, "winerror", None)
    errno_value = getattr(exc, "errno", None)
    if winerror is not None:
        parts.append(f"winerror={winerror}")
    if errno_value is not None:
        parts.append(f"errno={errno_value}")
    if winerror is not None or errno_value is not None:
        return " ".join(parts)
    text = str(exc).strip()
    if text:
        parts.append(text)
    return " ".join(parts)


def handle_uncaught(exc_type, exc, tb) -> None:
    """Log an uncaught exception from the main thread.

    ``KeyboardInterrupt`` / ``SystemExit`` are expected user-initiated exits,
    not crashes, so they are recorded as a plain warning without a traceback.
    """
    logger = logging.getLogger("QSOCapture.main")
    if issubclass(exc_type, KeyboardInterrupt):
        logger.warning("Interrupted by user")
        return
    if issubclass(exc_type, SystemExit):
        return
    logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))


def _handle_thread_exception(args) -> None:
    logging.getLogger("QSOCapture.main").critical(
        "Unhandled exception in thread %s",
        getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def install_excepthooks() -> None:
    """Route uncaught main-thread and background-thread exceptions to the log."""
    sys.excepthook = handle_uncaught
    if hasattr(threading, "excepthook"):
        threading.excepthook = _handle_thread_exception
