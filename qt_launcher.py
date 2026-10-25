"""Desktop EXE entry point for QSOCapture (PySide6).

Replaces the old pywebview-based launcher with a PySide6 + QWebEngineView
window. Provides minimize-to-tray via QSystemTrayIcon and JS-Python bridge
via QWebChannel.

On launch:
1. Switch working directory to %LOCALAPPDATA%\\QSOCapture.
2. Copy index.html + icons into the writable app directory.
3. Start FastAPI/uvicorn in a background thread.
4. Open a QMainWindow with QWebEngineView pointing at the local server.
5. On close, gracefully shut down the server and exit.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import ctypes

import uvicorn
from PySide6.QtCore import QEvent, QLockFile, QObject, Qt, QUrl, Slot
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QMessageBox, QSystemTrayIcon

import config as config_module
import logging_setup

logger = logging.getLogger("QSOCapture.qt_launcher")


class Bridge(QObject):
    """Python object exposed to JavaScript via QWebChannel (replaces window.pywebview.api)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recordings_dir = config_module.RECORDINGS_DIR

    @Slot(str, result=str)
    def openUrl(self, url: str) -> str:
        """Open a URL in the system default browser (QWebEngineView cannot natively)."""
        import json
        import webbrowser
        try:
            webbrowser.open(url)
            return json.dumps({"ok": True})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @Slot(str, str, result=str)
    def saveRecording(self, rel_path: str, suggested_name: str) -> str:
        """Copy a recording to a user-chosen destination via native file dialog.

        Returns JSON: {"ok":true, "path":"..."} or {"ok":false, "error":"..."}
        or {"ok":false, "cancelled":true}.
        """
        import json
        try:
            rel = os.path.normpath(rel_path or "").lstrip("./\\")
            if (not rel or rel.startswith("..") or os.path.isabs(rel)
                    or ":" in rel):
                return json.dumps({"ok": False, "error": "invalid path"})
            root = os.path.realpath(self.recordings_dir)
            src = os.path.realpath(os.path.join(root, rel))
            try:
                inside = os.path.commonpath(
                    [os.path.normcase(root), os.path.normcase(src)]
                ) == os.path.normcase(root)
            except ValueError:
                inside = False
            if not inside or not os.path.isfile(src):
                return json.dumps({"ok": False, "error": "recording not found"})

            from PySide6.QtCore import QDir
            from PySide6.QtWidgets import QFileDialog

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


class MainWindow(QMainWindow):
    """Main application window with embedded QWebEngineView."""

    def __init__(self, app_dir: str, base_url: str, icon_path: str):
        super().__init__()
        self.app_dir = app_dir
        self.base_url = base_url
        self._closing = False

        self.setWindowTitle("QSOCapture")
        self.setMinimumSize(900, 600)
        self.resize(1280, 800)

        self._icon_path = icon_path
        self._taskbar_hicon = 0
        if icon_path and os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.web_view = QWebEngineView()
        self.web_view.setUrl(QUrl(base_url))
        self.setCentralWidget(self.web_view)

        self.channel = QWebChannel()
        self.bridge = Bridge()
        self.channel.registerObject("bridge", self.bridge)
        self.web_view.page().setWebChannel(self.channel)

        self.tray_icon = QSystemTrayIcon(self)
        if icon_path and os.path.isfile(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            self.tray_icon.setIcon(self.windowIcon())

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
        self.tray_icon.setToolTip("QSOCapture")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            event.accept()
            return
        reply = QMessageBox.question(
            self, "Quit QSOCapture", "Are you sure you want to quit QSOCapture?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._closing = True
            event.accept()
        else:
            event.ignore()

    def changeEvent(self, event):
        try:
            if event.type() == QEvent.Type.WindowStateChange:
                if self.windowState() & Qt.WindowMinimized:
                    self.hide()
                    self.tray_icon.showMessage(
                        "QSOCapture", "Application minimized to system tray.\n"
                        "Click the icon to restore the window.",
                        QSystemTrayIcon.Information, 3000,
                    )
                    return
            super().changeEvent(event)
        except KeyboardInterrupt:
            pass

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()
            self.raise_()
            self.web_view.update()
            from PySide6.QtCore import QTimer
            QTimer.singleShot(50, self._fix_webengine_after_show)

    def _fix_webengine_after_show(self) -> None:
        """Force Qt WebEngine to repaint after window restore."""
        if not self.isVisible():
            return
        page = self.web_view.page()
        page.setVisible(False)
        page.setVisible(True)
        size = self.web_view.size()
        self.web_view.resize(size.width() + 1, size.height())
        self.web_view.resize(size)

    def showEvent(self, event):
        """Set taskbar icon via Win32 API once the window handle is valid."""
        super().showEvent(event)
        if self._icon_path and os.path.isfile(self._icon_path):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, lambda: self._set_taskbar_icon(self._icon_path))
            self._icon_path = None

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_visibility()

    def _set_taskbar_icon(self, icon_path: str) -> None:
        """Set the taskbar icon via Win32 API (Qt's setWindowIcon is unreliable on Windows)."""
        if sys.platform != "win32":
            return
        try:
            hicon = ctypes.windll.user32.LoadImageW(
                None, icon_path, 1, 0, 0, 0x10 | 0x40,
            )
            if not hicon:
                return
            hwnd = int(self.winId())
            # Destroy our previously loaded icon to avoid leaking GDI handles
            # across repeated show events.
            if self._taskbar_hicon:
                ctypes.windll.user32.DestroyIcon(self._taskbar_hicon)
            self._taskbar_hicon = hicon
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 0, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 1, hicon)
            ctypes.windll.user32.SendMessageW(hwnd, 0x80, 2, hicon)
            GWL_EXSTYLE = -20
            current_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, current_style | 0x40000)
        except Exception:
            pass

    def quit_application(self) -> None:
        self._closing = True
        QApplication.quit()


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)
                or "__compiled__" in globals()
                or hasattr(sys, "_MEIPASS"))


