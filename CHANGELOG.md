# Changelog

All notable changes to QSOCapture are documented here. This file is written
in plain business language, without going into implementation details.

---

## Version 1.1.0

The headline improvements in this release are an easier Windows installation
and more complete logging of each contact (QSO).

### Windows installer and automated builds

- Added a **Windows installer** (Inno Setup). You can now download the ready
  to use `QSOCapture-setup-x.y.z.exe`, which installs the program into
  `Program Files`, creates Start Menu and desktop shortcuts, and adds an
  uninstall entry.
- The GitHub repository now **builds the release files automatically** whenever
  a new version (a tag starting with `v`) is published. Users do not have to
  compile anything — they just download the finished file from the *Releases*
  page.
- Added an **MIT license** to the project — the software is free to use,
  modify and share.

### Richer N1MM Logger+ data

- The program now captures **many more details** from every contact broadcast
  by N1MM Logger+: among others the WPX prefix, continent, multipliers,
  precedence, check, transmitter power, and the entry GUID.
- As a result, clicking a QSO in the dashboard shows the full, detailed
  contact information — which makes contest review and log verification much
  easier.

### Security

- The web dashboard binds to **localhost** (`127.0.0.1`) by default, so it is
  not reachable from outside the local network. Exposing it on the LAN
  requires a deliberate change in the configuration.

---

## Version 1.0.0

First public release of QSOCapture.

### What the program does

- **Automatic QSO recording** — listens for N1MM Logger+ messages and cuts a
  short audio slice (with a few seconds of lead and tail) for every logged
  contact.
- **Continuous recording** — the whole band can be recorded as time-sliced
  files, so no QSO is ever missed.
- **Two audio sources** — ExpertSDR via the TCI protocol, or any system
  soundcard input.
- **SO2R support** — in stereo the left channel is RX1 and the right channel
  is RX2; each is recorded and logged separately.
- **Web dashboard** — browse, filter and replay recordings from the browser,
  with adjustable playback speed.
- **Enhanced contest filtering** — you can type a fragment of the contest name
  (e.g. "CQWW") instead of picking from a list, making it easy to find
  recordings quickly.

### Desktop (EXE) build

- Added the ability to build a **standalone `QSOCapture.exe`** that launches
  the server and opens the dashboard in an embedded browser (WebView2), with
  an automatic fallback to the system browser if the WebView2 runtime is not
  available.