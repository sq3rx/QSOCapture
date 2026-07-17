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


def _is_frozen() -> bool:
    """Return True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _app_dir() -> str:
    """Directory that should hold config/recordings/index.html.

    Data is stored in the current user's ``%LOCALAPPDATA%\\QSOCapture`` which
    is always writable without administrator privileges (unlike
    ``C:\\Program Files`` where the executable is installed). Keeping user data
    out of Program Files is what lets the app run normally for a non-admin
    user.
    """
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "QSOCapture",
    )


# Name of the bundled application icon (multi-size .ico).
ICON_BASENAME = "icon.ico"


def _ensure_assets(app_dir: str) -> None:
    """Make sure ``index.html`` (and ``icon.ico``) exist in the writable app dir.

    ``app_dir`` is already ``%LOCALAPPDATA%\\QSOCapture`` (always writable
    without admin rights), so we can simply copy the bundled assets there. The
    icon is served as the dashboard favicon by main.py and used as the WebView2
    window icon by the launcher.
    """
    for basename in ("index.html", ICON_BASENAME):
        target = os.path.join(app_dir, basename)
        if os.path.isfile(target):
            continue
        # Look for the asset inside the bundle first, then the source tree.
        candidates = []
        if _is_frozen():
            candidates.append(os.path.join(sys._MEIPASS, basename))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), basename))
        for src in candidates:
            if os.path.isfile(src):
                try:
                    shutil.copyfile(src, target)
                except OSError:
                    pass  # best-effort; app still works without this asset
                break


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


def _migrate_legacy_data(app_dir: str) -> None:
    """Move user data left in the executable directory (e.g. an old
    ``C:\\Program Files\\QSOCapture`` install) into the per-user *app_dir*.

    Before this change the app stored ``config.cfg``, ``qsos.db`` and
    ``recordings/`` next to the executable, i.e. inside Program Files where a
    normal user cannot write. We now keep everything in ``%LOCALAPPDATA%``.
    On first launch we relocate any existing data so previously recorded QSOs
    are not lost. The move is best-effort: failures (e.g. locked files) are
    skipped and the new location simply starts fresh for that item.
    """
    # The executable directory (frozen) or script directory (dev).
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        legacy_dir = os.path.dirname(sys.executable)
    else:
        legacy_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.normcase(os.path.abspath(legacy_dir)) == os.path.normcase(os.path.abspath(app_dir)):
        return  # already the same location (dev mode)

    items = ["config.cfg", "qsos.db", "qsos.db-wal", "qsos.db-shm", "recordings"]
    for name in items:
        src = os.path.join(legacy_dir, name)
        dst = os.path.join(app_dir, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
        except OSError:
            pass  # keep going; app will recreate what is missing


def main() -> None:
    app_dir = _app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.chdir(app_dir)

    # Import main AFTER switching into the writable app dir so that its
    # module-level ``load_config()`` reads/writes ``config.cfg`` from AppData
    # rather than the read-only Program Files location.
    import main as qso_main  # reuse the already-built FastAPI app + config

    # Bring over any data left behind by an older installation.
    _migrate_legacy_data(app_dir)

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

        # Locate the application icon (next to the executable, in _MEIPASS when
        # frozen, or in the source tree) and use it as the window icon.
        icon_path = ""
        for cand in (
            os.path.join(app_dir, ICON_BASENAME),
            os.path.join(sys._MEIPASS, ICON_BASENAME) if _is_frozen() else "",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ICON_BASENAME),
        ):
            if cand and os.path.isfile(cand):
                icon_path = cand
                break

        # Open the dashboard inside the embedded WebView2 browser.
        webview.create_window(
            "QSOCapture",
            base_url,
            width=1280,
            height=800,
            min_size=(900, 600),
            text_select=True,
            confirm_close=False,
            icon=icon_path or None,
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
