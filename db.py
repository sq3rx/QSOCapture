"""db.py - SQLite storage for N1MM QSO records.

This module keeps a persistent, queryable history of every contact received
from N1MM Logger+. The schema captures far more fields than the filesystem
filename convention used for the audio slices, so the web UI can offer rich
filtering (mode, frequency, date range, etc.) and show detailed info.

Standard library :mod:`sqlite3` is used so there are no extra dependencies.
All access is serialized through a module-level lock since both the N1MM
listener thread and the FastAPI request handlers may write/read concurrently.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import List, Optional

DB_PATH = "qsos.db"
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    """Open a connection with WAL mode and a REGEXP helper.

    WAL improves read/write concurrency (the web API can read while the audio
    thread inserts). The REGEXP function lets :func:`query_contacts` push the
    ``call`` filter down into SQL instead of fetching thousands of rows and
    filtering them in Python.
    """
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass

    def _regexp(pattern: str, value: Optional[str]) -> bool:
        if value is None:
            return False
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            # Fall back to a literal case-insensitive substring match.
            return pattern.lower() in value.lower()

    con.create_function("REGEXP", 2, _regexp)
    return con


def init_db() -> None:
    """Create the ``qsos`` table if it does not yet exist."""
    with _lock, _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS qsos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                contest   TEXT,
                call      TEXT,
                band      TEXT,
                mode      TEXT,
                freq      TEXT,
                name      TEXT,
                qth       TEXT,
                grid      TEXT,
                comment   TEXT,
                exchange  TEXT,
                exchange2 TEXT,
                exchange3 TEXT,
                rcv       TEXT,
                snt       TEXT,
                rcvnr     TEXT,
                sntnr     TEXT,
                section   TEXT,
                mycall    TEXT,
                countryprefix TEXT,
                wpxprefix TEXT,
                continent TEXT,
                operator  TEXT,
                station   TEXT,
                contest_nr TEXT,
                points    TEXT,
                multiplier TEXT,
                multiplier2 TEXT,
                multiplier3 TEXT,
                prec      TEXT,
                ck        TEXT,
                power     TEXT,
                n1mm_id   TEXT,
                is_claimed TEXT,
                sent_exchange TEXT,
                timestamp REAL,
                raw_ts    TEXT,
                duration  REAL,
                file_path TEXT UNIQUE,
                created_at REAL DEFAULT (strftime('%s','now'))
            )
            """
        )
        # Migrate older databases that lack the newer columns BEFORE creating
        # any index that references them.
        cols = [r[1] for r in con.execute("PRAGMA table_info(qsos)")]
        for col, ctype in _MIGRATE_COLUMNS:
            if col not in cols:
                con.execute(f"ALTER TABLE qsos ADD COLUMN {col} {ctype}")
        # Index for fast lookup by N1MM GUID (used by replace/delete packets).
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_qsos_n1mm_id ON qsos(n1mm_id)"
        )
        # Additional indexes to speed up the dashboard filter / sort.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_qsos_timestamp ON qsos(timestamp DESC)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_qsos_call ON qsos(call)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_qsos_band ON qsos(band)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_qsos_mode ON qsos(mode)"
        )


# Columns added after the initial schema, with their SQL types, for migration.
_MIGRATE_COLUMNS = [
    ("rcv", "TEXT"),
    ("snt", "TEXT"),
    ("rcvnr", "TEXT"),
    ("sntnr", "TEXT"),
    ("section", "TEXT"),
    ("mycall", "TEXT"),
    ("countryprefix", "TEXT"),
    ("wpxprefix", "TEXT"),
    ("continent", "TEXT"),
    ("multiplier2", "TEXT"),
    ("multiplier3", "TEXT"),
    ("prec", "TEXT"),
    ("ck", "TEXT"),
    ("power", "TEXT"),
    ("n1mm_id", "TEXT"),
    ("is_claimed", "TEXT"),
    ("sent_exchange", "TEXT"),
    ("duration", "REAL"),
]


