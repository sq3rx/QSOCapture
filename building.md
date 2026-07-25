# Building QSOCapture

> **Note:** For general usage instructions, see the main [README](README.md).

---

## Requirements

- Python **3.9+** (Windows 10/11) for the standard build.
- The following Python packages (see `requirements.txt`):
  - `fastapi`, `uvicorn`
  - `numpy`
  - `sounddevice` (only needed for soundcard mode)
  - `websockets` (only needed for TCI mode)
  - `lameenc` (optional — only if you want MP3 output instead of WAV)
  - `pywebview` (needed for the desktop EXE / embedded WebView2 launcher — `launcher.py`)

Install everything with:

```bash
pip install -r requirements.txt
```

## Desktop EXE & Windows Installer

The app can also be shipped as a single standalone ``QSOCapture-portable-x.y.z.exe`` that
launches the web server and opens the dashboard in an **embedded browser**
(Edge WebView2 on Windows) — no external browser or Python install required.

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
the dashboard opens in its own window. If the embedded WebView2 engine is
missing, the app automatically falls back to opening your default system
browser.

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
python launcher.py
```

> Note: the WebView2 runtime ships with modern Windows 10/11. On older systems
> install it from Microsoft, otherwise the system-browser fallback is used.

### Windows 7 / 8 (legacy build)

**Windows 7 and 8 are NOT supported by the standard build.** Python 3.9+ cannot
run on those systems, and the Edge WebView2 engine does not exist there. For
Windows 7/8 download the separate **`QSOCapture-Win7-setup-x.y.z.exe`** (or the
portable **`QSOCapture-portable-Win7-x.y.z.exe`** from the legacy release). That
build is produced with **Python 3.8 + CEF** (`cefpython3`), which is bundled
into the EXE, so no external browser or runtime is required. The launcher
automatically detects Windows 7/8 and uses the CEF backend instead of Edge
WebView2. The portable legacy executable is named
`QSOCapture-portable-Win7-x.y.z.exe` to avoid clashing with the modern
`QSOCapture-portable-x.y.z.exe`.