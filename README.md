# QSOCapture by SQ3RX

<p align="center">
  <img src="logo_with_text.png" alt="QSOCapture logo" width="600" />
</p>

[![Build and Release](https://github.com/sq3rx/QSOCapture/actions/workflows/main.yml/badge.svg)](https://github.com/sq3rx/QSOCapture/actions/workflows/main.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org)

**Version:** 0.5.0beta

**QSOCapture** is a lightweight contest audio recorder and log player for
amateur radio operators. It captures audio from your receiver (via the
**TCI** protocol from ExpertSDR or a regular **soundcard** input), slices out
each QSO the moment **N1MM Logger+** logs it, and presents everything in a
clean, color-coded web dashboard.

---

## Table of Contents

- [Features](#features)
- [Building](building.md)
- [How to use it](#how-to-use-it)
- [Configuration](#configuration)
 - [Using the dashboard](#using-the-dashboard)
 - [Screenshots](#screenshots)
 - [Project layout](#project-layout)
 - [API reference](#api-reference)
 - [Troubleshooting](#troubleshooting)
 - [Changelog](#changelog)
 - [Development approach](#development-approach)
 - [License](#license)

---

## Features

 - **Automatic QSO slicing** — listens to N1MM Logger+ UDP broadcasts and
   records a few seconds before/after each contact (pre-roll / post-roll).
 - **Continuous recording** — records the whole band into time-sliced chunks
   so no QSO is ever missed. Controlled by the **Continuous recording
   autostart** setting and the dashboard **Start/Stop recording** button.
 - **Two audio sources** — ExpertSDR via the TCI WebSocket protocol, or any
   system soundcard input device.
 - **WAV / MP3 output** — recordings are saved as lossless WAV by default;
   optionally switch to MP3 (via `lameenc`) for smaller files.
 - **SO2R ready** — in stereo (`channels = 2`) the left channel is recorded as
   **RX1** and the right channel as **RX2**, each into its own buffer and its
   own audio file. In mono (SO1R) only RX1 is used.
 - **Per-RX buffer badges** — the dashboard header shows a live buffer-fill
   badge for every receiver (`RX1 30s`, `RX2 30s` in SO2R) so you always know
   how much audio is buffered.
 - **Per-RX logging** — pause/resume/start messages and QSO slices are logged
   and stored separately per receiver (`[RX1]`, `[RX2]`).
 - **Web dashboard** — browse, filter and replay recordings from the local
   machine (binds to 127.0.0.1 by default; change `web_host` to `0.0.0.0` in
   settings only if you intentionally want to expose it on the network).
 - **Rich filtering** — by contest, callsign / prefix (supports regex), band,
   mode, **RX (All / RX1 / RX2)**, and an exact date/time range.
 - **Separate views** — N1MM QSOs and Continuous recordings are kept apart,
   each with the columns that matter.
 - **Color-coded log** — the live application log and the QSO table use
   friendly color badges (mode, band, RX) for at-a-glance readability.
 - **Factory reset** — one click to wipe the log, recordings and settings
   (restore defaults).

---

## Building

For system requirements and instructions on building the standalone EXE or
Windows installer, see [building.md](building.md).

---

## How to use it

Assuming **QSOCapture is already installed** on your machine (desktop EXE /
installer, or a checked-out source folder with dependencies present), here is
how to get from zero to your first replayed QSO.

> **Tip:** the shipped default values are sensible for most setups — you can
> usually leave everything at its default and only tweak what is specific to
> your station.

### 1. Launch the app

 - **Desktop build:** double-click **QSOCapture** from the Start Menu or
   desktop shortcut (or run `QSOCapture-portable-x.y.z.exe`). The dashboard
   opens in the embedded browser.
 - **From source:** from the project folder run `python main.py`.

### 2. Open the dashboard

The dashboard opens automatically in the embedded browser. If the embedded
browser fails to initialise, QSOCapture falls back to your default system
browser and opens the dashboard there.

### 3. Configure your audio source and N1MM

 1. Click **⚙ Settings** in the dashboard header.
 2. Set `audio_mode` to `tci` (ExpertSDR) or `soundcard`.
    - **TCI:** set `tci_host` / `tci_port` to match ExpertSDR's TCI server
      (default `127.0.0.1:50001`, enable it in ExpertSDR options).
    - **Soundcard:** type a `soundcard_device` substring that matches your
      receiver input.
 3. Set `channels` to **1 for SO1R (mono)** or **2 for SO2R (stereo)** —
    in SO2R the left channel is recorded as RX1 and the right as RX2.
 4. Under the **N1MM** section, confirm `n1mm_udp_port` matches the UDP port
    N1MM Logger+ broadcasts on (default `12060`). This is the only N1MM
    setting you normally need, and the default is fine in most cases.
 5. Click **Save** — the services restart automatically with the new
    settings. A green buffer badge (`RX1 30s` / `RX2 30s`) in the header
    confirms audio is flowing.

> **Tip:** from the **⚙ Settings** panel you can also open the
> **recordings directory** directly to browse the raw audio files on disk
> without leaving the app.

### 4. Start recording

 - Click **▶ Start recording**, or enable **Continuous recording autostart**
   so it begins on launch. The header badge fills as audio is buffered.

### 5. Log a contact in N1MM Logger+

Log a QSO in **N1MM Logger+** as usual. The moment it is logged, QSOCapture
slices the surrounding audio and the row appears in the **N1MM QSOs** view
with a player ready to use.

### 6. Replay and analyse

 - Switch between **N1MM QSOs** and **Continuous** recordings.
 - Use the filters (contest, call/prefix with regex, band, mode, RX,
   date/time) to find the contact.
 - Play the slice and use the **playback-speed selector** (0.8×–2.0×) to
   study it; click **Save** to download the file.
 - Open the **📜 Log** button for a color-coded live view of what the app
   is doing.

### 7. Stop when done

Click **⏹ Stop recording** to finalise the current chunk. Your QSO log and
recordings live in `%LOCALAPPDATA%\QSOCapture` (desktop) or the project
folder (source) and survive restarts.

---

## Configuration

All settings live in `config.cfg` (an INI file) and can also be changed live
from the web dashboard (**⚙ Settings**). Every field shows a **?** tooltip
with an explanation. The most important options:

| Section | Setting | Description |
| ------- | ------- | ----------- |
| general | `station_name` | Your station callsign / label shown in the header. |
| general | `recordings_dir` | Where audio files are stored. |
| general | `continuous_recording` | ON = the continuous-recording feature is available. |
| general | `continuous_autostart` | ON = start continuous recording automatically on launch. **OFF by default** — use the dashboard button to start it on demand. |
| general | `continuous_chunk_minutes` | Length of each continuous chunk. |
| general | `max_recordings_gb` | Disk cap for the `recordings/` folder in GB. `0` = unlimited; when exceeded, the oldest **continuous** chunks are pruned automatically (N1MM QSO slices are preserved). |
| tci | `tci_host` / `tci_port` | ExpertSDR TCI server address (default `127.0.0.1:50001`). |
| audio | `audio_mode` | `tci` (ExpertSDR) or `soundcard`. |
| audio | `sample_width` | Bytes per sample in the saved file (default `2` = 16-bit, standard for WAV). |
| audio | `audio_format` | `wav` (lossless) or `mp3` (smaller files). |
| audio | `sample_rate` | Must match your radio / TCI / soundcard (48000 typical). |
| audio | `channels` | 1 = SO1R (mono), 2 = SO2R (stereo). |
| audio | `pre_roll` | Seconds of audio kept **before** the QSO timestamp. |
| audio | `post_roll` | Seconds waited **after** the N1MM packet before slicing. |
| audio | `tci_receiver` | Which ExpertSDR receiver to record (0 = main RX). |
| audio | `soundcard_device` | Substring of the input device name (empty = default). |
| n1mm | `n1mm_udp_port` | UDP port N1MM broadcasts on (default `12060`). |
| n1mm | `n1mm_bind_ip` | Interface to listen on (`0.0.0.0` = all). |
| web | `web_host` / `web_port` | Dashboard bind address (default `127.0.0.1:8000` — local only; set `0.0.0.0` to expose on the LAN). |

When N1MM logs a contact, QSOCapture waits `post_roll` seconds (so the tail of
the QSO is captured), then cuts a slice from the circular audio buffer that
starts `pre_roll` seconds before the contact time. The result is a short,
self-contained audio file named like:

```
2026-07-13_2120_SQ3RX_20M_RX1.wav
```

---

## Using the dashboard

### Filters

 - **Contest** — pick a specific contest folder.
 - **Call / Prefix** — free-text search. Enter a plain fragment
   (`SQ3RX`), a country prefix (`SQ`), or a **regular expression**
   (`^SQ`, `3[A-Z]X$`, `SQ|SP`). Invalid regex is treated as a literal
   substring.
 - **Band / Mode** — exact match (e.g. `20M`, `CW`).
 - **RX** — filter by receiver: **All RX**, **RX1** or **RX2**. 
   Applies to both the QSO and Continuous views.
 - **Date/time from – to** — filter by an exact moment, not just a day.
 - **Type** — switch between **N1MM QSOs** and **Continuous** recordings.

### Recording control

The **⏹ Stop recording / ▶ Start recording** button starts or stops
continuous recording at any time. It is always visible — even if
`continuous_autostart` is OFF, you can begin recording whenever you need it.
Stopping finalises the current chunk into a complete, playable file.

### N1MM QSOs view

Shows `Timestamp · Call · Band · Mode · Freq · Exch · RX · Contest` plus an
inline audio player with a **playback-speed selector** (0.8×–2.0×) and a
**Save** button to download the recording. The player uses a **custom progress
slider** that fills the whole row width, with a play/pause button and a
current/total time readout (the native browser controls — including the mute
button and the "…" overflow menu — are no longer shown). Click a row for the
full QSO detail — name, QTH, grid, exchange, points, **WPX prefix, continent,
multipliers, precedence, check, power** and more, all captured automatically
from the N1MM Logger+ contact broadcast.

### Continuous view

Shows `Start · Stop · Duration · RX` and the player — no QSO metadata, just
the raw recorded chunks.

### Live Log

The **📜 Log** button opens a color-coded, auto-refreshing view of the
application log. Errors are red, warnings amber, info/debug muted.

### Settings

The **⚙ Settings** panel lets you change every option live. Hover the **?**
icon next to any field for an explanation.

**⚠ Factory Reset** — permanently deletes the entire QSO log database, all
recorded audio files (WAV/MP3), restores the configuration to defaults and
clears the application log buffer. If you want to keep your recordings, copy
them from the recordings folder before proceeding. A confirmation prompt
lists exactly what will be removed.

---

## Screenshots

A few views of the **QSOCapture** dashboard in action:

![QSOCapture dashboard — view 1](screenshots/screenshot1.png)

![QSOCapture dashboard — view 2](screenshots/screenshot2.png)

![QSOCapture dashboard — view 3](screenshots/screenshot3.png)

![QSOCapture dashboard — view 4](screenshots/screenshot4.png)

![QSOCapture dashboard — view 5](screenshots/screenshot5.png)

---

## Project layout

```
QSOCapture/
├── main.py            # FastAPI app, web dashboard, orchestration (python main.py)
├── launcher.py        # Desktop EXE entry point (embedded WebView2 browser)
├── config.py          # Config loading/saving + schema (with help text)
├── db.py              # SQLite storage of QSO records
├── audio_manager.py   # Audio capture (TCI / soundcard) + circular buffer
├── n1mm_listener.py   # N1MM Logger+ UDP listener
├── index.html         # Tailwind dashboard (served by main.py)
├── build.spec         # PyInstaller spec for building the standalone EXE
├── installer.iss      # Inno Setup script for the Windows installer
├── building.md        # Build instructions (requirements, EXE, installer)
├── gen_icon.py        # Icon generator (SVG → ICO)
├── CHANGELOG.md       # Release changelog
├── LICENSE            # MIT license
├── .gitignore
├── requirements.txt
├── screenshots/       # Dashboard screenshots
└── recordings/        # Recorded audio (created on first run)
```

---

## API reference (short)

| Method | Endpoint | Purpose |
| ------ | -------- | ------- |
| GET | `/api/contests` | List contest folders. |
| GET | `/api/qsos` | Filtered QSO list (`contest`, `call`, `band`, `mode`, `rx`, `date_from`, `date_to`, `continuous`). |
| GET | `/api/status` | TCI / N1MM connection state + per-RX buffer fill. |
| GET | `/api/log` | Recent application log lines. |
| GET | `/api/config` | Current config + UI schema (with help). |
| POST | `/api/config` | Update config live and restart services. |
| POST | `/api/continuous/pause` | Finalise the current continuous chunk and stop recording. |
| POST | `/api/continuous/resume` | Resume continuous recording with a fresh chunk. |
| GET | `/api/export` | Download all (or one contest's) recordings as a ZIP archive. |
| GET | `/api/audio_devices` | List available soundcard input devices (for `soundcard_device`). |
| GET | `/api/paths` | Absolute filesystem paths of `recordings/` and `config.cfg`. |
| POST | `/api/open_folder` | Open the recordings directory in the system file manager. |
| GET | `/api/version_check` | Compare running version against latest GitHub release tag. |
| GET | `/api/events` | Server-Sent Events stream for live dashboard updates. |
| POST | `/api/debug` | Enable or disable debug-level logging at runtime. |
| POST | `/api/factory_reset` | Wipe log + recordings + restore defaults. |
| GET | `/audio/{contest}/{file}` | Stream a recorded audio file. |

---

## Troubleshooting

 - **No audio recorded / buffer stays empty** — check the source in
   **⚙ Settings**: for `tci` mode verify `tci_host`/`tci_port` match ExpertSDR's
   TCI server (default `127.0.0.1:50001`, enabled in ExpertSDR options); for
   `soundcard` mode pick the correct `soundcard_device` substring and confirm
   the OS is routing receiver audio to that input.
 - **Windows 7 / 8 not supported** — these systems lack the Edge WebView2
   runtime required by the standard build. Use the dedicated legacy build
   (`QSOCapture-Win7-setup-x.y.z.exe` or `QSOCapture-portable-Win7-x.y.z.exe`)
   from the [Releases](https://github.com/sq3rx/QSOCapture/releases) page.
   See [building.md](building.md) for details.
 - **N1MM contacts not appearing** — ensure N1MM Logger+ broadcasts on the same
   UDP port configured in `n1mm_udp_port` (default `12060`) and that the
   machine's firewall allows the bind on `n1mm_bind_ip` (default `0.0.0.0`).
 - **Broken / unplayable continuous chunks** — if you stop the app while a
   continuous chunk is open it is finalised automatically; empty chunks (no
   audio received) are discarded so the continuous view never shows dead rows.

---

## Changelog

For a business-oriented summary of what changed in each release, see
[CHANGELOG.md](CHANGELOG.md).

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file
for details. Free to use, modify and share for amateur radio and beyond.

---

## Development approach

This project was created with an **agentic (AI-assisted) approach** — the code,
structure and documentation were iteratively developed with the help of an AI
coding agent. This workflow keeps the project easy to evolve: new capabilities
can be added by describing the desired behaviour in natural language.

**73 de SQ3RX**
