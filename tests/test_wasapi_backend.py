"""Tests for the WASAPI capture backend (no real audio hardware required)."""

import sys
import types

import numpy as np
import pytest

from audio_manager import WasapiAudioSource, create_audio_source
from config import AppConfig


class _FakeMic:
    def __init__(self, name, channels=2, loopback=False):
        self.name = name
        self.channels = channels
        self.isloopback = loopback


def _fake_soundcard(mics, default_name):
    default = next((m for m in mics if m.name == default_name),
                   mics[0] if mics else None)

    def _default():
        if default is None:
            raise RuntimeError("no default microphone")
        return default

    return types.SimpleNamespace(
        all_microphones=lambda include_loopback=True: mics,
        default_microphone=_default,
        get_microphone=lambda name, include_loopback=True: next(
            m for m in mics if m.name == name),
    )


def _source(**overrides) -> WasapiAudioSource:
    cfg = AppConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return WasapiAudioSource(cfg)


def test_list_devices_marks_the_default(monkeypatch):
    mics = [_FakeMic("DAX RX 1"), _FakeMic("DAX RX 2"),
            _FakeMic("Speakers", loopback=True)]
    monkeypatch.setitem(sys.modules, "soundcard",
                        _fake_soundcard(mics, "DAX RX 2"))

    devices = WasapiAudioSource.list_devices()

    assert [d["name"] for d in devices] == ["DAX RX 1", "DAX RX 2", "Speakers"]
    assert [d["default_input"] for d in devices] == [False, True, False]
    assert devices[0]["value"] == "0|DAX RX 1"


def test_saved_device_is_resolved_by_name(monkeypatch):
    mics = [_FakeMic("DAX RX 1"), _FakeMic("DAX RX 2")]
    monkeypatch.setitem(sys.modules, "soundcard",
                        _fake_soundcard(mics, "DAX RX 1"))

    mic, index, name = _source(soundcard_device="1|DAX RX 2")._resolve_device()

    assert name == "DAX RX 2"
    assert index == 1
    assert mic.name == "DAX RX 2"


def test_empty_setting_uses_the_windows_default(monkeypatch):
    mics = [_FakeMic("DAX RX 1"), _FakeMic("DAX RX 2")]
    monkeypatch.setitem(sys.modules, "soundcard",
                        _fake_soundcard(mics, "DAX RX 2"))

    _, index, name = _source(soundcard_device="")._resolve_device()

    assert (index, name) == (1, "DAX RX 2")


def test_no_devices_reports_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "soundcard", _fake_soundcard([], ""))

    source = _source(soundcard_device="")
    with pytest.raises((RuntimeError, IndexError, StopIteration)):
        source._resolve_device()


def test_frames_are_routed_to_both_receivers(monkeypatch):
    source = _source(channels=2, continuous_recording=False)
    frames = np.zeros((1024, 2), dtype=np.int16)
    frames[:, 0] = 100
    frames[:, 1] = 200

    source._route_frames(frames, 2)

    assert source.rx1_buf._filled == 1024
    assert source.rx2_buf._filled == 1024


def test_single_channel_is_written_to_the_main_buffer(monkeypatch):
    source = _source(channels=1, continuous_recording=False)
    frames = np.zeros((512, 1), dtype=np.int16)

    source._route_frames(frames, 1)

    assert source.buffer._filled == 512


def test_create_audio_source_selects_the_wasapi_backend():
    cfg = AppConfig()
    cfg.audio_mode = "soundcard"

    assert isinstance(create_audio_source(cfg, backend="wasapi"), WasapiAudioSource)
