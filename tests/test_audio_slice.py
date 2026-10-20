"""Audio slicing tests: window math, file/DB consistency and MP3 fallback."""

import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module


class SliceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qsoaudio-")
        self.rec = os.path.join(self.tmp, "recordings")
        os.makedirs(self.rec, exist_ok=True)
        config_module.RECORDINGS_DIR = self.rec
        self.audio = importlib.import_module("audio_manager")
        self.audio.RECORDINGS_DIR = self.rec
        self.db = importlib.import_module("db")
        self.db.RECORDINGS_DIR = self.rec
        self.db.DB_PATH = os.path.join(self.tmp, "test.db")
        self.db.close_db()
        self.db.init_db()
        cfg = config_module.AppConfig()
        cfg.audio_mode = "soundcard"
        cfg.channels = 1
        cfg.sample_rate = 8000
        cfg.pre_roll = 1.0
        cfg.post_roll = 1.0
        self.cfg = cfg
        self.source = self.audio.SoundcardAudioSource(cfg)
        self.source._running = True
        self.source.buffer.write(np.ones((8000 * 5, 1), dtype=np.int16) * 1000)

    def _request(self, fmt="wav"):
        req = self.audio.QSORequest(
            call="SP1ABC", band="20M", mode="SSB", contest="CQWW_SSB",
            timestamp=time.time(), pre_roll=1.0, post_roll=1.0,
            receive_ts=time.time() - 1.0, radio_nr="1",
        )
        return req

    def test_slice_writes_audio_and_db_row(self):
        path = self.source.slice_qso(self._request())
        self.assertTrue(path and os.path.isfile(path))
        rows = self.db.query_contacts(limit=10)["qsos"]
        self.assertEqual(len(rows), 1)
        rel = rows[0]["url"].lstrip("/").replace("audio/", "", 1)
        self.assertTrue(os.path.isfile(os.path.join(self.rec, rel)))

    def test_mp3_without_lameenc_falls_back_to_wav_and_db_matches(self):
        self.cfg.audio_format = "mp3"
        self.source.cfg.audio_format = "mp3"
        with mock.patch.dict(sys.modules, {"lameenc": None}):
            path = self.source.slice_qso(self._request())
        self.assertTrue(path and path.endswith(".wav"))
        rows = self.db.query_contacts(limit=10)["qsos"]
        self.assertEqual(len(rows), 1)
        rel = rows[0]["url"].lstrip("/").replace("audio/", "", 1)
        self.assertTrue(rel.endswith(".wav"))
        self.assertTrue(os.path.isfile(os.path.join(self.rec, rel)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
