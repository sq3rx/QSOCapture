"""Tests for the build-time package-data check."""

import build_nuitka


def test_missing_dist_dir_reports_everything(tmp_path):
    missing = build_nuitka.verify_package_data(str(tmp_path / "nope"))
    assert missing == list(build_nuitka.REQUIRED_PACKAGE_DATA)


def test_missing_header_is_detected(tmp_path):
    (tmp_path / "soundcard").mkdir()
    (tmp_path / "soundcard" / "mediafoundation.py").write_text("", encoding="utf-8")

    missing = build_nuitka.verify_package_data(str(tmp_path))

    assert "mediafoundation.py.h" in missing


def test_present_header_passes(tmp_path):
    (tmp_path / "soundcard").mkdir()
    (tmp_path / "soundcard" / "mediafoundation.py.h").write_text("", encoding="utf-8")

    assert build_nuitka.verify_package_data(str(tmp_path)) == []


def test_header_is_found_in_nested_directories(tmp_path):
    nested = tmp_path / "soundcard" / "nested"
    nested.mkdir(parents=True)
    (nested / "mediafoundation.py.h").write_text("", encoding="utf-8")

    assert build_nuitka.verify_package_data(str(tmp_path)) == []


def test_build_command_includes_package_data(monkeypatch):
    """The build must pass --include-package-data=soundcard to Nuitka."""
    captured = {}

    class _Result:
        returncode = 0

    def _fake_run(cmd, check=False):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(build_nuitka.subprocess, "run", _fake_run)
    monkeypatch.setattr(build_nuitka, "verify_package_data", lambda *a, **k: [])
    monkeypatch.setattr("sys.argv", ["build_nuitka.py"])

    build_nuitka.main()

    assert "--include-package-data=soundcard" in captured["cmd"]
    assert "--include-package=soundcard" in captured["cmd"]
