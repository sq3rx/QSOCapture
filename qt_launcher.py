"""qt_launcher.py - Desktop EXE entry point for QSOCapture (PySide6).

This module replaces the old pywebview-based launcher with a PySide6
(Qt for Python) window that embeds a QWebEngineView browser. Benefits:

* One installer for all Windows versions (7+).
* Native minimize-to-system-tray via QSystemTrayIcon.
* Simple icon management via setWindowIcon() / trayIcon.setIcon().
* JS-Python bridge via QWebChannel (replaces window.pywebview.api).

How it works
------------
1. Detect whether we are running from a PyInstaller bundle (sys._MEIPASS).
2. Switch the working directory to %LOCALAPPDATA%\\QSOCapture so that
   config.cfg / recordings/ are stored in a writable location.
3. Copy index.html (and icon.ico) into the writable app directory on every
   launch, overwriting any older copy so updates take effect.
4. Start the FastAPI/uvicorn server in a background thread, binding to
   127.0.0.1 so only the local machine can reach it.
5. Once the server is answering, open a QMainWindow with a QWebEngineView
   pointing at http://127.0.0.1:<port>/.
6. When the window is closed (or minimized to tray), gracefully shut the
   server down and exit.
"""

from __future__ import annotations

import os
import sys
import time
import threading
import shutil
import urllib.request
import logging

# When frozen with console=False, sys.stdout/stderr can be None which makes
# uvicorn's colourised log formatter crash on `.isatty()`. Guarantee real
# streams so any fall-back logging works.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn

import config as config_module

import ctypes
from PySide6.QtCore import QUrl, QObject, Slot, Signal, Qt, QEvent
from PySide6.QtGui import QIcon, QAction, QCloseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QMenu, QMessageBox
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

logger = logging.getLogger("QSOCapture.qt_launcher")


