"""Tests for the out-of-process audio probe (no PortAudio / hardware needed)."""

import json
import subprocess

import pytest

import audio_probe


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    audio_probe.reset_for_tests()
    monkeypatch.setattr(audio_probe, "BASE_DIR", str(tmp_path))
    yield
    audio_probe.reset_for_tests()


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_cache_roundtrip():
    assert audio_probe.read_cache() is None

    audio_probe._write_cache({"ok": True, "devices": [{"index": 0, "name": "DAX RX 1"}]})
    cached = audio_probe.read_cache()

    assert cached["ok"] is True
    assert cached["devices"][0]["name"] == "DAX RX 1"


def test_cache_ignores_broken_json():
    with open(audio_probe.cache_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert audio_probe.read_cache() is None


def test_probe_success_is_parsed_and_cached(monkeypatch):
    payload = {
        "ok": True,
        "devices": [{"index": 3, "name": "DAX RX 1", "default_input": True}],
        "hostapis": [{"index": 0, "name": "Windows WASAPI", "devices": 4}],
    }
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Completed(stdout=json.dumps(payload)))

    result = audio_probe.run_probe_subprocess(timeout=5)

    assert result["ok"] is True
    assert audio_probe.read_cache()["devices"][0]["name"] == "DAX RX 1"


def test_probe_crash_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Completed(returncode=0xC0000374, stderr="heap"))

    result = audio_probe.run_probe_subprocess(timeout=5)

    assert result["ok"] is False
    assert "0xC0000374" in result["error"]
    assert "heap corruption" in result["error"]
    assert audio_probe.read_cache()["ok"] is False


def test_probe_timeout_is_reported(monkeypatch):
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)

    result = audio_probe.run_probe_subprocess(timeout=1)

    assert result["ok"] is False
    assert "timed out" in result["error"]


def test_probe_without_json_output(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Completed(stdout="", stderr="boom"))

    result = audio_probe.run_probe_subprocess(timeout=5)

    assert result["ok"] is False
    assert "no result" in result["error"]


def test_probe_command_points_at_this_program():
    cmd = audio_probe.probe_command()
    assert audio_probe.PROBE_FLAG in cmd or any(
        part.endswith("audio_probe.py") for part in cmd)


def test_main_writes_cache(monkeypatch, capsys):
    monkeypatch.setattr(audio_probe, "run_probe",
                        lambda test_stream=False: {"ok": True, "devices": []})

    code = audio_probe.main([audio_probe.PROBE_FLAG, audio_probe.TEST_STREAM_FLAG])
    out = capsys.readouterr().out

    assert code == 0
    assert json.loads(out.strip().splitlines()[-1])["ok"] is True
    assert audio_probe.read_cache()["ok"] is True