def _app_dir() -> str:
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "QSOCapture",
    )


ICON_BASENAME = "icon.ico"


def _bundle_dirs() -> list:
    """Candidate directories that may contain bundled data files."""
    dirs = []
    if hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    if getattr(sys, "executable", ""):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    if getattr(sys, "argv", None):
        dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    dirs.append(os.getcwd())
    unique = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in unique:
            unique.append(d)
    return unique


def _ensure_assets(app_dir: str) -> None:
    """Copy index.html and icons from bundle to writable app dir."""
    for basename in ("index.html", "icon.svg", ICON_BASENAME):
        target = os.path.join(app_dir, basename)
        for directory in _bundle_dirs():
            src = os.path.join(directory, basename)
            if os.path.isfile(src):
                try:
                    shutil.copyfile(src, target)
                except OSError as exc:
                    logger.debug("Could not copy asset %s -> %s: %s", src, target, exc)
                break


def _wait_for_server(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except KeyboardInterrupt:
            # A user abort during startup must not be reported as a crash; let
            # the installed excepthook record it as a clean interruption.
            logger.warning("Startup interrupted while waiting for the server")
            raise
        except Exception:
            time.sleep(0.05)
    return False


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _pick_port(host: str, preferred: int) -> int:
    if _port_available(host, preferred):
        return preferred
    for port in range(preferred + 1, min(preferred + 20, 65536)):
        if _port_available(host, port):
            logger.warning("Web port %d is busy; using %d", preferred, port)
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    logger.warning("Web port %d is busy; using ephemeral port %d", preferred, port)
    return port


def _legacy_dir() -> str:
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _migrate_legacy_data(app_dir: str) -> None:
    """Move user data from the executable directory into the per-user app_dir."""
    legacy_dir = _legacy_dir()
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
            logger.debug("Migrated legacy data: %s -> %s", src, dst)
        except OSError as exc:
            logger.warning("Could not migrate legacy data %s -> %s: %s", src, dst, exc)


def main() -> None:
    app_dir = _app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.chdir(app_dir)

    # Configure logging first so even a failure to import the app is recorded.
    logging_setup.setup_logging()
    logging_setup.install_excepthooks()

    try:
        import main as qso_main
    except Exception as exc:
        logger.critical("Failed to import the main application module", exc_info=True)
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

    qso_main.cfg.web_host = "127.0.0.1"
    qso_main.cfg.web_port = int(qso_main.cfg.web_port)

    icon_path = ""
    for cand in (
        os.path.join(app_dir, ICON_BASENAME),
        os.path.join(sys._MEIPASS, ICON_BASENAME) if hasattr(sys, "_MEIPASS") else "",
        *[os.path.join(d, ICON_BASENAME) for d in _bundle_dirs()],
    ):
        if cand and os.path.isfile(cand):
            icon_path = cand
            break

    app = QApplication(sys.argv)
    app.setApplicationName("QSOCapture")
    app.setOrganizationName("SQ3RX")
    if icon_path and os.path.isfile(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QSOCapture")
        except Exception:
            pass

    # Only one instance may record and write the database at a time.
    lock = QLockFile(os.path.join(app_dir, "QSOCapture.lock"))
    # A crashed instance leaves a lock file behind; allow Qt to reclaim it
    # after 30 seconds while still preventing a second live instance.
    lock.setStaleLockTime(30000)
    if not lock.tryLock(100):
        QMessageBox.information(
            None, "QSOCapture",
            "QSOCapture is already running.\n"
            "Use the existing window or tray icon.",
        )
        return

    port = _pick_port("127.0.0.1", qso_main.cfg.web_port)
    qso_main.cfg.web_port = port
    base_url = f"http://127.0.0.1:{port}/"

    config = uvicorn.Config(
        qso_main.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(base_url, timeout=10.0):
        server.should_exit = True
        logger.error("Local dashboard server failed to start on port %d", port)
        QMessageBox.critical(
            None, "QSOCapture",
            f"The local dashboard server failed to start on port {port}.\n"
            "See QSOCapture.log in the application data folder for details.",
        )
        return

    window = MainWindow(app_dir, base_url, icon_path)
    window.show()

    exit_code = app.exec()

    server.should_exit = True
    try:
        server_thread.join(timeout=2.0)
    except Exception:
        pass
    try:
        lock.unlock()
    except Exception:
        pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
