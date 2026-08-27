# Changelog

All notable changes to QSOCapture are documented here. This file is written
in plain business language, without going into implementation details.

## Version 0.8.0beta

### ✨ Improvements
* **Clearer "Resets and deletes" settings section:** Destructive buttons now show a tooltip describing exactly what they do before you click, and the date range for deleting continuous recordings is now clearly labelled.
* **Deleting a QSO in N1MM Logger+ now actually removes it:** Previously the database row and its audio recording were left in place because the delete packet was not processed correctly. Deleted QSOs are now properly removed from the dashboard.
* **Correct QSO times:** N1MM sends the QSO `<timestamp>` in UTC, but QSOCapture was interpreting it in your computer's local timezone, shifting every displayed QSO time by the UTC offset (up to 11 hours). Timestamps are now interpreted correctly as UTC, and existing QSOs are automatically migrated to the correct time on first start.
* **Dashboard always shows QSO times in UTC:** Recording playback and the QSO list used to display times in your local timezone, while the underlying data was UTC — so the two could disagree. The dashboard and file names now consistently use UTC, matching the time N1MM Logger+ actually logged, no matter what timezone your computer is set to.
* **Exchange column no longer shows a stale `0` for most contests:** For contests that do not use zones, N1MM sends `<zone>0</zone>`, which previously hijacked the Exch column and hid the real serial, section or exchange. The zone value `0` is now ignored, so the correct exchange (serial, section, etc.) is shown instead.
* **60 m QSOs now show the correct band:** QSOs made on the 60 m band (5.3 MHz) were previously labelled `5.3` instead of `60M`, sorting and filtering apart from every other band. 60 m is now recognized and displayed as `60M`.
* **Editing a QSO in N1MM Logger+ now updates the QSO and audio:** Editing a contact (contactreplace) previously had no effect when the packet did not wrap the details in a `<contactinfo>` element, so the callsign/band change never reached the dashboard and the audio file kept its old name. The packet is now processed regardless of its layout, updating the QSO in place and renaming the recording to match.
* **Hardened the delete/export endpoints:** Deleting or exporting a contest by name no longer accepts arbitrary paths — only a valid contest folder inside the recordings directory can be removed or exported, protecting against attempts to delete or pull files outside it.
* **"Max recordings (GB)" limit now actually persists:** The disk-space cap you set in Settings (oldest continuous chunks are removed when exceeded) was silently reset back to unlimited every time the app restarted. The value is now saved and remembered correctly.
* **Continuous recording can be turned on/off in Settings:** The master switch for continuous recording is now exposed in Settings (separate from its autostart). Turning it off stops writing continuous chunks — QSO slices are still recorded.
* **Safer Settings validation:** Saving Settings now politely rejects invalid audio configurations instead of silently applying them — for example, Dual-card SO2R without the second soundcard selected, an unrecognised audio mode, or a missing second device.
* **Removed the misleading "Sample width" setting:** Audio is always captured and stored as 16-bit, so the sample-width option (1/2/4 bytes) had no effect and confused users. It has been removed from the Settings window.

## Version 0.7.0beta

### 🚀 New Features
* **SO2R Dual Card mode:** Added a new "Dual card" option for SO2R setups. Instead of a single stereo soundcard (left channel → RX1, right channel → RX2), you can now use **two separate soundcards**, each capturing one receiver in mono. Configure it in Settings → Audio → SO2R mode toggle (Stereo / Dual card). When Dual card is enabled, two device selectors appear in Settings → Soundcard, letting you pick independent input devices for RX1 and RX2.

