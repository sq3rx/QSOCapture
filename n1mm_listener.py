"""Async UDP listener for N1MM Logger+ broadcasts.

Opens a non-blocking UDP socket, parses XML contact messages, and schedules
a delayed background slicing task for each logged QSO (delay = post_roll
seconds so the audio tail is in the circular buffer before slicing).

Decodes the full N1MM <contactinfo> schema plus <contactreplace> and
<contactdelete> variants so edited/deleted QSOs stay in sync with audio files.

Key facts about the N1MM schema:
* Frequency is in units of 10 Hz (e.g. 352519 = 3.52519 MHz). We convert to MHz.
* <timestamp> is the actual QSO time; receive_ts (packet arrival) drives the
  slicing offset against the live audio buffer.
* <ID> is a 32-byte GUID for de-duplication and matching replace/delete.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from config import RECORDINGS_DIR

logger = logging.getLogger("QSOCapture.n1mm")


@dataclass
class N1MMContact:
    """Decoded contact from an N1MM UDP packet.

    Fields useful for an audio QSO recorder / contest logger, persisted to DB
    and shown on the dashboard.

    * timestamp  - epoch seconds of the QSO (from N1MM <timestamp>)
    * receive_ts - epoch seconds when the UDP packet arrived
    """

    call: str
    band: str
    mode: str
    contest: str
    timestamp: float
    raw_ts: str = ""
    receive_ts: float = 0.0
    freq: str = ""
    name: str = ""
    qth: str = ""
    grid: str = ""
    comment: str = ""
    exchange: str = ""
    exchange2: str = ""
    exchange3: str = ""
    rcvnr: str = ""
    rcv: str = ""
    snt: str = ""
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


ContactCallback = Callable[[N1MMContact], None]


class N1MMListener:
    """Bind to a UDP port and dispatch decoded contacts to a callback."""

    def __init__(self, cfg, on_contact: ContactCallback):
        self.cfg = cfg
        self.on_contact = on_contact
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.cfg.n1mm_bind_ip, self.cfg.n1mm_udp_port))
        self._sock.setblocking(False)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="n1mm-listener")
        self._thread.start()
        logger.info("N1MM listener started on %s:%d",
                    self.cfg.n1mm_bind_ip, self.cfg.n1mm_udp_port)

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except OSError:
                if self._running:
                    time.sleep(0.1)
                continue
            self._handle_packet(data)

    def _handle_packet(self, data: bytes) -> None:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            logger.debug("N1MM: failed to parse XML packet")
            return

        logger.debug("N1MM raw packet:\n%s", text)

        tag = root.tag.lower()
        if tag == "contactdelete":
            self._handle_delete(root)
            return
        if tag == "contactreplace":
            info = root.find("contactinfo") or root.find("ContactInfo")
            self._handle_replace(info if info is not None else root)
            return

        if "contactinfo" not in tag:
            return
        self._handle_contact(root)

    def _handle_contact(self, root: ET.Element) -> None:
        fields: dict = {}
        for child in root.iter():
            if child.text is not None and child.tag.lower() not in fields:
                fields[child.tag.lower()] = child.text

        def g(tag: str) -> str:
            return fields.get(tag.lower(), "") or ""

        call = g("call")
        if not call:
            return
        band = g("band") or "UNK"
        mode = g("mode") or "UNK"
        contest = g("ContestName") or "GENERAL"
        ts_raw = g("timestamp")

        # Frequency in units of 10 Hz (e.g. 352519 = 3.52519 MHz).
        freq_raw = (g("txfreq") or g("rxfreq") or g("freq")).strip()
        freq = self._parse_freq(freq_raw)

        # Build exchange: prefer zone, then rcvnr, then stripped RcvdExchange, then exch1.
        rcvd = g("RcvdExchange").strip()
        exch1 = (g("exch1") or g("exchange") or g("exchange1")).strip()
        rcvnr = g("rcvnr").strip()
        zone = g("zone").strip()

        def _strip_rst(s: str) -> str:
            parts = s.split()
            kept = [p for p in parts
                    if not (p.isdigit() and p[0] == "5" and 2 <= len(p) <= 3)]
            return " ".join(kept).strip() or s

        if zone and zone != "0":
            exchange = zone
        elif rcvnr and rcvnr != "0":
            exchange = rcvnr
        elif rcvd:
            exchange = _strip_rst(rcvd)
        elif exch1 and exch1 != "0":
            exchange = exch1
        else:
            exchange = ""

        qso_epoch = self._parse_ts(ts_raw)
        receive_ts = time.time()

        contact = N1MMContact(
            call=call.strip().upper(),
            band=self._normalize_band(band),
            mode=mode.strip().upper(),
            contest=contest.strip() or "GENERAL",
            timestamp=qso_epoch,
            raw_ts=ts_raw,
            receive_ts=receive_ts,
            freq=freq,
            name=g("name").strip(),
            qth=g("qth").strip(),
            grid=g("gridsquare").strip(),
            comment=g("comment").strip(),
            exchange=exchange.strip(),
            exchange2=(g("exch2") or g("exchange2")).strip(),
            exchange3=(g("exch3") or g("exchange3")).strip(),
            rcvnr=rcvnr.strip(),
            rcv=g("rcv").strip(),
            snt=g("snt").strip(),
            sntnr=g("sntnr").strip(),
            section=g("section").strip(),
            mycall=g("mycall").strip(),
            countryprefix=g("countryprefix").strip(),
            wpxprefix=g("wpxprefix").strip(),
            continent=g("continent").strip(),
            operator=g("operator").strip(),
            station=g("stationname").strip(),
            contest_nr=g("contestnr").strip(),
            points=g("points").strip(),
            multiplier=g("ismultiplier1").strip(),
            multiplier2=g("ismultiplier2").strip(),
            multiplier3=g("ismultiplier3").strip(),
            prec=g("prec").strip(),
            ck=g("ck").strip(),
            power=g("power").strip(),
            n1mm_id=g("id").strip(),
            is_claimed=g("isclaimedqso").strip(),
            sent_exchange=g("sentexchange").strip(),
            radio_nr=(g("radionr") or "1").strip(),
        )
        logger.info("N1MM contact: %s %s %s", contact.call, contact.band, contact.mode)
        logger.debug("N1MM contact fields: freq=%s exch=%s n1mm_id=%s rcv=%s snt=%s section=%s",
                     contact.freq, contact.exchange, contact.n1mm_id,
                     contact.rcv, contact.snt, contact.section)
        try:
            self.on_contact(contact)
        except Exception as e:
            logger.error("N1MM callback error: %s", e)

    def _handle_delete(self, root: ET.Element) -> None:
        """Handle a <contactdelete> packet: drop the DB row + audio file."""
        fields: dict = {}
        for child in root.iter():
            if child.text is not None and child.tag.lower() not in fields:
                fields[child.tag.lower()] = child.text
        n1mm_id = (fields.get("id") or "").strip()
        if not n1mm_id:
            return
        logger.debug("N1MM raw delete packet:\n%s", ET.tostring(root, encoding="unicode"))
        logger.info("N1MM contactdelete: %s", n1mm_id)
        try:
            import db as qso_db
            fp = qso_db.delete_qso_by_n1mm_id(n1mm_id)
            if fp:
                full = os.path.join(RECORDINGS_DIR, fp)
                try:
                    if os.path.isfile(full):
                        os.remove(full)
                        logger.info("Removed audio file for deleted QSO: %s", fp)
                except OSError as e:
                    logger.warning("Could not remove audio file %s: %s", full, e)
        except Exception as e:
            logger.error("N1MM delete handling error: %s", e)

    def _handle_replace(self, root: ET.Element) -> None:
        """Handle a <contactreplace> packet: rename audio file + update DB in-place.

        Unlike _handle_contact() which creates a new audio slice, this method
        renames the existing audio file on disk to reflect the updated call/band
        and updates the DB record — the original recording is preserved.
        """
        fields: dict = {}
        for child in root.iter():
            if child.text is not None and child.tag.lower() not in fields:
                fields[child.tag.lower()] = child.text

        def g(tag: str) -> str:
            return fields.get(tag.lower(), "") or ""

        n1mm_id = g("id").strip()
        if not n1mm_id:
            logger.debug("N1MM contactreplace without id, ignoring")
            return

        new_call = g("call").strip().upper()
        if not new_call:
            logger.debug("N1MM contactreplace without call, ignoring")
            return

        new_band = self._normalize_band(g("band") or "UNK")
        ts_raw = g("timestamp")
        qso_epoch = self._parse_ts(ts_raw)
        rx_label = "RX2" if (g("radionr") or "1").strip() == "2" else "RX1"
        contest = g("ContestName") or "GENERAL"
        year = time.strftime("%Y", time.localtime(qso_epoch))
        contest_dir = f"{year}_{contest}" if contest else f"{year}_GENERAL"

        logger.info("N1MM contactreplace: %s -> %s (n1mm_id=%s)", n1mm_id, new_call, n1mm_id)

        try:
            import db as qso_db
            # Build freq and exchange the same way as _handle_contact
            freq_raw = (g("txfreq") or g("rxfreq") or g("freq")).strip()
            freq = self._parse_freq(freq_raw)
            rcvd = g("RcvdExchange").strip()
            exch1 = (g("exch1") or g("exchange") or g("exchange1")).strip()
            rcvnr = g("rcvnr").strip()
            zone = g("zone").strip()

            def _strip_rst(s: str) -> str:
                parts = s.split()
                kept = [p for p in parts
                        if not (p.isdigit() and p[0] == "5" and 2 <= len(p) <= 3)]
                return " ".join(kept).strip() or s

            if zone and zone != "0":
                exchange = zone
            elif rcvnr and rcvnr != "0":
                exchange = rcvnr
            elif rcvd:
                exchange = _strip_rst(rcvd)
            elif exch1 and exch1 != "0":
                exchange = exch1
            else:
                exchange = ""

            new_path = qso_db.rename_qso_audio(
                n1mm_id=n1mm_id,
                new_call=new_call,
                new_band=new_band,
                new_contest_dir=contest_dir,
                new_rx_label=rx_label,
                timestamp=qso_epoch,
                contest=contest_dir,
                call=new_call,
                band=new_band,
                mode=g("mode").strip().upper(),
                freq=freq,
                name=g("name").strip(),
                qth=g("qth").strip(),
                grid=g("gridsquare").strip(),
                comment=g("comment").strip(),
                exchange=exchange.strip(),
                exchange2=(g("exch2") or g("exchange2")).strip(),
                exchange3=(g("exch3") or g("exchange3")).strip(),
                rcv=g("rcv").strip(),
                snt=g("snt").strip(),
                rcvnr=rcvnr.strip(),
                sntnr=g("sntnr").strip(),
                section=g("section").strip(),
                mycall=g("mycall").strip(),
                countryprefix=g("countryprefix").strip(),
                wpxprefix=g("wpxprefix").strip(),
                continent=g("continent").strip(),
                operator=g("operator").strip(),
                station=g("stationname").strip(),
                contest_nr=g("contestnr").strip(),
                points=g("points").strip(),
                multiplier=g("ismultiplier1").strip(),
                multiplier2=g("ismultiplier2").strip(),
                multiplier3=g("ismultiplier3").strip(),
                prec=g("prec").strip(),
                ck=g("ck").strip(),
                power=g("power").strip(),
                is_claimed=g("isclaimedqso").strip(),
                sent_exchange=g("sentexchange").strip(),
                raw_ts=ts_raw,
            )
            if new_path:
                logger.info("Audio file renamed and DB updated for edited QSO: %s", new_path)
            else:
                logger.debug("N1MM contactreplace: no existing record for %s (may be first contact)", n1mm_id)
        except Exception as e:
            logger.error("N1MM contactreplace handling error: %s", e)

    @staticmethod
    def _parse_freq(raw: str) -> str:
        """Convert N1MM frequency (units of 10 Hz) to MHz string. Example: "352519" -> "3.52519"."""
        raw = (raw or "").strip()
        if not raw:
            return ""
        try:
            hz = int(raw) * 10
        except ValueError:
            return raw
        mhz = hz / 1_000_000.0
        return f"{mhz:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _parse_ts(ts: str) -> float:
        """Parse N1MM <timestamp> (YYYY-MM-DD HH:MM[:SS]).

        N1MM broadcasts <timestamp> in UTC, so parse it as an aware UTC datetime
        and convert to an epoch seconds value. No local-timezone interpretation.
        """
        ts = (ts or "").strip()
        if not ts:
            return time.time()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return time.time()

    @staticmethod
    def _normalize_band(band: str) -> str:
        """Map N1MM band strings (e.g. '14MHz', '20m', '14') to a short 'XM' label."""
        import re
        b = band.strip().lower()
        mapping = {
            "160m": "160M", "80m": "80M", "40m": "40M", "30m": "30M",
            "20m": "20M", "17m": "17M", "15m": "15M", "12m": "12M",
            "10m": "10M", "6m": "6M", "2m": "2M",
            "1.8mhz": "160M", "3.5mhz": "80M", "5.3mhz": "60M", "7mhz": "40M",
            "14mhz": "20M", "21mhz": "15M", "28mhz": "10M",
        }
        if b in mapping:
            return mapping[b]
        # Bare MHz number, e.g. '14' -> 20M, '7' -> 40M.
        m = re.search(r'(\d+(?:\.\d+)?)', b)
        if m:
            try:
                mhz = float(m.group(1))
            except ValueError:
                return band.strip().upper()
            bands = [
                (1.8, "160M"), (3.5, "80M"), (5.3, "60M"), (7.0, "40M"),
                (10.0, "30M"), (14.0, "20M"), (18.0, "17M"), (21.0, "15M"),
                (24.0, "12M"), (28.0, "10M"), (50.0, "6M"), (144.0, "2M"),
            ]
            best = min(bands, key=lambda x: abs(x[0] - mhz))
            if abs(best[0] - mhz) < 1.0:
                return best[1]
        return band.strip().upper()


def schedule_qso_slice(contact: N1MMContact, source, cfg) -> None:
    """Run a post-roll delayed slice for a contact.

    Defers slicing by cfg.post_roll seconds so the audio tail is captured.
    Called from main.py via a bounded ThreadPoolExecutor.
    """
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
        qth=contact.qth,
        grid=contact.grid,
        comment=contact.comment,
        exchange=contact.exchange,
        exchange2=contact.exchange2,
        exchange3=contact.exchange3,
        rcv=contact.rcv,
        snt=contact.snt,
        rcvnr=contact.rcvnr,
        sntnr=contact.sntnr,
        section=contact.section,
        mycall=contact.mycall,
        countryprefix=contact.countryprefix,
        wpxprefix=contact.wpxprefix,
        continent=contact.continent,
        operator=contact.operator,
        station=contact.station,
        contest_nr=contact.contest_nr,
        points=contact.points,
        multiplier=contact.multiplier,
        multiplier2=contact.multiplier2,
        multiplier3=contact.multiplier3,
        prec=contact.prec,
        ck=contact.ck,
        power=contact.power,
        n1mm_id=contact.n1mm_id,
        is_claimed=contact.is_claimed,
        sent_exchange=contact.sent_exchange,
        radio_nr=contact.radio_nr,
        raw_ts=contact.raw_ts,
    )
    try:
        source.slice_qso(req)
    except Exception as e:
        logger.error("QSO slice failed: %s", e)