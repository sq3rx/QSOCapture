"""audio_manager.py - Thread-safe audio capture and circular buffering."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import threading
import wave
import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger("QSOCapture.audio")


def _emit_event(name: str, data: Optional[dict] = None) -> None:
    """Emit a dashboard event to the web layer (best-effort, lazy import).

    Uses a lazy import of :func:`main.push_event` so importing
    :mod:`audio_manager` does not create a circular import with :mod:`main`
    (which imports this module at module load). If the web layer is not
    running (e.g. unit tests) the call is silently ignored.
    """
    try:
        from main import push_event
        push_event(name, data)
    except Exception:
        pass


class CircularAudioBuffer:
    """Fixed-capacity ring buffer for raw int16 PCM frames."""

    def __init__(self, sample_rate: int, channels: int, capacity_seconds: float):
        self.sample_rate = sample_rate
        self.channels = channels
        self.capacity_seconds = capacity_seconds
        self.capacity_frames = int(sample_rate * capacity_seconds)
        self._data = np.zeros((self.capacity_frames, channels), dtype=np.int16)
        self._write_idx = 0
        self._filled = 0
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    def write(self, frames: np.ndarray) -> None:
        if frames.size == 0:
            return
        n = frames.shape[0]
        with self._lock:
            idx = self._write_idx
            cap = self.capacity_frames
            if idx + n <= cap:
                self._data[idx:idx + n] = frames
            else:
                first = cap - idx
                self._data[idx:cap] = frames[:first]
                remainder = n - first
                self._data[0:remainder] = frames[first:]
            self._write_idx = (idx + n) % cap
            self._filled = min(self._filled + n, cap)

    def get_slice(self, start_offset: float, end_offset: float) -> Optional[np.ndarray]:
        with self._lock:
            filled = self._filled
            if filled == 0:
                return None
            total = self.capacity_frames
            start_back = int(start_offset * self.sample_rate)
            end_back = int(end_offset * self.sample_rate)
            if start_back > filled:
                start_back = filled
            if end_back < 0:
                end_back = 0
            if start_back <= end_back:
                return np.zeros((0, self.channels), dtype=np.int16)
            head = self._write_idx
            oldest = (head - filled) % total
            start_pos = (head - start_back) % total
            end_pos = (head - end_back) % total
            if start_pos < end_pos:
                out = self._data[start_pos:end_pos].copy()
            else:
                part1 = self._data[start_pos:total]
                part2 = self._data[0:end_pos]
                out = np.concatenate([part1, part2], axis=0)
            return out

    def snapshot_all(self) -> np.ndarray:
        with self._lock:
            if self._filled == 0:
                return np.zeros((0, self.channels), dtype=np.int16)
            total = self.capacity_frames
            head = self._write_idx
            oldest = (head - self._filled) % total
            if oldest + self._filled <= total:
                return self._data[oldest:oldest + self._filled].copy()
            part1 = self._data[oldest:total]
            part2 = self._data[0:(head % total)]
            return np.concatenate([part1, part2], axis=0)


@dataclass
class QSORequest:
    call: str
    band: str
    mode: str
    contest: str
    timestamp: float          # epoch seconds of the QSO time (from N1MM)
    pre_roll: float
    post_roll: float
    # epoch seconds when the contact packet was received. This drives the
    # slicing offset against the live audio buffer (the buffer only contains
    # audio captured after the packet arrived).
    receive_ts: float = 0.0
    # Rich metadata forwarded from N1MM (persisted to the DB so the
    # dashboard can show frequency / exchange / name / QTH etc.).
    freq: str = ""
    name: str = ""
    qth: str = ""
    grid: str = ""
    comment: str = ""
    exchange: str = ""
    exchange2: str = ""
    exchange3: str = ""
    rcv: str = ""
    snt: str = ""
    rcvnr: str = ""
    sntnr: str = ""
    section: str = ""
    mycall: str = ""
    countryprefix: str = ""
    wpxprefix: str = ""
    continent: str = ""
    operator: str = ""
    station: str = ""
    contest_nr: str = ""
    points: str = ""
    multiplier: str = ""
    multiplier2: str = ""
    multiplier3: str = ""
    prec: str = ""
    ck: str = ""
    power: str = ""
    n1mm_id: str = ""
    is_claimed: str = ""
    sent_exchange: str = ""
    radio_nr: str = "1"        # Radio number (1 or 2) from N1MM <RadioNr> (SO2R)
    raw_ts: str = ""


def _write_pcm(path: str, frames: np.ndarray, sample_rate: int, channels: int,
               fmt: str = "wav") -> None:
    """Write int16 frames to a WAV or MP3 file at path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Force C-contiguous layout so tobytes() never depends on the strides of a
    # column slice (e.g. pcm[:, 1:2]) from a stereo buffer. .astype already
    # yields a contiguous array; this is a defensive guarantee.
    pcm = np.ascontiguousarray(frames, dtype=np.int16)
    if fmt == "mp3":
        try:
            import lameenc
        except ImportError:
            logger.warning("lameenc not installed, falling back to WAV for %s", path)
            path = os.path.splitext(path)[0] + ".wav"
            fmt = "wav"
    if fmt == "mp3":
        enc = lameenc.Encoder()
        enc.set_bit_rate(128)
        enc.set_in_sample_rate(sample_rate)
        enc.set_channels(channels)
        enc.set_quality(2)
        data = pcm.tobytes()
        mp3 = enc.encode(data) + enc.flush()
        with open(path, "wb") as f:
            f.write(mp3)
    else:
        with wave.open(path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())


def write_pcm_to_wav(path: str, frames: np.ndarray, sample_rate: int, channels: int) -> None:
    """Write int16 frames to a 16-bit PCM WAV file (legacy alias)."""
    _write_pcm(path, frames, sample_rate, channels, fmt="wav")


