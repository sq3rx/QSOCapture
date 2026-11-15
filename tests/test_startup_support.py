"""Tests for the Qt-free launcher helpers (startup markers and flags)."""

import os
from pathlib import Path

import pytest

import startup_support


def test_marker_lifecycle(tmp_path):
    app_dir = str(tmp_path)

    assert not startup_support.launch_pending(app_dir)
    assert not os.path.exists(startup_support.launch_marker_path(app_dir))

    startup_support.mark_launch_pending(app_dir)
    assert startup_support.launch_pending(app_dir)
    content = Path(startup_support.launch_marker_path(app_dir)).read_text(encoding="utf-8")
    assert "pid=" in content

    startup_support.clear_launch_marker(app_dir)
    assert not startup_support.launch_pending(app_dir)


def test_clear_marker_is_idempotent(tmp_path):
    startup_support.clear_launch_marker(str(tmp_path))
    assert not startup_support.launch_pending(str(tmp_path))


def test_mark_creates_missing_directory(tmp_path):
    app_dir = str(tmp_path / "nested" / "QSOCapture")
    startup_support.mark_launch_pending(app_dir)
    assert os.path.isfile(startup_support.launch_marker_path(app_dir))


def test_build_flags_adds_base_flags_once():
    flags = startup_support.build_chromium_flags("", safe_mode=False)

    for flag in startup_support.BASE_CHROMIUM_FLAGS:
        assert flags.split().count(flag) == 1
    for flag in startup_support.SAFE_MODE_CHROMIUM_FLAGS:
        assert flag not in flags.split()


def test_build_flags_does_not_duplicate_existing():
    existing = " ".join(startup_support.BASE_CHROMIUM_FLAGS)
    flags = startup_support.build_chromium_flags(existing, safe_mode=False)
    assert flags == existing


def test_build_flags_safe_mode():
    flags = startup_support.build_chromium_flags("--user-flag", safe_mode=True).split()

    assert "--user-flag" in flags
    for flag in startup_support.SAFE_MODE_CHROMIUM_FLAGS:
        assert flags.count(flag) == 1


def test_safe_mode_requested_by_marker(tmp_path):
    app_dir = str(tmp_path)
    assert not startup_support.safe_mode_requested(app_dir)

    startup_support.mark_launch_pending(app_dir)
    assert startup_support.safe_mode_requested(app_dir)


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_safe_mode_requested_by_env(tmp_path, monkeypatch, value):
    monkeypatch.setenv(startup_support.SAFE_MODE_ENV, value)
    assert startup_support.safe_mode_requested(str(tmp_path))


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_safe_mode_not_requested_by_env(tmp_path, monkeypatch, value):
    monkeypatch.setenv(startup_support.SAFE_MODE_ENV, value)
    assert not startup_support.safe_mode_requested(str(tmp_path))


def test_audio_disabled(monkeypatch):
    monkeypatch.delenv(startup_support.DISABLE_AUDIO_ENV, raising=False)
    assert not startup_support.audio_disabled()

    monkeypatch.setenv(startup_support.DISABLE_AUDIO_ENV, "1")
    assert startup_support.audio_disabled()
