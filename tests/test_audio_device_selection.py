"""Tests for soundcard device selection (mocked sounddevice, no real hardware)."""

import sys
import types

import pytest

from audio_manager import SoundcardAudioSource
from config import AppConfig


def _dev(name, inputs=0, outputs=0):
    return {"name": name, "max_input_channels": inputs, "max_output_channels": outputs}


def _install(monkeypatch, devices, default_device):
    fake = types.SimpleNamespace(
        query_devices=lambda: devices,
        default=types.SimpleNamespace(device=default_device),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)


def _source(**overrides) -> SoundcardAudioSource:
    cfg = AppConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return SoundcardAudioSource(cfg)


def test_empty_setting_prefers_windows_default(monkeypatch):
    devices = [_dev("Speakers", outputs=2), _dev("Line In", inputs=2), _dev("Microphone", inputs=1)]
    _install(monkeypatch, devices, (1, 0))

    assert _source(soundcard_device="")._resolve_device() == 1


def test_empty_setting_falls_back_to_first_input_without_default(monkeypatch):
    devices = [_dev("Speakers", outputs=2), _dev("Line In", inputs=2)]
    _install(monkeypatch, devices, (-1, -1))

    assert _source(soundcard_device="")._resolve_device() == 1


def test_no_input_devices_reports_actionable_error(monkeypatch):
    devices = [_dev("Speakers", outputs=2)]
    _install(monkeypatch, devices, (-1, -1))

    source = _source(soundcard_device="")
    with pytest.raises(RuntimeError, match="No audio input device"):
        source._resolve_device()
    assert "Line In" in source._last_error


def test_explicit_index_is_used(monkeypatch):
    devices = [_dev("Line In", inputs=2), _dev("USB Audio", inputs=1)]
    _install(monkeypatch, devices, (0, -1))

    assert _source(soundcard_device="1|USB Audio")._resolve_device() == 1


def test_legacy_name_is_matched(monkeypatch):
    devices = [_dev("Line In", inputs=2), _dev("USB Audio", inputs=1)]
    _install(monkeypatch, devices, (0, -1))

    assert _source(soundcard_device="USB Audio")._resolve_device() == 1


def test_missing_saved_device_falls_back_to_first_input(monkeypatch):
    devices = [_dev("Speakers", outputs=2), _dev("Line In", inputs=2)]
    _install(monkeypatch, devices, (-1, -1))

    assert _source(soundcard_device="9|Unplugged USB")._resolve_device() == 1
