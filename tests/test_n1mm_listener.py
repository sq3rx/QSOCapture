"""Unit tests for the N1MM contactreplace / contactdelete data path.

Exercises the DB-driven paths (metadata edit/rename and deletion) against
parsed packets, without any live UDP socket or audio capture.
"""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module
import n1mm_listener as nf


def qts(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


class ReplaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="n1mmt-")
        rec_dir = os.path.join(cls._tmp, "recordings")
        os.makedirs(rec_dir, exist_ok=True)
        config_module.RECORDINGS_DIR = rec_dir
        cls.db = importlib.import_module("db")
        cls.db.RECORDINGS_DIR = rec_dir
        cls.db.DB_PATH = os.path.join(cls._tmp, "test.db")
        cls.db.init_db()
        cfg = config_module.AppConfig()
        cfg.n1mm_bind_ip = "127.0.0.1"
        cfg.n1mm_udp_port = 12060
        cls.listener = nf.N1MMListener(cfg, on_contact=lambda c: None)

    def _record(self, guid, call, band, ts_raw):
        contest_dir = "2026_CQ-WPX-CW"
        out_dir = os.path.join(self.db.RECORDINGS_DIR, contest_dir)
        os.makedirs(out_dir, exist_ok=True)
        fname = f"{call}_{band}.wav"
        fp = os.path.join(out_dir, fname)
        if not os.path.exists(fp):
            with open(fp, "wb") as f:
                f.write(b"0")
        rel = f"{contest_dir}/{fname}"
        self.db.insert_qso(contest="CQ-WPX-CW", call=call, band=band,
                           mode="CW", timestamp=qts(2026, 7, 23, 12, 0, 0),
                           raw_ts="2026-07-23 12:00:00", n1mm_id=guid,
                           file_path=rel)
        return rel

    def test_replace_flat_packet_updates_row_and_renames_file(self):
        guid = "GUID001"
        self._record(guid, "RI1ALP", "20M", "14.025")
        packet = (
            "<ContactReplace>"
            "<id>%s</id><timestamp>2026-07-23 12:00:00</timestamp>"
            "<call>RI1BET</call><band>40m</band><mode>CW</mode>"
            "<freq>3525</freq><name>Zoo</name>"
            "</ContactReplace>"
        ) % guid
        self.listener._handle_packet(packet.encode())
        rows = self.db.query_contacts(limit=10)["qsos"]
        edited = next(r for r in rows if r["n1mm_id"] == guid)
        self.assertEqual(edited["call"], "RI1BET")
        self.assertEqual(edited["band"], "40M")
        self.assertEqual(edited["name"], "Zoo")
        rel_path = edited["url"].lstrip("/")
        if rel_path.startswith("audio/"):
            rel_path = rel_path[len("audio/"):]
        self.assertTrue(os.path.exists(
            os.path.join(self.db.RECORDINGS_DIR, rel_path)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