# --- Audio normalization (hardcoded, always-on, no UI) -------------------
# Goal: bring every saved file to a consistent perceived loudness so the
# user is not jumping between very quiet and very loud recordings. We target
# a fixed RMS level (ReplayGain-style) but cap the applied gain so we never
# amplify near-silent / noisy audio into a roar, and hard-clip the peaks just
# below full scale to avoid distortion.
NORM_TARGET_DBFS = -24.0       # desired RMS level (quieter than -18 to avoid loud files)
NORM_MAX_GAIN_DB = 18.0        # upper bound on applied gain (protects quiet noise)
NORM_PEAK_CEIL_DB = -6.0       # peak ceiling, anti-clipping safety margin


def _normalize_frames(frames: np.ndarray):
    """Normalize int16 PCM frames to a consistent level.

    Returns a tuple ``(frames, gain_db)`` where ``gain_db`` is the applied
    gain in decibels (negative when the audio was attenuated, positive when
    boosted). The algorithm:
      1. compute RMS of the float signal in [-1, 1]
      2. desired gain = target_rms / rms, capped at ``NORM_MAX_GAIN_DB``
      3. apply gain, then if any peak exceeds ``NORM_PEAK_CEIL_DB`` scale the
         whole buffer down so the loudest sample sits exactly at the ceiling
    A silent buffer (rms == 0) is returned untouched with a gain of 0.0 dB.
    """
    if frames is None or frames.size == 0:
        return frames, 0.0
    x = frames.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 0.0:
        return frames, 0.0
    target_lin = 10.0 ** (NORM_TARGET_DBFS / 20.0)
    max_gain = 10.0 ** (NORM_MAX_GAIN_DB / 20.0)
    gain_lin = min(target_lin / rms, max_gain)
    x *= gain_lin
    ceil = 10.0 ** (NORM_PEAK_CEIL_DB / 20.0)
    peak = float(np.max(np.abs(x)))
    if peak > ceil:
        x *= ceil / peak
    gain_db = 20.0 * math.log10(gain_lin) if gain_lin > 0 else 0.0
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16), round(gain_db, 1)


def _normalize_wav_inplace(path: str) -> Optional[float]:
    """Read a WAV file, normalize its samples, and overwrite it in place.

    Used for continuous WAV chunks that are written streaming and can only be
    normalized once the whole chunk is closed. A failure is silently ignored
    (the original file is left intact) so recording never breaks on a glitch.
    Returns the applied gain in dB, or ``None`` if normalization did not run.
    """
    try:
        with wave.open(path, "rb") as wf:
            ch = wf.getnchannels()
            sr = wf.getframerate()
            width = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if width != 2:
            return None
        pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)
        pcm, gain_db = _normalize_frames(pcm)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(ch)
            wf.setsampwidth(width)
            wf.setframerate(sr)
            wf.writeframes(pcm.tobytes())
        return gain_db
    except Exception as e:
        logger.warning("WAV normalization failed for %s: %s", path, e)
        return None


