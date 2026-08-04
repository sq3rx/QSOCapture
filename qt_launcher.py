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

import os
import sys
import time
import threading
import shutil
import urllib.request
import logging

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
            if not rel or rel.startswith("..") or os.path.isabs(rel):
                return json.dumps({"ok": False, "error": "invalid path"})
            src = os.path.join(self.recordings_dir, rel)
            if not os.path.isfile(src):
                return json.dumps({"ok": False, "error": "recording not found"})

            from PySide6.QtWidgets import QFileDialog
            from PySide6.QtCore import QDir

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
                    event.ignore()
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
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _app_dir() -> str:
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "QSOCapture",
    )


ICON_BASENAME = "icon.ico"


def _ensure_assets(app_dir: str) -> None:
    """Copy index.html and icons from bundle to writable app dir."""
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
    """Move user data from the executable directory into the per-user app_dir."""
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


def main() -> None:
    app_dir = _app_dir()
    os.makedirs(app_dir, exist_ok=True)
    os.chdir(app_dir)

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

    qso_main.cfg.web_host = "127.0.0.1"
    qso_main.cfg.web_port = int(qso_main.cfg.web_port)

    port = qso_main.cfg.web_port
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

    icon_path = ""
    for cand in (
        os.path.join(app_dir, ICON_BASENAME),
        os.path.join(sys._MEIPASS, ICON_BASENAME) if _is_frozen() else "",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ICON_BASENAME),
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

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(base_url, timeout=10.0):
        print("ERROR: QSOCapture server failed to start.", file=sys.stderr)
        return

    window = MainWindow(app_dir, base_url, icon_path)
    window.show()

    exit_code = app.exec()

    server.should_exit = True
    try:
        server_thread.join(timeout=2.0)
    except Exception:
        pass

    sys.exit(exit_code)


if __name__ == "__main__":
    main()