def insert_qso(
    contest: str,
    call: str,
    band: str,
    mode: str,
    freq: str = "",
    name: str = "",
    qth: str = "",
    grid: str = "",
    comment: str = "",
    exchange: str = "",
    exchange2: str = "",
    exchange3: str = "",
    rcv: str = "",
    snt: str = "",
    rcvnr: str = "",
    sntnr: str = "",
    section: str = "",
    mycall: str = "",
    countryprefix: str = "",
    wpxprefix: str = "",
    continent: str = "",
    operator: str = "",
    station: str = "",
    contest_nr: str = "",
    points: str = "",
    multiplier: str = "",
    multiplier2: str = "",
    multiplier3: str = "",
    prec: str = "",
    ck: str = "",
    power: str = "",
    n1mm_id: str = "",
    is_claimed: str = "",
    sent_exchange: str = "",
    timestamp: float = 0.0,
    raw_ts: str = "",
    duration: float = 0.0,
    file_path: Optional[str] = None,
) -> Optional[str]:
    """Insert a QSO record, upserting by N1MM GUID when one is supplied.

    Behaviour:
      * No ``n1mm_id``  -> plain ``INSERT OR IGNORE`` (continuous chunks,
        migrated files, or contacts without a GUID).
      * ``n1mm_id`` present and the same ``file_path`` already stored
        (e.g. N1MM re-sent the contact with an identical timestamp) -> the
        row's metadata (call, exchange, band, ...) is **updated** so an edit
        in N1MM is reflected on the dashboard. The previous early-``return``
        here silently discarded such edits.
      * ``n1mm_id`` present with a *different* ``file_path`` (N1MM edited the
        timestamp, producing a new slice filename) -> the stale row is deleted
        and a fresh one inserted. The superseded file path is returned so the
        caller can remove the now-orphaned audio file from disk.

    Returns the previous ``file_path`` when an existing row was replaced by a
    new file path, otherwise ``None``.
    """
    superseded: Optional[str] = None
    # An empty n1mm_id must be stored as NULL, not the empty string. The
    # UNIQUE index on n1mm_id would otherwise treat every "" as the same
    # value, so the second INSERT OR IGNORE (e.g. the next continuous chunk
    # or a QSO without a GUID) would be silently dropped — leaving only the
    # first recording visible in the dashboard.
    n1mm_id_val = n1mm_id or None
    with _lock, _connect() as con:
        if n1mm_id_val:
            existing = con.execute(
                "SELECT file_path FROM qsos WHERE n1mm_id=?", (n1mm_id_val,)
            ).fetchone()
            if existing is not None:
                old_fp = existing[0]
                if old_fp == file_path:
                    # Same audio file but metadata may have changed in N1MM:
                    # refresh every editable column instead of returning early.
                    con.execute(
                        """
                        UPDATE qsos SET
                            contest=?, call=?, band=?, mode=?, freq=?, name=?,
                            qth=?, grid=?, comment=?, exchange=?, exchange2=?,
                            exchange3=?, rcv=?, snt=?, rcvnr=?, sntnr=?,
                            section=?, mycall=?, countryprefix=?, wpxprefix=?,
                            continent=?, operator=?, station=?, contest_nr=?,
                            points=?, multiplier=?, multiplier2=?, multiplier3=?,
                            prec=?, ck=?, power=?, is_claimed=?,
                            sent_exchange=?, timestamp=?, raw_ts=?, duration=?
                        WHERE n1mm_id=?
                        """,
                        (contest, call, band, mode, freq, name, qth, grid,
                         comment, exchange, exchange2, exchange3, rcv, snt,
                         rcvnr, sntnr, section, mycall, countryprefix,
                         wpxprefix, continent, operator, station, contest_nr,
                         points, multiplier, multiplier2, multiplier3, prec,
                         ck, power, is_claimed, sent_exchange, timestamp,
                         raw_ts, duration, n1mm_id_val),
                    )
                    return None
                # Different file (edited timestamp -> new slice filename):
                # drop the stale row. The new one is inserted below and the
                # orphaned audio file is reported back to the caller.
                con.execute("DELETE FROM qsos WHERE n1mm_id=?", (n1mm_id_val,))
                superseded = old_fp
        params = (contest, call, band, mode, freq, name, qth, grid, comment,
                  exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr,
                  section, mycall, countryprefix, wpxprefix, continent,
                  operator, station, contest_nr, points, multiplier,
                  multiplier2, multiplier3, prec, ck, power, n1mm_id_val,
                  is_claimed, sent_exchange, timestamp, raw_ts, duration, file_path)
        con.execute(
            "INSERT OR IGNORE INTO qsos ("
            "contest, call, band, mode, freq, name, qth, grid, comment, "
            "exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr, "
            "section, mycall, countryprefix, wpxprefix, continent, "
            "operator, station, contest_nr, points, multiplier, "
            "multiplier2, multiplier3, prec, ck, power, n1mm_id, "
            "is_claimed, sent_exchange, timestamp, raw_ts, duration, file_path) "
            "VALUES (" + ",".join("?" for _ in params) + ")",
            params,
        )
    return superseded


