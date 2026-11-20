"""Tests for the audio-mode aware device list and the guarded audio restart."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

import config as config_module
import main as main_module


def _devices_payload(response) -> dict:
    return json.loads(response.body)


def test_tci_mode_explains_that_devices_are_soundcard_only():
    main_module.cfg = config_module.AppConfig()
    main_module.cfg.audio_mode = "tci"

    payload = _devices_payload(main_module.api_audio_devices())

    assert payload["applicable"] is False
    assert payload["devices"] == []
    # The old text sent users to restart the app; it must point at the mode.
    assert "soundcard" in payload["hint"].lower()
    assert "no restart needed" in payload["hint"].lower()


def test_soundcard_mode_without_probe_asks_for_a_restart():
    main_module.cfg = config_module.AppConfig()
    main_module.cfg.audio_mode = "soundcard"

    with mock.patch.object(main_module.audio_probe, "read_cache", return_value=None):
        payload = _devices_payload(main_module.api_audio_devices())

    assert payload["applicable"] is True
    assert "Restart" in payload["error"]


def test_soundcard_mode_returns_probed_devices():
    main_module.cfg = config_module.AppConfig()
    main_module.cfg.audio_mode = "soundcard"
    probe = {"ok": True, "backend": "wasapi",
             "devices": [{"index": 1, "name": "DAX RX 1", "value": "1|DAX RX 1"}],
             "hostapis": []}

    with mock.patch.object(main_module.audio_probe, "read_cache", return_value=probe):
        payload = _devices_payload(main_module.api_audio_devices())

    assert payload["applicable"] is True
    assert payload["devices"][0]["name"] == "DAX RX 1"
    assert payload["backend"] == "wasapi"


def test_settings_restart_uses_the_probe_gated_path():
    """Switching TCI -> soundcard must not load PortAudio unguarded."""
    main_module.cfg = config_module.AppConfig()
    main_module.cfg.audio_mode = "soundcard"
    fake_source = mock.MagicMock()

    with mock.patch.object(main_module, "audio_source", None), \
            mock.patch.object(main_module, "_start_audio_source",
                              return_value=fake_source) as guarded, \
            mock.patch.object(main_module, "create_audio_source") as unguarded, \
            mock.patch.object(main_module, "n1mm", None):
        main_module._apply_and_restart(restart_audio=True, restart_n1mm=False)
        assert main_module.audio_source is fake_source

    guarded.assert_called_once()
    unguarded.assert_not_called()


def test_start_audio_source_uses_the_probed_backend():
    cfg = config_module.AppConfig()
    cfg.audio_mode = "soundcard"
    fake_source = mock.MagicMock()
    probe = {"ok": True, "backend": "wasapi"}

    with mock.patch.object(main_module.audio_probe, "ensure_probe",
                           return_value=probe), \
            mock.patch.object(main_module, "create_audio_source",
                              return_value=fake_source) as factory:
        source = main_module._start_audio_source(cfg)

    assert source is fake_source
    assert factory.call_args.kwargs["backend"] == "wasapi"
    assert main_module.audio_backend == "wasapi"


def test_start_audio_source_refuses_when_the_probe_failed():
    cfg = config_module.AppConfig()
    cfg.audio_mode = "soundcard"
    probe = {"ok": False, "error": "heap corruption inside PortAudio"}

    with mock.patch.object(main_module.audio_probe, "ensure_probe",
                           return_value=probe), \
            mock.patch.object(main_module, "create_audio_source") as factory:
        source = main_module._start_audio_source(cfg)

    assert source is None
    assert factory.called is False
    assert "heap corruption" in main_module.audio_probe_error
    assert main_module.audio_backend == ""
