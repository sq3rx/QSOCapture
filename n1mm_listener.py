"""n1mm_listener.py - Async UDP listener for N1MM Logger+ broadcasts.

N1MM Logger+ emits UDP datagrams on port 12060 describing contacts and radio
state. This module opens a non-blocking UDP socket, parses the XML contact
messages, and schedules a *delayed* background slicing task for each logged
QSO. The delay equals ``post_roll`` seconds so that the tail of the QSO audio
is guaranteed to be present in the circular buffer before we slice.

We decode the full N1MM ``<contactinfo>`` schema that is relevant for an audio
QSO recorder / contest logger, plus the ``<contactreplace>`` and
``<contactdelete>`` variants so edited/deleted QSOs stay in sync with the
audio files on disk.

Key facts about the N1MM schema handled here:

* Frequency is reported in **units of 10 Hz** (e.g. ``352519`` = 3.52519 MHz).
  We convert it to a human-readable MHz string.
* ``<timestamp>`` is the *actual* QSO time; we keep that for display/filtering
  (``timestamp``) and also record ``receive_ts`` (packet arrival) for the
  slicing offset against the live audio buffer.
* ``<ID>`` is a 32-byte GUID that uniquely identifies each contact and is used
  for de-duplication and for matching replace/delete packets.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Optional

from config import RECORDINGS_DIR

logger = logging.getLogger("QSOCapture.n1mm")


@dataclass
class N1MMContact:
    """Decoded contact information from an N1MM UDP packet.

    N1MM ``<contactinfo>`` packets carry a rich set of fields. We keep the
    ones that are useful for an audio QSO recorder / contest logger so they
    can be persisted and shown on the dashboard.

    Time handling:
      * ``timestamp``  - epoch seconds of the actual QSO, taken from the N1MM
        ``<timestamp>`` tag (falling back to packet arrival time).
      * ``receive_ts`` - epoch seconds when the UDP packet arrived. Used to
        compute the slicing offset against the live audio buffer.
    """

    call: str
    band: str
    mode: str
    contest: str
    timestamp: float          # epoch seconds of the QSO (from N1MM <timestamp>)
    raw_ts: str = ""          # original N1MM timestamp string if present
    receive_ts: float = 0.0   # epoch seconds when the packet was received
    freq: str = ""            # frequency in MHz (converted from N1MM 10 Hz units)
    name: str = ""            # operator name (other station)
    qth: str = ""             # QTH / state
    grid: str = ""            # locator / gridsquare (e.g. JO90)
    comment: str = ""         # free comment
    exchange: str = ""        # received exchange component (exch1/rcvnr/zone)
    exchange2: str = ""       # exch2
    exchange3: str = ""       # exch3
    rcvnr: str = ""           # received serial/zone number (<rcvnr>)
    rcv: str = ""             # received RST (e.g. "599")
    snt: str = ""             # sent RST (e.g. "599")
    sntnr: str = ""           # sent serial number (<sntnr>)
    section: str = ""         # contest section / state (<section>)
    mycall: str = ""          # our own callsign (<mycall>)
    countryprefix: str = ""   # DXCC country prefix (<countryprefix>)
    wpxprefix: str = ""       # WPX prefix (<wpxprefix>)
    continent: str = ""       # continent (e.g. NA)
    operator: str = ""        # logging operator (our side)
    station: str = ""         # station name (StationName)
    contest_nr: str = ""      # contest number (<contestnr>)
    points: str = ""          # points awarded (<points>)
    multiplier: str = ""      # ismultiplier1 (1/0)
    multiplier2: str = ""     # ismultiplier2 (1/0)
    multiplier3: str = ""     # ismultiplier3 (1/0)
    prec: str = ""            # precedence (<prec>)
    ck: str = ""              # check (<ck>)
    power: str = ""           # received power exchange (<power>)
    n1mm_id: str = ""         # 32-byte GUID identifier for the contact (<ID>)
    is_claimed: str = ""      # IsClaimedQso (1 default, 0 for X-QSO)
    sent_exchange: str = ""   # SentExchange (our sent exchange)
    radio_nr: str = "1"       # Radio number (1 or 2) from N1MM <RadioNr> (SO2R)


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
            # A contactreplace wraps a full <contactinfo> plus the original
            # call/timestamp (oldcall/oldtimestamp) used by N1MM to identify
            # the record being edited. The GUID <ID> stays the same, so we can
            # just re-parse the embedded contactinfo and upsert by ID.
            info = root.find("contactinfo")
            if info is None:
                info = root.find("ContactInfo")
            if info is None:
                logger.debug("N1MM: contactreplace without nested contactinfo")
                return
            self._handle_contact(info)
            return

        # Ignore radio packets / other message types.
        if "contactinfo" not in tag:
            return
        self._handle_contact(root)

    def _handle_contact(self, root: ET.Element) -> None:
        # Build a single case-insensitive {tag: text} map once instead of
        # calling root.iter() for every field (which is O(n) per lookup).
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

        # Frequency: prefer transmitted frequency, fall back to received.
        # N1MM sends frequency in units of 10 Hz (e.g. 352519 = 3.52519 MHz).
        freq_raw = (g("txfreq") or g("rxfreq") or g("freq")).strip()
        freq = self._parse_freq(freq_raw)
        logger.debug("N1MM freq debug: TXFreq=%r RXFreq=%r Freq=%r -> freq_raw=%r -> parsed=%r",
                     g("txfreq"), g("rxfreq"), g("freq"), freq_raw, freq)

        # The dashboard 'Exch' column is driven by ``exchange``. N1MM puts
        # the *received* exchange in different places depending on the contest:
        #   * <RcvdExchange>  - full received exchange (e.g. "599 40")
        #   * <exch1>/<exchange> - exchange component(s)
        #   * <rcv> + <rcvnr>  - received RST + received serial/zone
        # We prefer the zone, then the received serial, then the full received
        # exchange (with RST stripped), then the bare exch1.
        rcvd = g("RcvdExchange").strip()
        exch1 = (g("exch1") or g("exchange") or g("exchange1")).strip()
        rcvnr = g("rcvnr").strip()
        zone = g("zone").strip()

        def _strip_rst(s: str) -> str:
            # Drop a leading RST token like "599" / "59" from a full exchange.
            parts = s.split()
            kept = [p for p in parts
                    if not (p.isdigit() and p[0] == "5" and 2 <= len(p) <= 3)]
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
        n1mm_id = (root.findtext("id") or "").strip()
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

    @staticmethod
    def _parse_freq(raw: str) -> str:
        """Convert an N1MM frequency value (units of 10 Hz) to a MHz string.

        Example: ``"352519"`` -> ``"3.52519"``. Non-integer values are returned
        verbatim.
        """
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
        """Parse an N1MM ``<timestamp>`` string (``YYYY-MM-DD HH:MM[:SS]``)."""
        ts = (ts or "").strip()
        if not ts:
            return time.time()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return time.mktime(time.strptime(ts, fmt))
            except ValueError:
                continue
        return time.time()

    @staticmethod
    def _normalize_band(band: str) -> str:
        """Map N1MM band strings (e.g. '14MHz', '20m', '14') to a short label.

        N1MM sometimes sends just the MHz number (e.g. '14' for 20 m,
        '7' for 40 m, '21' for 15 m). We convert those to the familiar
        'XM' label so the dashboard and the band filter stay consistent
        regardless of how the value arrived.
        """
        import re
        b = band.strip().lower()
        mapping = {
            "160m": "160M", "80m": "80M", "40m": "40M", "30m": "30M",
            "20m": "20M", "17m": "17M", "15m": "15M", "12m": "12M",
            "10m": "10M", "6m": "6M", "2m": "2M",
            "1.8mhz": "160M", "3.5mhz": "80M", "7mhz": "40M",
            "14mhz": "20M", "21mhz": "15M", "28mhz": "10M",
        }
        if b in mapping:
            return mapping[b]
        # Bare MHz number, e.g. '14' -> 20M, '7' -> 40M, '21' -> 15M.
        m = re.search(r'(\d+(?:\.\d+)?)', b)
        if m:
            try:
                mhz = float(m.group(1))
            except ValueError:
                return band.strip().upper()
            # Closest standard band by centre frequency (MHz).
            bands = [
                (1.8, "160M"), (3.5, "80M"), (7.0, "40M"), (10.0, "30M"),
                (14.0, "20M"), (18.0, "17M"), (21.0, "15M"), (24.0, "12M"),
                (28.0, "10M"), (50.0, "6M"), (144.0, "2M"),
            ]
            best = min(bands, key=lambda x: abs(x[0] - mhz))
            if abs(best[0] - mhz) < 1.0:
                return best[1]
        return band.strip().upper()


# ---------------------------------------------------------------------------
# Delayed slicing helper (used by main.py)
# ---------------------------------------------------------------------------
def schedule_qso_slice(contact: N1MMContact, source, cfg) -> None:
    """Run a post-roll delayed slice for a contact.

    The actual slicing is deferred by ``cfg.post_roll`` seconds so the audio
    tail is captured. This mirrors classic qsorder behaviour. The caller (main
    ``on_contact``) submits this via a bounded ``ThreadPoolExecutor`` so the
    per-contact work does not spawn an unbounded number of threads; the pool
    worker itself sleeps here and then performs the slice.
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
