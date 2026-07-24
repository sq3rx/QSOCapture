"""config.py - Configuration parsing for QSOCapture.

This module loads the INI-style ``config.cfg`` file using :mod:`configparser`
and exposes a single :class:`AppConfig` dataclass-like object with sensible
defaults for every setting. It is intentionally dependency-free so that it can
be imported from any thread without side effects.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AppConfig:
    """Strongly-typed, fully defaulted application configuration."""

    # general
    station_name: str = "MYSTATION"
    recordings_dir: str = "recordings"
    default_contest: str = "GENERAL"
    continuous_recording: bool = True
    continuous_autostart: bool = False  # begin continuous recording automatically on startup
    continuous_chunk_minutes: int = 60
    normalize_continuous: bool = True   # normalize continuous WAV chunks to consistent loudness
    max_recordings_gb: float = 0.0  # max disk usage for recordings (0 = unlimited)

    # audio
    audio_mode: str = "soundcard"          # "tci" | "soundcard"
    audio_format: str = "wav"              # "wav" | "mp3"
    sample_rate: int = 48000
    channels: int = 2                      # 1 = mono (SO1R), 2 = stereo (SO2R)
    pre_roll: float = 8.0                  # seconds kept before QSO start
    post_roll: float = 5.0                 # seconds waited after N1MM packet
    sample_width: int = 2                  # bytes per sample

    # tci
    tci_host: str = "127.0.0.1"
    tci_port: int = 50001
    tci_version: int = 2

    # soundcard
    soundcard_device: str = ""             # empty = default input device

    # n1mm
    n1mm_udp_port: int = 12060
    n1mm_bind_ip: str = "127.0.0.1"

    # web
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    dashboard_file: str = "index.html"

    # ---- derived helpers -------------------------------------------------
    @property
    def is_stereo(self) -> bool:
        """True when the source provides two channels (SO2R)."""
        return self.channels >= 2

    @property
    def frames_per_second(self) -> int:
        return self.sample_rate


def _get(config: configparser.ConfigParser, section: str, key: str, default):
    """Return a value from *config* falling back to *default*.

    The type of *default* drives the conversion (bool/int/float/str).
    """
    if not config.has_section(section) or not config.has_option(section, key):
        return default
    raw = config.get(section, key).strip()
    if isinstance(default, bool):
        return config.getboolean(section, key)
    if isinstance(default, int):
        return config.getint(section, key)
    if isinstance(default, float):
        return config.getfloat(section, key)
    return raw


def load_config(path: str = "config.cfg") -> AppConfig:
    """Parse *path* (an INI file) and return a populated :class:`AppConfig`.

    Missing sections/keys are replaced with the dataclass defaults so the
    application can always assume a fully populated configuration object.
    """
    parser = configparser.ConfigParser()
    if os.path.isfile(path):
        # keep comments-free parsing; configparser ignores ';' / '#' comments
        parser.read(path, encoding="utf-8")
    else:
        # No config file -> use defaults only (still a valid run).
        parser = configparser.ConfigParser()

    cfg = AppConfig(
        station_name=_get(parser, "general", "station_name", AppConfig.station_name),
        recordings_dir=_get(parser, "general", "recordings_dir", AppConfig.recordings_dir),
        default_contest=_get(parser, "general", "default_contest", AppConfig.default_contest),
        continuous_recording=_get(parser, "general", "continuous_recording", AppConfig.continuous_recording),
        continuous_autostart=_get(parser, "general", "continuous_autostart", AppConfig.continuous_autostart),
        continuous_chunk_minutes=_get(parser, "general", "continuous_chunk_minutes", AppConfig.continuous_chunk_minutes),
        normalize_continuous=_get(parser, "general", "normalize_continuous", AppConfig.normalize_continuous),

        audio_mode=_get(parser, "audio", "mode", AppConfig.audio_mode).lower(),
        audio_format=_get(parser, "audio", "audio_format", AppConfig.audio_format).lower(),
        sample_rate=_get(parser, "audio", "sample_rate", AppConfig.sample_rate),
        channels=_get(parser, "audio", "channels", AppConfig.channels),
        pre_roll=_get(parser, "audio", "pre_roll", AppConfig.pre_roll),
        post_roll=_get(parser, "audio", "post_roll", AppConfig.post_roll),
        sample_width=_get(parser, "audio", "sample_width", AppConfig.sample_width),

        tci_host=_get(parser, "audio", "tci_host", AppConfig.tci_host),
        tci_port=_get(parser, "audio", "tci_port", AppConfig.tci_port),
        tci_version=_get(parser, "audio", "tci_version", AppConfig.tci_version),

        soundcard_device=_get(parser, "audio", "soundcard_device", AppConfig.soundcard_device),

        n1mm_udp_port=_get(parser, "n1mm", "udp_port", AppConfig.n1mm_udp_port),
        n1mm_bind_ip=_get(parser, "n1mm", "bind_ip", AppConfig.n1mm_bind_ip),

        web_host=_get(parser, "web", "host", AppConfig.web_host),
        web_port=_get(parser, "web", "port", AppConfig.web_port),
        dashboard_file=_get(parser, "web", "dashboard_file", AppConfig.dashboard_file),
    )
    return cfg


# Metadata describing each config field for the web UI (label, type, section,
# optional choices). When ``choices`` is provided the web UI renders a
# <select> dropdown instead of a free-text / number input.
CONFIG_SCHEMA = [
    ("general", "station_name", "Station name", "text", None,
     "Your station callsign / identifier shown in the header and used as a label in logs."),
    ("general", "recordings_dir", "Recordings directory", "text", None,
     "Folder where recorded QSO slices and continuous chunks are stored. Relative to the app directory."),
    ("general", "default_contest", "Default contest", "text", None,
     "Contest name used when N1MM does not supply one (e.g. GENERAL for everyday logging)."),
    ("general", "continuous_autostart", "Continuous recording autostart", "bool", None,
     "When ON, continuous recording starts automatically on app startup. When OFF, you can still start it anytime from the dashboard (Stop/Start recording button)."),
    ("general", "continuous_chunk_minutes", "Continuous chunk (min)", "int", None,
     "Length of each continuous recording chunk in minutes. Larger values = fewer, bigger files."),
    ("general", "normalize_continuous", "Normalize continuous recordings", "bool", None,
     "When ON, continuous WAV chunks are normalized to a consistent loudness level after each chunk is finalized. Disable to save CPU time and memory on long recordings (QSO slices are always normalized regardless of this setting)."),
    ("general", "max_recordings_gb", "Max recordings (GB)", "float", None,
     "Hard cap on total disk usage of the recordings folder. When exceeded, the oldest continuous chunks are deleted automatically (0 = unlimited)."),
    ("audio", "audio_mode", "Audio mode", "text", ["tci", "soundcard"],
     "Source of audio: 'tci' streams from ExpertSDR via the TCI protocol, 'soundcard' captures a system input device."),
    ("audio", "sample_rate", "Sample rate (Hz)", "int", [8000, 16000, 22050, 44100, 48000, 96000],
     "Audio sample rate. Must match your radio/TCI or soundcard setting (48000 is typical)."),
    ("audio", "channels", "Channels (1=SO1R,2=SO2R)", "int", [1, 2],
     "1 = mono single receiver (SO1R), 2 = stereo two receivers (SO2R)."),
    ("audio", "pre_roll", "Pre-roll (s)", "float", None,
     "Seconds of audio kept BEFORE the N1MM contact timestamp, so the start of the QSO is captured."),
    ("audio", "post_roll", "Post-roll (s)", "float", None,
     "Seconds to wait AFTER the N1MM packet before slicing, so the tail of the QSO is included."),
    ("audio", "sample_width", "Sample width (bytes)", "int", [1, 2, 4],
     "Bytes per sample in the recorded file (2 = 16-bit, standard for WAV)."),
    ("audio", "audio_format", "Audio format", "text", ["wav", "mp3"],
     "Container for saved audio. WAV is lossless and fast; MP3 saves disk space (requires lameenc)."),
    ("audio", "tci_host", "TCI host", "text", None,
     "IP address of the ExpertSDR TCI server (usually 127.0.0.1 when running on the same PC)."),
    ("audio", "tci_port", "TCI port", "int", None,
     "TCP port of the ExpertSDR TCI server (default 50001)."),
    ("audio", "tci_version", "TCI version", "int", [1, 2],
     "TCI protocol version advertised by ExpertSDR (usually 2)."),
    ("audio", "soundcard_device", "Soundcard device (substr)", "text", None,
     "Substring match of the system input device name to capture (leave empty for the default device)."),
    ("n1mm", "n1mm_udp_port", "N1MM UDP port", "int", None,
     "UDP port N1MM Logger+ sends contact broadcasts on (default 12060)."),
    ("n1mm", "n1mm_bind_ip", "N1MM bind IP", "text", None,
     "Network interface to listen on for N1MM packets (127.0.0.1 = local only, safer default; use 0.0.0.0 only if N1MM runs on another machine)."),
    ("web", "web_host", "Web host", "text", None,
     "Network interface the web dashboard binds to (127.0.0.1 = local only, safer default; 0.0.0.0 = accessible from other devices on the network)."),
    ("web", "web_port", "Web port", "int", None,
     "TCP port for the web dashboard (open http://localhost:PORT in your browser)."),
]


def config_to_dict(cfg: AppConfig) -> dict:
    """Return the full configuration as a plain dict (for JSON / web UI)."""
    return {k: v for k, v in cfg.__dict__.items()}


# Maps AppConfig attribute names to the (section, ini_key) used in config.cfg.
# Only entries that differ from the attribute name need to be listed; the
# save routine falls back to (schema_section, attribute_name) otherwise.
INI_KEYS = {
    "audio_mode": ("audio", "mode"),
    "n1mm_udp_port": ("n1mm", "udp_port"),
    "n1mm_bind_ip": ("n1mm", "bind_ip"),
    "web_host": ("web", "host"),
    "web_port": ("web", "port"),
}


def save_config(cfg: AppConfig, path: str = "config.cfg") -> None:
    """Persist *cfg* back to an INI file grouped by section.

    The mapping from flat field names to INI sections/keys is derived from
    :data:`CONFIG_SCHEMA` (section) and :data:`INI_KEYS` (key override), so the
    written file matches the ``config.cfg`` layout that :func:`load_config`
    expects.
    """
    parser = configparser.ConfigParser()
    for section, field, _label, _ftype, _choices, _help in CONFIG_SCHEMA:
        ini_section, ini_key = INI_KEYS.get(field, (section, field))
        if not parser.has_section(ini_section):
            parser.add_section(ini_section)
        value = getattr(cfg, field)
        if isinstance(value, bool):
            value = "true" if value else "false"
        parser.set(ini_section, ini_key, str(value))
    with open(path, "w", encoding="utf-8") as f:
        parser.write(f)


if __name__ == "__main__":
    import json

    c = load_config()
    print(json.dumps(c.__dict__, indent=2, default=str))