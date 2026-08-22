"""Configuration parsing for QSOCapture.

Loads the INI-style config.cfg via configparser and exposes an AppConfig
dataclass with sensible defaults.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from typing import Optional


def _get_base_dir() -> str:
    """Return the writable app data directory (%LOCALAPPDATA%\\QSOCapture, fallback ~/QSOCapture)."""
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "QSOCapture",
    )


BASE_DIR = _get_base_dir()
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")


@dataclass
class AppConfig:
    """Strongly-typed, fully defaulted application configuration."""

    # general
    station_name: str = "MYSTATION"
    continuous_recording: bool = True
    continuous_autostart: bool = False
    continuous_chunk_minutes: int = 60
    normalize_continuous: bool = True
    max_recordings_gb: float = 0.0

    # audio
    audio_mode: str = "soundcard"          # "tci" | "soundcard"
    audio_format: str = "wav"              # "wav" | "mp3"
    sample_rate: int = 48000
    channels: int = 2                      # 1 = mono (SO1R), 2 = stereo (SO2R)
    pre_roll: float = 8.0
    post_roll: float = 5.0
    sample_width: int = 2
    so2r_mode: str = "stereo"            # "stereo" | "dual_card"  — how SO2R is wired
    soundcard_device2: str = ""           # second device name for dual_card mode

    # tci
    tci_host: str = "127.0.0.1"
    tci_port: int = 50001

    # soundcard
    soundcard_device: str = ""

    # logger source
    logger_source: str = "n1mm"              # "n1mm" | "n3fjp"

    # n1mm
    n1mm_udp_port: int = 12060
    n1mm_bind_ip: str = "127.0.0.1"

    # n3fjp (TCP API, N3FJP is the server)
    n3fjp_host: str = "127.0.0.1"
    n3fjp_port: int = 1100
    n3fjp_contest: str = ""
    n3fjp_reconcile_interval_s: int = 60
    n3fjp_list_window: int = 100

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
    """Parse *path* (an INI file) and return a populated AppConfig.

    Missing sections/keys fall back to dataclass defaults.
    """
    parser = configparser.ConfigParser()
    if os.path.isfile(path):
        parser.read(path, encoding="utf-8")

    cfg = AppConfig(
        station_name=_get(parser, "general", "station_name", AppConfig.station_name),
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
        so2r_mode=_get(parser, "audio", "so2r_mode", AppConfig.so2r_mode).lower(),
        soundcard_device2=_get(parser, "audio", "soundcard_device2", AppConfig.soundcard_device2),

        # Backward compatibility: tci_host/tci_port moved [audio] → [general] → [tci].
        tci_host=_get(parser, "tci", "tci_host",
                      _get(parser, "general", "tci_host",
                            _get(parser, "audio", "tci_host", AppConfig.tci_host))),
        tci_port=_get(parser, "tci", "tci_port",
                      _get(parser, "general", "tci_port",
                            _get(parser, "audio", "tci_port", AppConfig.tci_port))),

        soundcard_device=_get(parser, "audio", "soundcard_device", AppConfig.soundcard_device),

        logger_source=_get(parser, "general", "logger_source", AppConfig.logger_source).lower(),

        n1mm_udp_port=_get(parser, "n1mm", "udp_port", AppConfig.n1mm_udp_port),
        n1mm_bind_ip=_get(parser, "n1mm", "bind_ip", AppConfig.n1mm_bind_ip),

        n3fjp_host=_get(parser, "n3fjp", "host", AppConfig.n3fjp_host),
        n3fjp_port=_get(parser, "n3fjp", "port", AppConfig.n3fjp_port),
        n3fjp_contest=_get(parser, "n3fjp", "contest", AppConfig.n3fjp_contest),
        n3fjp_reconcile_interval_s=_get(parser, "n3fjp", "reconcile_interval_s", AppConfig.n3fjp_reconcile_interval_s),
        n3fjp_list_window=_get(parser, "n3fjp", "list_window", AppConfig.n3fjp_list_window),

        web_host=_get(parser, "web", "host", AppConfig.web_host),
        web_port=_get(parser, "web", "port", AppConfig.web_port),
        dashboard_file=_get(parser, "web", "dashboard_file", AppConfig.dashboard_file),
    )
    return cfg


# UI schema: (section, field, label, type, choices, help_text)
# When ``choices`` is provided the web UI renders a <select> dropdown.
# When ``type`` is "device" the web UI renders a <select> populated from /api/audio_devices.
CONFIG_SCHEMA = [
    ("general", "station_name", "Station name", "text", None,
     "Your station callsign / identifier shown in the header and used as a label in logs."),
    ("general", "continuous_autostart", "Continuous recording autostart", "bool", None,
     "When ON, continuous recording starts automatically on app startup."),
    ("general", "continuous_chunk_minutes", "Continuous chunk (min)", "int", None,
     "Length of each continuous recording chunk in minutes."),
    ("general", "normalize_continuous", "Normalize continuous recordings", "bool", None,
     "When ON, continuous WAV chunks are normalized after each chunk. Disable to save CPU on long recordings (QSO slices are always normalized)."),
    ("general", "max_recordings_gb", "Max recordings (GB)", "float", None,
     "Hard cap on recordings folder disk usage. Oldest continuous chunks deleted when exceeded (0 = unlimited)."),
    ("general", "logger_source", "Logger source", "text", ["n1mm", "n3fjp"],
     "Which logging program drives QSO capture: 'n1mm' = N1MM Logger+ UDP broadcasts, 'n3fjp' = the N3FJP TCP API."),
    ("tci", "tci_host", "TCI host", "text", None,
     "IP address of the ExpertSDR TCI server (usually 127.0.0.1)."),
    ("tci", "tci_port", "TCI port", "int", None,
     "TCP port of the ExpertSDR TCI server (default 50001)."),
    ("audio", "audio_mode", "Audio mode", "text", ["tci", "soundcard"],
     "'tci' streams from ExpertSDR, 'soundcard' captures a system input device."),
    ("audio", "sample_rate", "Sample rate (Hz)", "int", [8000, 16000, 22050, 44100, 48000, 96000],
     "Audio sample rate. Must match your radio/TCI or soundcard setting."),
    ("audio", "channels", "Channels (1=SO1R,2=SO2R)", "int", [1, 2],
     "1 = mono single receiver (SO1R), 2 = stereo two receivers (SO2R)."),
    ("audio", "pre_roll", "Pre-roll (s)", "float", None,
     "Seconds of audio kept BEFORE the N1MM contact timestamp."),
    ("audio", "post_roll", "Post-roll (s)", "float", None,
     "Seconds to wait AFTER the N1MM packet before slicing."),
    ("audio", "sample_width", "Sample width (bytes)", "int", [1, 2, 4],
     "Bytes per sample (2 = 16-bit, standard for WAV)."),
    ("audio", "audio_format", "Audio format", "text", ["wav", "mp3"],
     "WAV is lossless; MP3 saves disk space (requires lameenc)."),
    ("audio", "so2r_mode", "SO2R mode", "select", ["stereo", "dual_card"],
     "'stereo' = one soundcard, left channel RX1 / right channel RX2. 'dual_card' = two separate soundcards, each mono."),
    ("audio", "soundcard_device", "Soundcard device (RX1)", "device", None,
     "Select the system input device for RX1 (or for stereo mode)."),
    ("audio", "soundcard_device2", "Soundcard device 2 (RX2)", "device", None,
     "Second soundcard for RX2 when SO2R mode is 'dual_card'."),
    ("n1mm", "n1mm_udp_port", "N1MM UDP port", "int", None,
     "UDP port N1MM Logger+ sends contact broadcasts on (default 12060)."),
    ("n1mm", "n1mm_bind_ip", "N1MM bind IP", "text", None,
     "Interface to listen on for N1MM packets. 127.0.0.1 = local only."),
    ("n3fjp", "n3fjp_host", "N3FJP host", "text", None,
     "Host where N3FJP software listens for TCP API connections (default 127.0.0.1)."),
    ("n3fjp", "n3fjp_port", "N3FJP TCP API port", "int", None,
     "TCP port of the N3FJP Application Program Interface (default 1100)."),
    ("n3fjp", "n3fjp_contest", "N3FJP contest name", "text", None,
     "Optional override for the contest name shown in recordings. Leave blank to use the detected N3FJP program name (or GENERAL)."),
    ("n3fjp", "n3fjp_reconcile_interval_s", "N3FJP reconcile interval (s)", "int", None,
     "How often to re-check N3FJP's QSO list against this database to catch edited/deleted contacts (default 60)."),
    ("n3fjp", "n3fjp_list_window", "N3FJP list window", "int", None,
     "How many of the most recent N3FJP QSOs to compare during reconciliation (default 100)."),
    ("web", "web_host", "Web host", "text", None,
     "Interface the web dashboard binds to. 127.0.0.1 = local only."),
    ("web", "web_port", "Web port", "int", None,
     "TCP port for the web dashboard."),
]


def config_to_dict(cfg: AppConfig) -> dict:
    """Return the full configuration as a plain dict (for JSON / web UI)."""
    return {k: v for k, v in cfg.__dict__.items()}


# Maps AppConfig attribute names to (section, ini_key) for save_config.
# Only entries differing from the attribute name need listing.
INI_KEYS = {
    "audio_mode": ("audio", "mode"),
    "audio_format": ("audio", "audio_format"),
    "tci_host": ("tci", "tci_host"),
    "tci_port": ("tci", "tci_port"),
    "n1mm_udp_port": ("n1mm", "udp_port"),
    "n1mm_bind_ip": ("n1mm", "bind_ip"),
    "n3fjp_host": ("n3fjp", "host"),
    "n3fjp_port": ("n3fjp", "port"),
    "n3fjp_contest": ("n3fjp", "contest"),
    "n3fjp_reconcile_interval_s": ("n3fjp", "reconcile_interval_s"),
    "n3fjp_list_window": ("n3fjp", "list_window"),
    "web_host": ("web", "host"),
    "web_port": ("web", "port"),
}


def save_config(cfg: AppConfig, path: str = "config.cfg") -> None:
    """Persist *cfg* to an INI file grouped by section (matching load_config layout)."""
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