### 🎨 Dashboard
* **Readable contest names:** Contest names are now displayed in a human-friendly format (e.g. `2025 CQWW CW` instead of `20256_CQWW_CW`) in the filter dropdown, QSO table, QSO details and Settings. The filter still works with the raw database value internally, and manually typing a formatted name (e.g. `2025 CQWW CW`) is automatically resolved to the correct contest.
* **Clear buttons for Contest and Call/Prefix filters:** Added a small "×" button inside the Contest and Call/Prefix input fields that appears when the field has a value. Clicking it clears the filter and immediately reloads the QSO list.
* **Filter cleanup when switching tabs:** Switching between the N1MM QSOs and Continuous tabs now clears all remaining filters, so each view always starts from a clean, unfiltered state. In the Continuous tab, the irrelevant filters (Contest, Call, Band, Mode) are additionally disabled and greyed out — only RX and date range remain active.
* **Frequency formatting in QSO table:** The Freq column now converts kHz values to MHz (e.g. `14195` → `14.195`) and always displays at least three decimal places, preserving higher precision when available. Previously, whole-kilohertz values like `14195` were shown without any decimals.
* **Resets and deletes section:** Added a dedicated section in Settings with seven data-clearing options — reset config, clear QSO log, delete all data, delete contest (files + QSOs), delete contest recordings (files only), delete continuous by date range, and factory reset. Each operation shows a clear confirmation prompt.

### 📚 Documentation
* **User manual:** Added [MANUAL.md](MANUAL.md) — a complete user guide covering installation, configuration, all settings, SO2R setup, filtering, playback, data management, update checking and troubleshooting. The README now links to the manual instead of including the "How to use it" section inline.
* **Manual Quick Start:** Added a "Quick Start" section to [MANUAL.md](MANUAL.md) that walks new users from first launch to reviewing their recordings — configure settings, start the recorder, set up N1MM Logger+ broadcast data, filter with the TYPE box, play/save and locate the raw files.

### 🐛 Bug Fixes
* **QSO list not auto-refreshing during continuous recording:** When continuous recording was active, newly logged N1MM QSOs no longer appeared on the dashboard until the user clicked Refresh. Fixed the event-driven refresh mechanism so the list always updates immediately when a new QSO is saved, regardless of whether continuous recording is running.
* **Orphaned "Recording…" entries after unclean shutdown:** When the application was closed (or crashed) during continuous recording, a database row with `duration=0.0` remained, showing "Recording…" forever in the dashboard's Stop column. Continuous chunks are now finalised immediately on shutdown, and any leftover orphaned records are cleaned up automatically on the next startup.
* **Contact editing no longer removes audio file:** When editing a QSO in N1MM Logger+ (contactreplace), the program previously deleted the original recording and created a brand-new audio slice. Now the existing audio file is simply renamed to reflect the updated callsign/band, and the database record is updated in-place — the original recording is preserved.
* **Settings window closes after saving:** Clicking **Save & Apply** in Settings now closes the window automatically on success, so you can immediately return to the dashboard. If saving fails, the window stays open and shows the error so you can fix it. The Reset/Delete actions continue to keep the window open.

## Version 0.6.1beta

### 🐛 Bug Fixes
* **QSO recordings not saving in installed version:** Fixed a compatibility issue with the compiled application (Nuitka) that prevented QSO recordings from being saved after installation. The application now correctly records and saves QSO slices in both the installed and source versions.

## Version 0.6.0beta

> **Note:** ⚠️
> * It is recommended to **uninstall any previous version** before installing this release, as the build system has changed from PyInstaller to Nuitka.
> * Starting with Windows 7 and newer, there is now **a single unified release** package.
> * A **portable version will not be released**, as Windows Defender / Antivirus incorrectly flags single-file builds as a false positive.

### 🚀 Major Improvements & Architecture
* **Migrated from pywebview to PySide6 (Qt WebEngine):** Desktop launcher has been completely rewritten in PySide6. It provides a single build for Windows 7+, minimize-to-tray support, a confirm-close dialog, and native file dialogs without any external WebView2/CEF dependencies.
* **New Build System (Nuitka):** Replaced PyInstaller with **Nuitka** for desktop EXE compilation. The installer is now built using a directory layout (`onedir`).

