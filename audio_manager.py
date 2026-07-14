"""audio_manager.py - Thread-safe audio capture and circular buffering."""

from __future__ import annotations

import asyncio
import logging
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
    operator: str = ""
    station: str = ""
    contest_nr: str = ""
    points: str = ""
    multiplier: str = ""
    raw_ts: str = ""


def _write_pcm(path: str, frames: np.ndarray, sample_rate: int, channels: int,
               fmt: str = "wav") -> None:
    """Write int16 frames to a WAV or MP3 file at path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pcm = frames.astype(np.int16)
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
        self._cont_queue: "queue.Queue" = queue.Queue()
        self._cont_files: dict = {}
        self._cont_lock = threading.Lock()
        self._cont_start = 0.0
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
            self._continuous = threading.Thread(target=self._continuous_loop,
                                                daemon=True, name=f"cont-{self.label}")
            self._continuous.start()
        rxs = ",".join(lbl for lbl, _ in self.rx_buffers)
        for label, _buf in self.rx_buffers:
            logger.info("[%s] audio source started (mode=%s, rx=%s, so2r=%s)",
                        label, self.cfg.audio_mode, rxs, self.cfg.channels >= 2)

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

    # -- continuous recording pause / resume (dashboard control) ----------
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
        self._close_cont_files()
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
        self._open_cont_files()
        self._cont_start = time.time()
        self._continuous_paused = False
        for label, _buf in self.rx_buffers:
            logger.info("[%s] continuous recording resumed (new chunk)", label)

    def slice_qso(self, req: QSORequest) -> Optional[str]:
        now = time.time()
        start_off = (now - req.timestamp) + req.pre_roll
        end_off = (now - req.timestamp) - req.post_roll
        if end_off < 0:
            end_off = 0
        safe_call = "".join(ch for ch in req.call if ch.isalnum() or ch in "-_")
        stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(req.timestamp))
        ext = "mp3" if self.cfg.audio_format == "mp3" else "wav"
        # Year-prefix the contest folder so the same contest repeated in
        # different years (e.g. CQWW 2025 vs 2026) does not mix recordings.
        year = time.strftime("%Y", time.localtime(req.timestamp))
        contest_dir = f"{year}_{req.contest}" if req.contest else f"{year}_GENERAL"
        out_dir = os.path.join(self.cfg.recordings_dir, contest_dir)

        saved = []
        for rx_label, buf in self.rx_buffers:
            frames = buf.get_slice(start_off, end_off)
            if frames is None or frames.shape[0] == 0:
                avail = buf.snapshot_all()
                if avail.shape[0] > 0:
                    logger.warning("[%s] full window unavailable for %s; saving %d buffered frames",
                                   rx_label, req.call, avail.shape[0])
                    frames = avail
                else:
                    logger.warning("[%s] insufficient buffer for QSO %s", rx_label, req.call)
                    continue
            fname = f"{stamp}_{safe_call}_{req.band}_{rx_label}.{ext}"
            out_path = os.path.join(out_dir, fname)
            _write_pcm(out_path, frames, self.cfg.sample_rate, 1,
                       fmt=self.cfg.audio_format)
            logger.info("[%s] saved QSO slice -> %s (%d frames)", rx_label, out_path, frames.shape[0])
            saved.append((rx_label, out_path))
            # Persist a DB record so the dashboard can list/filter QSOs.
            # Forward the rich N1MM metadata (freq, exchange, name, ...) so
            # the dashboard can display it.
            try:
                import db as qso_db
                qso_db.insert_qso(
                    contest=f"{year}_{req.contest}" if req.contest else f"{year}_GENERAL",
                    call=req.call, band=req.band, mode=req.mode,
                    freq=req.freq, name=req.name, qth=req.qth, grid=req.grid,
                    comment=req.comment, exchange=req.exchange,
                    exchange2=req.exchange2, exchange3=req.exchange3,
                    operator=req.operator, station=req.station,
                    contest_nr=req.contest_nr, points=req.points,
                    multiplier=req.multiplier, timestamp=req.timestamp,
                    raw_ts=req.raw_ts,
                    file_path=f"{contest_dir}/{fname}",
                )
            except Exception as e:
                logger.debug("qso db insert failed: %s", e)
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
        return {
            "connected": self._running,
            "frames_received": 0,
            "buffer_filled_sec": self._buffer_filled_sec(),
            "buffers": self._buffers_detail(),
            "continuous_paused": getattr(self, "_continuous_paused", False),
        }

    def _buffers_detail(self) -> list:
        """Per-RX buffer fill detail for the dashboard (e.g. RX1/RX2)."""
        detail = []
        for label, buf in self.rx_buffers:
            detail.append({
                "label": label,
                "filled_sec": round(getattr(buf, "_filled", 0) / max(self.cfg.sample_rate, 1), 1),
            })
        return detail

    def _continuous_loop(self) -> None:
        """Drain the continuous queue, appending audio to per-RX chunk files.

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
        self._close_cont_files()

    def _open_cont_files(self) -> None:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
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
        if self.cfg.audio_format == "mp3":
            old = rel
            path = self._encode_mp3(path, self.cfg.sample_rate)
            rel = "_continuous/" + os.path.basename(path)
        logger.info("[%s] continuous chunk saved (%s, %.1f s)",
                    rx_label, os.path.basename(path), duration)
        try:
            import db as qso_db
            if self.cfg.audio_format == "mp3":
                qso_db.update_qso_file_path(old, rel)
            qso_db.update_qso_duration(rel, duration)
        except Exception as e:
            logger.debug("continuous db update failed: %s", e)

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
    def _encode_mp3(wav_path: str, sample_rate: int) -> str:
        try:
            import lameenc
        except ImportError:
            return wav_path
        with wave.open(wav_path, "rb") as wf:
            data = wf.readframes(wf.getnframes())
            ch = wf.getnchannels()
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
                    self._cont_queue.put(('RX1', pcm[:, 0:1].copy()))
                    self._cont_queue.put(('RX2', pcm[:, 1:2].copy()))
            else:
                self.buffer.write(pcm)
                if self.cfg.continuous_recording and self._running:
                    self._cont_queue.put(('RX1', pcm.copy()))

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
            self._rx_map = {cfg.tci_receiver: self.buffer}
            self._rx_label_map = {cfg.tci_receiver: 'RX1'}

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
                    logger.info("[%s] TCI connected to %s (receiver=%d)",
                                self.label, uri, self.cfg.tci_receiver)
                    self._connected = True
                    rx = self.cfg.tci_receiver

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
                    await ws.send("SET_CLIENT_NAME:QSOCapture;")
                    await ws.send("AUDIO_STREAM_SAMPLE_TYPE:int16;")
                    await ws.send(f"AUDIO_STREAM_CHANNELS:{self.cfg.channels};")
                    # Clamp to a rate the TCI server accepts for the audio stream.
                    audio_sr = min(max(int(self.cfg.sample_rate), 8000), 48000)
                    await ws.send(f"AUDIO_SAMPLERATE:{audio_sr};")
                    await ws.send("AUDIO_STREAM_SAMPLES:1024;")
                    await ws.send("MUTE:false;")
                    await ws.send("MON_ENABLE:true;")
                    if self.cfg.channels >= 2:
                        # SO2R: capture receiver 0 (RX1) and receiver 1 (RX2).
                        await ws.send("AUDIO_START:0;")
                        await ws.send("AUDIO_START:1;")
                        logger.info("[%s] TCI audio requested (SO2R rx=0,1, sr=%d)",
                                    self.label, audio_sr)
                    else:
                        await ws.send(f"AUDIO_START:{rx};")
                        logger.info("[%s] TCI audio requested (rx=%d, sr=%d)",
                                    self.label, rx, audio_sr)

                    # Diagnostics: count every binary frame actually received
                    # so the dashboard/log shows whether the server is sending
                    # audio at all.
                    self._frames_received = 0
                    async for message in ws:
                        if not self._running:
                            break
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
            pcm = pcm.reshape(-1, nchan)[:, 0]
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
            self._cont_queue.put((label, pcm.copy()))
        now = time.monotonic()
        if now - self._last_log_time >= 5.0:
            secs = self._frames_received / max(self.cfg.sample_rate, 1)
            logger.info("[%s] TCI audio flowing (rx=%d): +%d frames (%.1f s buffered total)",
                        self.label, rx_index, n, secs)
            self._last_log_time = now
        else:
            logger.debug("[%s] TCI audio frame (rx=%d): %d samples", self.label, rx_index, n)

    def get_status(self) -> dict:
        return {
            "connected": getattr(self, "_connected", False),
            "frames_received": getattr(self, "_frames_received", 0),
            "buffer_filled_sec": self._buffer_filled_sec(),
            "buffers": self._buffers_detail(),
            "continuous_paused": getattr(self, "_continuous_paused", False),
        }


def create_audio_source(cfg, label: str = "RX1") -> AudioSource:
    if cfg.audio_mode == "tci":
        return TCIAudioSource(cfg, label)
    return SoundcardAudioSource(cfg, label)