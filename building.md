# Building QSOCapture

> **Note:** For general usage instructions, see the main [README](README.md).

---

## Requirements

- Python **3.9+** (Windows 7+).
- The following Python packages (see `requirements.txt`):
  - `fastapi`, `uvicorn`
  - `numpy`
  - `sounddevice` (only needed for soundcard mode)
  - `websockets` (only needed for TCI mode)
  - `lameenc` (optional — only if you want MP3 output instead of WAV)
  - `PySide6` (needed for the desktop EXE / embedded Qt WebEngine browser — `qt_launcher.py`)

Install everything with:

```bash
pip install -r requirements.txt
```

## Desktop EXE & Windows Installer

The app can also be shipped as a single standalone ``QSOCapture-portable-x.y.z.exe`` that
launches the web server and opens the dashboard in an **embedded browser**
(PySide6 QWebEngineView / Qt WebEngine) — no external browser or Python install
required. Unlike the old pywebview-based build, this works on all Windows
versions (7+) with no external WebView2 or CEF dependency.

### Download ready-made builds

Go to the **[Releases](https://github.com/sq3rx/QSOCapture/releases)** page and
download either:

- **`QSOCapture-portable-x.y.z.exe`** — a portable, single-file executable
  (carries the version in its name). Just run it; no installation needed.
- **`QSOCapture-setup-x.y.z.exe`** — a Windows installer (Inno Setup) that
  places the app in `Program Files`, adds a Start Menu / desktop shortcut and
  an uninstall entry. Recommended for most users.

### Build it yourself (Windows)

```bash
pip install -r requirements.txt
pyinstaller build.spec
```

The result is ``dist/QSOCapture-portable-x.y.z.exe``. Double-click it and
the dashboard opens in its own window with the Qt WebEngine browser.

> **Where are my data stored?** The executable is installed in ``Program
> Files`` (read-only for normal users), but all your personal data —
> ``config.cfg``, ``qsos.db`` (QSO log) and the ``recordings/`` folder — live
> in ``%LOCALAPPDATA%\QSOCapture`` (e.g.
> ``C:\Users\<you>\AppData\Local\QSOCapture``). This means the app runs without
> administrator rights, and your recordings survive an uninstall. On first
> launch any data left behind in an older ``Program Files`` install is moved
> automatically into that folder.

To build the **Windows installer** (requires [Inno Setup](https://jrsoftware.org/isinfo.php)):

```bash
iscc installer.iss
```

This produces ``installer/QSOCapture-setup-<version>.exe``.

To run the desktop wrapper directly (without building):

```bash
python qt_launcher.py
```

> Note: Qt WebEngine is bundled with PySide6, so no additional runtime is
> required. The application window supports minimize-to-system-tray and
> native file dialogs (see [README](README.md#features) for details).
