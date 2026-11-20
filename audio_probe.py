"""Isolated audio-device probing.

PortAudio (loaded by ``sounddevice``) can kill the whole process on some
systems: on Windows machines with FlexRadio DAX devices ``Pa_Initialize()``
has been observed to corrupt the heap (``0xc0000374``) while enumerating
devices. ``faulthandler`` can only *report* such a crash, not prevent it, so
every interaction with PortAudio that does not have to be in the main process
happens in a short-lived child process instead.

The parent reads a JSON result and stays alive even when PortAudio dies; the
dashboard and the Settings device list keep working, and the log states
plainly that the audio engine could not start.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Optional

from config import BASE_DIR

logger = logging.getLogger("QSOCapture.audio_probe")

PROBE_FLAG = "--audio-probe"
TEST_STREAM_FLAG = "--test-stream"
CACHE_FILENAME = "audio_devices.json"
PROBE_TIMEOUT = 45.0
STREAM_TEST_SECONDS = 0.5

_result: Optional[dict] = None
_result_event = threading.Event()
_started = False
_start_lock = threading.Lock()


# --------------------------------------------------------------------------
# child process
# --------------------------------------------------------------------------

def cache_path() -> str:
    return os.path.join(BASE_DIR, CACHE_FILENAME)


def _bool(value) -> bool:
    return bool(value)


def _device_entries(devices, default_input: int) -> list:
    """Normalise PortAudio devices into the shape the dashboard expects."""
    entries = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = d.get("name", f"Device {i}")
        entries.append({
            "index": i,
            "name": name,
            "value": f"{i}|{name}",
            "hostapi": d.get("hostapi", -1),
            "max_input_channels": d.get("max_input_channels", 0),
            "default_samplerate": d.get("default_samplerate", 0),
            "default_input": i == default_input,
        })
    return entries


def _stream_test(sd, entries: list) -> dict:
    """Open the chosen input for a fraction of a second to validate capture."""
    candidates = [d for d in entries if d.get("default_input")] or entries
    if not candidates:
        return {"ok": False, "error": "no input device to test"}
    device = candidates[0]
    channels = max(1, min(2, int(device.get("max_input_channels") or 1)))
    rate = int(device.get("default_samplerate") or 48000)
    frames = max(256, int(rate * STREAM_TEST_SECONDS))
    try:
        stream = sd.InputStream(device=device["index"], channels=channels,
                                samplerate=rate, dtype="float32", blocksize=2048)
        with stream:
            stream.read(frames)
        return {"ok": True, "device": device["name"], "channels": channels,
                "samplerate": rate}
    except Exception as exc:
        return {"ok": False, "device": device.get("name", ""),
                "error": f"{type(exc).__name__}: {exc}"}


def run_probe(test_stream: bool = False) -> dict:
    """Collect host APIs and input devices. Runs inside the child process."""
    started = time.time()
    result: dict = {"ok": False, "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                            time.gmtime())}
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        try:
            pair = sd.default.device
            default_in = int(pair[0]) if pair is not None else -1
        except Exception:
            default_in = -1

        entries = _device_entries(devices, default_in)
        result.update({
            "ok": True,
            "python": sys.version.split()[0],
            "hostapis": [{"index": i, "name": h.get("name", ""),
                          "devices": len(h.get("devices", []) or [])}
                         for i, h in enumerate(hostapis)],
            "default_input": default_in,
            "devices": entries,
        })
        if test_stream:
            result["stream_test"] = _stream_test(sd, entries)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["duration_s"] = round(time.time() - started, 2)
    return result


def main(argv: Optional[list] = None) -> int:
    """Entry point for the child process (``QSOCapture.exe --audio-probe``)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    result = run_probe(test_stream=TEST_STREAM_FLAG in argv)
    try:
        with open(cache_path(), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError:
        pass
    if PROBE_FLAG in argv:
        print(json.dumps(result))
        sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------
# parent process
# --------------------------------------------------------------------------

def probe_command() -> list:
    """Command line that re-runs this program in probe mode."""
    frozen = bool(getattr(sys, "frozen", False)
                  or "__compiled__" in globals()
                  or hasattr(sys, "_MEIPASS"))
    if frozen:
        return [sys.executable, PROBE_FLAG, TEST_STREAM_FLAG]
    return [sys.executable, os.path.abspath(__file__), PROBE_FLAG, TEST_STREAM_FLAG]


def _no_window_kwargs() -> dict:
    if sys.platform != "win32":
        return {}
    flags = 0
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flags}