class AudioSource(ABC):
    """Common interface for all audio capture backends."""

    def __init__(self, cfg, label: str = "RX1"):
        self.cfg = cfg
        self.label = label
        cap = max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0)
        self.buffer = CircularAudioBuffer(cfg.sample_rate, cfg.channels, cap)
        # List of (label, buffer) pairs. For SO2R there are two receivers
        # (RX1 = left channel, RX2 = right channel); otherwise just RX1.
        self.rx_buffers = [('RX1', self.buffer)]
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._continuous: Optional[threading.Thread] = None
        # Continuous-recording writer state (queue-fed from the capture loop).
        # The queue is *bounded* so a slow disk / encoder can never make the
        # capture callback block indefinitely (which would stall live audio).
        # When the queue is full the oldest pending chunk is dropped so we
        # always keep the freshest audio.
        self._cont_queue: "queue.Queue" = queue.Queue(maxsize=1600)
        self._cont_files: dict = {}
        self._cont_lock = threading.Lock()
        self._cont_start = 0.0
        # Total number of continuous audio chunks dropped because the queue
        # was full. Exposed via get_status() so the dashboard can warn the
        # operator.
        self._cont_dropped = 0
        # Dedicated encoder worker: MP3 encoding of finalized chunks happens
        # here (off the writer thread) so a long encode never blocks draining
        # of the continuous queue during multi-hour sessions.
        self._enc_queue: "queue.Queue" = queue.Queue()
        self._enc_thread: Optional[threading.Thread] = None
        self._enc_started = False
        # True while the encoder thread is actively encoding a chunk (so the
        # drain helper can wait until the DB row is fully migrated to .mp3).
        self._enc_busy = False
        # When True the continuous writer stops accepting audio and keeps the
        # current chunk finalised (used by the dashboard "stop recording" btn).
        self._continuous_paused = False

    @abstractmethod
    def _capture_loop(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Continuous *feature* is always enabled; whether it begins recording
        # immediately is controlled by ``continuous_autostart``. When False we
        # start the writer thread paused (buffers still fill) so it can be
        # resumed on demand from the dashboard.
        self._continuous_paused = not getattr(self.cfg, "continuous_autostart", True)
        self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                        name=f"capture-{self.label}")
        self._thread.start()
        if self.cfg.continuous_recording:
            # Dedicated MP3 encoder worker so a (potentially slow) encode of a
            # finalized chunk never blocks the writer thread that drains the
            # live audio queue.
            self._ensure_encoder()
            self._continuous = threading.Thread(target=self._continuous_loop,
                                                daemon=True, name=f"cont-{self.label}")
            self._continuous.start()
        rxs = ",".join(lbl for lbl, _ in self.rx_buffers)
        for label, _buf in self.rx_buffers:
            logger.info("[%s] audio source started (mode=%s, rx=%s, so2r=%s)",
                        label, self.cfg.audio_mode, rxs, self.cfg.channels >= 2)

    def _ensure_encoder(self) -> None:
        """Start the MP3 encoder worker thread once (idempotent)."""
        if self._enc_started:
            return
        self._enc_started = True
        self._enc_thread = threading.Thread(target=self._encoder_loop,
                                            daemon=True, name=f"enc-{self.label}")
        self._enc_thread.start()

    def _encoder_loop(self) -> None:
        """Off-thread MP3 encoder: pulls finalized WAV chunks from ``_enc_queue``
        and encodes them to MP3 without blocking the continuous writer thread.
        """
        while self._running or not self._enc_queue.empty():
            try:
                item = self._enc_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._enc_busy = True
            try:
                rx_label, wav_path, sample_rate, db_old_rel, duration = item
                try:
                    mp3_path = self._encode_mp3(wav_path, sample_rate,
                                                normalize=self.cfg.normalize_continuous)
                    new_rel = "_continuous/" + os.path.basename(mp3_path)
                    try:
                        import db as qso_db
                        qso_db.update_qso_file_path(db_old_rel, new_rel)
                        qso_db.update_qso_duration(new_rel, duration)
                    except Exception as e:
                        logger.debug("encoder db update failed: %s", e)
                except Exception as e:
                    logger.warning("[%s] MP3 encode failed for %s: %s",
                                   rx_label, wav_path, e)
            finally:
                self._enc_busy = False

    def stop(self) -> None:
        """Stop capture and clean up threads promptly (no shutdown hang)."""
        self._running = False
        # For soundcard, abort the PortAudio stream so the blocking `with`
        # context manager in the capture loop exits immediately.
        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        # For TCI, cancel the running asyncio task(s) so the websocket
        # connection (and its internal keepalive task) is closed gracefully
        # via the `async with` context manager instead of being left pending
        # when the loop is closed. Calling loop.stop() here left the
        # keepalive() task pending and produced
        # "Task was destroyed but it is pending!" / "Event loop is closed".
        loop = getattr(self, "_loop", None)
        if loop is not None and not loop.is_closed():
            def _cancel_tasks() -> None:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
            try:
                loop.call_soon_threadsafe(_cancel_tasks)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._continuous:
            self._continuous.join(timeout=2.0)
        if self._enc_thread:
            self._enc_thread.join(timeout=2.0)

    # -- continuous recording pause / resume (dashboard control) ----------
    def _drain_encoder(self, timeout: float = 30.0) -> None:
        """Block until the MP3 encoder queue is empty and all pending chunks
        have been finalised and registered in the DB.

        Called after a chunk is closed so the dashboard (which refreshes
        immediately on Stop) sees the fully migrated ``.mp3`` row with its
        duration already set, instead of a transient ``.wav`` row or nothing
        at all. We wait for both the queue to drain *and* the encoder thread
        to finish its current encode (``_enc_busy``), because the DB row is
        only migrated to ``.mp3`` once the encode + DB update completes.
        """
        if not self.cfg.continuous_recording or self.cfg.audio_format != "mp3":
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._enc_queue.empty() and not self._enc_busy:
                # Give the encoder loop one last chance to pull a freshly
                # queued item; if the queue stays empty and the encoder is
                # idle we are done.
                time.sleep(0.05)
                if self._enc_queue.empty() and not self._enc_busy:
                    return
            time.sleep(0.05)

    def pause_continuous(self) -> None:
        """Finalise the current continuous chunk and stop writing new audio.

        The open chunk is closed (and converted to MP3 if configured) so a
        complete, playable file remains. The writer thread stays alive but
        discards queued audio until :meth:`resume_continuous` is called. The
        in-memory ring buffers are cleared so no stale audio survives the
        pause and the next recording starts fresh.
        """
        if not self.cfg.continuous_recording or self._continuous_paused:
            return
        self._continuous_paused = True
        _emit_event("continuous_paused")
        self._close_cont_files()
        # Wait for the encoder to finish so the DB row is fully migrated to
        # .mp3 (with its duration) before the dashboard refreshes. Without
        # this the UI could show a transient .wav row or nothing at all until
        # the next app restart re-derived the duration.
        self._drain_encoder()
        self._clear_buffers()
        for label, _buf in self.rx_buffers:
            logger.info("[%s] continuous recording paused (chunk finalised, buffers cleared)",
                        label)

    def _rx_label_str(self) -> str:
        """Comma-joined RX labels, e.g. 'RX1' or 'RX1,RX2' (SO2R)."""
        return ",".join(lbl for lbl, _ in self.rx_buffers)

    def _clear_buffers(self) -> None:
        """Reset every ring buffer so stale audio is dropped on pause/resume."""
        for _label, buf in self.rx_buffers:
            with buf._lock:
                buf._write_idx = 0
                buf._filled = 0

    def resume_continuous(self) -> None:
        """Re-open a fresh continuous chunk and resume writing."""
        if not self.cfg.continuous_recording or not self._continuous_paused:
            return
        if not self.is_connected():
            # In TCI mode there is no audio source until the radio is linked.
            # Refuse to start recording so we don't create empty/gap files
            # against a disconnected transceiver. The dashboard "Record"
            # button should be disabled when TCI is not connected.
            logger.warning("[%s] cannot resume continuous recording: audio source "
                           "not connected (TCI not linked)", self.label)
            return
        self._open_cont_files()
        self._cont_start = time.time()
        self._continuous_paused = False
        _emit_event("continuous_resumed")
        for label, _buf in self.rx_buffers:
            logger.info("[%s] continuous recording resumed (new chunk)", label)

    def slice_qso(self, req: QSORequest) -> Optional[str]:
        now = time.time()
        # The audio buffer only contains samples captured since the contact
        # packet arrived, so base the slice window on the *receive* time
        # (when audio actually started being recorded) rather than the QSO
        # time reported by N1MM (which can be in the past).
        ref_ts = req.receive_ts if req.receive_ts else req.timestamp
        start_off = (now - ref_ts) + req.pre_roll
        end_off = (now - ref_ts) - req.post_roll
        if end_off < 0:
            end_off = 0
        logger.debug("[%s] slice window for %s: start_off=%.1f end_off=%.1f (ref_ts=%.1f, now=%.1f)",
                     self.label, req.call, start_off, end_off, ref_ts, now)
        safe_call = "".join(ch for ch in req.call if ch.isalnum() or ch in "-_")
        stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(req.timestamp))
        ext = "mp3" if self.cfg.audio_format == "mp3" else "wav"
        # Year-prefix the contest folder so the same contest repeated in
        # different years (e.g. CQWW 2025 vs 2026) does not mix recordings.
        year = time.strftime("%Y", time.localtime(req.timestamp))
        contest_dir = f"{year}_{req.contest}" if req.contest else f"{year}_GENERAL"
        out_dir = os.path.join(self.cfg.recordings_dir, contest_dir)

        # In SO2R we capture two independent receivers (RX1 = radio 1,
        # RX2 = radio 2). N1MM reports which radio made the QSO via <RadioNr>,
        # so we slice only the matching receiver's buffer instead of blindly
        # iterating over both (which produced two DB rows sharing the same
        # n1mm_id; the unique index then kept only the RX2 row, making every
        # QSO appear as if it were logged on RX2).
        if len(self.rx_buffers) > 1:
            want_label = "RX2" if str(req.radio_nr).strip() == "2" else "RX1"
            target = next((buf for lbl, buf in self.rx_buffers if lbl == want_label),
                          self.rx_buffers[0][1])
        else:
            want_label, target = self.rx_buffers[0]

        saved = []
        rx_label = want_label
        buf = target
        frames = buf.get_slice(start_off, end_off)
        if frames is None or frames.shape[0] == 0:
            avail = buf.snapshot_all()
            if avail.shape[0] > 0:
                logger.warning("[%s] full window unavailable for %s; saving %d buffered frames",
                                 rx_label, req.call, avail.shape[0])
                frames = avail
            else:
                logger.warning("[%s] insufficient buffer for QSO %s", rx_label, req.call)
                return None
        logger.debug("[%s] sliced %d frames for %s (contest=%s)",
                      rx_label, frames.shape[0], req.call, contest_dir)
        # Normalize so every QSO slice has a consistent loudness regardless
        # of how strong the received signal was (quiet vs loud stations).
        frames, gain_db = _normalize_frames(frames)
        fname = f"{stamp}_{safe_call}_{req.band}_{rx_label}.{ext}"
        out_path = os.path.join(out_dir, fname)
        _write_pcm(out_path, frames, self.cfg.sample_rate, 1,
                   fmt=self.cfg.audio_format)
        logger.info("[%s] saved QSO slice -> %s (%d frames, gain=%+.1f dB)",
                     rx_label, out_path, frames.shape[0], gain_db)
        saved.append((rx_label, out_path))
        # Persist a DB record so the dashboard can list/filter QSOs.
        # Forward the rich N1MM metadata (freq, exchange, name, ...) so
        # the dashboard can display it.
        try:
            import db as qso_db
            superseded = qso_db.insert_qso(
                contest=f"{year}_{req.contest}" if req.contest else f"{year}_GENERAL",
                call=req.call, band=req.band, mode=req.mode,
                freq=req.freq, name=req.name, qth=req.qth, grid=req.grid,
                comment=req.comment, exchange=req.exchange,
                exchange2=req.exchange2, exchange3=req.exchange3,
                rcv=req.rcv, snt=req.snt, rcvnr=req.rcvnr, sntnr=req.sntnr,
                section=req.section, mycall=req.mycall,
                countryprefix=req.countryprefix, wpxprefix=req.wpxprefix,
                continent=req.continent, operator=req.operator, station=req.station,
                contest_nr=req.contest_nr, points=req.points,
                multiplier=req.multiplier, multiplier2=req.multiplier2,
                multiplier3=req.multiplier3, prec=req.prec, ck=req.ck,
                power=req.power, n1mm_id=req.n1mm_id, is_claimed=req.is_claimed,
                sent_exchange=req.sent_exchange, timestamp=req.timestamp,
                raw_ts=req.raw_ts,
                file_path=f"{contest_dir}/{fname}",
            )
            # If an edited N1MM contact (contactreplace) produced a new
            # slice filename, the old audio file is now orphaned — remove
            # it so it does not linger in the recordings folder.
            if superseded:
                old_path = os.path.join(self.cfg.recordings_dir, superseded)
                try:
                    if os.path.isfile(old_path):
                        os.remove(old_path)
                        logger.info("[%s] removed superseded audio file: %s",
                                     rx_label, old_path)
                except OSError as e:
                    logger.warning("Could not remove superseded file %s: %s",
                                   old_path, e)
        except Exception as e:
            logger.debug("qso db insert failed: %s", e)
        if saved:
            _emit_event("qso_saved")
        return saved[0][1] if saved else None

    def _buffer_filled_sec(self) -> float:
        """Return the fill level (seconds) of the most-filled RX buffer.

        In SO2R (``channels >= 2``) the per-receiver ``rx1_buf`` / ``rx2_buf``
        buffers are written, while the legacy ``self.buffer`` stays empty. This
        helper always reports the real fill level regardless of SO1R/SO2R.
        """
        total = 0
        for _label, buf in self.rx_buffers:
            total = max(total, getattr(buf, "_filled", 0))
        return total / max(self.cfg.sample_rate, 1)

    def get_status(self) -> dict:
        """Return a generic status dict. Overridden by TCI source."""
        qsize = getattr(self._cont_queue, 'qsize', lambda: 0)()
        qmax = self._cont_queue.maxsize
        return {
            "connected": self._running,
            "frames_received": 0,
            "buffer_filled_sec": self._buffer_filled_sec(),
            "buffers": self._buffers_detail(),
            "continuous_paused": getattr(self, "_continuous_paused", False),
            "cont_queue_fill_pct": round(100.0 * qsize / qmax, 1) if qmax else 0.0,
            "cont_queue_dropped": getattr(self, "_cont_dropped", 0),
        }

    def is_connected(self) -> bool:
        """Return whether the audio source is actually receiving audio.

        For soundcard mode this is always True (the device is treated as
        always available). For TCI it reflects the real websocket connection
        state so the dashboard cannot start recording against a radio that is
        not connected.
        """
        return True

    def _buffers_detail(self) -> list:
        """Per-RX buffer fill detail for the dashboard (e.g. RX1/RX2)."""
        detail = []
        for label, buf in self.rx_buffers:
            detail.append({
                "label": label,
                "filled_sec": round(getattr(buf, "_filled", 0) / max(self.cfg.sample_rate, 1), 1),
            })
        return detail

    def _enqueue_cont(self, rx_label: str, frames: np.ndarray) -> None:
        """Non-blocking enqueue of continuous audio frames.

        Uses a *bounded* queue. If the queue is full (writer/encoder falling
        behind, e.g. on a slow disk) the oldest pending chunk is dropped so the
        capture callback never blocks on a full queue — live audio keeps
        flowing and only a small tail of continuous recording is skipped.
        """
        # Force a C-contiguous copy so a column slice from a stereo buffer
        # (e.g. pcm[:, 1:2]) gets its own physically-sequential memory
        # before it is written to a WAV via tobytes()/writeframes().
        frames = np.ascontiguousarray(frames, dtype=np.int16).copy()
        while True:
            try:
                self._cont_queue.put_nowait((rx_label, frames))
                return
            except queue.Full:
                try:
                    self._cont_queue.get_nowait()
                    self._cont_dropped += 1
                    # Emit an event on the first drop so the dashboard can
                    # react; subsequent drops in the same burst are counted
                    # but do not flood the event queue.
                    if self._cont_dropped == 1:
                        _emit_event("continuous_dropped", {"dropped": self._cont_dropped})
                except queue.Empty:
                    return

    def _continuous_loop(self) -> None:
        """Drain the continuous queue, appending audio to per-RX style chunk files.

        A fresh chunk file is opened for every RX (RX1 / RX2 in SO2R) and
        rolled over every ``continuous_chunk_minutes`` so each file is exactly
        that long. This avoids keeping a huge in-memory buffer and fixes the
        old behaviour where only a single short buffer snapshot was saved.

        Chunk files are opened *lazily* — only once recording is actually
        running (not paused). This prevents the old bug where, with
        ``continuous_autostart = false``, the loop opened (and DB-registered)
        empty chunk files at startup that were then silently discarded on the
        first resume, leaving "start without stop / no audio" rows in the
        continuous view.
        """
        if not self.cfg.continuous_recording:
            return
        chunk_sec = max(1, int(self.cfg.continuous_chunk_minutes * 60))
        logger.debug("[%s] continuous loop started (chunk=%ds)", self.label, chunk_sec)
        while self._running:
            if self._continuous_paused:
                # Drop queued audio so the queue does not grow while paused.
                try:
                    while True:
                        self._cont_queue.get_nowait()
                except queue.Empty:
                    pass
                time.sleep(0.2)
                continue
            # Not paused: ensure a chunk file is open (re-open after a pause).
            if not self._cont_files:
                # In TCI mode, do not open a chunk (and thus start recording)
                # until the radio is actually linked. Without this guard the
                # continuous autostart path would write silent gap files while
                # TCI is still connecting / disconnected. Soundcard mode is
                # always "connected" so it is unaffected.
                if not self.is_connected():
                    time.sleep(0.5)
                    continue
                self._open_cont_files()
                self._cont_start = time.time()
            try:
                item = self._cont_queue.get(timeout=0.5)
            except queue.Empty:
                item = None
            if item is not None:
                rx_label, frames = item
                self._write_cont(rx_label, frames)
            if time.time() - self._cont_start >= chunk_sec:
                self._roll_cont_files()
                self._cont_start = time.time()
        logger.debug("[%s] continuous loop exiting", self.label)
        self._close_cont_files()

    def _open_cont_files(self) -> None:
        # Include a millisecond component derived from the FULL epoch time (not
        # just the fractional part of the current second) so two chunks opened
        # in the same wall-clock second — e.g. rapid Stop -> Start cycles after
        # a factory reset — get distinct filenames and therefore distinct
        # UNIQUE file_path rows in the DB. Without the full-time base the
        # sub-second part repeats every second and the second chunk's
        # INSERT OR IGNORE was silently dropped, so the recording never
        # appeared in the dashboard.
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime()) + \
                f"{int(time.time() * 1000) % 100000:05d}"
        out_dir = os.path.join(self.cfg.recordings_dir, "_continuous")
        os.makedirs(out_dir, exist_ok=True)
        with self._cont_lock:
            for rx_label, _buf in self.rx_buffers:
                fname = f"{stamp}_{rx_label}.wav"
                path = os.path.join(out_dir, fname)
                wf = wave.open(path, "wb")
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.cfg.sample_rate)
                self._cont_files[rx_label] = {
                    "wf": wf, "path": path, "frames": 0, "start": time.time()
                }
                # Insert a DB record immediately so the in-progress chunk
                # shows up in the dashboard (duration is updated at rollover).
                try:
                    import db as qso_db
                    qso_db.insert_qso(
                        contest="_continuous", call="CONTINUOUS", band="", mode="",
                        timestamp=time.time(), duration=0.0,
                        file_path="_continuous/" + os.path.basename(path),
                    )
                except Exception:
                    pass

    def _write_cont(self, rx_label: str, frames: np.ndarray) -> None:
        with self._cont_lock:
            f = self._cont_files.get(rx_label)
            if not f:
                return
            f["wf"].writeframes(np.asarray(frames, dtype=np.int16).tobytes())
            f["frames"] += frames.shape[0]

    def _finalise_cont_file(self, rx_label: str, f: dict) -> None:
        """Close one chunk file and persist its DB record.

        If the chunk contains no audio frames it is discarded (file deleted
        and DB row removed) so the continuous view never shows a "start
        without stop / no audio" entry.

        When MP3 is configured, the (potentially slow) encode is handed off to
        the dedicated encoder thread (``_encoder_loop``) so this writer thread
        never blocks on it during long multi-hour sessions.
        """
        try:
            f["wf"].close()
        except Exception:
            pass
        path = f["path"]
        duration = f["frames"] / max(self.cfg.sample_rate, 1)
        rel = "_continuous/" + os.path.basename(path)
        if f["frames"] == 0:
            # Empty chunk (e.g. silence or started while paused) -> drop it.
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            try:
                import db as qso_db
                qso_db.delete_qso(rel)
            except Exception:
                pass
            logger.info("[%s] continuous chunk discarded (empty, no audio)", rx_label)
            return
        logger.info("[%s] continuous chunk saved (%s, %.1f s)",
                    rx_label, os.path.basename(path), duration)
        _emit_event("continuous_chunk_saved")
        try:
            import db as qso_db
            qso_db.update_qso_duration(rel, duration)
        except Exception as e:
            logger.debug("continuous db update failed: %s", e)
        # WAV chunks are written streaming, so they can only be normalized once
        # closed. Normalize in place to bring the chunk to a consistent level
        # (only when the user opted in – disabled for performance on long
        # recordings where consistent loudness is less critical).
        if self.cfg.audio_format == "wav" and self.cfg.normalize_continuous:
            gain_db = _normalize_wav_inplace(path)
            if gain_db is not None:
                logger.info("[%s] continuous WAV normalized (gain=%+.1f dB) %s",
                            rx_label, gain_db, os.path.basename(path))
        # MP3 encoding happens off-thread so it never blocks the writer.
        if self.cfg.audio_format == "mp3":
            # Pass the computed duration so the encoder thread can persist it
            # on the *renamed* .mp3 row (otherwise the duration update above
            # would target the soon-to-be-deleted .wav row and be lost).
            self._enc_queue.put((rx_label, path, self.cfg.sample_rate, rel, duration))

    def _roll_cont_files(self) -> None:
        with self._cont_lock:
            files = self._cont_files
            self._cont_files = {}
        for rx_label, f in files.items():
            self._finalise_cont_file(rx_label, f)
        self._open_cont_files()

    def _close_cont_files(self) -> None:
        with self._cont_lock:
            files = self._cont_files
            self._cont_files = {}
        for rx_label, f in files.items():
            self._finalise_cont_file(rx_label, f)

    @staticmethod
    def _encode_mp3(wav_path: str, sample_rate: int, normalize: bool = True) -> str:
        try:
            import lameenc
        except ImportError:
            return wav_path
        with wave.open(wav_path, "rb") as wf:
            data = wf.readframes(wf.getnframes())
            ch = wf.getnchannels()
        pcm = np.frombuffer(data, dtype=np.int16).reshape(-1, ch) if ch > 1 \
            else np.frombuffer(data, dtype=np.int16)
        gain_db = 0.0
        # Normalize the PCM to a consistent loudness before encoding to MP3
        # (only when the user opted in – disabled for performance on long
        # recordings where consistent loudness is less critical).
        if normalize:
            pcm, gain_db = _normalize_frames(pcm)
        data = pcm.tobytes()
        enc = lameenc.Encoder()
        enc.set_bit_rate(128)
        enc.set_in_sample_rate(sample_rate)
        enc.set_channels(ch)
        enc.set_quality(2)
        mp3 = enc.encode(data) + enc.flush()
        out = os.path.splitext(wav_path)[0] + ".mp3"
        with open(out, "wb") as f:
            f.write(mp3)
        try:
            os.remove(wav_path)
        except OSError:
            pass
        if normalize:
            logger.info("continuous MP3 normalized (gain=%+.1f dB) %s",
                        gain_db, os.path.basename(out))
        else:
            logger.info("continuous MP3 encoded (normalization disabled) %s",
                        os.path.basename(out))
        return out