### 🎨 Dashboard & UI Overhaul
* **Multi-Select Band Filter:** Replaced the plain-text input with a new dropdown featuring checkboxes for common ham bands (160M–2M), a custom band input field, and Apply/Clear buttons. Multiple bands can now be filtered simultaneously using `LIKE` pattern queries.
* **Date/Time Picker:** Added a calendar icon with an integrated `flatpickr` datetime picker. Supports both manual typing and interactive selection, with auto-close on click-outside.
* **Missing Audio Warning Badge:** QSO rows without an associated recording now display a prominent warning badge (⚠️ *no audio file*).
* **Settings UI Improvements:** Modernized "Continuous recording autostart" and "Normalize continuous recordings" options with clean ON/OFF toggle switches instead of standard checkboxes.
* **UI Polish & Quality of Life:** 
  * Improved status badge sizing, recording badge alignment, icons, and general layout.
  * Search/filter inputs now trigger on change instead of firing on every keystroke.

### ⚙️ Core & Storage Changes
* **Fixed Recordings Directory:** Removed the `recordings_dir` setting. Recordings are now strictly stored in `%LOCALAPPDATA%\QSOCapture\recordings` on Windows to eliminate folder clutter. The setting has been removed from the Settings panel.
* **Update Checker:** Added an automatic startup check against GitHub Releases. Displays a compact badge in the header and detailed status with download links in the *About* window.

### 🐛 Bug Fixes
* **Audio Slicing:** Fixed audio gaps/holes in QSO recordings after pause/resume cycles (removed unnecessary shared ring buffer clearing).
* **TCI Integration:** Removed `MUTE:false` and `MON_ENABLE:true` TCI commands that were overriding user radio configurations.
* **Band Filter Dropdown:** Fixed an issue where `className.includes()` incorrectly matched `overflow-hidden`, preventing the dropdown menu from closing and reopening.
* **Crash Fix:** Fixed a `TypeError` crash when rendering QSO list rows missing an audio file (`q.url` is `null`).
* **App Lifecycle:** Resolved `KeyboardInterrupt` exception thrown upon closing the application window.

### 📚 Documentation
* Overhauled `README.md` and relocated developer build instructions to `BUILDING.md`.
* Added a new logo image.

## Version 0.5.0beta

### Dashboard: SVG icon in header
- The dashboard header now shows the QSOCapture SVG icon next to the title,
  served via a dedicated `/icon.svg` route.

### Config cleanup
- Removed unused `default_contest` and `tci_version` settings. TCI address
  (`tci_host`/`tci_port`) moved to its own `[tci]` section in config.cfg.

### Date/time filter improvements
- Date/time fields now use `YYYY-MM-DD HH:mm` format with auto-formatting mask,
  consistent with table columns. Fixed `date_from=null` 422 error. RX filter
  moved to the second row for a cleaner layout.

### Performance
- Contest list fetched from DB instead of filesystem. QSO query merged into
  single SQL pass (`COUNT(*) OVER()`). Contest filter uses exact index-backed
  match. Startup no longer scans recordings if DB has records. SQLite
  connections cached per thread.

### Continuous queue monitoring
- Queue size increased to 1800. Dashboard RX badges show queue fill and drop
  count with colour coding. New `continuous_dropped` SSE event.

### Bug fixes
- RX filter broke pagination (applied after `LIMIT/OFFSET`) — fixed by adding
  `rx` DB column. Contest autocomplete dropdown now works. Contest filter no
  longer clears on partial input. Clearing Contest field reloads all QSOs.
  Contest list was always empty (`_` wildcard in SQLite) — fixed.

### Normalisation of continuous recordings now optional
- New toggle in Settings to disable loudness normalisation for continuous
  chunks (QSO slices are always normalised).

---

## Version 0.4.0beta

### Fix: crash on Windows 7/8 (APPCRASH in libcef.dll)
- On Windows 7/8 the app uses the CEF (Chromium Embedded Framework) backend
  because Edge WebView2 does not exist there. CEF was started with its default
  settings, whose sandbox (GPU / zygote subprocesses) crashes with an
  Access Violation inside `libcef.dll` (APPCRASH, exception `c0000005`).
