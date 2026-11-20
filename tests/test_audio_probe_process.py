"""End-to-end check: a crashing audio probe must not take the app down."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

CRASH_SCRIPT = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, os.getcwd())
    import audio_probe
    # Emulate the Windows heap-corruption crash of Pa_Initialize.
    print(json.dumps({"ok": False, "error": "crashed"}))
    sys.exit(0xC0000374)
    """
)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows exit-code semantics")
def test_crashing_probe_is_reported_without_killing_the_parent(tmp_path, monkeypatch):
    import audio_probe

    script = tmp_path / "crash_probe.py"
    script.write_text(CRASH_SCRIPT, encoding="utf-8")

    monkeypatch.setattr(audio_probe, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(audio_probe, "probe_command",
                        lambda: [sys.executable, str(script)])

    result = audio_probe.run_probe_subprocess(timeout=30)

    assert result["ok"] is False
    assert "0xC0000374" in result["error"]
    cached = json.loads((tmp_path / audio_probe.CACHE_FILENAME).read_text(encoding="utf-8"))
    assert cached["ok"] is False


def test_real_probe_runs_in_a_child_process(tmp_path, monkeypatch):
    """The probe script must be importable on its own and produce JSON."""
    import audio_probe

    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(tmp_path)
    cmd = [sys.executable, os.path.abspath(audio_probe.__file__),
           audio_probe.TEST_STREAM_FLAG]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "ok" in payload
    if payload["ok"]:
        assert isinstance(payload.get("devices"), list)
        assert isinstance(payload.get("hostapis"), list)