# ---------------------------------------------------------------------------
# Bridge object exposed to JavaScript via QWebChannel
# ---------------------------------------------------------------------------
class Bridge(QObject):
    """Python object exposed to the JavaScript frontend via QWebChannel.

    Replaces the old ``window.pywebview.api`` from pywebview. Methods
    decorated with ``@Slot`` are callable from JavaScript.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recordings_dir = config_module.RECORDINGS_DIR

    @Slot(str, result=str)
    def openUrl(self, url: str) -> str:
        """Open a URL in the system default browser.

        QWebEngineView cannot open external links natively, so the frontend
        calls this method via the QWebChannel bridge.
        """
        import json
        import webbrowser
        try:
            webbrowser.open(url)
            return json.dumps({"ok": True})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, str, result=str)
    def saveRecording(self, rel_path: str, suggested_name: str) -> str:
        """Copy a recording to a user-chosen destination.

        This is called from JavaScript (via the compatibility layer in
        index.html). Returns a JSON string with the result so the JS
        Promise can resolve/reject accordingly.

        Args:
            rel_path: Recording path relative to the recordings dir.
            suggested_name: Default filename for the save dialog.

        Returns:
            JSON string: {"ok": true, "path": "..."} or
                         {"ok": false, "error": "..."} or
                         {"ok": false, "cancelled": true}
        """
        import json
        try:
            # Normalise and strip any traversal components so the resolved
            # source can never escape the recordings directory.
            rel = os.path.normpath(rel_path or "").lstrip("./\\")
            if not rel or rel.startswith("..") or os.path.isabs(rel):
                return json.dumps({"ok": False, "error": "invalid path"})
            src = os.path.join(self.recordings_dir, rel)
            if not os.path.isfile(src):
                return json.dumps({"ok": False, "error": "recording not found"})

            # Use Qt's native file dialog
            from PySide6.QtWidgets import QFileDialog
            from PySide6.QtCore import QDir

            # Find the main window to parent the dialog
            parent_widget = None
            for w in QApplication.topLevelWidgets():
                if isinstance(w, QMainWindow):
                    parent_widget = w
                    break

            dst, _ = QFileDialog.getSaveFileName(
                parent_widget,
                "Save Recording",
                os.path.join(QDir.homePath(), suggested_name or os.path.basename(src)),
                "Audio Files (*.wav *.mp3);;All Files (*)",
            )
            if not dst:
                return json.dumps({"ok": False, "cancelled": True})

            shutil.copy2(src, dst)
            return json.dumps({"ok": True, "path": dst})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Main application window with embedded QWebEngineView."""

    def __init__(self, app_dir: str, base_url: str, icon_path: str):
        super().__init__()
        self.app_dir = app_dir
        self.base_url = base_url
        self._closing = False  # flag to distinguish close vs minimize-to-tray

        # Window setup
        self.setWindowTitle("QSOCapture")
        self.setMinimumSize(900, 600)
        self.resize(1280, 800)

        # Set window icon
        self._icon_path = icon_path
        if icon_path and os.path.isfile(icon_path):
            icon = QIcon(icon_path)
            self.setWindowIcon(icon)

        # Create the web view
        self.web_view = QWebEngineView()
        self.web_view.setUrl(QUrl(base_url))
        self.setCentralWidget(self.web_view)

        # Set up QWebChannel for JS-Python bridge
        self.channel = QWebChannel()
        self.bridge = Bridge()
        self.channel.registerObject("bridge", self.bridge)
        self.web_view.page().setWebChannel(self.channel)

        # System tray icon
        self.tray_icon = QSystemTrayIcon(self)
        if icon_path and os.path.isfile(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            self.tray_icon.setIcon(self.windowIcon())

        # Tray menu
        tray_menu = QMenu()
        show_action = QAction("Show / Hide", self)
        show_action.triggered.connect(self.toggle_visibility)
        tray_menu.addAction(show_action)

        quit_action = QAction("Quit QSOCapture", self)
        quit_action.triggered.connect(self.quit_application)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

        # Set tray tooltip
        self.tray_icon.setToolTip("QSOCapture")

    def closeEvent(self, event: QCloseEvent) -> None:
        """Override close event to show a confirmation dialog.

        If the user confirms, the application quits. Otherwise the event
        is ignored and the window stays open.
        """
        if self._closing:
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            "Quit QSOCapture",
            "Are you sure you want to quit QSOCapture?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._closing = True
            event.accept()
        else:
            event.ignore()

    def changeEvent(self, event):
        """Override changeEvent to minimize to tray on window minimize."""
        try:
            if event.type() == QEvent.Type.WindowStateChange:
                if self.windowState() & Qt.WindowMinimized:
                    event.ignore()
                    self.hide()
                    self.tray_icon.showMessage(
                        "QSOCapture",
                        "Application minimized to system tray.\n"
                        "Click the icon to restore the window.",
                        QSystemTrayIcon.Information,
                        3000,
                    )
                    return
            super().changeEvent(event)
        except KeyboardInterrupt:
            # Ignore KeyboardInterrupt during shutdown — it may be raised
            # by Python's signal handler when the process is being killed.
            pass

    def toggle_visibility(self) -> None:
        """Show or hide the main window."""
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()
            self.raise_()
            # Workaround for Qt WebEngine bug: after hide/show the page
            # becomes unresponsive (dropdowns, clicks). Forcing a resize
            # and repaint on the web view fixes it.
            self.web_view.update()
            from PySide6.QtCore import QTimer
            QTimer.singleShot(50, self._fix_webengine_after_show)

    def _fix_webengine_after_show(self) -> None:
        """Force Qt WebEngine to repaint properly after window restore."""
        if not self.isVisible():
            return
        # Toggle the page visibility to force a full repaint
        page = self.web_view.page()
        page.setVisible(False)
        page.setVisible(True)
        # Also force a resize event on the web view
        size = self.web_view.size()
        self.web_view.resize(size.width() + 1, size.height())
        self.web_view.resize(size)

    def showEvent(self, event):
        """Override showEvent to set the taskbar icon once the window is visible.

        Qt's setWindowIcon() does not reliably set the taskbar icon on Windows
        (it often shows the default Python icon instead). We use the Win32 API
        directly on the window handle, but the handle is only valid after the
        window is shown, so we do it here (once).
        """
        super().showEvent(event)
        if self._icon_path and os.path.isfile(self._icon_path):
            # Schedule the taskbar icon update after the event loop starts,
            # to ensure the window handle is fully valid.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, lambda: self._set_taskbar_icon(self._icon_path))
            self._icon_path = None  # only run once

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handle tray icon click (single click restores window)."""
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_visibility()

    def _set_taskbar_icon(self, icon_path: str) -> None:
        """Set the application icon on the Windows taskbar using the Win32 API.

        Qt's setWindowIcon() does not reliably set the taskbar icon on Windows
        (it often shows the default Python icon instead). This method uses
        ctypes to call the underlying Win32 API directly on the window handle.

        We also try to find and update the QWebEngineView child window handle,
        because Qt WebEngine may create a separate sub-process window that
        shows the default Python icon on the taskbar.
        """
        if sys.platform != "win32":
            return
        try:
            # Load the icon from the .ico file using LoadImageW.
            # LR_LOADFROMFILE = 0x10, LR_DEFAULTSIZE = 0x40
            hicon = ctypes.windll.user32.LoadImageW(
                None,                    # hInstance (NULL = load from file)
                icon_path,               # lpszName (path to .ico)
                1,                       # IMAGE_ICON = 1
                0, 0,                    # desired width/height (0 = use default)
                0x10 | 0x40,             # LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            if not hicon:
                return  # failed to load the icon

            # Set the icon on the main window: WM_SETICON = 0x80
            # ICON_SMALL = 0, ICON_BIG = 1, ICON_SMALL2 = 2
            hwnd = int(self.winId())
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 0, hicon)   # small icon (title bar)
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 1, hicon)   # big icon (taskbar / Alt+Tab)
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 2, hicon)   # small2 icon (taskbar)

            # Also try to update the window style to force the icon to appear
            # on the taskbar. WS_EX_APPWINDOW = 0x40000 forces a top-level
            # window onto the taskbar.
            GWL_EXSTYLE = -20
            current_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, current_style | 0x40000)
        except Exception:
            pass  # non-Windows or ctypes unavailable

    def quit_application(self) -> None:
        """Actually quit the application (bypass minimize-to-tray)."""
        self._closing = True
        QApplication.quit()


# ---------------------------------------------------------------------------
# Helper functions (ported from launcher.py)
# ---------------------------------------------------------------------------
def _is_frozen() -> bool:
    """Return True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _app_dir() -> str:
    """Directory that should hold config/recordings/index.html.

    Data is stored in the current user's ``%LOCALAPPDATA%\\QSOCapture`` which
    is always writable without administrator privileges.
    """
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "QSOCapture",
    )


