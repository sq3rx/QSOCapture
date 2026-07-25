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
3. Copy ``index.html`` (and ``icon.ico``) into the writable app directory on
   every launch, **overwriting any older copy** left by a previous version (the
   file is frozen inside the bundle but must live in the writable directory so
   ``main.py`` can serve it — overwriting is what makes updates take effect).
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


def _is_windows_legacy() -> bool:
    """Return True on Windows versions older than Windows 10 (i.e. Win7/Win8).

    Python 3.9+ and the Edge WebView2 runtime do not run on these systems, so
    the embedded browser must use the CEF (Chromium Embedded Framework)
    backend instead of ``edgechromium``. On modern Windows (10/11) WebView2 is
    preferred because it ships with the OS and needs no extra dependency.
    """
    if sys.platform != "win32":
        return False
    try:
        # sys.getwindowsversion() -> (major, minor, build, platform, service_pack)
        return sys.getwindowsversion().major < 10
    except Exception:
        return False


def _init_cef_legacy(app_dir: str) -> bool:
    """Initialise CEF (cefpython3) explicitly for legacy Windows (7/8).

    On Windows 7/8 the default CEF sandbox (GPU / zygote subprocesses) crashes
    with an Access Violation inside ``libcef.dll`` (APPCRASH, code c0000005).
    pywebview 5.x calls ``cef.Initialize`` itself only with default settings, so
    we must initialise CEF *first* with safe switches and a writable cache path
    before ``webview.start(gui="cef")``. pywebview skips its own initialisation
    when ``cef.Initialized()`` is already True, so there is no double-init.

    Safe settings for legacy Windows:
      * ``no_sandbox`` + ``--no-sandbox``  -> disables the sandbox that AVs.
      * ``--disable-gpu`` / ``--disable-gpu-sandbox`` -> avoids GPU compositing
        crashes on Win7.
      * ``cache_path`` in ``%LOCALAPPDATA%\\QSOCapture\\cef_cache`` -> CEF must
        not write its singleton lock / cache next to the EXE (often read-only
        ``Program Files``), which also caused silent crashes.
      * ``log_file`` -> ``cef_debug.log`` for diagnostics.

    Returns True when CEF was initialised successfully, False otherwise (the
    caller then falls back to the default backend / system browser instead of
    crashing).
    """
    try:
        import cefpython3 as cef
    except Exception as exc:
        print(f"CEF not importable for legacy init: {exc}")
        return False
    try:
        # In some cefpython3 builds (e.g. the CEF 66 legacy wheel) there is no
        # ``Initialized`` attribute, so guard the call instead of assuming it
        # exists. When present and already initialised we skip re-init.
        initialized_fn = getattr(cef, "Initialized", None)
        if initialized_fn is not None and initialized_fn():
            return True
        cache_path = os.path.join(app_dir, "cef_cache")
        os.makedirs(cache_path, exist_ok=True)
        cef_dir = os.path.dirname(cef.__file__)
        settings = {
            "cache_path": cache_path,
            "no_sandbox": True,
            "log_severity": getattr(cef, "LOGSEVERITY_INFO", 0),
            "log_file": os.path.join(app_dir, "cef_debug.log"),
            "locales_dir_path": os.path.join(cef_dir, "locales"),
            "resources_dir_path": cef_dir,
            "browser_subprocess_path": os.path.join(cef_dir, "subprocess.exe"),
        }
        switches = {
            "no-sandbox": "",
            "disable-gpu": "",
            "disable-gpu-sandbox": "",
            "disable-software-rasterizer": "",
        }
        cef.Initialize(settings, switches)
        return True
    except Exception as exc:
        # Log but do NOT crash: let the launcher fall back to another backend.
        try:
            with open(os.path.join(app_dir, "cef_debug.log"), "a", encoding="utf-8") as lf:
                lf.write(time.strftime("%Y-%m-%d %H:%M:%S")
                         + " CEF Initialize failed:\n")
                import traceback
                lf.write(traceback.format_exc())
                lf.write("\n")
        except Exception:
            pass
        print(f"CEF Initialize failed ({exc}); will try fallback backend.")
        return False


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
    """Make sure ``index.html`` (and ``icon.ico``) in the writable app dir are
    the versions bundled with *this* build.

    ``app_dir`` is ``%LOCALAPPDATA%\\QSOCapture`` (always writable without admin
    rights). On every launch we (re)copy the bundled assets there, **overwriting
    any previous copy**. This is what makes application updates take effect: the
    old ``index.html`` left behind by a previous version would otherwise be
    served forever (the dashboard is read from this file), so without the
    overwrite the UI would never change after an upgrade. The copy is
    best-effort — if no bundled asset is found we keep whatever is already there
    so the app still starts.
    """
    for basename in ("index.html", "icon.svg", ICON_BASENAME):
        target = os.path.join(app_dir, basename)
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