- The launcher now initialises CEF explicitly before opening the window, with
  safe flags for legacy Windows: `--no-sandbox`, `--disable-gpu`,
  `--disable-gpu-sandbox`, `--disable-software-rasterizer`, and a writable
  `cache_path` in `%LOCALAPPDATA%\QSOCapture\cef_cache` (instead of next to the
  EXE inside read-only `Program Files`).
- If CEF still fails to initialise, the failure is logged to `cef_debug.log`
  and the app falls back to another browser backend instead of crashing.
- Fixed an `AttributeError: module 'cefpython3' has no attribute 'Initialized'`
  on the legacy (CEF 66) wheel, where `cef.Initialized` does not exist. The
  launcher now guards the call with `getattr` and only skips re-init when the
  function is present and returns True.

### CI / build
- The GitHub Actions pipeline can now be triggered manually from the Actions
  tab (`workflow_dispatch`), so the EXE files can be built and downloaded as
  artifacts without publishing a GitHub Release.
- The Release job now runs only when a `v*` tag is pushed; manual runs build
  both the modern and Windows 7/8 packages but create no release.

### Fix: corrupted audio when recording a single stereo channel (SO2R)
- When recording only one side of a stereo signal (for example the left or
  right channel in SO2R, or a single receiver pulled out of a stereo stream),
  the saved audio could come out **wrong / garbled** because the program wrote
  the raw bytes straight from a non-contiguous in-memory slice of the stereo
  buffer.
- The recorder now makes sure every channel it writes (to WAV, MP3, and the
  continuous-recording buffer) is laid out as a plain, contiguous block of
  samples before writing it, so the recorded file always matches what was
  actually received — for both N1MM QSO slices and continuous recordings.

### Fix: SO2R QSO always recorded on RX2 regardless of the radio used
- In SO2R (two receivers, RX1 = radio 1, RX2 = radio 2) every logged QSO was
  saved **only from the RX2 channel**, no matter which radio actually made the
  contact.
- The cause was that the slicer created one database row per receiver using the
  same N1MM contact GUID. The unique index on that GUID then kept only the
  last-written row (RX2), so the dashboard and the audio file always pointed
  at RX2.
- The recorder now reads the **`<RadioNr>`** field from the N1MM contact and
  slices **only the matching receiver's** buffer (RX1 for radio 1, RX2 for
  radio 2). Each QSO produces a single file and a single database row, so the
  recording now reflects the radio that was actually used. Continuous recording
  still captures both receivers independently as before.

### Consistent audio loudness (automatic gain normalisation)
- Saved recordings (both N1MM QSO slices and continuous chunks) are now
  normalised to a **consistent perceived loudness** so you no longer jump
  between very quiet and very loud files.
- The normaliser targets a fixed RMS level, but **caps the applied gain** so it
  never amplifies near-silent / noisy audio into a roar, and **hard-clips the
  peaks** just below full scale to avoid distortion. This applies to WAV and
  MP3 output alike.

---

## Version 0.3.0beta

This release makes the recorder **safer to use in TCI mode** and adds
**on-demand debug logging** so connection problems can be diagnosed without
restarting the app.

### Blocked recording when TCI is not connected
- Previously the dashboard let you **start continuous recording even when the
  radio was not linked** over TCI (ExpertSDR). This produced silent gap files
  (empty chunks) that cluttered the Continuous view.
- The continuous recorder now **refuses to start** while the TCI connection is
  down, on three levels:
  - the backend audio loop does not open a chunk file until the radio is
    actually linked,
  - the `/api/continuous/resume` API returns **HTTP 409** when TCI is not
    connected,
  - the dashboard **Start recording** button is **disabled and greyed out**
    (with the tooltip *"Connect TCI (ExpertSDR) before starting recording"*)
    whenever TCI mode is active and the radio is offline.
