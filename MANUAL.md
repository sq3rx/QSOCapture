# QSOCapture – User Manual

QSOCapture is a lightweight contest audio recorder and log player for amateur radio operators. It listens for **N1MM Logger+** UDP broadcasts, slices out audio for each logged QSO, and presents everything in a clean, color-coded web dashboard.

---

## Table of Contents

1. [Installation and First Launch](#1-installation-and-first-launch)
2. [Dashboard Overview](#2-dashboard-overview)
3. [Configuration – Detailed Settings](#3-configuration--detailed-settings)
   - [3.1 General](#31-general)
   - [3.2 Audio](#32-audio)
   - [3.3 Soundcard](#33-soundcard)
   - [3.4 TCI](#34-tci)
4. [SO2R – Dual Receiver Operation](#4-so2r--dual-receiver-operation)
5. [Continuous Recording](#5-continuous-recording)
6. [N1MM Logger+ Integration](#6-n1mm-logger-integration)
7. [Filtering and Searching](#7-filtering-and-searching)
8. [Playback and Analysis](#8-playback-and-analysis)
9. [Data Management](#9-data-management)
10. [Update Checking](#10-update-checking)
11. [Log Viewing and Debugging](#11-log-viewing-and-debugging)
12. [Troubleshooting](#12-troubleshooting)
13. [Tips and Best Practices](#13-tips-and-best-practices)

---

## 1. Installation and First Launch

1. Download the latest installer `QSOCapture-setup-X.Y.Z.exe` from the [GitHub Releases](https://github.com/sq3rx/QSOCapture/releases) page.
2. Run the installer and follow the on-screen instructions.
3. After installation, launch the program from the Start Menu or desktop shortcut.
4. The dashboard opens automatically in the program's built-in window.

---

## 2. Dashboard Overview

The dashboard header contains:

| Element | Description |
| ------- | ----------- |
| **Status** | Station name, audio mode (TCI/SOUNDCARD), sample rate, SO1R/SO2R mode, and whether the service is active. |
| **TCI badge** | ExpertSDR connection status (TCI mode only). |
| **N1MM badge** | N1MM listener status. |
| **Rec badge** | Pulsing red indicator shown during active continuous recording. |
| **RX buffers** | Buffer fill level for each receiver (e.g. `RX1 30s`, `RX2 30s`). |
| **Buttons** | `⚙ Settings`, `📜 Log`, `🔄 Refresh`, `⏹ Stop / ▶ Start recording`. |

The main area has two views selected via the **Type** filter:

- **N1MM QSOs** – list of sliced contacts showing Timestamp, Call, Band, Mode, Freq, Exch, RX, Contest and an inline audio player.
- **Continuous** – list of continuous recording chunks showing Start, Stop, Duration, RX and the player.

---

## 3. Configuration – Detailed Settings

Click **⚙ Settings** to open the configuration panel. All changes are applied after clicking **Save** – services restart automatically with the new settings.

### 3.1 General

| Field | Description |
| ----- | ----------- |
| **Station name** | Your callsign or station identifier shown in the dashboard header and used in logs. |
| **Continuous recording autostart** | When ON, continuous recording starts automatically when the program launches. |
| **Continuous chunk (min)** | Length of each continuous recording file in minutes. |
| **Normalize continuous recordings** | When ON, continuous WAV chunks are normalized (volume levelled). Disable to save CPU on long recordings. QSO slices are always normalized. |
| **Web address (host:port)** | Web interface address and port. Default `127.0.0.1:8000` (local only). Set to `0.0.0.0:8000` to expose the dashboard on your LAN. |
| **TCI address (host:port)** | ExpertSDR TCI server address (default `127.0.0.1:50001`). |
| **N1MM address (bind_ip:udp_port)** | UDP address and port for receiving N1MM Logger+ broadcasts (default `127.0.0.1:12060`). |

### 3.2 Audio

| Field | Description |
| ----- | ----------- |
| **Audio mode** | `tci` – stream audio from ExpertSDR via the TCI protocol. `soundcard` – capture from a system input device. |
| **Audio format** | `wav` – lossless (larger files). `mp3` – smaller files (requires lameenc library). |
| **Sample rate (Hz)** | Audio sample rate. Must match your radio/TCI or soundcard settings. |
| **Channels (1=SO1R, 2=SO2R)** | `1` – single receiver (mono). `2` – dual receivers (SO2R). |
| **SO2R mode** | SO2R mode selector (visible only when **Audio mode = soundcard** and **Channels = 2**). |
| **Pre-roll (s)** | Seconds of audio kept **before** the N1MM contact timestamp. |
| **Post-roll (s)** | Seconds to wait **after** receiving the N1MM packet before slicing. |
| **Sample width (bytes)** | Bytes per sample (2 = 16-bit, standard for WAV). |

#### SO2R mode details

Two modes available (toggle switch):

- **Stereo** (default) – single soundcard, left channel → RX1, right channel → RX2.
- **Dual card** – two separate soundcards, each recording one receiver in mono. When selected, a second device selector appears in the Soundcard section.

### 3.3 Soundcard

Visible only when **Audio mode = soundcard**.

| Field | Description |
| ----- | ----------- |
| **Soundcard device (RX1)** | Select the system input device for the first receiver. In stereo mode this is the only device used (L→RX1, R→RX2). |
| **Soundcard device 2 (RX2)** | Select the second device for RX2. Visible only when **Channels = 2** and **SO2R mode = Dual card**. |

Devices are chosen from a list of available soundcards – no need to manually type names.

### 3.4 TCI

Visible only when **Audio mode = tci**.

| Field | Description |
| ----- | ----------- |
| **TCI host / TCI port** | ExpertSDR3 TCI server IP address and port. Default `127.0.0.1:50001`. Make sure TCI is enabled in ExpertSDR settings. |

In TCI mode, SO2R is handled by the TCI protocol – the program automatically requests audio streams for receivers 0 (RX1) and 1 (RX2) when **Channels = 2**. No separate soundcard configuration is needed. The **SO2R mode** setting does not apply in TCI mode.

---

## 4. SO2R – Dual Receiver Operation

QSOCapture fully supports dual-receiver (SO2R) operation. Each receiver has its own circular buffer, its own output file, and is independently logged.

### In soundcard mode

- **Stereo:** a single soundcard with two channels. Left → RX1, right → RX2. You only select **Soundcard device (RX1)**.
- **Dual card:** two separate soundcards, each recording one receiver in mono. Select individual devices for RX1 and RX2. Useful when using two separate audio interfaces.

### In TCI mode

SO2R is handled by the TCI protocol. The program automatically opens audio streams for receiver 0 (RX1) and receiver 1 (RX2). No additional hardware configuration is needed. Setting **Channels = 2** enables both receivers.

### Receiver number in QSOs

The program reads the `<RadioNr>` field from N1MM Logger+ messages. Each contact is assigned to the correct receiver:
- Radio 1 → saved as `..._RX1.ext`
- Radio 2 → saved as `..._RX2.ext`

---

## 5. Continuous Recording

Continuous recording captures the entire band into files split into chunks of the length set in **Continuous chunk (min)**.

### Starting

- **Automatically:** enable **Continuous recording autostart** in settings. Recording begins when the program starts.
- **Manually:** click **▶ Start recording** on the dashboard. The button changes to **⏹ Stop recording**.

### Stopping

Click **⏹ Stop recording** – the current chunk is finalised (closed) and immediately appears in the **Continuous** tab with a player ready to use.

### Viewing

The **Continuous** tab shows all saved chunks with columns:
- **Start** – chunk start date and time
- **Stop** – chunk end date and time
- **Duration** – recording length
- **RX** – which receiver (RX1 or RX2)
- **Player** – built-in audio player

Empty chunks (no audio received) are automatically discarded.

---

## 6. N1MM Logger+ Integration

### Configuration

1. In N1MM Logger+, open **Tools → Configure Ports → UDP**.
2. Set the port to **12060** (or match the port configured in QSOCapture).
3. Ensure **Broadcast contacts** is enabled.
4. In QSOCapture settings, confirm the **N1MM address** port matches (default `127.0.0.1:12060`).

### How QSO slicing works

When N1MM Logger+ broadcasts a logged contact:

1. The program waits **post_roll** seconds (to capture the end of the QSO).
2. It slices audio from the circular buffer starting **pre_roll** seconds before the contact's timestamp.
3. It saves the audio file with a name containing: date, time, callsign, band and receiver label.
4. It stores the QSO details in the local SQLite database.

### Example filename

```
2026-07-13_2120_SQ3RX_20M_RX1.wav
```

### Editing and deleting QSOs in N1MM

* **Deleting a contact** (`contactdelete`) – the program removes the database record and also deletes the associated audio file from the recordings directory.
* **Editing a contact** (`contactreplace`) – the program renames the existing audio file to reflect the updated callsign and band, and updates all QSO details in the database. The original audio recording is preserved – only its filename changes.

---

## 7. Filtering and Searching

The dashboard offers several filters above the QSO list:

| Filter | Description |
| ------ | ----------- |
| **Contest** | Text field with autocomplete. Type a contest name fragment (e.g. `CQWW`) and select from the dropdown. |
| **Call / Prefix** | Free-text search. Enter a callsign (`SQ3RX`), prefix (`SQ`), or a **regular expression** (`^SQ`, `3[A-Z]X$`, `SQ\|SP`). Invalid regex is treated as plain text. |
| **Band** | Dropdown with checkboxes for bands 160M–2M plus a custom band field. Multiple bands can be selected at once. |
| **Mode** | Exact match: CW, SSB, RTTY, FT8, PSK, DIGI, or all. |
| **RX** | Filter by receiver: All RX, RX1 or RX2. Works for both QSO and Continuous views. |
| **Date/time from – to** | Filter by date and time. Type manually `YYYY-MM-DD HH:mm` or pick from the calendar. |
| **Type** | Switch between **N1MM QSOs** and **Continuous** recordings. |

The list refreshes automatically after each filter change.

---

## 8. Playback and Analysis

Every row in the QSO and Continuous views includes a built-in audio player.

### Player controls

| Element | Description |
| ------- | ----------- |
| **▶/⏸** | Play / Pause button. |
| **Slider** | Progress bar – drag to seek through the recording. |
| **Time** | Displays current time / total time (e.g. `0:23 / 1:45`). |
| **Speed** | Playback speed selector: 0.8×, 1.0×, 1.2×, 1.5×, 2.0×. |
| **⭳ Save** | Downloads the recording file to your disk. |

### QSO details

Click on a QSO row (but not on player controls) to open a details panel with: name, QTH, grid, exchange, points, **WPX prefix, continent, multipliers, prec, CK, power** and more – all captured automatically from the N1MM Logger+ contact broadcast.

---

## 9. Data Management

The **⚠️ Resets and deletes** section in Settings provides the following operations:

| Action | What it does |
| ------ | ------------ |
| **Reset config** | Restores all settings to defaults. Recordings and QSO log remain untouched. |
| **Clear QSO log** | Deletes all QSO records from the database. Audio files are **not** affected. |
| **Delete all data** | Removes all audio files **and** clears the QSO database. Configuration is kept. |
| **Delete contest** | Removes the selected contest folder (audio files) **and** deletes its QSO records. |
| **Delete contest recordings** | Removes audio files only for the selected contest. QSO records remain. |
| **Delete continuous** | Deletes continuous recordings within a specified date range. |
| **Factory Reset** | Wipes the entire QSO database, all audio files, restores default settings and clears the log buffer. |

> **Note:** Before any file-deleting operation, a confirmation dialog appears listing exactly what will be removed.

---

## 10. Update Checking

QSOCapture automatically checks for new versions against GitHub Releases on startup.

### How it works

- On launch, the program queries the GitHub API for the latest release tag.
- If a newer version is found, a compact **amber badge** appears in the dashboard header saying "New X.Y.Z".
- Click the badge to open the release page on GitHub.
- Click the **×** on the badge to dismiss it (the dismissal is remembered for that version).

### Manual check

Open the **About** modal (click the **?** button in the header) to see the current version and check for updates manually. The status shows:
- "You have the latest version" – up to date.
- "New version X.Y.Z – Download" – an update is available.
- "Could not reach version server" – offline or GitHub unreachable.

---

## 11. Log Viewing and Debugging

Click the **📜 Log** button to open the log viewer.

- Logs are colour-coded: **ERROR** – red, **WARNING** – amber, **INFO** – grey, **DEBUG** – dark grey.
- Check the **debug** box to enable detailed logging (useful for diagnosing TCI connection issues, buffering, etc.).
- Enable **auto-refresh** to update the log every 2 seconds automatically.
- Click **🔄** to refresh manually.

---

## 12. Troubleshooting

### No audio / empty buffer

- **TCI mode:** verify ExpertSDR is running and TCI is enabled (default `127.0.0.1:50001`). Check the log – enable debug mode to see connection details.
- **Soundcard mode:** make sure you selected the correct input device from the list. Verify that your OS routes receiver audio to that device.
- In both modes: confirm the buffer badges on the dashboard show increasing values (e.g. `RX1 30s`).

### N1MM contacts not appearing

- Verify N1MM Logger+ is broadcasting on the correct UDP port (default `12060`).
- Ensure your firewall allows traffic on that port.
- Confirm that **Broadcast contacts** is enabled in N1MM Logger+.
- The N1MM badge on the dashboard should be green and say "listening".

### QSO save failure

- If recordings do not save in the installed version, make sure you are using the latest release (affects older Nuitka builds).
- Check that the folder `%LOCALAPPDATA%\QSOCapture\recordings` exists and is writable.

### Broken / inconsistent continuous chunks

- When the program closes, the current chunk is automatically finalised.
- Empty chunks (no audio) are automatically discarded.
- If an unclean shutdown leaves a `Recording…` entry, it is cleaned up on the next launch.

---

## 13. Tips and Best Practices

### For contests

- Set **pre-roll** to 8–10 seconds and **post-roll** to 15-20 seconds for a safe margin before and after each contact.
- Keep **Continuous recording** enabled so no QSO is ever missed, even if the N1MM broadcast is delayed.
- Use **MP3** format to save disk space.
- Check the logs regularly to ensure everything is working correctly.

### Saving disk space

- Use **MP3** instead of WAV – files are several times smaller.
- Set shorter continuous chunks (e.g. 15–30 minutes) – easier to browse and delete unneeded segments.
- Regularly remove old contests or continuous recordings through the Settings panel.

### Backup

All recordings and the QSO database are stored in:
```
%LOCALAPPDATA%\QSOCapture\

```
Make regular backups of this folder, especially before important contests.

---

*73 de SQ3RX*