class DownloadApi:
    """JavaScript-callable API exposed to the WebView2 frontend.

    The embedded browser cannot trigger normal file downloads (WebView2 simply
    does nothing), so the dashboard calls :meth:`save_recording` to open a
    native "Save As" dialog and copy the recording to the location the user
    picks. The object is attached to ``window.pywebview.api`` by pywebview.
    """

    def __init__(self, recordings_dir: str) -> None:
        self.recordings_dir = recordings_dir

    def save_recording(self, rel_path: str, suggested_name: str) -> dict:
        """Copy a recording to a user-chosen destination.

        * ``rel_path``       – recording path relative to the recordings dir
          (same form as the ``/audio/<rel>`` URL, e.g.
          ``2026_CQWW/SQ3RX_..._RX1.wav`` or ``_continuous/....wav``).
        * ``suggested_name`` – default filename for the save dialog.
        Returns ``{"ok": True, "path": <dst>}`` on success,
        ``{"ok": False, "cancelled": True}`` if the user aborts the dialog, or
        ``{"ok": False, "error": <msg>}`` on failure.
        """
        import webview  # imported lazily; available once the window exists.

        try:
            # Normalise and strip any traversal components so the resolved
            # source can never escape the recordings directory.
            rel = os.path.normpath(rel_path or "").lstrip("./\\")
            if not rel or rel.startswith("..") or os.path.isabs(rel):
                return {"ok": False, "error": "invalid path"}
            src = os.path.join(self.recordings_dir, rel)
            if not os.path.isfile(src):
                return {"ok": False, "error": "recording not found"}
            if not webview.windows:
                return {"ok": False, "error": "browser window unavailable"}
            window = webview.windows[0]
            result = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=suggested_name or os.path.basename(src),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            dst = result if isinstance(result, str) else result[0]
            shutil.copy2(src, dst)
            return {"ok": True, "path": dst}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": str(exc)}


def _set_window_icon_from_thread(icon_path: str) -> None:
    """Set the application icon on the taskbar using the Windows API.

    pywebview's ``icon`` parameter in ``webview.start()`` does not reliably
    set the taskbar icon when using the ``edgechromium`` backend on Windows.
    This function uses ``ctypes`` to call the underlying Win32 API directly
    on the window handle once the window exists.

    Must be called from a background thread after ``webview.start()`` has
    created the window (it blocks on the GUI event loop).
    """
    import ctypes
    import time

    # Wait for the pywebview window to appear (it is created asynchronously
    # inside webview.start()). Poll every 200 ms for up to 10 seconds.
    deadline = time.time() + 10.0
    hwnd = None
    while time.time() < deadline:
        try:
            import webview
            if webview.windows:
                # Get the native window handle from the first (and only) window.
                window = webview.windows[0]
                hwnd = window.get_native_window_handle()
                if hwnd:
                    break
        except Exception:
            pass
        time.sleep(0.2)

    if not hwnd:
        return  # give up; the window will lack a taskbar icon

    # Load the icon from the .ico file using LoadImageW.
    user32 = ctypes.windll.user32
    # LR_LOADFROMFILE = 0x10, LR_DEFAULTSIZE = 0x40
    hicon = user32.LoadImageW(
        None,                    # hInstance (NULL = load from file)
        icon_path,               # lpszName (path to .ico)
        1,                       # IMAGE_ICON = 1
        0, 0,                    # desired width/height (0 = use default)
        0x10 | 0x40,             # LR_LOADFROMFILE | LR_DEFAULTSIZE
    )
    if not hicon:
        return  # failed to load the icon

    # Set the icon on the window: WM_SETICON = 0x80
    # ICON_SMALL = 0, ICON_BIG = 1, ICON_SMALL2 = 2
    user32.SendMessageW(hwnd, 0x80, 0, hicon)   # small icon (title bar)
    user32.SendMessageW(hwnd, 0x80, 1, hicon)   # big icon (taskbar / Alt+Tab)
    user32.SendMessageW(hwnd, 0x80, 2, hicon)   # small2 icon (taskbar)


