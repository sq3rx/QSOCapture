"""Seed the database with test data from a CBR file plus generated contests.

Usage:
    python seed_test_data.py          # uses tests/*.cbr and creates dummy audio
    python seed_test_data.py --clean  # removes existing DB first
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import random
import sys
import time
import wave
from typing import List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as qso_db

RECORDINGS_DIR = "recordings"
TEST_DIR = "tests"

BAND_CENTRES = {
    "160": 1.8, "80": 3.5, "40": 7.0, "30": 10.0, "20": 14.0,
    "17": 18.0, "15": 21.0, "12": 24.0, "10": 28.0, "6": 50.0,
}

FREQ_RANGES = {
    "40M": (7000, 7100), "20M": (14000, 14100), "15M": (21000, 21100),
    "10M": (28000, 28200), "80M": (3500, 3600), "160M": (1800, 1850),
    "30M": (10100, 10150), "12M": (24900, 25000), "17M": (18068, 18168),
}

BAND_CYCLE = ["40M", "20M", "15M", "10M", "80M", "20M", "40M", "15M"]

PREFIX_CONTINENT = [
    ("UA9", "AS"), ("UA", "EU"), ("K", "NA"), ("W", "NA"), ("N", "NA"),
    ("VE", "NA"), ("PY", "SA"), ("LU", "SA"), ("CE", "SA"),
    ("JA", "AS"), ("HL", "AS"), ("BY", "AS"), ("VK", "OC"), ("ZL", "OC"),
    ("ZS", "AF"), ("5Z", "AF"), ("CT3", "AF"), ("EA8", "AF"),
    ("SP", "EU"), ("DL", "EU"), ("OK", "EU"), ("HA", "EU"), ("LY", "EU"),
    ("OM", "EU"), ("UR", "EU"), ("YO", "EU"), ("YU", "EU"),
    ("9A", "EU"), ("OH", "EU"), ("SM", "EU"), ("PA", "EU"), ("OZ", "EU"),
    ("LA", "EU"), ("F", "EU"), ("G", "EU"), ("GM", "EU"), ("GW", "EU"),
    ("HB", "EU"), ("I", "EU"), ("IT", "EU"), ("LX", "EU"), ("LZ", "EU"),
    ("OE", "EU"), ("S5", "EU"), ("CT", "EU"), ("E7", "EU"), ("EA", "EU"),
    ("KP", "NA"), ("NP", "NA"), ("KL", "NA"),
]

CALLS = [
    "SQ3RX", "SP9XCN", "OK1KKI", "HA3GO", "OM0WR", "LY2K",
    "PJ2T", "3Z1K", "SP4Z", "SZ1A", "AO275AZ", "CN3A", "EF5Y", "PY5FB",
    "ZW8T", "ED7W", "P3AA", "RL3A", "NR4M", "VY2TT", "WC1M", "PJ4A",
    "IP3A", "E7DX", "SE4E", "N2MF", "OK7O", "9A1A", "CR6K", "DR4A",
    "DD1A", "SX9V", "EF1A", "WP3C", "OL7T", "UW5Y", "EU2F", "E79Q",
    "UW73X", "EF6T", "YT4T", "DK2OY", "SP2MKI", "DL3YM", "EU8U", "HA3NU",
    "LY2DX", "LY3I", "C7A", "DK7ZT", "E70A", "SN7O", "LY9A", "DR5X",
    "UR5R", "OM0RX", "F5SGI", "DJ0SP", "ZF2SS", "RM1T", "S53M", "N3RD",
    "G6T", "VC2A", "IP8T", "II4M", "P35A", "OG3B", "HG7T", "NI4W",
    "DM4X", "EW5A", "UA7K", "LZ9W", "S56M", "KP2M", "VE3EJ", "YR8D",
    "DL7LX", "OH1NA", "EW2A", "SP7JLH", "IV3FPX", "OM5KM", "UA3RF",
    "LY7R", "EW8OM", "YU1A", "UR5KO", "OK1TRJ", "OK8SMS", "9A5M", "DJ5AN",
    "S54A", "YU1Q", "S53AR", "DA3X", "IO0A", "UY2UZ", "US6EX", "G6A",
    "Z33F", "OR5Z", "UT5EL", "SN1F", "DM7W", "F5NKX", "HB9HDC",
    "HA7PO", "YO5AVN", "DF1DT", "LZ3ZZ", "AA3B", "K1LZ", "K3RA", "K3SW",
    "NU5A", "NY4A", "AD4EB", "II2C", "I2IFT", "UA4Q", "SX5R", "UW4E",
    "9A3XV", "DR0W", "S570W", "AC1U", "NY6DX", "4X6FR", "AK1W", "CT3KN",
    "PZ5DX", "N3RS", "IP8A", "YT7A", "YU2NPC", "YO4NF", "EU6RO", "DR7T",
    "DM6EE", "OR1Z", "E79D", "LY4A", "DB100FK", "S53F", "SP4AWE", "VE5MX",
    "AA4NC", "CR3DX", "IP1M", "W8MJ", "VE2FK", "K1ZZ", "LZ5EE", "NQ1DX",
    "WZ7F", "NR7DX", "N5CW", "VC7X", "K8MP", "K5WA", "AD5A", "NJ3K",
    "P40L", "TI7W", "K7QA", "KU8E", "NQ2F", "ND7K", "N7AT", "K9CT",
    "KM7W", "HG6O", "DM7A", "RN3BL", "DL3DXX", "HB9IJC", "HA1TJ",
    "DJ3HW", "YU7KW", "SN5T", "S53FO", "DP9A", "EI8X", "IO6A", "RG6G",
    "R9GM", "WN2O", "WK1O", "KQ2M", "WP4X", "OH0V", "N1LN", "KR2Q",
    "UC7A", "RF9C", "K3TC", "TM7A", "NJ4U", "KL5DX", "W1RCR", "XM3R",
    "C4W", "KQ1F", "MW4R", "RA9P", "NN3Q", "W4NZ", "H25A", "8P5A",
    "UP2L", "YL3FT", "WX0B", "DD100FK", "LA2AB", "LZ5R", "UA3R", "YT5A",
    "YT2B", "SO4R", "HG8R", "EI7M", "KB4DX", "UW2U", "9A1P", "II9P",
    "YO6FGZ", "RU1A", "KO8SCA", "RT4F", "NF6A", "K4PV", "SP8GQU", "HA1AG",
    "DP6A", "PA3BUD", "CR3W", "MM9I", "EW2ES", "9A5Y", "YO5ODT", "IZ2OOS",
    "9A7A", "DJ2XY", "UW1M", "OG6N", "LZ3FN", "AK1MD", "ER4A", "K3WW",
    "YO4RDW", "SP2R", "SJ2W", "YT5RA", "Z39A", "TM1A", "9H6A", "MX3W",
    "E77EA", "OK6Y", "S57AL", "IK1PMR", "R7KX", "OK1OA", "OL87OK", "OL4N",
    "OM2XW", "OL9R", "LA8OM", "D4Z", "SM2U", "G5O", "HA8DU", "IQ2DN",
    "F6ARC", "G0MTN", "YL4U", "HA7A", "LZ5PL", "OM7M", "OM3CPF", "YO3GEK",
    "HA7RY", "YO4AR", "HA3DX", "BY4SZ", "RO9O", "UP7L", "RQ9O", "B7C",
    "R9TV", "UN9GD", "UT3UZ", "N4QS", "KA6BIM", "WF9A", "II8K", "UT4LW",
    "NO4Y", "K6AR", "N5RZ", "JE6RPM", "UA9MA", "AT3K", "DL1STG", "SN5J",
    "OK1DEP", "OK2BLD", "OK1RR", "OZ3SM", "DL2NBY", "UT3UV", "SO3O",
    "HB4FG", "YR0K", "LZ7DX", "DK9PY", "PE6X", "PC0A", "SN5N", "TM6M",
    "S59A", "HG3N", "F8DGY", "II1P", "II2Q", "UN0L", "P44W", "N3QE",
    "OH8X", "EA2W", "KF3P", "BG0DLA", "9H6EE", "UA6LUQ", "NU7Y", "RX3Q",
    "NN7CW", "YU5M", "SV8OVJ", "ZM4T", "Z35W", "WA3AAN", "4X7M", "W1CW",
    "IZ8GUQ", "K3LR", "K0ZR", "IZ0DHC", "YO8FC", "YP3X", "UY7LM", "NN5J",
    "F4VTC", "EF3T", "NO3Y", "SV5DKL", "R8CT", "R4WAX", "LZ1ND",
    "IK3UNA", "VK1A", "IQ1LA", "KM9P", "LZ3R", "IK0YVV", "IB9R", "RA1QD",
    "LA3BO", "NG3R", "TF3W", "YU9YAU", "K8LX", "YU1NR", "EA4JI", "YT6X",
    "S58MU", "ES1BH", "SV1FWV", "NP3X", "RA3XM", "YL2BJ",
    "WO4O", "NR6O", "IT9ESW", "RN4HAB", "IB6B", "EA8KR", "LZ7M", "RM3F",
    "RU4SO", "UA6AA", "UP0L", "UF5A", "EA5DF", "A71WW", "LZ4TX", "ME5W",
    "OG1F", "LZ6Y", "IZ4OSH", "SF6J", "3V8SS", "MM2T", "MD2C", "SV4FFL",
    "OP6A", "IP4E", "LZ05RN", "R5QQ", "UF1F", "EA3EE", "UR7LY", "IO6T",
    "YT9W", "S52W", "SK0QO", "RA7R", "F5TYY", "F5KLE", "UT2II", "OG55W",
    "LZ2MP", "IU2ECB", "YO8DOH", "LA1TV", "DL6RDE", "GJ0KYZ", "GM2S",
    "M1X", "EI6KW", "ED4J", "GE3VQO", "IK2IKW", "ED3O", "MD4K", "JA1ZGO",
    "JA3YBK", "JE2YRB", "JJ2JQF", "JR4OZR", "MI5I", "YL7X", "ON6MG",
    "S51YI", "PG2V", "9A3MA", "LY5I", "HG5E", "DL1EFW", "N1RM", "YT1X",
    "EA3AR", "IB8A", "IK1JJM", "IP9T", "IO5X", "EC3A", "YR9F", "Z35F",
    "YU0T", "RK7A", "IZ3NYG", "JI1RXQ", "IO4K", "IU0ITX", "AO375JW",
    "EA1CS", "9A5D", "YT0Z", "JJ0VNR", "G4IIY", "ZA1RR", "RC9J", "JA6AVT",
    "PY7RP", "I4IKW", "I1EIS", "IK2CZQ",
    "M2G", "UT8EU", "EA7OR", "LZ2HT", "F1PUX", "IU8RQW",
    "S52KJ", "UX0LL", "RK9AY", "UR3LPM", "G3SJJ", "M7N", "OI7AX",
    "F5NBX", "ON6LO", "PX2A", "PS2E", "PW2F", "LT3E", "PT5J", "YL5W",
    "UZ0U", "ON3DI", "EW1I", "PI4CC", "YT4W", "WG3J",
    "VE3JM", "KI7WX", "RU3A", "IO3F", "KX2NY", "VX3A", "WT2J", "P3C",
    "UR5ECW", "UP5B", "K3UA", "FY5KE", "TM8O", "UG8C", "NT2DR", "RU7M",
    "NY4A", "RK3ER", "E70X", "IO4R", "ON4ZD", "HB7X",
    "SV2JAO", "G8X", "W2YC", "KM5G",
    "UR8RF", "SE5E", "EG3DZ", "NB1N", "UV1IX", "NA8V",
    "K1TR", "YT8A", "VA3SB", "AD4EB",
    "NE3MD", "WM3T", "XM3T", "WT1M", "KR2AA", "VP5M",
    "TM9C", "WK9M", "M4T", "EC5K", "PT2AW", "EF8R", "PP4T", "EI4II",
    "E75M", "GX4GA", "3D2SP",
    "TA2DA", "F2JD",
    "W0AAE", "GM6XX", "WR3O",
    "WX3B", "NT6Q", "M6T", "M3AWD", "PY2PT",
    "RG9A", "YO9SW", "ND3D",
    "UD8A", "KG9N", "IS0AFM", "RT5C",
    "UX1VT", "R3BC", "B1Z", "CT7BJG", "ZA1RST",
    "LZ1DQ", "EA4KA",
    "KU2M", "KQ7I",
    "YB0ECT", "BA4DL",
    "K9NW", "AC0W", "WK1Q", "W9RE", "NI9F", "9M6NA",
    "ZF2SS", "TA3D", "BY5EA", "RA7C", "F8DFP", "RW9JZ", "R9MM",
    "UT0RS", "JR6CSY", "OI5AY", "UC8Y", "IZ8EFD", "IS0LYN", "UT7NI",
    "PR1T", "RM4F", "UB7B", "LZ4R", "2E0CVN", "HL2BQG",
    "OK1AY", "YT1A", "PA3AAV", "HA2KMR", "SP3VT", "SP9DLY", "HA6NL",
    "OK3EQ", "SE5T", "DL4WA", "DL5RMH", "DK3YD", "DA0BCC", "UR4UWY",
    "ED5R", "GM4X", "PA5KT", "LZ8A", "EU1DC", "JH7QXJ",
    "Z61DX", "R1DX", "BY1AS",
    "OH7KBF", "OH4X",
    "LW1F", "WK5T", "NO9E", "OP7T", "CT1BOH", "OH2T", "E2A",
    "WM9C", "SN3A",
    "PV2K", "ES7A", "RK3P", "VL2N", "9N7AA",
    "OI3V", "UA5R", "CE3CT", "AG3I",
    "OM6NM", "ON5WL", "YT3EWW", "SP1D", "OE5OHO", "SM5IMO",
    "OR2F", "AJ9C", "V3AU", "IP3T",
    "GW2CWO", "OZ5W",
    "OT6M", "DS4EOI",
    "7L4MDM", "N2RC",
    "OP5T", "JH1EAQ", "BI4SSB", "DL9NEI", "RY5A", "LX1NO",
    "JH1GEX", "DD5M", "DL1NEO", "RN5AA", "OK6OK", "PA2A",
    "OH10A", "DK1KC", "LY6C", "OE5TXF", "G4N", "DL6RDR",
    "OM0EE", "IP0O", "S51J", "SC7DX", "DL6TK", "F6HDI", "DM2DZM", "OL3Z",
    "AB3CX", "VE1ANU", "LU5HCB", "YC1DGZ",
    "LS5H", "VA7OM", "KA2MGE", "NB6U",
    "ON7PQ", "PY2WH", "LV1F",
    "LU4HK", "LU8QT", "LU7HN", "PY2EX", "RL4F", "KY2N", "PY1AX",
    "PY2NY", "PV2K", "ZX9X", "F6JOE", "DL8TG", "OM8DD", "DJ2FR",
    "YO2NAA", "DL2RMC", "UZ1U", "S53WW", "SQ9MZ", "F6FJE", "OL0W",
    "DP5X", "HA8TP", "DF7A", "YL7A",
    "SP2QG", "IW3ILM", "DK6SP",
    "SP3A", "OK2HBR", "UZ1WW", "DA3M", "DL1SAN", "HB9HTF",
    "G4PVM", "OK2MBP", "RV3ZN", "OE3G", "S53N", "OE1XA",
    "SP3GTS", "ZL4TT",
    "E74C", "BA7MT", "M5P", "HG0Y",
    "NC1CC", "YU1ED", "RM2U",
    "R5WW", "F4WEJ", "DJ9RR", "OK2ABU", "SP5ELA",
    "DL8ZAJ", "PA8MM", "F6HJO", "OK1UKY", "UN4Q", "SM0Q", "DK7HA",
    "HA4A", "LY1M", "OG50YL", "JM4MGM", "V85RH", "JH6WDG", "DQ2C",
    "LY20EU", "JN4MMO", "JH0NEC", "CB1T",
]

NAMES = [
    "John", "Peter", "Robert", "Michael", "David", "Richard", "Thomas",
    "Mark", "Steve", "Paul", "Andrew", "George", "James", "Brian", "Kevin",
    "Martin", "Tomasz", "Piotr", "Krzysztof", "Andrzej", "Marek", "Jan",
    "Hans", "Wolfgang", "Gunter", "Karl", "Jean", "Pierre", "Jose", "Manuel",
]

QTHS = [
    "Warsaw", "Berlin", "Prague", "Vienna", "Paris", "London", "Madrid",
    "Rome", "Amsterdam", "Brussels", "Stockholm", "Oslo", "Helsinki",
    "Copenhagen", "Dublin", "Budapest", "Bucharest", "Sofia", "Belgrade",
    "Moscow", "New York", "Chicago", "Los Angeles", "Toronto", "Tokyo",
    "Beijing", "Sydney", "Melbourne", "Rio de Janeiro", "Buenos Aires",
]


def safe_call(call: str) -> str:
    return "".join(ch for ch in call if ch.isalnum() or ch in "-_")


def freq_to_band(freq_khz: float) -> str:
    mhz = freq_khz / 1000.0
    best = min(BAND_CENTRES, key=lambda lbl: abs(mhz - BAND_CENTRES[lbl]))
    return f"{best}M"


def get_continent(call: str) -> str:
    c = call.upper().replace("/", " ").strip().split()[0]
    for prefix, cont in PREFIX_CONTINENT:
        if c.startswith(prefix):
            return cont
    return "EU"


def make_wav(path: str, duration: float, sr: int = 16000) -> None:
    """Create a short WAV with sine tones + noise."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = int(sr * duration)
    t = np.arange(n, dtype=np.float32) / sr
    sig = (8000 * np.sin(2 * np.pi * 440 * t) +
           3000 * np.sin(2 * np.pi * 880 * t) +
           np.random.normal(0, 600, n).astype(np.float32))
    sig = np.clip(sig, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(sig.tobytes())


def parse_cbr(path: str) -> List[dict]:
    qsos: List[dict] = []
    contest_name = "UNKNOWN"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.upper().startswith("CONTEST:"):
                contest_name = s.split(":", 1)[1].strip()
            elif s.startswith("QSO:"):
                parts = s.split()
                if len(parts) < 10:
                    continue
                try:
                    freq_khz = float(parts[1])
                except ValueError:
                    freq_khz = 7060.0
                mode = parts[2]
                date_str = parts[3]
                time_str = parts[4]
                their_call = parts[7]
                rcv_rst = parts[8] if len(parts) > 8 else "599"
                rcv_nr = parts[9] if len(parts) > 9 else "001"
                try:
                    ts = time.mktime(time.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M"))
                except (ValueError, OverflowError):
                    ts = time.time()
                qsos.append({
                    "contest": contest_name, "call": their_call,
                    "band": freq_to_band(freq_khz), "mode": mode,
                    "timestamp": ts, "freq": f"{freq_khz/1000.0:.3f}",
                    "rcv": rcv_rst, "snt": "599",
                    "rcvnr": rcv_nr, "sntnr": f"{len(qsos) + 1:04d}",
                })
    return qsos


def generate_synthetic(name: str, base_date: float, count: int) -> List[dict]:
    qsos: List[dict] = []
    for i in range(count):
        band = BAND_CYCLE[i % len(BAND_CYCLE)]
        call = random.choice(CALLS)
        mode = random.choice(["CW", "SSB", "FT8", "RTTY"])
        fr = FREQ_RANGES.get(band, (7000, 7100))
        freq_khz = random.randint(fr[0], fr[1])
        ts = base_date + (i * 48 * 3600) // count + random.uniform(-30, 30)
        rcv = random.choice(["599", "59", "579", "559"])
        rcv_nr = f"{random.randint(1, 9999):04d}"
        qsos.append({
            "contest": name, "call": call, "band": band, "mode": mode,
            "timestamp": ts, "freq": f"{freq_khz/1000.0:.3f}",
            "rcv": rcv, "snt": "599",
            "rcvnr": rcv_nr, "sntnr": f"{i + 1:04d}",
        })
    return qsos


def seed_qsos(qsos: List[dict], contest_dir: str, dur_range=(3.0, 8.0)) -> int:
    n = len(qsos)
    for i, q in enumerate(qsos):
        ts = q["timestamp"]
        stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(ts))
        call_safe = safe_call(q["call"])
        label = random.choice(["RX1", "RX2"])
        fname = f"{stamp}_{call_safe}_{q['band']}_{label}.wav"
        rel = f"{contest_dir}/{fname}"
        dur = random.uniform(*dur_range)
        make_wav(os.path.join(RECORDINGS_DIR, rel), dur)
        continent = get_continent(q["call"])
        qso_db.insert_qso(
            contest=contest_dir, call=q["call"], band=q["band"],
            mode=q["mode"], freq=q["freq"],
            name=random.choice(NAMES), qth=random.choice(QTHS),
            grid="", comment="",
            exchange=q["rcvnr"], exchange2="", exchange3="",
            rcv=q["rcv"], snt=q["snt"],
            rcvnr=q["rcvnr"], sntnr=q["sntnr"],
            section="", mycall="SQ3RX",
            countryprefix=q["call"][:2], wpxprefix=q["call"][:2],
            continent=continent, operator="SQ3RX", station="",
            contest_nr=q["sntnr"], points="", multiplier=continent,
            multiplier2="", multiplier3="", prec="", ck="", power="",
            n1mm_id="{" + "".join(random.choice("0123456789abcdef") for _ in range(36)) + "}",
            is_claimed="", sent_exchange=q["sntnr"],
            timestamp=ts,
            raw_ts=time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
            duration=dur, file_path=rel,
        )
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{n}")
    return n


def seed_continuous(start_ts: float, end_ts: float,
                    chunk_min: int = 60, label: str = "RX1") -> int:
    """Create continuous chunks spanning [start_ts, end_ts).

    WAV files are short (5 s) for speed; DB duration reflects real chunk length.
    """
    chunk_sec = chunk_min * 60
    cur = start_ts
    count = 0
    out_dir = os.path.join(RECORDINGS_DIR, "_continuous")
    os.makedirs(out_dir, exist_ok=True)

    while cur < end_ts:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(cur))
        fname = f"{stamp}_{label}.wav"
        rel = f"_continuous/{fname}"
        dur = max(min(chunk_sec, end_ts - cur), 1.0)
        make_wav(os.path.join(out_dir, fname), min(5.0, dur))
        qso_db.insert_qso(
            contest="_continuous", call="CONTINUOUS", band="", mode="",
            timestamp=cur, duration=dur, file_path=rel,
        )
        count += 1
        cur += chunk_sec
        if count % 5 == 0:
            print(f"  continuous chunks: {count}")
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed test data into QSOCapture")
    parser.add_argument("--clean", action="store_true",
                        help="Remove existing DB before seeding")
    parser.add_argument("--cbr", default=None,
                        help="Path to .cbr file (auto-searches tests/ if omitted)")
    parser.add_argument("--qsos", type=int, default=1000,
                        help="Synthetic QSOs per contest (default: 1000)")
    args = parser.parse_args()

    if args.clean and os.path.exists(qso_db.DB_PATH):
        print(f"Removing DB: {qso_db.DB_PATH}")
        try:
            os.remove(qso_db.DB_PATH)
        except PermissionError:
            print("  DB in use, can't remove. Try closing the app first.")
            return

    qso_db.init_db()
    os.makedirs(RECORDINGS_DIR, exist_ok=True)

    total_qsos = 0
    total_cont = 0

    # Seed from CBR file
    cbr_path = args.cbr
    if not cbr_path and os.path.isdir(TEST_DIR):
        files = glob.glob(os.path.join(TEST_DIR, "*.cbr"))
        if files:
            cbr_path = files[0]

    if cbr_path and os.path.exists(cbr_path):
        print(f"Seeding from CBR: {cbr_path}")
        qsos = parse_cbr(cbr_path)
        print(f"  Parsed {len(qsos)} QSOs")
        if qsos:
            first_ts = qsos[0]["timestamp"]
            year = time.strftime("%Y", time.localtime(first_ts))
            contest_dir = f"{year}_{qsos[0]['contest']}"
            total_qsos += seed_qsos(qsos, contest_dir, (3.0, 8.0))
            t0 = min(q["timestamp"] for q in qsos)
            t1 = max(q["timestamp"] for q in qsos)
            total_cont += seed_continuous(t0 - 3600, t1 + 3600, 60, "RX1")
            total_cont += seed_continuous(t0 - 1800, t1 + 1800, 120, "RX2")
    else:
        print("No CBR file found – skipping CBR seeding.")

    # Synthetic contests
    contests = [
        ("CQ-WPX-SSB", "2024-06-01 0000"),
        ("SP-DX-RTTY", "2024-07-13 0000"),
        ("IARU-HF",    "2024-08-10 0000"),
    ]
    for name, date_str in contests:
        dt = time.mktime(time.strptime(date_str, "%Y-%m-%d %H%M"))
        year = time.strftime("%Y", time.localtime(dt))
        contest_dir = f"{year}_{name}"
        print(f"Seeding {contest_dir} ({args.qsos} QSOs)")
        qsos = generate_synthetic(name, dt, args.qsos)
        total_qsos += seed_qsos(qsos, contest_dir, (3.0, 8.0))

    # Continuous chunks for synthetic contests
    cont_spans = [
        ("2024-06-01 0000", "2024-06-03 0000", "RX1"),
        ("2024-07-13 0000", "2024-07-15 0000", "RX1"),
        ("2024-08-10 0000", "2024-08-12 0000", "RX1"),
        ("2024-06-02 0000", "2024-06-03 0000", "RX2"),
        ("2024-08-11 0000", "2024-08-12 0000", "RX2"),
    ]
    for ds, de, lbl in cont_spans:
        ts = time.mktime(time.strptime(ds, "%Y-%m-%d %H%M"))
        te = time.mktime(time.strptime(de, "%Y-%m-%d %H%M"))
        total_cont += seed_continuous(ts, te, 60, lbl)

    print(f"\n{'='*60}")
    print(f"Seeding complete!")
    print(f"  QSOs inserted:          {total_qsos}")
    print(f"  Continuous chunks:      {total_cont}")
    print(f"  Database:               {qso_db.DB_PATH}")
    print(f"  Recordings directory:   {os.path.abspath(RECORDINGS_DIR)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()