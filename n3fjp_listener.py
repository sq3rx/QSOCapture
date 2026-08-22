"""Async TCP client for the N3FJP logging software API.

N3FJP programs (Amateur Contact Log and ~100 contest loggers) act as a TCP
*server* (default port 1100). QSOCapture connects as a client, opts in to the
all-updates notification stream, builds a QSO record from ``<ENTEREVENT>``
pushes, and keeps the local database in sync when the operator edits or deletes
a contact.

Unlike the N1MM UDP broadcast, the N3FJP TCP API provides:

* ``<ENTEREVENT>`` — fired automatically after the operator logs a contact
  (carries CALL, BAND, MODE, QSO_DATE (YYYYMMDD), TIME_ON (HHMMSS), ...).
* ``<UPDATERESPONSE>`` / ``<READBMFRESPONSE>`` — live field values (frequency,
  exchange, RST, section, ...) once ``SETUPDATESTATE TRUE`` is enabled.
* ``<EDITDELETEEVENT>`` — an *empty* signal sent when a contact was edited or
  deleted (no record id). N3FJP never tells us *which* QSO changed, so this
  module reconciles by pulling ``LIST`` and diffing it against the local DB.

Protocol framing: each message is a ``<CMD>...</CMD>`` envelope, CR LF
terminated, the first token after ``<CMD>`` is the command/response id, the rest
are ``<TAG>value</TAG>`` pairs. One TCP packet may carry several envelopes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from config import RECORDINGS_DIR

logger = logging.getLogger("QSOCapture.n3fjp")

# One <CMD>...</CMD> envelope (non-greedy so adjacent envelopes don't merge).
_BLOCK_RE = re.compile(r"<CMD>(.*?)</CMD>", re.DOTALL | re.IGNORECASE)
# A closed <TAG>value</TAG> pair. Tag names may start with a digit (e.g. 20MIN).
_FIELD_RE = re.compile(r"<([A-Za-z0-9_]+)>(.*?)</\1>", re.DOTALL)
# The leading command/response ID (an opening tag N3FJP usually does not close).
_ID_RE = re.compile(r"\s*<([A-Za-z0-9_]+)>")


def extract_blocks(buffer: str) -> Tuple[List[str], str]:
    """Split a receive buffer into complete inner blocks and the leftover tail.

    Returns (inners, remaining) where each *inner* is the text between ``<CMD>``
    and ``</CMD>`` and *remaining* is any trailing partial envelope.
    """
    inners: List[str] = []
    last_end = 0
    for match in _BLOCK_RE.finditer(buffer):
        inners.append(match.group(1))
        last_end = match.end()
    return inners, buffer[last_end:]


def parse_block(inner: str) -> Tuple[str, dict]:
    """Parse one inner block into ``(cmd_id, fields)``."""
    id_match = _ID_RE.match(inner)
    cmd_id = id_match.group(1).upper() if id_match else ""
    fields: dict = {}
    for fmatch in _FIELD_RE.finditer(inner):
        tag = fmatch.group(1).upper()
        if tag == cmd_id:
            continue
        fields.setdefault(tag, fmatch.group(2))
    return cmd_id, fields


def _parse_date_time(qso_date: str, time_on: str) -> float:
    """Parse QSO_DATE (YYYYMMDD) and TIME_ON (HHMMSS) as UTC epoch seconds.

    N3FJP publishes timestamps in UTC, so we parse as timezone-aware UTC.
    """
    y = m = d = 0
    if qso_date:
        try:
            y = int(qso_date[:4])
            m = int(qso_date[4:6])
            d = int(qso_date[6:8])
        except (TypeError, ValueError):
            y = m = d = 0
    try:
        t = int((time_on or "0").strip() or 0)
    except (TypeError, ValueError):
        t = 0
    hr = (t // 10000) % 100
    mi = (t // 100) % 100
    se = t % 100
    if y and m and d:
        try:
            return datetime(y, m, d, hr, mi, se, tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return time.time()


def _fmt_date_time(qso_date: str, time_on: str) -> str:
    date = (qso_date or "").strip()
    tm = (time_on or "").strip()
    return f"{date} {tm}".strip()


def compute_source_key(call: str, qso_date: str, time_on: str,
                       band: str, mode: str) -> str:
    """Stable identity key for a QSO recorded from source='n3fjp'.

    Derived from the parts that define *which* QSO it is. Editing exchange / RST
    / frequency keeps the same key; changing call / date / time / band / mode
    changes it (detected and treated as a rename).
    """
    call_b = re.sub(r"[^A-Z0-9]", "", (call or "").upper())
    band_b = str(band or "").strip().upper()
    mode_b = re.sub(r"[^A-Z0-9]", "", (mode or "").upper())
    ts = _parse_date_time(qso_date or "", time_on or "")
    seed = f"{call_b}|{ts:.0f}|{band_b}|{mode_b}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def normalize_band(raw: str) -> str:
    """Map N3FJP band labels ('10', '40', '20M', '14MHz') to 'XM' format.

    N3FJP BAND values are *meter* bands ("10" = the 10 m band), unlike N1MM
    which broadcasts MHz. Bare integers are therefore treated as meters first.
    """
    b = (raw or "").strip().lower()
    if not b:
        return "UNK"
    mapping = {
        "160m": "160M", "80m": "80M", "60m": "60M", "40m": "40M",
        "30m": "30M", "20m": "20M", "17m": "17M", "15m": "15M",
        "12m": "12M", "10m": "10M", "6m": "6M", "2m": "2M",
        # meter-band bare numbers (range 1.25 m .. 160 m)
        "160": "160M", "80": "80M", "60": "60M", "40": "40M",
        "30": "30M", "20": "20M", "17": "17M", "15": "15M",
        "12": "12M", "10": "10M", "6": "6M", "2": "2M",
        "1.25": "1.25M", "222": "222M", "420": "70CM", "432": "70CM",
        "902": "33CM", "1296": "23CM",
    }
    if b in mapping:
        return mapping[b]
    # Fallback: a value like '14MHz' or '7' interpreted as MHz.
    m = re.search(r"(\d+(?:\.\d+)?)", b)
    if m:
        try:
            mhz = float(m.group(1))
        except ValueError:
            return b
        bands = [
            (1.8, "160M"), (3.5, "80M"), (5.3, "60M"), (7.0, "40M"),
            (10.0, "30M"), (14.0, "20M"), (18.0, "17M"), (21.0, "15M"),
            (24.0, "12M"), (28.0, "10M"), (50.0, "6M"), (144.0, "2M"),
        ]
        best = min(bands, key=lambda x: abs(x[0] - mhz))
        if abs(best[0] - mhz) < 1.0:
            return best[1]
    return b


def normalize_mode(raw: str) -> str:
    """Normalise an N3FJP mode value to a friendly contest mode."""
    m = (raw or "").strip().upper()
    if not m:
        return "UNK"
    if m == "PH" or m in ("LSB", "USB"):
        return "SSB"
    return m


@dataclass
class N3FJPContact:
    """Decoded contact that mirrors the N1MMContact / QSORequest contract.

    The audio pipeline (``schedule_qso_slice`` / ``QSORequest``) reads these
    attributes; ``source``/``source_key`` are N3FJP reconciliation keys.
    """

    call: str
    band: str
    mode: str
    contest: str
    timestamp: float
    receive_ts: float = 0.0
    freq: str = field(default="")
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
    source: str = "n3fjp"
    source_key: str = ""


ContactCallback = Callable[["N3FJPContact"], None]


class N3FJPListener:
    """Connect to the N3FJP TCP API, dispatch contacts, reconcile edits."""

    def __init__(self, cfg, on_contact: ContactCallback):
        self.cfg = cfg
        self.on_contact = on_contact
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._reconcile_thread: Optional[threading.Thread] = None
        self._reconcile_cond = threading.Condition()
        self._reconcile_pending = False
        self._buf = ""
        self._state: dict = {}
        self._last_reconcile = 0.0
        self._last_list = 0.0
        self._last_error = ""

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="n3fjp-listener")
        self._thread.start()
        if self.cfg.n3fjp_reconcile_interval_s > 0:
            self._reconcile_thread = threading.Thread(
                target=self._reconcile_loop, daemon=True,
                name="n3fjp-reconcile")
            self._reconcile_thread.start()
        logger.info("N3FJP listener started (host=%s port=%d)",
                    self.cfg.n3fjp_host, self.cfg.n3fjp_port)

    def stop(self) -> None:
        self._running = False
        with self._reconcile_cond:
            self._reconcile_cond.notify_all()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    def is_connected(self) -> bool:
        return bool(self._connected)

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "host": f"{self.cfg.n3fjp_host}:{self.cfg.n3fjp_port}",
            "last_reconcile": self._last_reconcile,
            "last_list": self._last_list,
            "last_error": self._last_error,
        }

    # ── connection + read loop ───────────────────────────────────────────────
    def _run(self) -> None:
        while self._running:
            try:
                self._connect_once()
            except Exception as e:
                logger.debug("N3FJP run loop error: %s", e)
                time.sleep(3.0)

    def _connect_once(self) -> None:
        try:
            sock = socket.create_connection(
                (self.cfg.n3fjp_host, int(self.cfg.n3fjp_port)), timeout=5.0)
            sock.settimeout(1.0)
        except OSError as e:
            self._last_error = str(e)
            self._connected = False
            time.sleep(3.0)
            return
        self._sock = sock
        self._connected = True
        self._last_error = ""
        self._buf = ""
        logger.info("N3FJP connected: %s:%d", self.cfg.n3fjp_host,
                    self.cfg.n3fjp_port)
        try:
            self._handshake()
            self._read_loop()
        except (OSError, socket.timeout) as e:
            logger.debug("N3FJP connection dropped: %s", e)
        finally:
            self._connected = False
            try:
                sock.close()
            except Exception:
                pass
            self._sock = None
            if self._running:
                time.sleep(3.0)

    def _send(self, cmd: str) -> None:
        if self._sock is None:
            raise OSError("N3FJP not connected")
        payload = (cmd if cmd.endswith("\r\n") else cmd + "\r\n").encode("utf-8")
        self._sock.sendall(payload)

    def _handshake(self) -> None:
        self._send("<CMD><SETUPDATESTATE><VALUE>TRUE</VALUE></CMD>")
        self._send("<CMD><CALLTABENTEREVENTS><VALUE>TRUE</VALUE></CMD>")

    def _read_loop(self) -> None:
        while self._running:
            try:
                data = self._sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self._feed(data.decode("utf-8", "replace"))

    def _feed(self, text: str) -> None:
        self._buf += text
        inners, self._buf = extract_blocks(self._buf)
        for inner in inners:
            cmd_id, fields = parse_block(inner)
            self._handle_block(cmd_id, fields)

    # ── block dispatch ───────────────────────────────────────────────────────
    def _handle_block(self, cmd_id: str, fields: dict) -> None:
        cmd = cmd_id.upper()
        if cmd in ("ENTER", "ENTEREVENT"):
            self._handle_enter(fields)
        elif cmd == "UPDATERESPONSE":
            ctrl = (fields.get("CONTROL") or "").upper()
            if ctrl:
                self._state[ctrl] = fields.get("VALUE", "")
        elif cmd in ("READBMFRESPONSE", "READBMF"):
            if "FREQ" in fields:
                self._state["FREQ"] = fields.get("FREQ", "")
            if "BAND" in fields:
                self._state["BAND"] = fields.get("BAND", "")
            if "MODE" in fields:
                self._state["MODE"] = fields.get("MODE", "")
        elif cmd in ("EDITDELETEEVENT",):
            self._schedule_reconcile()
        elif cmd in ("CALLTABEVENT", "CALLTAB"):
            if fields.get("MYCALL"):
                self._state["MYCALL"] = fields.get("MYCALL", "")
            if fields.get("OPERATOR"):
                self._state["OPERATOR"] = fields.get("OPERATOR", "")
        else:
            logger.debug("N3FJP unhandled block: %s %s", cmd, fields)

    # ── ENTER event -> QSO ───────────────────────────────────────────────────
    def _handle_enter(self, fields: dict) -> None:
        f = {k.lower(): v for k, v in fields.items()}
        call = (f.get("call") or "").strip().upper()
        if not call:
            return
        band = normalize_band(f.get("band") or self._state.get("BAND", ""))
        mode = normalize_mode(f.get("mode") or f.get("modetest")
                              or self._state.get("MODE", ""))
        qso_date = (f.get("qso_date") or "").strip()
        time_on = (f.get("time_on") or "").strip()
        ts = _parse_date_time(qso_date, time_on)
        freq = (f.get("freq") or self._state.get("FREQ", "")).strip()
        name = (f.get("name") or "").strip()
        grid = (f.get("grid") or "").strip()
        snt = (f.get("snt") or self._state.get("TXTENTRYRSTS", "")).strip()
        rcv = (f.get("rcv") or self._state.get("TXTENTRYRSTR", "")).strip()
        section = (f.get("section")
                   or self._state.get("TXTENTRYSECTION", "")
                   or self._state.get("TXTENTRYSPC", "")).strip()
        rcvnr = (f.get("rcvnr")
                 or self._state.get("TXTENTRYSerialNoR", "")).strip()
        mycall = (f.get("mycall") or self._state.get("MYCALL", "")).strip()
        op = (f.get("operator") or self._state.get("OPERATOR", "")).strip()
        contest = (self.cfg.n3fjp_contest or "GENERAL").strip()
        contest = re.sub(r"\s+", "_", contest)

        source_key = compute_source_key(call, qso_date, time_on, band, mode)

        contact = N3FJPContact(
            call=call,
            band=band,
            mode=mode,
            contest=contest,
            timestamp=ts,
            receive_ts=time.time(),
            freq=freq,
            name=name,
            grid=grid,
            snt=snt,
            rcv=rcv,
            section=section,
            rcvnr=rcvnr,
            exchange=section or rcvnr,
            mycall=mycall,
            operator=op,
            raw_ts=_fmt_date_time(qso_date, time_on),
            source="n3fjp",
            source_key=source_key,
        )
        logger.info("N3FJP QSO: %s %s %s (key=%s)", call, band, mode,
                    source_key[:12])
        try:
            self.on_contact(contact)
        except Exception as e:
            logger.error("N3FJP callback error: %s", e)

    # ── reconciliation ───────────────────────────────────────────────────────
    def _schedule_reconcile(self) -> None:
        with self._reconcile_cond:
            self._reconcile_pending = True
            self._reconcile_cond.notify_all()

    def _reconcile_loop(self) -> None:
        interval = max(10, int(getattr(self.cfg, "n3fjp_reconcile_interval_s", 60)))
        last_check = 0.0
        while self._running:
            with self._reconcile_cond:
                self._reconcile_cond.wait(timeout=1.0)
                pending = self._reconcile_pending
                self._reconcile_pending = False
            if not self._connected:
                continue
            if pending or time.time() - last_check >= interval:
                last_check = time.time()
                try:
                    self._do_reconcile()
                except Exception as e:
                    logger.debug("N3FJP reconcile error: %s", e)

    def _do_reconcile(self) -> None:
        if not self._connected:
            return
        window = max(5, int(getattr(self.cfg, "n3fjp_list_window", 100)))
        cmd = "<CMD><LIST><INCLUDEALL><VALUE>%d</VALUE></CMD>" % window
        blocks = self._request(cmd)
        self._last_list = time.time()
        self._last_reconcile = time.time()
        if not blocks:
            return

        recs = self._parse_records(blocks)
        if not recs:
            logger.debug("N3FJP reconcile: no records parsed from LIST")
            return

        keys: dict = {}
        for r in recs:
            k = compute_source_key(r["call"], r["date"], r["time"], r["band"],
                                   r["mode"])
            keys.setdefault(k, r)
        floor = min((_parse_date_time(r["date"], r["time"]) for r in recs),
                    default=0.0)

        import db as qso_db
        rows = qso_db.list_source_rows("n3fjp", window * 2)
        in_window = [r for r in rows
                     if r.get("timestamp") and r["timestamp"] >= floor - 60]

        for r in in_window:
            k = r.get("source_key")
            if not k:
                continue
            if k in keys:
                self._sync_metadata(r, keys[k])
            else:
                self._treat_missing(r, keys)

    def _sync_metadata(self, dbrow: dict, remote: dict) -> None:
        """Update changed metadata for a QSO still present on both sides."""
        import db as qso_db
        band = normalize_band(remote.get("band", ""))
        mode = normalize_mode(remote.get("mode", remote.get("modetest", "")))
        freq = (remote.get("freq") or "").strip()
        fields: dict = {}
        if band and band != "UNK" and band != dbrow.get("band"):
            fields["band"] = band
        if mode and mode != "UNK" and mode != dbrow.get("mode"):
            fields["mode"] = mode
        if freq and freq != dbrow.get("freq"):
            fields["freq"] = freq
        if fields:
            qso_db.update_fields_by_source_key("n3fjp", dbrow["source_key"],
                                               **fields)
            logger.info("N3FJP reconciled metadata for %s: %s",
                        dbrow.get("call"), fields)

    def _treat_missing(self, dbrow: dict, keys: dict) -> None:
        """Handle a DB row absent from the logger LIST (delete or rename)."""
        import db as qso_db
        k = dbrow.get("source_key")
        if not k:
            return
        db_ts = dbrow.get("timestamp") or 0.0

        renames = []
        for other_key, r in keys.items():
            if other_key == k:
                continue
            ts = _parse_date_time(r.get("date", ""), r.get("time", ""))
            if abs(ts - db_ts) <= 180 and r.get("call"):
                renames.append((other_key, r, ts))
        if len(renames) == 1:
            other_key, r, ts = renames[0]
            new_call = (r.get("call") or "").strip().upper()
            if new_call:
                logger.info("N3FJP edit detected: %s -> %s (rename audio)",
                            dbrow.get("call"), new_call)
                new_band = normalize_band(r.get("band", ""))
                new_mode = normalize_mode(r.get("mode", r.get("modetest", "")))
                year = time.strftime("%Y", time.localtime(ts))
                contest = getattr(self.cfg, "n3fjp_contest", "") or "GENERAL"
                contest_dir = f"{year}_{contest}" if contest else f"{year}_GENERAL"
                try:
                    qso_db.rename_qso_audio_by_key(
                        "n3fjp", k, other_key, new_call, new_band,
                        contest_dir, "RX1", ts,
                        call=new_call, band=new_band, mode=new_mode,
                        freq=(r.get("freq") or "").strip(),
                        raw_ts=_fmt_date_time(r.get("date", ""), r.get("time", "")),
                    )
                    keys.pop(other_key, None)
                except Exception as e:
                    logger.warning("N3FJP rename failed for %s: %s", k, e)
                return

        # Several QSOs were logged near this record's time -> we cannot tell a
        # rename from a deletion; leave the row untouched rather than risk
        # removing the wrong recording.
        if renames:
            logger.debug("N3FJP %d ambiguous candidate(s) near %s; leaving as-is",
                         len(renames), k[:12])
            return

        # No rename candidate -> actual deletion.
        age = time.time() - db_ts
        if age > 7 * 86400:
            logger.debug("N3FJP skip unreconciled old record %s (age %.0f d)",
                         k, age / 86400)
            return
        fp = qso_db.delete_qso_by_source_key("n3fjp", k)
        if fp:
            full = os.path.join(RECORDINGS_DIR, fp)
            try:
                if os.path.isfile(full):
                    os.remove(full)
                    logger.info("Removed audio for deleted N3FJP QSO: %s", fp)
            except OSError as e:
                logger.warning("Could not remove audio %s: %s", full, e)

    def _parse_records(self, blocks: List[Tuple[str, dict]]) -> List[dict]:
        """Extract QSO records from LIST response blocks (tolerant parser)."""
        recs: List[dict] = []
        for _cid, fields in blocks:
            call = (fields.get("CALL") or "").strip()
            if not call:
                continue
            recs.append({
                "call": call,
                "date": (fields.get("QSO_DATE") or fields.get("DATE")
                         or fields.get("DATEON") or "").strip(),
                "time": (fields.get("TIME_ON") or fields.get("TIME")
                         or fields.get("TIMEON") or "").strip(),
                "band": (fields.get("BAND") or "").strip(),
                "mode": (fields.get("MODE") or fields.get("MODETEST")
                         or "").strip(),
                "freq": (fields.get("FREQ") or "").strip(),
            })
        return recs

    def _request(self, cmd: str) -> List[Tuple[str, dict]]:
        """Send *cmd* and collect LIST response blocks for a short window.

        Non-LIST blocks (e.g. a live ENTEREVENT / UPDATERESPONSE) that arrive
        while we wait are dispatched through the normal handler so a QSO is
        never lost, and only LIST/LISTRESPONSE blocks are returned.
        """
        out: List[Tuple[str, dict]] = []
        if not self._connected or self._sock is None:
            return out
        try:
            self._send(cmd)
        except OSError as e:
            logger.debug("N3FJP list send error: %s", e)
            return out
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                data = self._sock.recv(65535)
            except socket.timeout:
                if out:
                    break
                continue
            except OSError:
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            inners, _ = extract_blocks(text)
            for inner in inners:
                cid, fields = parse_block(inner)
                upper = cid.upper()
                if upper in ("LIST", "LISTRESPONSE"):
                    out.append((cid, fields))
                    deadline = min(deadline, time.time() + 0.6)
                else:
                    self._handle_block(cid, fields)
            if time.time() >= deadline:
                break
        return out


def schedule_qso_slice(contact: N3FJPContact, source, cfg) -> None:
    """Defer slicing by post_roll, then call Source.slice_qso (N3F source)."""
    time.sleep(cfg.post_roll)
    from audio_manager import QSORequest

    req = QSORequest(
        call=contact.call,
        band=contact.band,
        mode=contact.mode,
        contest=contact.contest,
        timestamp=contact.timestamp,
        receive_ts=contact.receive_ts,
        pre_roll=cfg.pre_roll,
        post_roll=cfg.post_roll,
        freq=contact.freq,
        name=contact.name,
        grid=contact.grid,
        exchange=contact.exchange,
        rcv=contact.rcv,
        snt=contact.snt,
        rcvnr=contact.rcvnr,
        sntnr=contact.sntnr,
        section=contact.section,
        mycall=contact.mycall,
        operator=contact.operator,
        raw_ts=contact.raw_ts,
        source=getattr(contact, "source", ""),
        source_key=getattr(contact, "source_key", ""),
    )
    try:
        source.slice_qso(req)
    except Exception as e:
        logger.error("N3FJP slice failed: %s", e)