- In **soundcard** mode this guard never triggers — the input device is always
  treated as ready, so recording works exactly as before.
- The TCI connection flag is also reported correctly in both the status API and
  the dashboard badge (it was previously showing "connected" even in soundcard
  mode).

### Live debug logging (no restart needed)
- The **Live Log** modal now has a **Debug** checkbox. Ticking it enables
  `DEBUG`-level logging on the server **at runtime** — no config edit, no
  restart.
- With debug on, the log shows the **full TCI exchange** (every command sent
  to ExpertSDR and every status/keepalive message received), QSO slice windows,
  buffer-fill levels and a 5-second **heartbeat** of the current state, so a
  missing-audio problem can be traced live.
- The in-memory log buffer now **always stores DEBUG lines**; the dashboard only
  shows them when you ask for them, so normal operation stays quiet while the
  detailed trail is available on demand.

### Dashboard / API
- N1MM contact and delete packets are now logged at DEBUG level for easier
  troubleshooting.

### Dashboard polish (UI)
- The **installer** no longer ships a Polish language file — the setup is
  English only (all installer strings and comments are now in English).
- The dashboard header gained a **"?" (About)** button that opens a small
  modal with the application description, the running **version** and a link to
  the project on GitHub (`https://github.com/sq3rx/QSOCapture`).
- The **Settings** modal no longer shows the version / GitHub icon in its
  header — the version and project link now live only in the About modal.
- Fixed the dashboard tables looking **collapsed to the left** when the
  database is empty: both the N1MM and Continuous tables now use fixed column
  widths (via `table-fixed` + `<colgroup>`), so column proportions stay
  consistent whether the log is empty or full. The empty-list placeholder is
  rendered as a full-width row inside the table (not a separate paragraph).
- Fixed **duplicate "No QSOs / No continuous recordings yet" rows** appearing
  every time the **Refresh** button or any filter was changed. The refresh
  handlers were passing the DOM event object as the `append` flag, which made
  the list append a new empty-state row instead of rebuilding from scratch;
  they now always rebuild the first page cleanly.

---

## Version 0.2.1beta

This release fixes the **"missing Python DLL" error on Windows 7/8** reported by
users and modernises the build toolchain.

### Fix: application did not start on Windows 7/8 (silent crash)
- The legacy build (Python 3.8 + CEF) failed to import ``main`` because the
  daemon thread pool used a worker-thread API that only exists on **Python 3.9+**
  (``_create_worker_context()`` and the 3-argument ``_worker``). On Python 3.8
  this raised ``AttributeError`` at import time, and because the frozen
  ``console=False`` executable discards stderr, the failure was **completely
  silent** — the app simply did nothing.
- ``main.py`` now branches on the Python version: the modern 3.9+ path keeps the
  new worker context; the legacy 3.8 path uses the 2-argument worker, so the
  import succeeds and the app starts.
- The launcher now wraps ``import main`` in a ``try/except`` and writes the full
  traceback to ``%LOCALAPPDATA%\QSOCapture\launcher_error.log`` if import ever
  fails again (this applies to **all** Windows versions, not just Win7/8), so a
  future failure is no longer invisible.

### Portable EXE filenames now include the version
- The standalone portable executables are renamed to make the download
  unambiguous and avoid collisions in a GitHub Release:
  - **Modern (Windows 10/11):** ``QSOCapture-portable-<version>.exe``
    (e.g. ``QSOCapture-portable-0.2.1beta.exe``)
  - **Legacy (Windows 7/8):** ``QSOCapture-portable-Win7-<version>.exe``
    (e.g. ``QSOCapture-portable-Win7-0.2.1beta.exe``)
- The installers keep their existing names (``QSOCapture-setup-<version>.exe``
  and ``QSOCapture-Win7-setup-<version>.exe``).

