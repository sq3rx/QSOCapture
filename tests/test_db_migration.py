"""Database migration and RX-label tests."""

import importlib
import os
import sys
import tempfile
import unittest
import wave
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_wav(path, seconds=1.0, rate=8000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qsodb-")
        self.rec = os.path.join(self.tmp, "recordings")
        os.makedirs(self.rec, exist_ok=True)
        self.db = importlib.import_module("db")
        self.db.RECORDINGS_DIR = self.rec
        self.db.DB_PATH = os.path.join(self.tmp, "test.db")
        self.db.close_db()
        self.db.init_db()

    def test_new_continuous_filename_gets_real_timestamp(self):
        rel = "_continuous/20260804_19444812345_RX1.wav"
        make_wav(os.path.join(self.rec, rel))
        self.db.migrate_existing(self.rec)
        rows = self.db.query_contacts(continuous=True, limit=10)["qsos"]
        self.assertEqual(len(rows), 1)
        expected = datetime(2026, 8, 4, 19, 44, 48, tzinfo=timezone.utc).timestamp()
        con = self.db._connect()
        got = con.execute("SELECT timestamp FROM qsos LIMIT 1").fetchone()[0]
        self.assertEqual(got, expected)
        self.assertEqual(rows[0]["label"], "RX1")

    def test_old_continuous_filename_gets_timestamp(self):
        rel = "_continuous/20260804_194448_RX2.wav"
        make_wav(os.path.join(self.rec, rel))
        self.db.migrate_existing(self.rec)
        rows = self.db.query_contacts(continuous=True, limit=10)["qsos"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "RX2")
        self.assertNotEqual(rows[0]["timestamp"], "")

    def test_legacy_file_without_rx_suffix_defaults_to_rx1(self):
        self.assertEqual(self.db._extract_rx("2025_CQWW/2026-01-01_1200_SP1ABC_20M.wav"), "RX1")
        self.assertEqual(self.db._extract_rx("2025_CQWW/2026-01-01_1200_SP1ABC_20M_RX2.wav"), "RX2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
