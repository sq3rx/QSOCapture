"""n1mm_listener.py - Async UDP listener for N1MM Logger+ broadcasts.

N1MM Logger+ emits UDP datagrams on port 12060 describing contacts and radio
state. This module opens a non-blocking UDP socket, parses the XML contact
messages, and schedules a *delayed* background slicing task for each logged
QSO. The delay equals ``post_roll`` seconds so that the tail of the QSO audio
is guaranteed to be present in the circular buffer before we slice.

Only the relevant subset of the N1MM schema is decoded:

* ``<contactinfo>`` packets (type 0 / contact logged) carry: call, band,
  mode, timestamp, contest name.

The listener is intentionally transport-only; it hands parsed QSO data to a
callback supplied by the application (which then talks to the audio sources).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("QSOCapture.n1mm")


@dataclass
class N1MMContact:
    """Decoded contact information from an N1MM UDP packet.

    N1MM ``<contactinfo>`` packets carry a rich set of fields. We keep the
    ones that are useful for an audio QSO recorder / contest logger so they
    can be persisted and shown on the dashboard.
    """

    call: str
    band: str
    mode: str
    contest: str
    timestamp: float          # epoch seconds (received time)
    raw_ts: str = ""          # original N1MM timestamp string if present
    freq: str = ""            # frequency reported by N1MM (e.g. "14.023")
    name: str = ""            # operator name (other station)
    qth: str = ""             # QTH / state
    grid: str = ""            # locator (e.g. JO90)
    comment: str = ""         # free comment
    exchange: str = ""        # exchange / RST (exch1)
    exchange2: str = ""       # exch2
    exchange3: str = ""       # exch3
    rcvnr: str = ""          # received serial number (<rcvnr>)
    operator: str = ""        # logging operator (our side)
    station: str = ""         # station name (our side)
    contest_nr: str = ""      # contest number
    points: str = ""          # points awarded
    multiplier: str = ""      # is multiplier (1/0)


# Function signature the listener calls when a contact is received.
#   contact -> None
ContactCallback = Callable[[N1MMContact], None]


class N1MMListener:
    """Bind to a UDP port and dispatch decoded contacts to a callback."""

    def __init__(self, cfg, on_contact: ContactCallback):
        self.cfg = cfg
        self.on_contact = on_contact
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
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

    # -- receive loop -----------------------------------------------------
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

    # -- parsing ----------------------------------------------------------
    def _handle_packet(self, data: bytes) -> None:
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            return

        if "<contactinfo>" not in text and "<ContactInfo>" not in text:
            # Ignore radio packets / other message types for now.
            return

        # Log the full received N1MM packet so we can inspect exactly which
        # fields (e.g. RcvdExchange, exch1, rcvnr) carry the data we want.
        logger.info("N1MM raw packet:\n%s", text)

        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            logger.debug("N1MM: failed to parse XML packet")
            return

        # <contactinfo> or <ContactInfo>
        call = self._find_text(root, "call")
        if not call:
            return
        band = self._find_text(root, "band") or "UNK"
        mode = self._find_text(root, "mode") or "UNK"
        contest = self._find_text(root, "ContestName") or self.cfg.default_contest
        ts_raw = self._find_text(root, "timestamp") or ""
        # Frequency: prefer transmitted frequency, fall back to received.
        freq = (self._find_text(root, "txfreq") or self._find_text(root, "rxfreq")
                or self._find_text(root, "freq") or "").strip()

        # The dashboard 'Exch' column is driven by ``exchange``. N1MM puts
        # the *received* exchange in different places depending on the contest:
        #   * <RcvdExchange>  - full received exchange (e.g. "599 40")
        #   * <exch1>/<exchange> - exchange component(s); N1MM sometimes
        #                         yields a literal "0" here for zone contests
        #   * <rcv> + <rcvnr>  - received RST + received serial/zone
        # We prefer the full received exchange, then the exchange component
        # (skipping a bare "0"), then fall back to the RST/serial combo. This
        # keeps the real received value (e.g. the CQWW zone) instead of "0".
        rcvd = (self._find_text(root, "RcvdExchange") or "").strip()
        exch1 = (self._find_text(root, "exch1") or self._find_text(root, "exchange") or "").strip()
        rcvnr = (self._find_text(root, "rcvnr") or "").strip()
        # Zone contests (e.g. CQWW) report the received zone in <zone>;
        # <rcvnr> is "0" there. The <RcvdExchange>/<rcv> fields also carry
        # the RST (e.g. "599 25"), but for the Exch column we only want the
        # received exchange component (zone/serial), never the RST.
        zone = (self._find_text(root, "zone") or "").strip()

        def _strip_rst(s: str) -> str:
            # Drop a leading RST token like "599" / "59" from a full exchange.
            parts = s.split()
            kept = [p for p in parts if not (p.isdigit() and p[0] == "5" and 2 <= len(p) <= 3)]
            return " ".join(kept).strip() or s

        if zone:
            exchange = zone
        elif rcvnr and rcvnr != "0":
            exchange = rcvnr
        elif rcvd:
            exchange = _strip_rst(rcvd)
        elif exch1 and exch1 != "0":
            exchange = exch1
        else:
            exchange = ""

        contact = N1MMContact(
            call=call.strip().upper(),
            band=self._normalize_band(band),
            mode=mode.strip().upper(),
            contest=contest.strip() or self.cfg.default_contest,
            timestamp=time.time(),
            raw_ts=ts_raw,
            freq=freq,
            name=(self._find_text(root, "name") or "").strip(),
            qth=(self._find_text(root, "qth") or "").strip(),
            grid=(self._find_text(root, "grid") or "").strip(),
            comment=(self._find_text(root, "comment") or "").strip(),
            exchange=exchange.strip(),
            exchange2=(self._find_text(root, "exch2") or "").strip(),
            exchange3=(self._find_text(root, "exch3") or "").strip(),
            rcvnr=rcvnr.strip(),
            operator=(self._find_text(root, "operator") or "").strip(),
            station=(self._find_text(root, "stationname") or "").strip(),
            contest_nr=(self._find_text(root, "contestnr") or "").strip(),
            points=(self._find_text(root, "points") or "").strip(),
            multiplier=(self._find_text(root, "ismultiplier1") or "").strip(),
        )
        logger.info("N1MM contact: %s %s %s", contact.call, contact.band, contact.mode)
        try:
            self.on_contact(contact)
        except Exception as e:
            logger.error("N1MM callback error: %s", e)

    @staticmethod
    def _find_text(root: ET.Element, tag: str) -> Optional[str]:
        """Case-insensitive tag lookup."""
        for child in root.iter():
            if child.tag.lower() == tag.lower():
                return child.text
        return None

    @staticmethod
    def _normalize_band(band: str) -> str:
        """Map N1MM band strings (e.g. '14MHz', '20m') to a short label."""
        b = band.strip().lower()
        mapping = {
            "160m": "160M", "80m": "80M", "40m": "40M", "30m": "30M",
            "20m": "20M", "17m": "17M", "15m": "15M", "12m": "12M",
            "10m": "10M", "6m": "6M", "2m": "2M",
            "1.8mhz": "160M", "3.5mhz": "80M", "7mhz": "40M",
            "14mhz": "20M", "21mhz": "15M", "28mhz": "10M",
        }
        return mapping.get(b, band.strip().upper())


# ---------------------------------------------------------------------------
# Delayed slicing helper (used by main.py)
# ---------------------------------------------------------------------------
def schedule_qso_slice(contact: N1MMContact, source, cfg,
                       executor: threading.Thread) -> None:
    """Schedule a post-roll delayed slice on a dedicated worker thread.

    The actual slicing is deferred by ``cfg.post_roll`` seconds so the audio
    tail is captured. This mirrors classic qsorder behaviour.
    """

    def _delayed() -> None:
        time.sleep(cfg.post_roll)
        from audio_manager import QSORequest

        req = QSORequest(
            call=contact.call,
            band=contact.band,
            mode=contact.mode,
            contest=contact.contest,
            timestamp=contact.timestamp,
            pre_roll=cfg.pre_roll,
            post_roll=cfg.post_roll,
            # Forward the rich N1MM metadata so it is persisted to the DB
            # and shown in the dashboard (freq, exchange, name, QTH, ...).
            freq=contact.freq,
            name=contact.name,
            qth=contact.qth,
            grid=contact.grid,
            comment=contact.comment,
            exchange=contact.exchange,
            exchange2=contact.exchange2,
            exchange3=contact.exchange3,
            operator=contact.operator,
            station=contact.station,
            contest_nr=contact.contest_nr,
            points=contact.points,
            multiplier=contact.multiplier,
            raw_ts=contact.raw_ts,
        )
        try:
            source.slice_qso(req)
        except Exception as e:
            logger.error("QSO slice failed: %s", e)

    t = threading.Thread(target=_delayed, daemon=True, name="qso-slice")
    t.start()