def main() -> None:
    app_dir = _app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.chdir(app_dir)

    # Import main AFTER switching into the writable app dir so that its
    # module-level ``load_config()`` reads/writes ``config.cfg`` from AppData
    # rather than the read-only Program Files location.
    try:
        import main as qso_main  # reuse the already-built FastAPI app + config
    except Exception as exc:  # any import-time failure (e.g. a syntax /
        # version incompatibility) must NOT be silent -- the frozen console
        # build discards stderr, so without this the app would "do nothing".
        try:
            err_path = os.path.join(app_dir, "launcher_error.log")
            with open(err_path, "a", encoding="utf-8") as lf:
                lf.write(time.strftime("%Y-%m-%d %H:%M:%S")
                         + " FATAL: failed to import main module:\n")
                import traceback
                lf.write(traceback.format_exc())
                lf.write("\n")
        except Exception:
            pass
        # Also surface on stderr in case a console is attached.
        print("FATAL: QSOCapture failed to start:", exc, file=sys.stderr)
        return

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

        # JS API that lets the dashboard open a native "Save As" dialog for a
        # recording (WebView2 cannot perform normal downloads). Its methods are
        # reachable from the frontend via ``window.pywebview.api``.
        download_api = DownloadApi(qso_main.cfg.recordings_dir)

        # Open the dashboard inside the embedded WebView2 browser.
        # Force the native Edge WebView2 backend (gui='edgechromium') so the
        # app opens in its OWN window instead of falling back to the system
        # browser. The default pywebview backend on Windows is WinForms/IE
        # (which needs pythonnet); when that is not available the launcher
        # silently dropped to webbrowser.open(). Edge WebView2 ships with
        # modern Windows 10/11 and requires no extra dependency.
        webview.create_window(
            "QSOCapture",
            base_url,
            width=1280,
            height=800,
            min_size=(900, 600),
            text_select=True,
            confirm_close=True,
            js_api=download_api,
        )

        # Decide which GUI backend to use. The goal is ALWAYS to open the
        # dashboard in its OWN native window -- never in the system browser.
        #
        # Backend priority on MODERN Windows (10/11):
        #   1. edgechromium (Edge WebView2) -- ships with the OS, needs NO
        #      extra install. Reliable default on modern Windows.
        #   2. cef (Chromium Embedded Framework) -- only if cefpython3 is
        #      installed/importable. Bundling a full Chromium engine is heavy
        #      and wheels are not available for every Python version, so we
        #      treat it as an OPTIONAL fallback rather than the primary backend.
        #   3. default (whatever pywebview auto-detects) -- last embedded try.
        #   4. webbrowser -- ONLY if every embedded backend failed.
        #
        # Backend priority on LEGACY Windows (7/8, major < 10):
        #   Edge WebView2 does NOT exist there, and Python 3.9+ (hence the
        #   modern build) cannot even run. The legacy build is therefore made
        #   with Python 3.8 + cefpython3, so the CEF backend is the PRIMARY
        #   (and only embedded) choice; we skip edgechromium entirely.
        def _log_fallback(reason: str) -> None:
            try:
                with open(os.path.join(app_dir, "webview_fallback.log"), "a", encoding="utf-8") as lf:
                    lf.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + reason + "\n")
            except Exception:
                pass

        cef_available = False
        try:
            import cefpython3  # noqa: F401  (only check importability)
            cef_available = True
        except Exception:
            cef_available = False

        legacy_windows = _is_windows_legacy()

        started = False
        last_exc: Exception | None = None

        # The application icon is passed to webview.start() (not create_window,
        # which does NOT accept an `icon` argument in this pywebview version).
        # This avoids a TypeError at window creation that previously forced the
        # launcher to fall back to the system browser.
        icon_arg = icon_path or None

        if legacy_windows:
            # ---- Legacy Windows (7/8): CEF first, no Edge WebView2 ----
            if cef_available:
                # Initialise CEF explicitly with safe flags BEFORE pywebview
                # starts it. On Win7/8 the default CEF sandbox crashes with an
                # Access Violation in libcef.dll (APPCRASH c0000005); the
                # explicit init disables the sandbox, the GPU compositing and
                # points the cache at a writable AppData dir. pywebview skips
                # its own cef.Initialize() when already initialised.
                if not _init_cef_legacy(app_dir):
                    cef_available = False
                if cef_available:
                    try:
                        if icon_path:
                            threading.Thread(target=_set_window_icon_from_thread, args=(icon_path,), daemon=True).start()
                        webview.start(gui="cef", icon=icon_arg)
                        started = True
                    except Exception as exc:
                        last_exc = exc
                        print(f"CEF backend failed ({exc}); trying default backend.")
            else:
                print("CEF backend not available -- legacy Windows needs cefpython3.")

            # default (last embedded try) then system browser.
            if not started:
                try:
                    if icon_path:
                        threading.Thread(target=_set_window_icon_from_thread, args=(icon_path,), daemon=True).start()
                    webview.start(icon=icon_arg)
                    started = True
                except Exception as exc:
                    last_exc = exc
                    print(f"Default backend failed ({exc}).")
        else:
            # ---- Modern Windows (10/11): Edge WebView2 first ----
            # 1) Edge WebView2 (preferred, zero extra dependency).
            try:
                if icon_path:
                    threading.Thread(target=_set_window_icon_from_thread, args=(icon_path,), daemon=True).start()
                webview.start(gui="edgechromium", icon=icon_arg)
                started = True
            except Exception as exc:
                last_exc = exc
                print(f"Edge WebView2 backend failed ({exc}); trying next backend.")

            # 2) CEF -- only if the package is installed.
            if not started and cef_available:
                try:
                    if icon_path:
                        threading.Thread(target=_set_window_icon_from_thread, args=(icon_path,), daemon=True).start()
                    webview.start(gui="cef", icon=icon_arg)
                    started = True
                except Exception as exc:
                    last_exc = exc
                    print(f"CEF backend failed ({exc}); trying default backend.")

            # 3) Default auto-detected backend.
            if not started:
                try:
                    if icon_path:
                        threading.Thread(target=_set_window_icon_from_thread, args=(icon_path,), daemon=True).start()
                    webview.start(icon=icon_arg)
                    started = True
                except Exception as exc:
                    last_exc = exc
                    print(f"Default backend failed ({exc}).")

        # 4) Last resort: system browser (logs the failure for diagnostics).
        if not started:
            reason = f"all embedded backends failed: {last_exc}"
            print(f"Embedded browser unavailable ({last_exc}); opening system browser.")
            _log_fallback(reason)
            webbrowser.open(base_url)
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass
    except Exception as exc:  # webview import or window creation failed.
        print(f"Embedded browser unavailable ({exc}); opening system browser.")
        try:
            with open(os.path.join(app_dir, "webview_fallback.log"), "a", encoding="utf-8") as lf:
                lf.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(exc) + "\n")
        except Exception:
            pass
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