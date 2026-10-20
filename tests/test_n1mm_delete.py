"""N1MM contactdelete variants and replace retry registration."""

import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module
import n1mm_listener as nf


class DeleteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="n1mmdel-")
        rec = os.path.join(self.tmp, "recordings")
        os.makedirs(rec, exist_ok=True)
        config_module.RECORDINGS_DIR = rec
        nf.RECORDINGS_DIR = rec
        self.db = importlib.import_module("db")
        self.db.RECORDINGS_DIR = rec
        self.db.DB_PATH = os.path.join(self.tmp, "test.db")
        self.db.close_db()
        self.db.init_db()
        self.listener = nf.N1MMListener(config_module.AppConfig(), on_contact=lambda c: None)

    def _add(self, guid):
        rel = "2026_CQWW_SSB/2026-10-24_1200_SP1ABC_20M_RX1.wav"
        full = os.path.join(self.db.RECORDINGS_DIR, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(b"0")
        self.db.insert_qso(
            contest="2026_CQWW_SSB", call="SP1ABC", band="20M", mode="SSB",
            n1mm_id=guid, file_path=rel,
        )
        return rel

    def test_delete_with_contactid(self):
        rel = self._add("GUID-CONTACTID")
        packet = b"<contactdelete><contactid>GUID-CONTACTID</contactid></contactdelete>"
        self.listener._handle_packet(packet)
        self.assertEqual(self.db.query_contacts(limit=10)["total"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.db.RECORDINGS_DIR, rel)))

    def test_delete_with_id(self):
        self._add("GUID-ID")
        packet = b"<contactdelete><id>GUID-ID</id></contactdelete>"
        self.listener._handle_packet(packet)
        self.assertEqual(self.db.query_contacts(limit=10)["total"], 0)

    def test_replace_without_row_schedules_retry(self):
        packet = (
            b"<contactreplace><id>NEW-GUID</id><call>SP2XYZ</call>"
            b"<band>20m</band><mode>SSB</mode><timestamp>2026-10-24 12:00:00</timestamp>"
            b"</contactreplace>"
        )
        with mock.patch("threading.Thread.start") as start:
            self.listener._handle_packet(packet)
            self.assertTrue(start.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