def insert_contact(contact, file_path: Optional[str] = None) -> None:
    """Convenience wrapper that pulls fields off an :class:`N1MMContact`."""
    insert_qso(
        contest=getattr(contact, "contest", ""),
        call=contact.call,
        band=contact.band,
        mode=getattr(contact, "mode", ""),
        freq=getattr(contact, "freq", ""),
        name=getattr(contact, "name", ""),
        qth=getattr(contact, "qth", ""),
        grid=getattr(contact, "grid", ""),
        comment=getattr(contact, "comment", ""),
        exchange=getattr(contact, "exchange", ""),
        exchange2=getattr(contact, "exchange2", ""),
        exchange3=getattr(contact, "exchange3", ""),
        rcv=getattr(contact, "rcv", ""),
        snt=getattr(contact, "snt", ""),
        rcvnr=getattr(contact, "rcvnr", ""),
        sntnr=getattr(contact, "sntnr", ""),
        section=getattr(contact, "section", ""),
        mycall=getattr(contact, "mycall", ""),
        countryprefix=getattr(contact, "countryprefix", ""),
        wpxprefix=getattr(contact, "wpxprefix", ""),
        continent=getattr(contact, "continent", ""),
        operator=getattr(contact, "operator", ""),
        station=getattr(contact, "station", ""),
        contest_nr=getattr(contact, "contest_nr", ""),
        points=getattr(contact, "points", ""),
        multiplier=getattr(contact, "multiplier", ""),
        multiplier2=getattr(contact, "multiplier2", ""),
        multiplier3=getattr(contact, "multiplier3", ""),
        prec=getattr(contact, "prec", ""),
        ck=getattr(contact, "ck", ""),
        power=getattr(contact, "power", ""),
        n1mm_id=getattr(contact, "n1mm_id", ""),
        is_claimed=getattr(contact, "is_claimed", ""),
        sent_exchange=getattr(contact, "sent_exchange", ""),
        timestamp=contact.timestamp,
        raw_ts=getattr(contact, "raw_ts", ""),
        file_path=file_path,
    )


