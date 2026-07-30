# Building QSOCapture

> **Note:** For general usage instructions, see the main [README](README.md).

---

## Requirements

- Python **3.13** (Windows 7+). Nuitka works best with Python 3.13.
- A C compiler (MSVC or MinGW). On Windows with Python 3.13, MSVC is included
  in the Python installer (Build Tools for Visual Studio).
- The following Python packages (see `requirements.txt`):
  - `fastapi`, `uvicorn`
  - `numpy`
  - `sounddevice` (only needed for soundcard mode)
  - `websockets` (only needed for TCI mode)
  - `lameenc` (optional — only if you want MP3 output instead of WAV)
  - `PySide6` (needed for the desktop EXE / embedded Qt WebEngine browser — `qt_launcher.py`)
  - `nuitka` (compiler for building the standalone EXE)

Install everything with:

```bash
pip install -r requirements.txt
```

## Desktop EXE & Windows Installer

The app can be shipped as a **portable executable** or a **Windows installer**,
both using an **embedded browser** (PySide6 QWebEngineView / Qt WebEngine) —
no external browser or Python install required. Unlike the old pywebview-based
build, this works on all Windows versions (7+) with no external WebView2 or CEF
dependency.

### Download ready-made builds

Go to the **[Releases](https://github.com/sq3rx/QSOCapture/releases)** page and
download either:

- **`QSOCapture-portable-x.y.z.exe`** — a single-file portable executable.
  Just run it; no installation needed.
- **`QSOCapture-setup-x.y.z.exe`** — a Windows installer (Inno Setup) that
  places the app in `Program Files`, adds a Start Menu / desktop shortcut and
  an uninstall entry. Recommended for most users.

### Build it yourself (Windows)

```bash
pip install -r requirements.txt

# Portable single-file EXE
python build_nuitka.py --onefile
# Result: QSOCapture-portable-x.y.z.exe

# Installer source (folder, used by Inno Setup)
python build_nuitka.py
# Result: qt_launcher.dist/ (folder with EXE + DLLs)
```

> **Note:** The first Nuitka build can take a while (15–30 minutes) because it
> compiles all Python code to C++. Subsequent builds are faster thanks to
> caching.

> **Where are my data stored?** The executable is installed in ``Program
> Files`` (read-only for normal users), but all your personal data —
> ``config.cfg``, ``qsos.db`` (QSO log) and the ``recordings/`` folder — live
> in ``%LOCALAPPDATA%\QSOCapture`` (e.g.
> ``C:\Users\<you>\AppData\Local\QSOCapture``). This means the app runs without
> administrator rights, and your recordings survive an uninstall. On first
> launch any data left behind in an older ``Program Files`` install is moved
> automatically into that folder.

### Antivirus false positives

Windows Defender and other antivirus engines may occasionally flag the
portable executable (`QSOCapture-portable-*.exe`) as a threat. This is a
**false positive** — the executable is a legitimate amateur radio application
built with [Nuitka](https://nuitka.net), which compiles Python code into a
standalone Windows binary. The heuristic detection is triggered by the
combination of a large, self-contained executable with an embedded Python
interpreter, not by any malicious behaviour.

The following build flags have been added to `build_nuitka.py` to reduce the
likelihood of false positives:

- `--lto=yes` — link-time optimisation produces smaller, more optimised
  binaries that are less likely to trigger heuristic detection.
- `--windows-uac-uiaccess` — includes a proper UAC manifest so Windows
  recognises the application as a well-behaved desktop app.
- `--disable-ccache` — ensures fully reproducible builds (ccache can
  sometimes produce non-deterministic output that looks suspicious).
- `--python-flag=-OO` — strips docstrings to reduce binary size (smaller
  EXE is less likely to trigger heuristics).
- `--remove-output` — removes temporary build artefacts after compilation.
- `--windows-company-name=SQ3RX` / `--windows-product-name=QSOCapture` /
  `--windows-file-description=Amateur Radio Contest Audio Recorder` —
  embeds proper Windows VERSIONINFO metadata so the EXE is recognised as
  a legitimate desktop application rather than an unknown binary.

If the portable EXE is still flagged, consider:

1. **Using the installer** (`QSOCapture-setup-*.exe`) instead — it is a
   standard Inno Setup installer and far less likely to be detected.
2. **Adding an exclusion** in Windows Security for the downloaded file or
   folder.
3. **Reporting the false positive** to Microsoft at
   [Microsoft Security Intelligence](https://www.microsoft.com/en-us/wdsi/filesubmission).
4. **Building from source** — a locally built binary will not be flagged.

---

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