class SoundcardAudioSource(AudioSource):
    """Capture audio from a system sound device via sounddevice.

    In SO2R (``channels == 2``) the left channel is recorded as **RX1** and
    the right channel as **RX2**, each into its own circular buffer, so two
    independent files are produced for every QSO / continuous chunk.
    """

    def __init__(self, cfg, label: str = "RX1"):
        super().__init__(cfg, label)
        self._stream = None
        if cfg.channels >= 2:
            # Separate mono buffers for each receiver.
            self.rx1_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx2_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx_buffers = [('RX1', self.rx1_buf), ('RX2', self.rx2_buf)]

    def _resolve_device(self):
        if not self.cfg.soundcard_device:
            return None
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                if self.cfg.soundcard_device.lower() in d["name"].lower():
                    return i
            logger.warning("Soundcard device '%s' not found, using default",
                           self.cfg.soundcard_device)
        except Exception as e:
            logger.error("sounddevice query failed: %s", e)
        return None

    def _capture_loop(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice not installed - cannot use soundcard mode")
            self._running = False
            return

        device = self._resolve_device()
        channels = self.cfg.channels
        sr = self.cfg.sample_rate

        def callback(indata, frames_count, time_info, status):
            if status:
                logger.debug("sounddevice status: %s", status)
            pcm = (indata * 32767.0).astype(np.int16)
            if pcm.ndim == 1:
                pcm = pcm.reshape(-1, 1)
            if self.cfg.channels >= 2 and pcm.shape[1] >= 2:
                # SO2R: left channel -> RX1, right channel -> RX2.
                self.rx1_buf.write(pcm[:, 0:1])
                self.rx2_buf.write(pcm[:, 1:2])
                if self.cfg.continuous_recording and self._running:
                    self._enqueue_cont('RX1', pcm[:, 0:1])
                    self._enqueue_cont('RX2', pcm[:, 1:2])
            else:
                self.buffer.write(pcm)
                if self.cfg.continuous_recording and self._running:
                    self._enqueue_cont('RX1', pcm)

        try:
            self._stream = sd.InputStream(
                device=device,
                samplerate=sr,
                channels=channels,
                dtype="float32",
                callback=callback,
                blocksize=2048,
            )
            with self._stream:
                while self._running:
                    time.sleep(0.1)
        except Exception as e:
            logger.error("[%s] soundcard capture error: %s", self.label, e)
        finally:
            self._running = False


class TCIAudioSource(AudioSource):
    """Capture audio from ExpertSDR via the TCI protocol (WebSocket)."""

    def __init__(self, cfg, label: str = "RX1"):
        super().__init__(cfg, label)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._frames_received = 0
        self._last_log_time = time.monotonic()
        # Map TCI receiver index -> circular buffer. In SO2R we capture
        # receiver 0 (RX1) and receiver 1 (RX2) into separate buffers so two
        # independent files are produced.
        if cfg.channels >= 2:
            self.rx1_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx2_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx_buffers = [('RX1', self.rx1_buf), ('RX2', self.rx2_buf)]
            self._rx_map = {0: self.rx1_buf, 1: self.rx2_buf}
            self._rx_label_map = {0: 'RX1', 1: 'RX2'}
        else:
            # SO1R: always record the main receiver (index 0).
            self._rx_map = {0: self.buffer}
            self._rx_label_map = {0: 'RX1'}

    def _capture_loop(self) -> None:
        try:
            import asyncio
            import websockets
        except ImportError:
            logger.error("websockets/asyncio not available - cannot use TCI mode")
            self._running = False
            return

        import asyncio
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_client())
        except asyncio.CancelledError:
            # Normal path on stop(): the task(s) are cancelled so the loop
            # returns cleanly instead of leaving pending tasks behind.
            logger.debug("[%s] TCI client cancelled", self.label)
        except Exception as e:
            logger.error("[%s] TCI client error: %s", self.label, e)
        finally:
            # Drain any tasks that refused to cancel gracefully so the loop
            # can be closed without "Task was destroyed but it is pending!".
            try:
                if not self._loop.is_closed():
                    for task in asyncio.all_tasks(self._loop):
                        task.cancel()
                    self._loop.run_until_complete(
                        asyncio.gather(*asyncio.all_tasks(self._loop),
                                       return_exceptions=True))
            except Exception:
                pass
            self._loop.close()
            self._running = False

    async def _ws_client(self) -> None:
        import asyncio
        import websockets

        uri = f"ws://{self.cfg.tci_host}:{self.cfg.tci_port}"
        while self._running:
            try:
                logger.info("[%s] TCI connecting to %s ...", self.label, uri)
                async with websockets.connect(uri, ping_interval=10,
                                              max_size=None) as ws:
                    logger.info("[%s] TCI connected to %s (receiver=0)",
                                self.label, uri)
                    self._connected = True
                    rx = 0

                    # The server broadcasts initialization commands and ends
                    # with "READY;" (case-insensitive per the TCI spec). We
                    # must detect that before requesting the audio stream,
                    # otherwise AUDIO_START is never sent and nothing records.
                    ready = False
                    got_any = False
                    deadline = time.monotonic() + 6.0
                    while self._running and time.monotonic() < deadline:
                        try:
                            banner = await asyncio.wait_for(
                                ws.recv(), timeout=max(0.5, deadline - time.monotonic()))
                        except asyncio.TimeoutError:
                            break
                        if isinstance(banner, str):
                            got_any = True
                            logger.debug("[%s] TCI init: %s", self.label, banner.strip())
                            if banner.strip().upper().rstrip(";") == "READY":
                                ready = True
                                break
                    if not ready and not got_any:
                        logger.warning("[%s] TCI: no init banner received, retrying",
                                       self.label)
                        continue
                    if not ready:
                        logger.warning("[%s] TCI: 'READY' not seen, proceeding anyway "
                                       "(some servers omit it)", self.label)

                    # Commands are sent exactly as named in the TCI spec
                    # (case-sensitive on many ExpertSDR builds). The audio
                    # stream requires: SAMPLE_TYPE, CHANNELS, SAMPLE_RATE and
                    # SAMPLES, in that order, before AUDIO_START. Sample rates
                    # allowed by the spec are 8/12/24/48 kHz.
                    async def _tci_send(cmd: str) -> None:
                        # Log the raw command we send to ExpertSDR (visible in
                        # the dashboard Log modal when debug is enabled) so the
                        # full TCI exchange can be inspected.
                        logger.debug("[%s] TCI >> %s", self.label, cmd)
                        await ws.send(cmd)

                    await _tci_send("SET_CLIENT_NAME:QSOCapture;")
                    await _tci_send("AUDIO_STREAM_SAMPLE_TYPE:int16;")
                    await _tci_send(f"AUDIO_STREAM_CHANNELS:{self.cfg.channels};")
                    # Clamp to a rate the TCI server accepts for the audio stream.
                    audio_sr = min(max(int(self.cfg.sample_rate), 8000), 48000)
                    await _tci_send(f"AUDIO_SAMPLERATE:{audio_sr};")
                    await _tci_send("AUDIO_STREAM_SAMPLES:1024;")
                    await _tci_send("MUTE:false;")
                    await _tci_send("MON_ENABLE:true;")
                    if self.cfg.channels >= 2:
                        # SO2R: capture receiver 0 (RX1) and receiver 1 (RX2).
                        await _tci_send("AUDIO_START:0;")
                        await _tci_send("AUDIO_START:1;")
                        logger.info("[%s] TCI audio requested (SO2R rx=0,1, sr=%d)",
                                    self.label, audio_sr)
                    else:
                        await _tci_send("AUDIO_START:0;")
                        logger.info("[%s] TCI audio requested (rx=0, sr=%d)",
                                    self.label, audio_sr)

                    # Diagnostics: count every binary frame actually received
                    # so the dashboard/log shows whether the server is sending
                    # audio at all.
                    self._frames_received = 0
                    async for message in ws:
                        if not self._running:
                            break
                        if isinstance(message, str):
                            # Raw text message received from ExpertSDR (status
                            # updates, TRX state, etc.). Log it so the full TCI
                            # exchange is visible in debug mode.
                            logger.debug("[%s] TCI << %s", self.label, message.strip())
                        self._handle_tci_message(message)
            except asyncio.CancelledError:
                # stop() cancelled the task: exit the reconnect loop cleanly.
                break
            except Exception as e:
                self._connected = False
                logger.warning("[%s] TCI connection error, reconnect in 3s: %s",
                               self.label, e)
                # Guard the reconnect sleep: if the loop is being torn down
                # (CancelledError) don't attempt to sleep on a closed loop.
                try:
                    await asyncio.sleep(3.0)
                except asyncio.CancelledError:
                    break
            finally:
                self._connected = False

    def _handle_tci_message(self, message) -> None:
        if not isinstance(message, (bytes, bytearray)):
            return
        try:
            raw = bytes(message)
            result = self._decode_binary_audio(raw)
            if result is None:
                # Non-audio binary frame (e.g. control/keepalive). Logged in
                # debug so the raw TCI stream is fully visible.
                logger.debug("[%s] TCI << binary (len=%d, not audio)", self.label, len(raw))
                return
            pcm, rx_index = result
            if pcm is None or pcm.size == 0:
                logger.debug("[%s] TCI binary frame not audio (len=%d)", self.label, len(raw))
                return
            self._push_pcm(pcm, rx_index)
        except Exception as e:
            logger.debug("TCI parse error: %s", e)

    def _decode_binary_audio(self, raw: bytes):
        HEADER = 64
        if len(raw) < HEADER + 4:
            return None
        hdr = np.frombuffer(raw[:HEADER], dtype=np.uint32)
        # TCI Stream header layout (all uint32, little-endian):
        #   [0] receiver   [1] sample_rate   [2] format   [3] codec
        #   [4] crc        [5] length        [6] type     [7] channels
        # The receiver number lives in hdr[0]; hdr[3] is the (unused) codec.
        rx_index = int(hdr[0])
        fmt = int(hdr[2])
        stype = int(hdr[6])
        nchan = int(hdr[7])
        if stype not in (1, 4):
            return None
        data = raw[HEADER:]
        if len(data) < 4:
            return None

        if fmt == 3:
            arr = np.frombuffer(data, dtype=np.float32)
            if arr.size == 0:
                return None
            pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16)
        elif fmt == 0:
            pcm = np.frombuffer(data, dtype=np.int16).astype(np.int16)
        elif fmt == 2:
            arr = np.frombuffer(data, dtype=np.int32)
            pcm = (arr >> 16).astype(np.int16)
        elif fmt == 1:
            n = len(data) // 3
            if n == 0:
                return None
            b = np.frombuffer(data[:n * 3], dtype=np.uint8).reshape(n, 3)
            vals = (b[:, 0].astype(np.int32)
                    | (b[:, 1].astype(np.int32) << 8)
                    | (b[:, 2].astype(np.int32) << 16))
            vals = np.where(vals >= (1 << 23), vals - (1 << 24), vals)
            pcm = (vals >> 8).astype(np.int16)
        else:
            return None

        if nchan > 1 and pcm.ndim == 1 and pcm.shape[0] % nchan == 0:
            # Force C-contiguous memory for the selected channel before it is
            # routed to a buffer / written to disk (defensive against any
            # strides-based copy assumptions downstream).
            pcm = np.ascontiguousarray(pcm.reshape(-1, nchan)[:, 0], dtype=np.int16)
        return pcm, rx_index

    def _push_pcm(self, pcm: np.ndarray, rx_index: int = 0) -> None:
        pcm = np.asarray(pcm, dtype=np.int16).reshape(-1, 1)
        # Route to the receiver-specific buffer (RX1 / RX2 in SO2R).
        buf = self._rx_map.get(rx_index, self.buffer)
        n = pcm.shape[0]
        buf.write(pcm)
        self._frames_received += n
        if self.cfg.continuous_recording and self._running:
            label = self._rx_label_map.get(rx_index, 'RX1')
            self._enqueue_cont(label, pcm)
        now = time.monotonic()
        if now - self._last_log_time >= 5.0:
            secs = self._frames_received / max(self.cfg.sample_rate, 1)
            logger.info("[%s] TCI audio flowing (rx=%d): +%d frames (%.1f s buffered total)",
                        self.label, rx_index, n, secs)
            self._last_log_time = now
        else:
            logger.debug("[%s] TCI audio frame (rx=%d): %d samples", self.label, rx_index, n)

    def get_status(self) -> dict:
        qsize = getattr(self._cont_queue, 'qsize', lambda: 0)()
        qmax = self._cont_queue.maxsize
        return {
            "connected": getattr(self, "_connected", False),
            "frames_received": getattr(self, "_frames_received", 0),
            "buffer_filled_sec": self._buffer_filled_sec(),
            "buffers": self._buffers_detail(),
            "continuous_paused": getattr(self, "_continuous_paused", False),
            "cont_queue_fill_pct": round(100.0 * qsize / qmax, 1) if qmax else 0.0,
            "cont_queue_dropped": getattr(self, "_cont_dropped", 0),
        }

    def is_connected(self) -> bool:
        """TCI is only connected once the websocket handshake completes."""
        return bool(getattr(self, "_connected", False))


def create_audio_source(cfg, label: str = "RX1") -> AudioSource:
    if cfg.audio_mode == "tci":
        return TCIAudioSource(cfg, label)
    return SoundcardAudioSource(cfg, label)