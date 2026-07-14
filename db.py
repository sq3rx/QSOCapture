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


def init_db() -> None:
    """Create the ``qsos`` table if it does not yet exist."""
    with _lock, sqlite3.connect(DB_PATH) as con:
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
                operator  TEXT,
                station   TEXT,
                contest_nr TEXT,
                points    TEXT,
                multiplier TEXT,
                timestamp REAL,
                raw_ts    TEXT,
                duration  REAL,
                file_path TEXT UNIQUE,
                created_at REAL DEFAULT (strftime('%s','now'))
            )
            """
        )
        # Migrate older databases that lack the duration column.
        cols = [r[1] for r in con.execute("PRAGMA table_info(qsos)")]
        if "duration" not in cols:
            con.execute("ALTER TABLE qsos ADD COLUMN duration REAL")


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
    operator: str = "",
    station: str = "",
    contest_nr: str = "",
    points: str = "",
    multiplier: str = "",
    timestamp: float = 0.0,
    raw_ts: str = "",
    duration: float = 0.0,
    file_path: Optional[str] = None,
) -> None:
    """Insert (or ignore if file_path already present) a QSO record."""
    with _lock, sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT OR IGNORE INTO qsos
               (contest, call, band, mode, freq, name, qth, grid, comment,
                exchange, exchange2, exchange3, operator, station,
                contest_nr, points, multiplier, timestamp, raw_ts, duration, file_path)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (contest, call, band, mode, freq, name, qth, grid, comment,
             exchange, exchange2, exchange3, operator, station,
             contest_nr, points, multiplier, timestamp, raw_ts, duration, file_path),
        )


def insert_contact(contact, file_path: Optional[str] = None) -> None:
    """Convenience wrapper that pulls fields off an :class:`N1MMContact`."""
    insert_qso(
        contest=contact.contest,
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
        operator=getattr(contact, "operator", ""),
        station=getattr(contact, "station", ""),
        contest_nr=getattr(contact, "contest_nr", ""),
        points=getattr(contact, "points", ""),
        multiplier=getattr(contact, "multiplier", ""),
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
    limit: int = 500,
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
        "exchange, exchange2, exchange3, comment, operator, station, "
        "contest_nr, points, multiplier, file_path, timestamp, duration "
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
        sql += " AND contest=?"
        args.append(contest)
    if band:
        # Substring match so "20M", "20m" and "20" all match a stored "20M".
        sql += " AND band LIKE ?"
        args.append(f"%{band.upper()}%")
    if mode:
        sql += " AND mode=?"
        args.append(mode.upper())
    if date_from is not None:
        sql += " AND timestamp >= ?"
        args.append(date_from)
    if date_to is not None:
        sql += " AND timestamp <= ?"
        args.append(date_to)
    # The call filter is applied in Python (regex/substring), so we fetch a
    # generous slice and trim afterwards.
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(max(limit, 5000))

    with _lock, sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, args).fetchall()

    # Compile the call matcher once (regex, fallback to literal substring).
    matcher = None
    if call:
        pattern = call.strip()
        if pattern:
            try:
                matcher = re.compile(pattern, re.IGNORECASE)
            except re.error:
                safe = re.escape(pattern)
                matcher = re.compile(safe, re.IGNORECASE)

    results = []
    for r in rows:
        if matcher and not matcher.search(r["call"] or ""):
            continue
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
            "operator": r["operator"] or "",
            "station": r["station"] or "",
            "contest_nr": r["contest_nr"] or "",
            "points": r["points"] or "",
            "multiplier": r["multiplier"] or "",
            "label": label,
            "timestamp": _fmt_ts(ts),
            "duration": r["duration"] or 0.0,
            "file": base,
            "url": f"/audio/{fp}" if fp else None,
        })
        if len(results) >= limit:
            break
    return results


def clear_all() -> None:
    """Remove every QSO record from the database (factory reset)."""
    with _lock, sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM qsos")


def delete_qso(file_path: str) -> None:
    """Remove a single QSO record matched by its file_path."""
    with _lock, sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM qsos WHERE file_path=?", (file_path,))


def update_qso_duration(file_path: str, duration: float) -> None:
    """Update the duration of an existing QSO record (matched by file_path)."""
    with _lock, sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE qsos SET duration=? WHERE file_path=?", (duration, file_path))


def update_qso_file_path(old_path: str, new_path: str) -> None:
    """Update the stored file_path of a QSO record (e.g. after WAV->MP3)."""
    with _lock, sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE qsos SET file_path=? WHERE file_path=?", (new_path, old_path))


def _fmt_ts(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def migrate_existing(recordings_dir: str) -> None:
    """Scan the recordings directory and seed the DB with existing files.

    Older audio files created before the DB existed have no rich metadata, so
    we parse what we can from the filename (call/band/label/timestamp) and
    store a minimal record. ``INSERT OR IGNORE`` keeps us idempotent.
    """
    if not os.path.isdir(recordings_dir):
        return
    for contest in os.listdir(recordings_dir):
        cdir = os.path.join(recordings_dir, contest)
        if not os.path.isdir(cdir):
            continue
        for fname in os.listdir(cdir):
            if not (fname.endswith(".wav") or fname.endswith(".mp3")):
                continue
            fp = f"{contest}/{fname}"
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
                insert_qso(
                    contest="_continuous",
                    call="CONTINUOUS",
                    band="",
                    mode="",
                    timestamp=ts,
                    file_path=fp,
                )
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
            insert_qso(
                contest=contest,
                call=call,
                band=band,
                mode="",
                timestamp=ts,
                file_path=fp,
            )