def _crash_result(returncode: int, stderr: str) -> dict:
    code = returncode & 0xFFFFFFFF if returncode is not None else 0
    message = (f"the audio library crashed during initialisation "
               f"(exit code 0x{code:08X})")
    if code == 0xC0000374:
        message += " - heap corruption inside PortAudio"
    return {
        "ok": False,
        "error": message,
        "returncode": code,
        "stderr": (stderr or "")[-2000:],
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def run_probe_subprocess(timeout: float = PROBE_TIMEOUT) -> dict:
    """Run the probe in a child process and return (and cache) its result."""
    cmd = probe_command()
    logger.info("Probing audio devices in a separate process%s",
                f" (timeout {timeout:.0f}s)" if timeout else "")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=True, **_no_window_kwargs())
    except subprocess.TimeoutExpired:
        result = {"ok": False,
                  "error": f"the audio probe timed out after {timeout:.0f}s",
                  "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except Exception as exc:
        result = {"ok": False,
                  "error": f"could not start the audio probe: {exc}",
                  "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    else:
        if proc.returncode != 0:
            result = _crash_result(proc.returncode, proc.stderr)
        else:
            result = _parse_stdout(proc.stdout, proc.stderr)
            if not result.get("ok") and "no result" in result.get("error", ""):
                # A windowed EXE may have no usable stdout pipe; the child
                # writes the same JSON to the cache before printing.
                cached = read_cache()
                if cached and cached.get("ok"):
                    result = cached

    _write_cache(result)
    _log_result(result)
    return result


def _parse_stdout(stdout: str, stderr: str) -> dict:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"ok": False,
            "error": "the audio probe returned no result",
            "stderr": (stderr or "")[-2000:],
            "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _write_cache(result: dict) -> None:
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(cache_path(), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except OSError as exc:
        logger.debug("could not cache the audio probe result: %s", exc)


def _log_result(result: dict) -> None:
    if not result.get("ok"):
        logger.error("Audio probe failed: %s", result.get("error", "unknown error"))
        stderr = (result.get("stderr") or "").strip()
        if stderr:
            logger.error("Audio probe output: %s", stderr[-800:])
        return
    apis = ", ".join(f"{h['name']}({h['devices']})"
                     for h in result.get("hostapis", [])) or "none"
    logger.info("Audio probe: host APIs: %s", apis)
    devices = result.get("devices", [])
    if not devices:
        logger.warning("Audio probe found no input devices")
    else:
        summary = "; ".join(
            f"{d['index']}={d['name']}{' (default)' if d.get('default_input') else ''}"
            for d in devices[:15])
        if len(devices) > 15:
            summary += f"; ... ({len(devices) - 15} more)"
        logger.info("Audio probe inputs (%d): %s", len(devices), summary)
    test = result.get("stream_test")
    if test:
        if test.get("ok"):
            logger.info("Audio probe capture test OK (device=%r %s Hz %s ch)",
                        test.get("device"), test.get("samplerate"),
                        test.get("channels"))
        else:
            logger.warning("Audio probe capture test failed: %s", test.get("error"))


def read_cache() -> Optional[dict]:
    try:
        with open(cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def start_probe(timeout: float = PROBE_TIMEOUT) -> None:
    """Start the probe in a background thread (idempotent)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def _run() -> None:
        global _result
        _result = run_probe_subprocess(timeout)
        _result_event.set()

    threading.Thread(target=_run, daemon=True, name="audio-probe").start()


def ensure_probe(timeout: float = PROBE_TIMEOUT + 10.0) -> Optional[dict]:
    """Return the probe result, starting (and waiting for) the probe if needed."""
    start_probe()
    _result_event.wait(timeout)
    if _result_event.is_set():
        return _result
    return None


def reset_for_tests() -> None:
    """Clear the cached in-process state (test helper)."""
    global _result, _started
    with _start_lock:
        _result = None
        _started = False
    _result_event.clear()


if __name__ == "__main__":
    sys.exit(main([PROBE_FLAG, *sys.argv[1:]]))
