"""launcher.py - Desktop EXE entry point for QSOCapture.

This module bundles the FastAPI web server and an embedded browser
(``pywebview`` -> Edge WebView2 on Windows) into a single native window so the
application can be shipped as a standalone ``.exe`` with no external browser.

How it works
------------
1. Detect whether we are running from a PyInstaller bundle (``sys._MEIPASS``).
2. Switch the working directory to the folder that contains the executable so
   that ``config.cfg`` / ``recordings/`` are stored next to the ``.exe``
   (writable location) instead of the temporary ``_MEIPASS`` directory.
3. Copy ``index.html`` next to the executable on first run (it is frozen inside
   the bundle but must live in the writable directory so ``main.py`` can serve
   it).
4. Start the FastAPI/uvicorn server in a background thread, binding to
   ``127.0.0.1`` so only the local machine can reach it.
5. Once the server is answering, open a borderless-ish native window pointing at
   ``http://127.0.0.1:<port>/`` using the system WebView2 engine.
6. When the window is closed, gracefully shut the server down and exit.
"""

from __future__ import annotations

import os
import sys
import time
import threading
import webbrowser
import shutil
import urllib.request

# When frozen with console=False, sys.stdout/stderr can be None which makes
# uvicorn's colourised log formatter crash on `.isatty()`. Guarantee real
# streams so any fall-back logging works.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn

import main as qso_main  # reuse the already-built FastAPI app + config


def _is_frozen() -> bool:
    """Return True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _app_dir() -> str:
    """Directory that should hold config/recordings/index.html.

    When frozen this is the directory containing the executable; otherwise the
    project source directory.
    """
    if _is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _ensure_assets(app_dir: str) -> None:
    """Make sure ``index.html`` exists in the writable app directory.

    When the executable lives in a read-only location (e.g. ``C:\\Program
    Files\\...`` installed by an admin installer), we cannot copy the bundled
    asset next to the ``.exe``. In that case we fall back to a per-user
    directory (``%LOCALAPPDATA%/QSOCapture``) which is always writable, and
    point ``main.py`` at that copy via ``qso_main.INDEX_HTML_OVERRIDE``.
    """
    target = os.path.join(app_dir, "index.html")
    if os.path.isfile(target):
        return
    # Look for the asset inside the bundle first, then the source tree.
    candidates = []
    if _is_frozen():
        candidates.append(os.path.join(sys._MEIPASS, "index.html"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))
    for src in candidates:
        if os.path.isfile(src):
            try:
                shutil.copyfile(src, target)
                return
            except PermissionError:
                # Read-only install dir (e.g. Program Files). Fall back to a
                # writable per-user location and tell main.py to serve from there.
                user_dir = os.path.join(
                    os.environ.get("LOCALAPPDATA", app_dir), "QSOCapture"
                )
                os.makedirs(user_dir, exist_ok=True)
                user_target = os.path.join(user_dir, "index.html")
                shutil.copyfile(src, user_target)
                qso_main.INDEX_HTML_OVERRIDE = user_target
                return
    # If we still don't have it, the dashboard simply won't render, but the
    # server keeps working (user can open a normal browser).


def _wait_for_server(url: str, timeout: float = 15.0) -> bool:
    """Block until the local server responds or *timeout* elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> None:
    app_dir = _app_dir()
    os.chdir(app_dir)
    _ensure_assets(app_dir)

    # Force the server to bind locally only (the embedded browser is local).
    qso_main.cfg.web_host = "127.0.0.1"
    qso_main.cfg.web_port = int(qso_main.cfg.web_port)

    port = qso_main.cfg.web_port
    base_url = f"http://127.0.0.1:{port}/"

    # log_config=None -> skip uvicorn's colourised formatter setup, which
    # crashes under a frozen (no-tty) executable. Application logging is
    # handled by main.py's own handlers.
    config = uvicorn.Config(
        qso_main.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)

    # Run uvicorn in a daemon thread so the GUI thread can own the process.
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(base_url, timeout=20.0):
        print("ERROR: QSOCapture server failed to start.", file=sys.stderr)
        return

    try:
        import webview  # imported late so the frozen bundle loads assets first

        # Open the dashboard inside the embedded WebView2 browser.
        webview.create_window(
            "QSOCapture",
            base_url,
            width=1280,
            height=800,
            min_size=(900, 600),
            text_select=True,
            confirm_close=False,
        )
        webview.start()
    except Exception as exc:  # WebView2 missing / init failure -> fall back.
        print(f"Embedded browser unavailable ({exc}); opening system browser.")
        webbrowser.open(base_url)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass

    # Window closed -> shut the server down cleanly.
    server.should_exit = True
    try:
        server_thread.join(timeout=5.0)
    except Exception:
        pass


if __name__ == "__main__":
    main()