### Windows 7 / 8 support (legacy build)
- The standard build now targets **Python 3.14** (Windows 10/11) and uses the
  native Edge WebView2 backend. Python 3.9+ and WebView2 do not run on Windows
  7/8, which caused the "system asks for Python dll" failure on those systems.
- A separate **legacy build** is now produced automatically for Windows 7/8.
  It is built with **Python 3.8** and bundles the **CEF** (Chromium Embedded
  Framework) engine (`cefpython3`), so it needs no external browser or runtime.
  Published as `QSOCapture-Win7-setup-x.y.z.exe` (and a portable
  `QSOCapture-Win7.exe`) in the same GitHub Release. The portable legacy
  executable is renamed to avoid clashing with the modern `QSOCapture.exe`.
- The launcher now detects the Windows version (`sys.getwindowsversion()`):
  on Windows 7/8 it uses the CEF backend directly (skipping Edge WebView2,
  which does not exist there); on Windows 10/11 it keeps the Edge-first order.

### Build / CI
- `.github/workflows/main.yml` now builds **two** artifacts in parallel:
  modern (Python 3.14, Edge) and legacy (Python 3.8 + CEF). Both are attached
  to the release.
- `build.spec` gained a `BUNDLE_CEF=1` switch that forces CEF to be bundled
  and **fails the legacy build** if `cefpython3` is not importable — so a
  legacy EXE is never shipped without a working embedded browser.
- `installer.iss` supports a `MyAppNameSuffix` (CI passes `-Win7`) so the
  legacy installer gets a distinct filename and does not collide with the
  modern one. It also accepts `MyExeName` / `MyAppExeName` so the installer
  bundles the correct `QSOCapture-Win7.exe` produced by the legacy PyInstaller
  build.

### Documentation
- README badge updated to Python 3.14+, Requirements note and a new
  "Windows 7 / 8 (legacy build)" section explain which download to use.

---

## Version 0.2.0beta

This release focuses on the **audio player** in the dashboard and a few
dashboard clean-ups, based on hands-on testing during contests.

### Custom audio player (replaces the native browser controls)
- The inline player no longer uses the browser's built-in `<audio controls>`.
  Instead it is a **custom player** with a play/pause button, a **progress
  slider that fills the whole row width**, and a current/total time readout.
- This removes the native **mute button** and the **"…" overflow menu**
  (Download / Playback Rate / Mute) entirely — those options now live only in
  the dashboard controls, so they are never shown twice.
- The custom slider also fixes the empty grey gap that the native control left
  behind after hiding the mute button — the progress bar now uses 100% of the
  available width.

### Playback speed up to 2× and a Save button
- The **playback-speed selector** now offers **0.8×, 1.0×, 1.2×, 1.5× and 2.0×**
  (previously it stopped at 1.5×).
- A **Save** button (⭳) was added next to the player so a recording can be
  downloaded / saved to a file directly from the dashboard row.

### Cleaner dashboard
- The **DXCC** column was removed from the main N1MM QSOs view — it was
  redundant on the dashboard (the full country / WPX prefix is still available
  in the QSO detail panel).
- The **Band** column now correctly shows the band (e.g. `20M`) even when N1MM
  stores the value as a frequency (e.g. `14.200`); the value is snapped to the
  nearest amateur band automatically.

### Bug fix (update reliability)
- Fixed the dashboard not updating after an upgrade. The launcher copied
  `index.html` into `%LOCALAPPDATA%\QSOCapture` only when the file was missing,
  so an **old `index.html` left by a previous version kept being served**
  forever (the UI is read from that file). The launcher now **overwrites**
  `index.html` (and `icon.ico`) from the bundled copy on **every launch**, so
  installing a newer build immediately shows the new interface.

---

## Version 0.1.0beta

This is the first numbered pre-release. From this point the project follows a
clear `0.1.0beta` → `0.1.0` → `0.2.0` … versioning scheme.

### Runs in its own window (no external browser)
- The desktop app opens the dashboard in a **native window** instead of
  silently launching the system browser.
