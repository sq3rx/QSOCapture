"""SQLite storage for N1MM QSO records.

Keeps a persistent, queryable history of every contact from N1MM Logger+.
Uses stdlib sqlite3. Write operations are serialized through a module-level
lock since N1MM listener thread and FastAPI handlers may write concurrently.
Read-only queries bypass the lock, relying on WAL mode for safe concurrent reads.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from typing import List, Optional

from config import RECORDINGS_DIR

DB_PATH = "qsos.db"
logger = logging.getLogger("QSOCapture.db")
_lock = threading.Lock()
_local = threading.local()


def _extract_rx(file_path: Optional[str]) -> str:
    """Extract the RX label (RX1/RX2) from an audio file path. Defaults to RX1."""
    if not file_path:
        return "RX1"
    base = os.path.basename(file_path)
    if not base:
        return "RX1"
    parts = base[:-4].split("_")
    return parts[-1] if parts else "RX1"


def _connect() -> sqlite3.Connection:
    """Return a thread-local connection (one per thread, WAL mode, REGEXP helper)."""
    con: sqlite3.Connection | None = getattr(_local, "con", None)
    if con is not None:
        try:
            con.execute("SELECT 1")
            return con
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            pass

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
            return pattern.lower() in value.lower()

    con.create_function("REGEXP", 2, _regexp)
    _local.con = con
    return con


def init_db() -> None:
    """Create/upgrade the qsos table and indexes, clean orphaned records."""
    con = _connect()
    with _lock:
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
        # Migrate older databases missing newer columns.
        cols = [r[1] for r in con.execute("PRAGMA table_info(qsos)")]
        for col, ctype in _MIGRATE_COLUMNS:
            if col not in cols:
                con.execute(f"ALTER TABLE qsos ADD COLUMN {col} {ctype}")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_qsos_n1mm_id ON qsos(n1mm_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_qsos_timestamp ON qsos(timestamp DESC)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_qsos_call ON qsos(call)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qsos_band ON qsos(band)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qsos_mode ON qsos(mode)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_qsos_contest ON qsos(contest)")

        # Backfill rx column for pre-existing rows.
        if "rx" not in cols:
            existing_rows = con.execute(
                "SELECT id, file_path FROM qsos WHERE rx IS NULL"
            ).fetchall()
            for row_id, fp in existing_rows:
                rx_val = _extract_rx(fp)
                con.execute("UPDATE qsos SET rx=? WHERE id=?", (rx_val, row_id))

        # Clean orphaned continuous records (duration=0, no file on disk).
        orphans = con.execute(
            "SELECT id, file_path, timestamp FROM qsos "
            "WHERE contest='_continuous' AND duration=0.0"
        ).fetchall()
        for row_id, fp, ts in orphans:
            if fp:
                full = os.path.join(RECORDINGS_DIR, fp)
                if not os.path.isfile(full):
                    con.execute("DELETE FROM qsos WHERE id=?", (row_id,))
                    logger.info("Removed orphaned continuous record (no file): %s", fp)
                else:
                    try:
                        dur = _file_duration(full)
                        if dur > 0:
                            con.execute("UPDATE qsos SET duration=? WHERE id=?", (dur, row_id))
                            logger.info("Fixed unset duration for continuous record: %s (%.1f s)", fp, dur)
                    except Exception:
                        pass
        con.commit()

        _migrate_timestamps(con)


_MIGRATE_COLUMNS = [
    ("rcv", "TEXT"), ("snt", "TEXT"), ("rcvnr", "TEXT"), ("sntnr", "TEXT"),
    ("section", "TEXT"), ("mycall", "TEXT"), ("countryprefix", "TEXT"),
    ("wpxprefix", "TEXT"), ("continent", "TEXT"), ("multiplier2", "TEXT"),
    ("multiplier3", "TEXT"), ("prec", "TEXT"), ("ck", "TEXT"), ("power", "TEXT"),
    ("n1mm_id", "TEXT"), ("is_claimed", "TEXT"), ("sent_exchange", "TEXT"),
    ("duration", "REAL"), ("rx", "TEXT"),
]

_TIMESTAMP_MIGRATION_VERSION = 1


def _migrate_timestamps(con: sqlite3.Connection) -> None:
    """Rewrite legacy local-interpreted timestamps to correct UTC epochs.

    Earlier versions parsed N1MM <timestamp> (which N1MM sends in UTC) with
    time.mktime, interpreting it in the host's local timezone. We recompute the
    epoch from the stored original N1MM string (raw_ts, UTC) so existing rows
    match new UTC-correct inserts. Runs once, guarded by PRAGMA user_version.
    Rows without raw_ts (continuous / migrated-from-file) are left untouched.
    """
    from datetime import datetime, timezone

    ver = con.execute("PRAGMA user_version").fetchone()[0]
    if ver >= _TIMESTAMP_MIGRATION_VERSION:
        return
    rows = con.execute(
        "SELECT id, raw_ts FROM qsos "
        "WHERE raw_ts IS NOT NULL AND raw_ts != ''"
    ).fetchall()
    for rid, raw in rows:
        raw = raw.strip()
        new_ts = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                new_ts = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
                break
            except ValueError:
                continue
        if new_ts is not None:
            con.execute("UPDATE qsos SET timestamp=? WHERE id=?", (new_ts, rid))
    con.execute(f"PRAGMA user_version = {_TIMESTAMP_MIGRATION_VERSION}")
    con.commit()


def insert_qso(
    contest: str, call: str, band: str, mode: str,
    freq: str = "", name: str = "", qth: str = "", grid: str = "",
    comment: str = "", exchange: str = "", exchange2: str = "",
    exchange3: str = "", rcv: str = "", snt: str = "", rcvnr: str = "",
    sntnr: str = "", section: str = "", mycall: str = "",
    countryprefix: str = "", wpxprefix: str = "", continent: str = "",
    operator: str = "", station: str = "", contest_nr: str = "",
    points: str = "", multiplier: str = "", multiplier2: str = "",
    multiplier3: str = "", prec: str = "", ck: str = "", power: str = "",
    n1mm_id: str = "", is_claimed: str = "", sent_exchange: str = "",
    timestamp: float = 0.0, raw_ts: str = "", duration: float = 0.0,
    file_path: Optional[str] = None,
) -> Optional[str]:
    """Insert/upsert a QSO record by N1MM GUID.

    * No n1mm_id -> plain INSERT OR IGNORE (continuous chunks, migrated files).
    * n1mm_id + same file_path -> update metadata (N1MM edited the contact).
    * n1mm_id + different file_path -> delete old row, insert new, return
      superseded file_path so caller can remove orphaned audio.

    Returns previous file_path when replaced, else None.
    """
    superseded: Optional[str] = None
    n1mm_id_val = n1mm_id or None
    con = _connect()
    with _lock:
        if n1mm_id_val:
            existing = con.execute(
                "SELECT file_path FROM qsos WHERE n1mm_id=?", (n1mm_id_val,)
            ).fetchone()
            if existing is not None:
                old_fp = existing[0]
                if old_fp == file_path:
                    con.execute(
                        """UPDATE qsos SET contest=?, call=?, band=?, mode=?, freq=?,
                           name=?, qth=?, grid=?, comment=?, exchange=?, exchange2=?,
                           exchange3=?, rcv=?, snt=?, rcvnr=?, sntnr=?, section=?,
                           mycall=?, countryprefix=?, wpxprefix=?, continent=?,
                           operator=?, station=?, contest_nr=?, points=?, multiplier=?,
                           multiplier2=?, multiplier3=?, prec=?, ck=?, power=?,
                           is_claimed=?, sent_exchange=?, timestamp=?, raw_ts=?,
                           duration=?, rx=? WHERE n1mm_id=?""",
                        (contest, call, band, mode, freq, name, qth, grid,
                         comment, exchange, exchange2, exchange3, rcv, snt,
                         rcvnr, sntnr, section, mycall, countryprefix,
                         wpxprefix, continent, operator, station, contest_nr,
                         points, multiplier, multiplier2, multiplier3, prec,
                         ck, power, is_claimed, sent_exchange, timestamp,
                         raw_ts, duration, _extract_rx(file_path), n1mm_id_val),
                    )
                    return None
                con.execute("DELETE FROM qsos WHERE n1mm_id=?", (n1mm_id_val,))
                superseded = old_fp
        rx_val = _extract_rx(file_path)
        params = (contest, call, band, mode, freq, name, qth, grid, comment,
                  exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr,
                  section, mycall, countryprefix, wpxprefix, continent,
                  operator, station, contest_nr, points, multiplier,
                  multiplier2, multiplier3, prec, ck, power, n1mm_id_val,
                  is_claimed, sent_exchange, timestamp, raw_ts, duration,
                  file_path, rx_val)
        con.execute(
            "INSERT OR IGNORE INTO qsos ("
            "contest, call, band, mode, freq, name, qth, grid, comment, "
            "exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr, "
            "section, mycall, countryprefix, wpxprefix, continent, "
            "operator, station, contest_nr, points, multiplier, "
            "multiplier2, multiplier3, prec, ck, power, n1mm_id, "
            "is_claimed, sent_exchange, timestamp, raw_ts, duration, "
            "file_path, rx) VALUES (" + ",".join("?" for _ in params) + ")",
            params,
        )
        con.commit()
    return superseded


def insert_contact(contact, file_path: Optional[str] = None) -> None:
    """Convenience wrapper pulling fields from an N1MMContact."""
    insert_qso(
        contest=getattr(contact, "contest", ""), call=contact.call,
        band=contact.band, mode=getattr(contact, "mode", ""),
        freq=getattr(contact, "freq", ""), name=getattr(contact, "name", ""),
        qth=getattr(contact, "qth", ""), grid=getattr(contact, "grid", ""),
        comment=getattr(contact, "comment", ""),
        exchange=getattr(contact, "exchange", ""),
        exchange2=getattr(contact, "exchange2", ""),
        exchange3=getattr(contact, "exchange3", ""),
        rcv=getattr(contact, "rcv", ""), snt=getattr(contact, "snt", ""),
        rcvnr=getattr(contact, "rcvnr", ""), sntnr=getattr(contact, "sntnr", ""),
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
        prec=getattr(contact, "prec", ""), ck=getattr(contact, "ck", ""),
        power=getattr(contact, "power", ""),
        n1mm_id=getattr(contact, "n1mm_id", ""),
        is_claimed=getattr(contact, "is_claimed", ""),
        sent_exchange=getattr(contact, "sent_exchange", ""),
        timestamp=contact.timestamp,
        raw_ts=getattr(contact, "raw_ts", ""), file_path=file_path,
    )


def format_contest_name(raw: str) -> str:
    """Format a raw contest name like '20256_CQWW_CW' into a readable label.

    * Strips a leading year prefix (e.g. '2025_') and any leading digits
      that are part of the contest name (e.g. '6_' in '20256_CQWW_CW').
    * Replaces underscores and hyphens with spaces.
    * Returns the original string if it cannot be parsed.
    """
    if not raw:
        return raw
    name = raw.strip()
    # Split off a leading year (4 digits) if present.
    year = ""
    rest = name
    m = re.match(r"^(\d{4})[_-]?(.*)$", name)
    if m:
        year = m.group(1)
        rest = m.group(2)
    # Strip any remaining leading digits + separator (e.g. '6_' in '20256_CQWW_CW').
    rest = re.sub(r"^\d+[_-]?", "", rest)
    # Replace separators with spaces and collapse multiple spaces.
    label = re.sub(r"[_-]+", " ", rest).strip()
    if year:
        label = f"{year} {label}".strip()
    return label or name


def _normalize_contest_key(s: str) -> str:
    """Normalize a contest name for fuzzy matching: lowercase, strip separators."""
    return re.sub(r"[_\-\s]+", "", (s or "").lower())


def resolve_contest(user_input: str) -> Optional[str]:
    """Resolve a user-entered contest name to the raw DB value.

    Tries exact match first, then normalized (case-insensitive, ignoring
    underscores/hyphens/spaces) match against all known contests.
    Returns None if no match found.
    """
    if not user_input:
        return None
    inp = user_input.strip()
    con = _connect()
    rows = con.execute(
        "SELECT DISTINCT contest FROM qsos "
        "WHERE contest != '' AND contest IS NOT NULL "
        "AND contest NOT GLOB '_*'"
    ).fetchall()
    # Exact match first.
    for (raw,) in rows:
        if raw == inp:
            return raw
    # Normalized match.
    key = _normalize_contest_key(inp)
    if not key:
        return None
    for (raw,) in rows:
        if _normalize_contest_key(raw) == key:
            return raw
    return None


def list_contests() -> List[dict]:
    """Return sorted distinct contest names (excluding internal _* entries).

    Each entry is a dict with:
      * value - raw contest name as stored in the DB (used for filtering)
      * label - human-readable formatted name (used for display)
    """
    con = _connect()
    rows = con.execute(
        "SELECT DISTINCT contest FROM qsos "
        "WHERE contest != '' AND contest IS NOT NULL "
        "AND contest NOT GLOB '_*' ORDER BY contest"
    ).fetchall()
    return [
        {"value": r[0], "label": format_contest_name(r[0])}
        for r in rows
    ]


def query_contacts(
    contest: Optional[str] = None, call: Optional[str] = None,
    band: Optional[str] = None, mode: Optional[str] = None,
    date_from: Optional[float] = None, date_to: Optional[float] = None,
    continuous: Optional[bool] = None, rx: Optional[str] = None,
    limit: int = 200, offset: int = 0,
    sort_by: str = "timestamp", sort_dir: str = "DESC",
) -> List[dict]:
    """Return QSO records matching filters.

    * call: regex or plain substring (case-insensitive).
    * continuous: True = only _continuous/ chunks, False = only N1MM slices,
      None = both.
    """
    sql = (
        "SELECT id, contest, call, band, mode, freq, name, qth, grid, "
        "exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr, section, "
        "mycall, countryprefix, wpxprefix, continent, comment, operator, "
        "station, contest_nr, points, multiplier, multiplier2, multiplier3, "
        "prec, ck, power, n1mm_id, is_claimed, sent_exchange, "
        "file_path, timestamp, duration, rx "
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
        sql += " AND contest = ?"
        args.append(contest)
    if call:
        sql += " AND call REGEXP ?"
        args.append(call.strip())
    if band:
        import re
        band_parts = [b.strip() for b in band.split(',') if b.strip()]
        all_patterns = []
        for bp in band_parts:
            b = bp.upper()
            m = re.search(r'(\d+(?:\.\d+)?)', b)
            patterns = set()
            if m:
                num = m.group(1)
                patterns.add(f"%{num}%")
                patterns.add(f"%{num}M%")
                patterns.add(f"%{num}MHz%")
                try:
                    label_mhz = {
                        160: 1.8, 80: 3.5, 40: 7.0, 30: 10.0, 20: 14.0,
                        17: 18.0, 15: 21.0, 12: 24.0, 10: 28.0, 6: 50.0, 2: 144.0,
                    }.get(int(float(num)))
                    if label_mhz is not None:
                        patterns.add(f"%{label_mhz:g}%")
                        patterns.add(f"%{label_mhz:g}MHz%")
                except (ValueError, TypeError):
                    pass
            else:
                patterns.add(f"%{b}%")
            all_patterns.extend(patterns)
        if all_patterns:
            sql += " AND (" + " OR ".join("band LIKE ?" for _ in all_patterns) + ")"
            args.extend(all_patterns)
    if mode:
        sql += " AND mode=?"
        args.append(mode.upper())
    if date_from is not None:
        sql += " AND timestamp >= ?"
        args.append(date_from)
    if date_to is not None:
        sql += " AND timestamp <= ?"
        args.append(date_to)
    if rx:
        sql += " AND rx=?"
        args.append(rx)

    allowed_cols = {
        "timestamp": "timestamp", "start": "timestamp",
        "stop": "(timestamp + duration)", "call": "call", "band": "band",
        "mode": "mode", "contest": "contest", "duration": "duration",
    }
    sort_col = allowed_cols.get(sort_by, "timestamp")
    sort_dir = "ASC" if sort_dir.upper() == "ASC" else "DESC"
    sql += f" ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?"
    args.append(limit)
    args.append(offset)

    con = _connect()
    con.row_factory = sqlite3.Row
    count_col = ", COUNT(*) OVER() AS _total"
    insert_pos = sql.find("FROM qsos")
    sql_with_count = sql[:insert_pos] + count_col + " " + sql[insert_pos:]
    rows = con.execute(sql_with_count, args).fetchall()
    total = rows[0]["_total"] if rows else 0

    results = []
    for r in rows:
        fp = r["file_path"]
        base = os.path.basename(fp) if fp else ""
        label = r["rx"] or "RX1"
        ts = r["timestamp"]
        results.append({
            "id": r["id"], "contest": r["contest"],
            "contest_display": format_contest_name(r["contest"]),
            "call": r["call"],
            "band": r["band"], "mode": r["mode"], "freq": r["freq"],
            "name": r["name"] or "", "qth": r["qth"] or "",
            "grid": r["grid"] or "", "comment": r["comment"] or "",
            "exchange": r["exchange"] or "", "exchange2": r["exchange2"] or "",
            "exchange3": r["exchange3"] or "", "rcv": r["rcv"] or "",
            "snt": r["snt"] or "", "rcvnr": r["rcvnr"] or "",
            "sntnr": r["sntnr"] or "", "section": r["section"] or "",
            "mycall": r["mycall"] or "", "countryprefix": r["countryprefix"] or "",
            "wpxprefix": r["wpxprefix"] or "", "continent": r["continent"] or "",
            "operator": r["operator"] or "", "station": r["station"] or "",
            "contest_nr": r["contest_nr"] or "", "points": r["points"] or "",
            "multiplier": r["multiplier"] or "", "multiplier2": r["multiplier2"] or "",
            "multiplier3": r["multiplier3"] or "", "prec": r["prec"] or "",
            "ck": r["ck"] or "", "power": r["power"] or "",
            "n1mm_id": r["n1mm_id"] or "", "is_claimed": r["is_claimed"] or "",
            "sent_exchange": r["sent_exchange"] or "", "label": label,
            "timestamp": _fmt_ts(ts), "duration": r["duration"] or 0.0,
            "file": base, "url": f"/audio/{fp}" if fp else None,
        })
        if len(results) >= limit:
            break
    return {"total": total, "qsos": results}


def clear_all() -> None:
    """Remove every QSO record from the database."""
    con = _connect()
    with _lock:
        con.execute("DELETE FROM qsos")
        con.commit()


def delete_qso(file_path: str) -> None:
    """Remove a single QSO record matched by its file_path."""
    con = _connect()
    with _lock:
        con.execute("DELETE FROM qsos WHERE file_path=?", (file_path,))
        con.commit()


def delete_qso_by_n1mm_id(n1mm_id: str) -> Optional[str]:
    """Remove a QSO matched by N1MM GUID, return its file_path for audio cleanup."""
    con = _connect()
    with _lock:
        row = con.execute(
            "SELECT file_path FROM qsos WHERE n1mm_id=?", (n1mm_id,)
        ).fetchone()
        if row is None:
            return None
        fp = row[0]
        con.execute("DELETE FROM qsos WHERE n1mm_id=?", (n1mm_id,))
        con.commit()
        return fp


def rename_qso_audio(n1mm_id: str, new_call: str, new_band: str,
                     new_contest_dir: str, new_rx_label: str,
                     timestamp: float, **metadata) -> Optional[str]:
    """Rename audio file on disk and update DB record for an edited QSO.

    Called when N1MM sends *contactreplace* for an existing contact.
    The audio file is kept (just renamed to reflect new call/band) and
    DB record is updated in-place — no new slice is created.

    Accepts optional metadata keyword arguments (freq, name, qth, grid,
    comment, exchange, exchange2, exchange3, rcv, snt, rcvnr, sntnr,
    section, mycall, countryprefix, wpxprefix, continent, operator,
    station, contest_nr, points, multiplier, multiplier2, multiplier3,
    prec, ck, power, is_claimed, sent_exchange, raw_ts) that are
    written into the DB row alongside the new file_path.

    Returns the new relative file_path on success, or None if n1mm_id not found.
    """
    con = _connect()
    with _lock:
        row = con.execute(
            "SELECT file_path FROM qsos WHERE n1mm_id=?", (n1mm_id,)
        ).fetchone()
        if row is None:
            logger.debug("rename_qso_audio: n1mm_id=%s not found", n1mm_id)
            return None
        old_rel = row[0]
        if not old_rel:
            logger.debug("rename_qso_audio: n1mm_id=%s has no file_path", n1mm_id)
            return None

    safe_call = "".join(ch for ch in new_call if ch.isalnum() or ch in "-_")
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(timestamp))
    ext = os.path.splitext(old_rel)[1]  # preserve existing extension
    new_fname = f"{stamp}_{safe_call}_{new_band}_{new_rx_label}{ext}"
    new_rel = f"{new_contest_dir}/{new_fname}"

    old_abs = os.path.join(RECORDINGS_DIR, old_rel)
    new_abs = os.path.join(RECORDINGS_DIR, new_rel)

    if os.path.isfile(old_abs):
        os.makedirs(os.path.dirname(new_abs), exist_ok=True)
        try:
            os.rename(old_abs, new_abs)
            logger.info("Renamed audio file: %s -> %s", old_rel, new_rel)
        except OSError as e:
            logger.warning("Failed to rename audio file %s -> %s: %s",
                           old_abs, new_abs, e)
            return None
    else:
        logger.debug("rename_qso_audio: old file missing %s (rename skipped)", old_abs)

    # Build dynamic UPDATE with provided metadata fields
    set_clauses = ["file_path=?"]
    set_params = [new_rel]
    allowed_meta = {
        "contest", "call", "band", "mode", "freq", "name", "qth", "grid",
        "comment", "exchange", "exchange2", "exchange3", "rcv", "snt",
        "rcvnr", "sntnr", "section", "mycall", "countryprefix", "wpxprefix",
        "continent", "operator", "station", "contest_nr", "points",
        "multiplier", "multiplier2", "multiplier3", "prec", "ck", "power",
        "is_claimed", "sent_exchange", "raw_ts",
    }
    for key in sorted(metadata):
        if key in allowed_meta:
            set_clauses.append(f"{key}=?")
            set_params.append(metadata[key])
    set_params.append(n1mm_id)
    sql = f"UPDATE qsos SET {', '.join(set_clauses)} WHERE n1mm_id=?"

    with _lock:
        con.execute(sql, set_params)
        con.commit()

    return new_rel


def delete_contest(contest: str) -> int:
    """Remove all QSO records for a contest. Returns row count."""
    con = _connect()
    with _lock:
        cur = con.execute("DELETE FROM qsos WHERE contest=?", (contest,))
        con.commit()
        return cur.rowcount


def delete_continuous_range(date_from: Optional[float] = None,
                            date_to: Optional[float] = None):
    """Remove continuous-recording QSOs within a time range.

    When both bounds are omitted all continuous chunks are deleted.
    Returns (deleted_rows, file_paths).
    """
    con = _connect()
    sql_sel = "SELECT file_path FROM qsos WHERE file_path LIKE '_continuous/%'"
    sql_del = "DELETE FROM qsos WHERE file_path LIKE '_continuous/%'"
    args: list = []
    if date_from is not None:
        sql_sel += " AND timestamp >= ?"
        sql_del += " AND timestamp >= ?"
        args.append(date_from)
    if date_to is not None:
        sql_sel += " AND timestamp <= ?"
        sql_del += " AND timestamp <= ?"
        args.append(date_to)
    with _lock:
        rows = con.execute(sql_sel, args).fetchall()
        file_paths = [row[0] for row in rows]
        cur = con.execute(sql_del, args)
        con.commit()
        return cur.rowcount, file_paths


def clear_contest_file_paths(contest: str) -> int:
    """Set file_path to NULL for all QSO records in a contest. Returns row count."""
    con = _connect()
    with _lock:
        cur = con.execute("UPDATE qsos SET file_path=NULL WHERE contest=?", (contest,))
        con.commit()
        return cur.rowcount


def update_qso_duration(file_path: str, duration: float) -> None:
    """Update the duration of an existing QSO record (matched by file_path)."""
    con = _connect()
    with _lock:
        con.execute("UPDATE qsos SET duration=? WHERE file_path=?", (duration, file_path))
        con.commit()


def update_qso_file_path(old_path: str, new_path: str) -> None:
    """Update the stored file_path (e.g. after WAV->MP3 conversion)."""
    con = _connect()
    with _lock:
        con.execute("UPDATE qsos SET file_path=? WHERE file_path=?", (new_path, old_path))
        con.commit()


def _fmt_ts(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _file_duration(path: str) -> float:
    """Return audio duration in seconds, or 0.0 on error.

    WAV reads from header. MP3 prefers mutagen, falls back to 128 kbps CBR estimate.
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
    """Scan recordings directory and seed the DB with existing files.

    Idempotent: skips if DB already has records (normal case after first run).
    Uses a single transaction for fast bulk insertion.
    """
    if not os.path.isdir(recordings_dir):
        return

    con = _connect()
    row_count = con.execute("SELECT COUNT(*) FROM qsos").fetchone()[0]
    if row_count > 0:
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
                parts = fname[:-4].split("_")
                ts = 0.0
                if len(parts) >= 2:
                    try:
                        ts = time.mktime(
                            time.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H%M%S")
                        )
                    except Exception:
                        ts = 0.0
                rows.append(("_continuous", "CONTINUOUS", "", ts, fp, dur, _extract_rx(fp)))
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
            rows.append((contest, call, band, ts, fp, dur, _extract_rx(fp)))

    if not rows:
        return

    con = _connect()
    with _lock:
        con.executemany(
            "INSERT OR IGNORE INTO qsos "
            "(contest, call, band, timestamp, file_path, duration, rx) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()