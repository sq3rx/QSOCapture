"""Configuration parsing for QSOCapture.

Loads the INI-style config.cfg via configparser and exposes an AppConfig
dataclass with sensible defaults.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("QSOCapture.config")


def _get_base_dir() -> str:
    """Return the writable app data directory (%LOCALAPPDATA%\\QSOCapture, fallback ~/QSOCapture)."""
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "QSOCapture",
    )


BASE_DIR = _get_base_dir()
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")


def sanitize_contest(name: str) -> str:
    """Make a contest name safe to use as a filesystem path component.

    Strips any directory components (so a name containing ``/`` or ``..`` cannot
    escape the recordings directory) and removes characters that are invalid or
    dangerous in paths. A safe value is always returned (falls back to GENERAL),
    so callers can use the result directly to build file paths.
    """
    name = (name or "").strip().replace("\\", "/")
    name = os.path.basename(name)
    safe = re.sub(r"[^A-Za-z0-9 _.\-]", "", name).strip(". ")
    return safe or "GENERAL"


def encode_device(index: Optional[int], name: str) -> str:
    """Encode a sound device as ``"<index>|<name>"`` for stable identification.

    Older config files store only the device name. Both forms are accepted by
    :func:`decode_device`; using the index disambiguates two identical cards
    (common in dual-card SO2R setups).
    """
    if index is None:
        return name or ""
    return f"{int(index)}|{name or ''}"


def decode_device(value: str) -> tuple[Optional[int], str]:
    """Split a stored device value into ``(index, name)``.

    Plain names (legacy configs) return ``(None, name)``. The index is only
    trusted when it is a non-negative integer.
    """
    raw = (value or "").strip()
    if "|" not in raw:
        return None, raw
    head, _sep, name = raw.partition("|")
    try:
        index = int(head)
    except (TypeError, ValueError):
        return None, raw
    if index < 0:
        return None, name
    return index, name


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
    so2r_mode: str = "stereo"            # "stereo" | "dual_card"  — how SO2R is wired
    soundcard_device2: str = ""           # second device name for dual_card mode

    # tci
    tci_host: str = "127.0.0.1"
    tci_port: int = 50001

    # soundcard
    soundcard_device: str = ""

    # n1mm
    n1mm_udp_port: int = 12060
    n1mm_bind_ip: str = "127.0.0.1"

    # web
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    dashboard_file: str = "index.html"

def _get(config: configparser.ConfigParser, section: str, key: str, default):
    """Return a value from *config* falling back to *default*.

    The type of *default* drives the conversion (bool/int/float/str). A malformed
    value is logged and treated as missing so a single hand-edited bad line can
    never stop the application from starting.
    """
    if not config.has_section(section) or not config.has_option(section, key):
        return default
    raw = config.get(section, key).strip()
    try:
        if isinstance(default, bool):
            return config.getboolean(section, key)
        if isinstance(default, int):
            return config.getint(section, key)
        if isinstance(default, float):
            return config.getfloat(section, key)
    except (ValueError, configparser.Error) as exc:
        logger.warning("Invalid config value [%s] %s=%r (%s); using %r",
                       section, key, raw, exc, default)
        return default
    return raw


def load_config(path: str = "config.cfg") -> AppConfig:
    """Parse *path* (an INI file) and return a populated AppConfig.

    Missing sections/keys fall back to dataclass defaults.
    """
    parser = configparser.ConfigParser()
    if os.path.isfile(path):
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError, OSError) as exc:
            logger.warning("Could not read config %s (%s); using defaults", path, exc)
            parser = configparser.ConfigParser()

    cfg = AppConfig(
        station_name=_get(parser, "general", "station_name", AppConfig.station_name),
        continuous_recording=_get(parser, "general", "continuous_recording", AppConfig.continuous_recording),
        continuous_autostart=_get(parser, "general", "continuous_autostart", AppConfig.continuous_autostart),
        continuous_chunk_minutes=_get(parser, "general", "continuous_chunk_minutes", AppConfig.continuous_chunk_minutes),
        normalize_continuous=_get(parser, "general", "normalize_continuous", AppConfig.normalize_continuous),
        max_recordings_gb=_get(parser, "general", "max_recordings_gb", AppConfig.max_recordings_gb),

        audio_mode=_get(parser, "audio", "mode", AppConfig.audio_mode).lower(),
        audio_format=_get(parser, "audio", "audio_format", AppConfig.audio_format).lower(),
        sample_rate=_get(parser, "audio", "sample_rate", AppConfig.sample_rate),
        channels=_get(parser, "audio", "channels", AppConfig.channels),
        pre_roll=_get(parser, "audio", "pre_roll", AppConfig.pre_roll),
        post_roll=_get(parser, "audio", "post_roll", AppConfig.post_roll),
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

        n1mm_udp_port=_get(parser, "n1mm", "udp_port", AppConfig.n1mm_udp_port),
        n1mm_bind_ip=_get(parser, "n1mm", "bind_ip", AppConfig.n1mm_bind_ip),

        web_host=_get(parser, "web", "host", AppConfig.web_host),
        web_port=_get(parser, "web", "port", AppConfig.web_port),
        dashboard_file=_get(parser, "web", "dashboard_file", AppConfig.dashboard_file),
    )
    reject_invalid_config(cfg)
    normalize_config(cfg)
    return cfg


def _clamp_int(value: int, low: int, high: int, field_name: str) -> int:
    if low <= value <= high:
        return value
    fixed = min(max(value, low), high)
    logger.warning("Config %s=%s out of range %s..%s; using %s",
                   field_name, value, low, high, fixed)
    return fixed


def _clamp_float(value: float, low: float, high: float, field_name: str) -> float:
    if low <= value <= high:
        return value
    fixed = min(max(value, low), high)
    logger.warning("Config %s=%s out of range %s..%s; using %s",
                   field_name, value, low, high, fixed)
    return fixed


def normalize_config(cfg: AppConfig) -> None:
    """Clamp/repair values loaded from a hand-edited or legacy config file."""
    cfg.sample_rate = _clamp_int(int(cfg.sample_rate), 8000, 192000, "sample_rate")
    cfg.channels = _clamp_int(int(cfg.channels), 1, 2, "channels")
    cfg.pre_roll = _clamp_float(float(cfg.pre_roll), 0.0, 120.0, "pre_roll")
    cfg.post_roll = _clamp_float(float(cfg.post_roll), 0.0, 120.0, "post_roll")
    cfg.continuous_chunk_minutes = _clamp_int(
        int(cfg.continuous_chunk_minutes), 1, 1440, "continuous_chunk_minutes")
    cfg.max_recordings_gb = _clamp_float(
        float(cfg.max_recordings_gb), 0.0, 100000.0, "max_recordings_gb")
    cfg.tci_port = _clamp_int(int(cfg.tci_port), 1, 65535, "tci_port")
    cfg.n1mm_udp_port = _clamp_int(int(cfg.n1mm_udp_port), 1, 65535, "n1mm_udp_port")
    cfg.web_port = _clamp_int(int(cfg.web_port), 1, 65535, "web_port")
    if cfg.audio_mode not in ("tci", "soundcard"):
        logger.warning("Unknown audio mode %r; falling back to soundcard", cfg.audio_mode)
        cfg.audio_mode = "soundcard"
    if cfg.audio_format not in ("wav", "mp3"):
        logger.warning("Unknown audio format %r; falling back to wav", cfg.audio_format)
        cfg.audio_format = "wav"
    if cfg.so2r_mode not in ("stereo", "dual_card"):
        cfg.so2r_mode = "stereo"


def reject_invalid_config(cfg: AppConfig) -> None:
    """Fall back to safe values when the loaded config is self-inconsistent.

    Prior to 0.8.0beta the UI could save ``so2r_mode=dual_card`` with
    ``channels=1`` or without a second soundcard device. The config POST
    endpoint hard-fails on that combination (HTTP 400), which permanently
    blocked saving any settings. Normalize to ``stereo`` on load so those
    legacy files work again.
    """
    if cfg.so2r_mode == "dual_card" and (cfg.channels < 2 or not cfg.soundcard_device2):
        cfg.so2r_mode = "stereo"


# UI schema: (section, field, label, type, choices, help_text)
# When ``choices`` is provided the web UI renders a <select> dropdown.
# When ``type`` is "device" the web UI renders a <select> populated from /api/audio_devices.
CONFIG_SCHEMA = [
    ("general", "station_name", "Station name", "text", None,
     "Your station callsign / identifier shown in the header and used as a label in logs."),
    ("general", "continuous_recording", "Continuous recording", "bool", None,
     "Master switch for continuous recording. When OFF, no continuous chunks are written (QSO slices are still recorded)."),
    ("general", "continuous_autostart", "Continuous recording autostart", "bool", None,
     "When ON, continuous recording starts automatically on app startup."),
    ("general", "continuous_chunk_minutes", "Continuous chunk (min)", "int", None,
     "Length of each continuous recording chunk in minutes."),
    ("general", "normalize_continuous", "Normalize continuous recordings", "bool", None,
     "When ON, continuous WAV chunks are normalized after each chunk. Disable to save CPU on long recordings (QSO slices are always normalized)."),
    ("general", "max_recordings_gb", "Max recordings (GB)", "float", None,
     "Hard cap on continuous recording disk usage. Oldest continuous chunks are deleted automatically when their total exceeds the limit. QSO slices are never deleted. Set 'Unlimited' to never delete recordings automatically."),
    ("tci", "tci_host", "TCI host", "text", None,
     "IP address of the ExpertSDR TCI server (usually 127.0.0.1)."),
    ("tci", "tci_port", "TCI port", "int", None,
     "TCP port of the ExpertSDR TCI server (default 50001)."),
    ("audio", "audio_mode", "Audio mode", "text", ["tci", "soundcard"],
     "'tci' streams from ExpertSDR, 'soundcard' captures a system input device."),
    ("audio", "sample_rate", "Sample rate (Hz)", "int", [8000, 16000, 22050, 44100, 48000, 96000],
     "Audio sample rate. Must match your radio/TCI or soundcard setting. TCI is limited to 48000 Hz; higher values are reduced to 48000 for TCI."),
    ("audio", "channels", "Channels (1=SO1R,2=SO2R)", "int", [1, 2],
     "1 = mono single receiver (SO1R), 2 = stereo two receivers (SO2R)."),
    ("audio", "pre_roll", "Pre-roll (s)", "float", None,
     "Seconds of audio kept BEFORE the N1MM contact timestamp."),
    ("audio", "post_roll", "Post-roll (s)", "float", None,
     "Seconds to wait AFTER the N1MM packet before slicing."),
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
    ("web", "web_host", "Web host", "text", None,
     "Interface the web dashboard binds to. 127.0.0.1 = local only. A changed host or port takes effect after the application is restarted."),
    ("web", "web_port", "Web port", "int", None,
     "TCP port for the web dashboard."),
    ("web", "dashboard_file", "Dashboard file", "text", None,
     "Path to the HTML dashboard file served at the web root."),
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
    "web_host": ("web", "host"),
    "web_port": ("web", "port"),
}


def save_config(cfg: AppConfig, path: str = "config.cfg") -> None:
    """Persist *cfg* atomically to an INI file grouped by section.

    Writes to a temporary file in the same directory and replaces the target
    with ``os.replace`` so a crash or power loss can never leave a half-written
    config behind. The previous file is kept as ``<path>.bak``.
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

    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            parser.write(f)
            f.flush()
            os.fsync(f.fileno())
        if os.path.isfile(target):
            try:
                shutil.copyfile(target, target + ".bak")
            except OSError as exc:
                logger.warning("Could not create config backup: %s", exc)
        os.replace(tmp_path, target)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


if __name__ == "__main__":
    import json
    c = load_config()
    print(json.dumps(c.__dict__, indent=2, default=str))
