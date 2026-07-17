# Changelog

All notable changes to QSOCapture are documented here. This file is written
in plain business language, without going into implementation details.

---

## Version 0.1.0beta

This is the first numbered pre-release. From this point the project follows a
clear `0.1.0beta` → `0.1.0` → `0.2.0` … versioning scheme.

### Runs in its own window (no external browser)
- The desktop app now opens the dashboard in a **native Edge WebView2 window**
  instead of silently launching the system browser. The previous build fell
  back to the default browser whenever the embedded WinForms/IE backend was
  unavailable; the window now uses the modern WebView2 engine that ships with
  Windows 10/11.
- The fallback to the system browser is kept only for the rare case where
  WebView2 is genuinely missing (e.g. very old Windows without the runtime).

### Branded favicon
- The dashboard tab now shows the QSOCapture logo consistently. A
  `/favicon.ico` route was added so browsers automatically pick up the icon
  (the same multi-size icon generated from `icon.svg`), in both the N1MM and
  Continuous views.

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