- The **Edge WebView2** backend (`edgechromium`) is used by default. It ships
  with Windows 10/11 and needs **no extra install**, so the app reliably opens
  in its own window on a normal Windows machine.
- The CEF (Chromium Embedded Framework) backend is only used as an **optional**
  fallback when `cefpython3` is actually installed (its wheels are not
  available for every Python version). When CEF is absent the app still opens
  in the native WebView2 window.
- The fallback to the system browser is kept **only** as a genuine last resort
  if every embedded engine fails to initialise.

### Branded favicon
- The dashboard tab now shows the QSOCapture logo consistently. A
  `/favicon.ico` route was added so browsers automatically pick up the icon
  (the same multi-size icon generated from `icon.svg`), in both the N1MM and
  Continuous views.

### Native window priority
- The launcher now tries backends in a strict order: **Edge WebView2 → CEF (if
  installed) → default → system browser**. This guarantees the app starts in a
  dedicated window on modern Windows rather than a browser tab, and the build
  continues to work even when the heavy CEF package is not installed.

### Consistent sort indicators
- The sort arrows in the **N1MM QSOs** and **Continuous** table headers now use
  the same highlight style. Previously the active sort column was only
  highlighted in one view, making the two tabs look inconsistent.

### Earlier development history
- All work done before this numbering started (initial release, Windows
  installer, richer N1MM data, no-admin data storage) is summarised under
  **Version 0.1.0alpha** below.

---

## Version 0.1.0alpha

Pre-numbering development phase — the foundation of QSOCapture, covering
everything shipped before the `0.1.0beta` numbering began.

### What the program does
- **Automatic QSO recording** — listens for N1MM Logger+ messages and cuts a
  short audio slice (with a few seconds of lead and tail) for every logged
  contact.
- **Continuous recording** — the whole band can be recorded as time-sliced
  files, so no QSO is ever missed.
- **Two audio sources** — ExpertSDR via the TCI protocol, or any system
  soundcard input.
- **SO2R support** — in stereo the left channel is RX1 and the right channel is
  RX2; each is recorded and logged separately.
- **Web dashboard** — browse, filter and replay recordings from the browser,
  with adjustable playback speed.
- **Enhanced contest filtering** — you can type a fragment of the contest name
  (e.g. "CQWW") instead of picking from a list.

### Windows installer and automated builds
- Added a **Windows installer** (Inno Setup). You can download the ready-to-use
  `QSOCapture-setup-x.y.z.exe`, which installs the program into `Program Files`,
  creates Start Menu and desktop shortcuts, and adds an uninstall entry.
- The GitHub repository **builds the release files automatically** whenever a
  new version (a tag starting with `v`) is published. Users do not have to
  compile anything — they just download the finished file from the *Releases*
  page.
- Added an **MIT license** — the software is free to use, modify and share.

### Richer N1MM Logger+ data
- The program captures **many more details** from every contact broadcast by
  N1MM Logger+: among others the WPX prefix, continent, multipliers,
  precedence, check, transmitter power, and the entry GUID.
- Clicking a QSO in the dashboard shows the full, detailed contact information
  — which makes contest review and log verification much easier.

### No more "Run as administrator" needed
- User data (`config.cfg`, the `qsos.db` log database and the `recordings/`
  folder) now lives in **`%LOCALAPPDATA%\QSOCapture`**, which is always writable
  without admin rights. The program launches normally for any user.
- On first launch, any data left behind in an older `Program Files` install is
  **moved automatically** into the new folder, so previously recorded QSOs are
  preserved.

### Security
- The web dashboard binds to **localhost** (`127.0.0.1`) by default, so it is
  not reachable from outside the local network. Exposing it on the LAN requires
  a deliberate change in the configuration.

### Desktop (EXE) build
- Added the ability to build a **standalone `QSOCapture.exe`** that launches the
  server and opens the dashboard in an embedded browser (WebView2), with an
  automatic fallback to the system browser if the WebView2 runtime is not
  available.