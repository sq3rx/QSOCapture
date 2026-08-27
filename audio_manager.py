"""Thread-safe audio capture and circular buffering."""

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

from config import RECORDINGS_DIR

logger = logging.getLogger("QSOCapture.audio")


def _emit_event(name: str, data: Optional[dict] = None) -> None:
    """Emit a dashboard event (best-effort, lazy import to avoid circular imports)."""
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
    timestamp: float
    pre_roll: float
    post_roll: float
    receive_ts: float = 0.0
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
    radio_nr: str = "1"
    raw_ts: str = ""


def _write_pcm(path: str, frames: np.ndarray, sample_rate: int, channels: int,
               fmt: str = "wav") -> None:
    """Write int16 frames to a WAV or MP3 file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    """Legacy alias for _write_pcm with fmt=wav."""
    _write_pcm(path, frames, sample_rate, channels, fmt="wav")


# Audio normalization constants (always-on, no UI)
NORM_TARGET_DBFS = -24.0
NORM_MAX_GAIN_DB = 18.0
NORM_PEAK_CEIL_DB = -6.0


def _normalize_frames(frames: np.ndarray):
    """Normalize int16 PCM to target RMS level, return (frames, gain_db).

    Computes RMS of float signal, applies capped gain, then clamps peaks to ceiling.
    Silent buffer (rms=0) returned untouched with 0 dB gain.
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
    """Read, normalize, and overwrite a WAV file in-place. Returns gain dB or None."""
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
        self.rx_buffers = [('RX1', self.buffer)]
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._continuous: Optional[threading.Thread] = None
        # Bounded queue so slow disk never blocks the capture callback.
        self._cont_queue: "queue.Queue" = queue.Queue(maxsize=1800)
        self._cont_files: dict = {}
        self._cont_lock = threading.Lock()
        self._cont_start = 0.0
        self._cont_dropped = 0
        self._enc_queue: "queue.Queue" = queue.Queue()
        self._enc_thread: Optional[threading.Thread] = None
        self._enc_started = False
        self._enc_busy = False
        self._continuous_paused = False

    @abstractmethod
    def _capture_loop(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._continuous_paused = not getattr(self.cfg, "continuous_autostart", True)
        self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                        name=f"capture-{self.label}")
        self._thread.start()
        if self.cfg.continuous_recording:
            self._ensure_encoder()
            self._continuous = threading.Thread(target=self._continuous_loop,
                                                daemon=True, name=f"cont-{self.label}")
            self._continuous.start()
        rxs = ",".join(lbl for lbl, _ in self.rx_buffers)
        for label, _buf in self.rx_buffers:
            logger.info("[%s] audio source started (mode=%s, rx=%s, so2r=%s)",
                        label, self.cfg.audio_mode, rxs, self.cfg.channels >= 2)

    def _ensure_encoder(self) -> None:
        """Start the MP3 encoder worker thread (idempotent)."""
        if self._enc_started:
            return
        self._enc_started = True
        self._enc_thread = threading.Thread(target=self._encoder_loop,
                                            daemon=True, name=f"enc-{self.label}")
        self._enc_thread.start()

    def _encoder_loop(self) -> None:
        """Off-thread MP3 encoder: pulls finalized WAV chunks from _enc_queue."""
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
        """Stop capture and clean up threads (finalises chunks, no shutdown hang)."""
        self._close_cont_files()
        self._running = False
        try:
            self._cont_queue.put_nowait(('RX1', np.zeros((0, 1), dtype=np.int16)))
        except queue.Full:
            pass
        # Abort any active sounddevice streams
        for attr in ("_stream", "_stream2"):
            stream = getattr(self, attr, None)
            if stream is not None:
                try:
                    stream.abort()
                except Exception:
                    pass
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
        self._close_cont_files()
        if self._enc_thread:
            self._enc_thread.join(timeout=2.0)

    def _drain_encoder(self, timeout: float = 30.0) -> None:
        """Block until encoder queue is empty and current encode finishes."""
        if not self.cfg.continuous_recording or self.cfg.audio_format != "mp3":
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._enc_queue.empty() and not self._enc_busy:
                time.sleep(0.05)
                if self._enc_queue.empty() and not self._enc_busy:
                    return
            time.sleep(0.05)

    def pause_continuous(self) -> None:
        """Finalise current continuous chunk and stop writing new audio.

        Ring buffers are preserved (needed for QSO slicing).
        """
        if not self.cfg.continuous_recording or self._continuous_paused:
            return
        self._continuous_paused = True
        _emit_event("continuous_paused")
        self._close_cont_files()
        self._drain_encoder()
        for label, _buf in self.rx_buffers:
            logger.info("[%s] continuous recording paused (chunk finalised, buffers kept for QSO slicing)",
                        label)

    def _rx_label_str(self) -> str:
        return ",".join(lbl for lbl, _ in self.rx_buffers)

    def _clear_buffers(self) -> None:
        for _label, buf in self.rx_buffers:
            with buf._lock:
                buf._write_idx = 0
                buf._filled = 0

    def resume_continuous(self) -> None:
        """Re-open a fresh continuous chunk and resume writing."""
        if not self.cfg.continuous_recording or not self._continuous_paused:
            return
        if not self.is_connected():
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
        ref_ts = req.receive_ts if req.receive_ts else req.timestamp
        start_off = (now - ref_ts) + req.pre_roll
        end_off = (now - ref_ts) - req.post_roll
        if end_off < 0:
            end_off = 0
        logger.debug("[%s] slice window for %s: start_off=%.1f end_off=%.1f (ref_ts=%.1f, now=%.1f)",
                     self.label, req.call, start_off, end_off, ref_ts, now)
        safe_call = "".join(ch for ch in req.call if ch.isalnum() or ch in "-_")
        stamp = time.strftime("%Y-%m-%d_%H%M", time.gmtime(req.timestamp))
        ext = "mp3" if self.cfg.audio_format == "mp3" else "wav"
        year = time.strftime("%Y", time.gmtime(req.timestamp))
        contest_dir = f"{year}_{req.contest}" if req.contest else f"{year}_GENERAL"
        out_dir = os.path.join(RECORDINGS_DIR, contest_dir)

        # For SO2R: slice only the matching receiver's buffer (by radio_nr).
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
        frames, gain_db = _normalize_frames(frames)
        fname = f"{stamp}_{safe_call}_{req.band}_{rx_label}.{ext}"
        out_path = os.path.join(out_dir, fname)
        _write_pcm(out_path, frames, self.cfg.sample_rate, 1,
                   fmt=self.cfg.audio_format)
        logger.info("[%s] saved QSO slice -> %s (%d frames, gain=%+.1f dB)",
                     rx_label, out_path, frames.shape[0], gain_db)
        saved.append((rx_label, out_path))

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
            if superseded:
                old_path = os.path.join(RECORDINGS_DIR, superseded)
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
        """Return the fill level (seconds) of the most-filled RX buffer."""
        total = 0
        for _label, buf in self.rx_buffers:
            total = max(total, getattr(buf, "_filled", 0))
        return total / max(self.cfg.sample_rate, 1)

    def get_status(self) -> dict:
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
        """For soundcard always True; TCI overrides to reflect WebSocket state."""
        return True

    def _buffers_detail(self) -> list:
        detail = []
        for label, buf in self.rx_buffers:
            detail.append({
                "label": label,
                "filled_sec": round(getattr(buf, "_filled", 0) / max(self.cfg.sample_rate, 1), 1),
            })
        return detail

    def _enqueue_cont(self, rx_label: str, frames: np.ndarray) -> None:
        """Non-blocking enqueue for continuous audio (drops oldest when full)."""
        frames = np.ascontiguousarray(frames, dtype=np.int16).copy()
        while True:
            try:
                self._cont_queue.put_nowait((rx_label, frames))
                return
            except queue.Full:
                try:
                    self._cont_queue.get_nowait()
                    self._cont_dropped += 1
                    if self._cont_dropped == 1:
                        _emit_event("continuous_dropped", {"dropped": self._cont_dropped})
                except queue.Empty:
                    return

    def _continuous_loop(self) -> None:
        """Drain continuous queue, writing per-RX chunk files rolled every chunk_minutes."""
        if not self.cfg.continuous_recording:
            return
        chunk_sec = max(1, int(self.cfg.continuous_chunk_minutes * 60))
        logger.debug("[%s] continuous loop started (chunk=%ds)", self.label, chunk_sec)
        while self._running:
            if self._continuous_paused:
                try:
                    while True:
                        self._cont_queue.get_nowait()
                except queue.Empty:
                    pass
                time.sleep(0.2)
                continue
            if not self._cont_files:
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
        """Open new chunk file(s) with a unique millisecond component for the filename."""
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime()) + \
                f"{int(time.time() * 1000) % 100000:05d}"
        out_dir = os.path.join(RECORDINGS_DIR, "_continuous")
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
        """Close a chunk file, persist DB record, and queue MP3 encode if needed."""
        try:
            f["wf"].close()
        except Exception:
            pass
        path = f["path"]
        duration = f["frames"] / max(self.cfg.sample_rate, 1)
        rel = "_continuous/" + os.path.basename(path)
        if f["frames"] == 0:
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
        if self.cfg.audio_format == "wav" and self.cfg.normalize_continuous:
            gain_db = _normalize_wav_inplace(path)
            if gain_db is not None:
                logger.info("[%s] continuous WAV normalized (gain=%+.1f dB) %s",
                            rx_label, gain_db, os.path.basename(path))
        if self.cfg.audio_format == "mp3":
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
    """Capture audio from system sound device(s) via sounddevice.

    In stereo mode (so2r_mode='stereo', channels=2):
      left channel -> RX1, right channel -> RX2.

    In dual_card mode (so2r_mode='dual_card', channels=2):
      two separate mono InputStreams, each to its own RX buffer.
    """

    def __init__(self, cfg, label: str = "RX1"):
        super().__init__(cfg, label)
        self._stream = None
        self._stream2 = None
        if cfg.channels >= 2:
            self.rx1_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx2_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx_buffers = [('RX1', self.rx1_buf), ('RX2', self.rx2_buf)]

    def _resolve_device(self, name_attr: str = "soundcard_device"):
        """Resolve device index from config attribute *name_attr* (exact name match)."""
        dev_name = getattr(self.cfg, name_attr, "")
        if not dev_name:
            return None
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                if d["name"] == dev_name:
                    return i
            logger.warning("Soundcard device '%s' not found, using default",
                           dev_name)
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

        sr = self.cfg.sample_rate

        if self.cfg.channels >= 2 and self.cfg.so2r_mode == "dual_card":
            # ── dual_card mode: two separate mono streams ──────────────
            dev1 = self._resolve_device("soundcard_device")
            dev2 = self._resolve_device("soundcard_device2")

            def callback1(indata, frames_count, time_info, status):
                if status:
                    logger.debug("sounddevice1 status: %s", status)
                pcm = (indata * 32767.0).astype(np.int16)
                if pcm.ndim == 1:
                    pcm = pcm.reshape(-1, 1)
                self.rx1_buf.write(pcm)
                if self.cfg.continuous_recording and self._running:
                    self._enqueue_cont('RX1', pcm)

            def callback2(indata, frames_count, time_info, status):
                if status:
                    logger.debug("sounddevice2 status: %s", status)
                pcm = (indata * 32767.0).astype(np.int16)
                if pcm.ndim == 1:
                    pcm = pcm.reshape(-1, 1)
                self.rx2_buf.write(pcm)
                if self.cfg.continuous_recording and self._running:
                    self._enqueue_cont('RX2', pcm)

            try:
                self._stream = sd.InputStream(
                    device=dev1, samplerate=sr, channels=1,
                    dtype="float32", callback=callback1, blocksize=2048,
                )
                self._stream2 = sd.InputStream(
                    device=dev2, samplerate=sr, channels=1,
                    dtype="float32", callback=callback2, blocksize=2048,
                )
                with self._stream, self._stream2:
                    while self._running:
                        time.sleep(0.1)
            except Exception as e:
                logger.error("[%s] dual_card capture error: %s", self.label, e)
            finally:
                self._running = False
        else:
            # ── stereo / mono mode: single stream ─────────────────────
            device = self._resolve_device("soundcard_device")
            channels = self.cfg.channels

            def callback(indata, frames_count, time_info, status):
                if status:
                    logger.debug("sounddevice status: %s", status)
                pcm = (indata * 32767.0).astype(np.int16)
                if pcm.ndim == 1:
                    pcm = pcm.reshape(-1, 1)
                if self.cfg.channels >= 2 and pcm.shape[1] >= 2:
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
                    device=device, samplerate=sr, channels=channels,
                    dtype="float32", callback=callback, blocksize=2048,
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
        if cfg.channels >= 2:
            self.rx1_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx2_buf = CircularAudioBuffer(cfg.sample_rate, 1,
                                               max(cfg.pre_roll + cfg.post_roll + 5.0, 30.0))
            self.rx_buffers = [('RX1', self.rx1_buf), ('RX2', self.rx2_buf)]
            self._rx_map = {0: self.rx1_buf, 1: self.rx2_buf}
            self._rx_label_map = {0: 'RX1', 1: 'RX2'}
        else:
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
            logger.debug("[%s] TCI client cancelled", self.label)
        except Exception as e:
            logger.error("[%s] TCI client error: %s", self.label, e)
        finally:
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

                    # Wait for READY before requesting audio stream.
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

                    async def _tci_send(cmd: str) -> None:
                        logger.debug("[%s] TCI >> %s", self.label, cmd)
                        await ws.send(cmd)

                    await _tci_send("SET_CLIENT_NAME:QSOCapture;")
                    await _tci_send("AUDIO_STREAM_SAMPLE_TYPE:int16;")
                    await _tci_send(f"AUDIO_STREAM_CHANNELS:{self.cfg.channels};")
                    audio_sr = min(max(int(self.cfg.sample_rate), 8000), 48000)
                    await _tci_send(f"AUDIO_SAMPLERATE:{audio_sr};")
                    await _tci_send("AUDIO_STREAM_SAMPLES:1024;")
                    if self.cfg.channels >= 2:
                        await _tci_send("AUDIO_START:0;")
                        await _tci_send("AUDIO_START:1;")
                        logger.info("[%s] TCI audio requested (SO2R rx=0,1, sr=%d)",
                                    self.label, audio_sr)
                    else:
                        await _tci_send("AUDIO_START:0;")
                        logger.info("[%s] TCI audio requested (rx=0, sr=%d)",
                                    self.label, audio_sr)

                    self._frames_received = 0
                    async for message in ws:
                        if not self._running:
                            break
                        if isinstance(message, str):
                            logger.debug("[%s] TCI << %s", self.label, message.strip())
                        self._handle_tci_message(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.warning("[%s] TCI connection error, reconnect in 3s: %s",
                               self.label, e)
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
            pcm = np.ascontiguousarray(pcm.reshape(-1, nchan)[:, 0], dtype=np.int16)
        return pcm, rx_index

    def _push_pcm(self, pcm: np.ndarray, rx_index: int = 0) -> None:
        pcm = np.asarray(pcm, dtype=np.int16).reshape(-1, 1)
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
        return bool(getattr(self, "_connected", False))


def create_audio_source(cfg, label: str = "RX1") -> AudioSource:
    if cfg.audio_mode == "tci":
        return TCIAudioSource(cfg, label)
    return SoundcardAudioSource(cfg, label)