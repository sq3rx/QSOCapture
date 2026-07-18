# Changelog

All notable changes to QSOCapture are documented here. This file is written
in plain business language, without going into implementation details.

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