def query_contacts(
    contest: Optional[str] = None,
    call: Optional[str] = None,
    band: Optional[str] = None,
    mode: Optional[str] = None,
    date_from: Optional[float] = None,
    date_to: Optional[float] = None,
    continuous: Optional[bool] = None,
    rx: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    sort_by: str = "timestamp",
    sort_dir: str = "DESC",
) -> List[dict]:
    """Return QSO records matching the given filters (newest first).

    ``call`` accepts either a plain fragment (substring match, case
    insensitive) or a regular expression. If the value is not a valid regex
    it is treated as a literal substring, so both ``SQ3RX`` and ``^SQ`` work.

    ``continuous``:
      * ``True``  -> only continuous-recording chunks (file_path starts with
        ``_continuous/``).
      * ``False`` -> only N1MM QSO slices (file_path does NOT start with
        ``_continuous/``).
      * ``None``  -> both.
    """
    sql = (
        "SELECT id, contest, call, band, mode, freq, name, qth, grid, "
        "exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr, section, "
        "mycall, countryprefix, wpxprefix, continent, comment, operator, "
        "station, contest_nr, points, multiplier, multiplier2, multiplier3, "
        "prec, ck, power, n1mm_id, is_claimed, sent_exchange, "
        "file_path, timestamp, duration "
        "FROM qsos WHERE 1=1"
    )
    args: List = []
    if continuous is True:
        sql += " AND file_path LIKE ?"
        args.append("_continuous/%")
    elif continuous is False:
        sql += " AND (file_path IS NULL OR file_path NOT LIKE ?)"
        args.append("_continuous/%")
    if contest:
        # Partial / fragmentary match (substring) so the dashboard contest
        # filter accepts e.g. "CQWW" or "2026_CQ" in addition to the
        # exact directory name. Case-insensitive via lower().
        sql += " AND lower(contest) LIKE lower(?)"
        args.append(f"%{contest}%")
    if call:
        # Pushed down into SQL via the REGEXP helper (regex or literal
        # substring fallback handled in Python). This avoids fetching
        # thousands of rows just to filter them in Python.
        sql += " AND call REGEXP ?"
        args.append(call.strip())
    if band:
        # Flexible band match. The user may type either the band *label*
        # (e.g. "20M" = 20 metres = 14 MHz) or the *frequency* in MHz
        # (e.g. "14", "14MHz"). Stored values can be any of these forms.
        # We expand the input into every equivalent form and OR them together
        # so "20M" finds a stored "14MHz" and "14" finds a stored "20M".
        import re
        b = band.strip().upper()
        m = re.search(r'(\d+(?:\.\d+)?)', b)
        patterns = set()
        if m:
            num = m.group(1)
            patterns.add(f"%{num}%")          # bare number (14 or 20)
            patterns.add(f"%{num}M%")         # label form (20M)
            patterns.add(f"%{num}MHZ%")      # explicit MHz (14MHZ)
            # If the number looks like a band *label* (20, 40, 80, 15, 10...),
            # also test the corresponding centre frequency in MHz.
            try:
                label_mhz = {
                    160: 1.8, 80: 3.5, 40: 7.0, 30: 10.0, 20: 14.0,
                    17: 18.0, 15: 21.0, 12: 24.0, 10: 28.0, 6: 50.0,
                    2: 144.0,
                }.get(int(float(num)))
                if label_mhz is not None:
                    patterns.add(f"%{label_mhz:g}%")
                    patterns.add(f"%{label_mhz:g}MHZ%")
            except (ValueError, TypeError):
                pass
        else:
            patterns.add(f"%{b}%")
        sql += " AND (" + " OR ".join("band LIKE ?" for _ in patterns) + ")"
        args.extend(patterns)
    if mode:
        sql += " AND mode=?"
        args.append(mode.upper())
    if date_from is not None:
        sql += " AND timestamp >= ?"
        args.append(date_from)
    if date_to is not None:
        sql += " AND timestamp <= ?"
        args.append(date_to)
    # Whitelist the sort column/direction so user input can never inject SQL.
    allowed_cols = {
        "timestamp": "timestamp",
        "start": "timestamp",
        "stop": "(timestamp + duration)",
        "call": "call",
        "band": "band",
        "mode": "mode",
        "contest": "contest",
        "duration": "duration",
    }
    sort_col = allowed_cols.get(sort_by, "timestamp")
    sort_dir = "ASC" if sort_dir.upper() == "ASC" else "DESC"
    sql += f" ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?"
    args.append(limit)
    args.append(offset)

    with _lock, _connect() as con:
        con.row_factory = sqlite3.Row
        # Total number of matching rows (ignoring the LIMIT/OFFSET) so the UI
        # can display the real count and offer "load more" pagination instead
        # of pulling thousands of rows into memory on every request.
        count_sql = "SELECT COUNT(*) FROM qsos" + sql.split("FROM qsos", 1)[1].split("ORDER BY", 1)[0]
        total = con.execute(count_sql, args[:-2]).fetchone()[0]
        rows = con.execute(sql, args).fetchall()

    results = []
    for r in rows:
        fp = r["file_path"]
        base = os.path.basename(fp) if fp else ""
        label = "RX1"
        if base:
            parts = base[:-4].split("_")
            # The RX label is always the last filename segment (RX1/RX2),
            # which works for both N1MM QSO slices and continuous chunks.
            label = parts[-1] if parts else "RX1"
        if rx and label != rx:
            continue
        ts = r["timestamp"]
        results.append({
            "id": r["id"],
            "contest": r["contest"],
            "call": r["call"],
            "band": r["band"],
            "mode": r["mode"],
            "freq": r["freq"],
            "name": r["name"] or "",
            "qth": r["qth"] or "",
            "grid": r["grid"] or "",
            "comment": r["comment"] or "",
            "exchange": r["exchange"] or "",
            "exchange2": r["exchange2"] or "",
            "exchange3": r["exchange3"] or "",
            "rcv": r["rcv"] or "",
            "snt": r["snt"] or "",
            "rcvnr": r["rcvnr"] or "",
            "sntnr": r["sntnr"] or "",
            "section": r["section"] or "",
            "mycall": r["mycall"] or "",
            "countryprefix": r["countryprefix"] or "",
            "wpxprefix": r["wpxprefix"] or "",
            "continent": r["continent"] or "",
            "operator": r["operator"] or "",
            "station": r["station"] or "",
            "contest_nr": r["contest_nr"] or "",
            "points": r["points"] or "",
            "multiplier": r["multiplier"] or "",
            "multiplier2": r["multiplier2"] or "",
            "multiplier3": r["multiplier3"] or "",
            "prec": r["prec"] or "",
            "ck": r["ck"] or "",
            "power": r["power"] or "",
            "n1mm_id": r["n1mm_id"] or "",
            "is_claimed": r["is_claimed"] or "",
            "sent_exchange": r["sent_exchange"] or "",
            "label": label,
            "timestamp": _fmt_ts(ts),
            "duration": r["duration"] or 0.0,
            "file": base,
            "url": f"/audio/{fp}" if fp else None,
        })
        if len(results) >= limit:
            break
    return {"total": total, "qsos": results}