ICON_BASENAME = "icon.ico"


def _ensure_assets(app_dir: str) -> None:
    """Make sure ``index.html`` (and ``icon.ico``) in the writable app dir are
    the versions bundled with *this* build."""
    for basename in ("index.html", "icon.svg", ICON_BASENAME):
        target = os.path.join(app_dir, basename)
        candidates = []
        if _is_frozen():
            candidates.append(os.path.join(sys._MEIPASS, basename))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), basename))
        for src in candidates:
            if os.path.isfile(src):
                try:
                    shutil.copyfile(src, target)
                except OSError:
                    pass
                break


def _wait_for_server(url: str, timeout: float = 10.0) -> bool:
    """Block until the local server responds or *timeout* elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.05)
    return False


def _migrate_legacy_data(app_dir: str) -> None:
    """Move user data left in the executable directory into the per-user app_dir."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        legacy_dir = os.path.dirname(sys.executable)
    else:
        legacy_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.normcase(os.path.abspath(legacy_dir)) == os.path.normcase(os.path.abspath(app_dir)):
        return

    items = ["config.cfg", "qsos.db", "qsos.db-wal", "qsos.db-shm", "recordings"]
    for name in items:
        src = os.path.join(legacy_dir, name)
        dst = os.path.join(app_dir, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    app_dir = _app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.chdir(app_dir)

    # Import main AFTER switching into the writable app dir so that its
    # module-level ``load_config()`` reads/writes ``config.cfg`` from AppData.
    try:
        import main as qso_main
    except Exception as exc:
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
        print("FATAL: QSOCapture failed to start:", exc, file=sys.stderr)
        return

    _migrate_legacy_data(app_dir)
    _ensure_assets(app_dir)

    # Force the server to bind locally only (the embedded browser is local).
    qso_main.cfg.web_host = "127.0.0.1"
    qso_main.cfg.web_port = int(qso_main.cfg.web_port)

    port = qso_main.cfg.web_port
    base_url = f"http://127.0.0.1:{port}/"

    # log_config=None -> skip uvicorn's colourised formatter setup, which
    # crashes under a frozen (no-tty) executable.
    config = uvicorn.Config(
        qso_main.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)

    # Locate the application icon BEFORE creating the Qt application.
    icon_path = ""
    for cand in (
        os.path.join(app_dir, ICON_BASENAME),
        os.path.join(sys._MEIPASS, ICON_BASENAME) if _is_frozen() else "",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ICON_BASENAME),
    ):
        if cand and os.path.isfile(cand):
            icon_path = cand
            break

    # Create the Qt application BEFORE starting the server so that Qt
    # WebEngine's Chromium sub-process starts in parallel with uvicorn.
    app = QApplication(sys.argv)
    app.setApplicationName("QSOCapture")
    app.setOrganizationName("SQ3RX")

    # Set the application icon globally BEFORE any window is created.
    # On Windows, the taskbar icon is determined by the App User Model ID
    # and the application icon. Setting it here ensures the taskbar gets
    # the correct icon instead of the default Python icon.
    if icon_path and os.path.isfile(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
        # Force the App User Model ID so Windows groups the taskbar icon
        # correctly and uses our icon instead of the Python default.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "QSOCapture"
            )
        except Exception:
            pass

    # Start the server in a background thread.
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # Wait for the server to be ready (this takes ~0.5-1s while Qt WebEngine
    # is also initialising in the background).
    if not _wait_for_server(base_url, timeout=10.0):
        print("ERROR: QSOCapture server failed to start.", file=sys.stderr)
        return

    # Create and show the main window. By this point Qt WebEngine's Chromium
    # process has already started (via QApplication), so the window appears
    # faster.
    window = MainWindow(app_dir, base_url, icon_path)
    window.show()

    # Run the Qt event loop
    exit_code = app.exec()

    # Window closed -> shut the server down cleanly.
    server.should_exit = True
    try:
        server_thread.join(timeout=2.0)
    except Exception:
        pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()