def clear_all() -> None:
    """Remove every QSO record from the database (factory reset)."""
    with _lock, _connect() as con:
        con.execute("DELETE FROM qsos")


def delete_qso(file_path: str) -> None:
    """Remove a single QSO record matched by its file_path."""
    with _lock, _connect() as con:
        con.execute("DELETE FROM qsos WHERE file_path=?", (file_path,))


def delete_qso_by_n1mm_id(n1mm_id: str) -> Optional[str]:
    """Remove a QSO matched by its N1MM GUID and return its file_path.

    Used by the ``<contactdelete>`` packet handler so the dashboard row and
    the associated audio file can both be removed.
    """
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT file_path FROM qsos WHERE n1mm_id=?", (n1mm_id,)
        ).fetchone()
        if row is None:
            return None
        fp = row[0]
        con.execute("DELETE FROM qsos WHERE n1mm_id=?", (n1mm_id,))
        return fp


def update_qso_duration(file_path: str, duration: float) -> None:
    """Update the duration of an existing QSO record (matched by file_path)."""
    with _lock, _connect() as con:
        con.execute("UPDATE qsos SET duration=? WHERE file_path=?", (duration, file_path))


def update_qso_file_path(old_path: str, new_path: str) -> None:
    """Update the stored file_path of a QSO record (e.g. after WAV->MP3)."""
    with _lock, _connect() as con:
        con.execute("UPDATE qsos SET file_path=? WHERE file_path=?", (new_path, old_path))


def _fmt_ts(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _file_duration(path: str) -> float:
    """Return the duration (seconds) of an audio file, or 0.0 on any error.

    WAV duration is read directly from the header. For MP3 we prefer
    :mod:`mutagen` (exact) and fall back to a 128 kbps CBR estimate from the
    file size so the Continuous view's Stop column is correct even when the
    library is not installed.
    """
    try:
        if path.endswith(".wav"):
            import wave as _wave
            with _wave.open(path, "rb") as wf:
                fr = wf.getframerate() or 1
                return wf.getnframes() / fr
        if path.endswith(".mp3"):
            try:
                from mutagen.mp3 import MP3
                return float(MP3(path).info.length)
            except Exception:
                return os.path.getsize(path) * 8.0 / 128000.0
    except Exception:
        pass
    return 0.0


def migrate_existing(recordings_dir: str) -> None:
    """Scan the recordings directory and seed the DB with existing files.

    Older audio files created before the DB existed have no rich metadata, so
    we parse what we can from the filename (call/band/label/timestamp) and
    store a minimal record. ``INSERT OR IGNORE`` keeps us idempotent.

    All rows are inserted inside a single connection / transaction so that
    seeding thousands of files at startup stays fast (opening one SQLite
    connection per file previously made startup take tens of seconds with
    only a couple of thousand recordings).

    The duration of each file is computed up front so the Continuous view's
    Stop column is populated correctly (previously it was left at 0, leaving
    the Stop cell blank until the next app restart that re-derived it).
    """
    if not os.path.isdir(recordings_dir):
        return

    rows: List[tuple] = []
    for contest in os.listdir(recordings_dir):
        cdir = os.path.join(recordings_dir, contest)
        if not os.path.isdir(cdir):
            continue
        for fname in os.listdir(cdir):
            if not (fname.endswith(".wav") or fname.endswith(".mp3")):
                continue
            fp = f"{contest}/{fname}"
            full = os.path.join(recordings_dir, fp)
            dur = _file_duration(full)
            if contest == "_continuous":
                # Continuous chunk filename: 20260713_212000_RX1.wav
                parts = fname[:-4].split("_")
                ts = 0.0
                if len(parts) >= 2:
                    try:
                        ts = time.mktime(
                            time.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H%M%S")
                        )
                    except Exception:
                        ts = 0.0
                rows.append(("_continuous", "CONTINUOUS", "", ts, fp, dur))
                continue
            call, band, ts = "UNKNOWN", "", 0.0
            parts = fname[:-4].split("_")
            if len(parts) >= 5:
                try:
                    ts = time.mktime(
                        time.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H%M")
                    )
                except Exception:
                    ts = 0.0
                call = parts[2]
                band = parts[3]
            rows.append((contest, call, band, ts, fp, dur))

    if not rows:
        return

    with _lock, _connect() as con:
        con.executemany(
            "INSERT OR IGNORE INTO qsos "
            "(contest, call, band, timestamp, file_path, duration) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
