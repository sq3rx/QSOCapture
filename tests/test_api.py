"""API-level regression tests for config atomicity, paths and streaming export."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

import config as config_module
import main as main_module


class ConfigApiTest(unittest.TestCase):
    def setUp(self):
        main_module.cfg = config_module.AppConfig()
        main_module.cfg.so2r_mode = "stereo"
        main_module.cfg.channels = 1

    def test_invalid_dual_card_does_not_mutate_live_config(self):
        original = config_module.config_to_dict(main_module.cfg)
        payload = {"so2r_mode": "dual_card", "channels": 1}
        with mock.patch.object(main_module, "save_config"), \
                mock.patch.object(main_module, "_apply_and_restart"):
            with self.assertRaises(HTTPException):
                main_module.api_set_config(payload)
        self.assertEqual(config_module.config_to_dict(main_module.cfg), original)

    def test_valid_config_replaces_global_atomically(self):
        with mock.patch.object(main_module, "save_config"), \
                mock.patch.object(main_module, "_apply_and_restart") as restart:
            result = main_module.api_set_config({"station_name": "SP1ABC"})
        self.assertEqual(main_module.cfg.station_name, "SP1ABC")
        self.assertTrue(result.status_code == 200)
        restart.assert_not_called()

    def test_service_restart_flags(self):
        old = config_module.AppConfig()
        new = config_module.AppConfig()
        new.station_name = "X"
        self.assertEqual(main_module._service_restart_flags(old, new), (False, False))
        new.sample_rate = 44100
        self.assertEqual(main_module._service_restart_flags(old, new), (True, False))
        old = config_module.AppConfig()
        new = config_module.AppConfig()
        new.n1mm_udp_port = 12061
        self.assertEqual(main_module._service_restart_flags(old, new), (False, True))


class PathSafetyTest(unittest.TestCase):
    def test_rejects_traversal_and_drive_paths(self):
        for bad in ("../x", "..\\x", "C:\\x", "C:x", "/etc", "a/b", ""):
            with self.assertRaises(HTTPException):
                main_module._safe_contest(bad)
        self.assertEqual(main_module._safe_contest("2026_CQWW_SSB"), "2026_CQWW_SSB")


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qsoexport-")
        self.rec = os.path.join(self.tmp, "recordings")
        os.makedirs(os.path.join(self.rec, "2026_CQWW_SSB"), exist_ok=True)
        with open(os.path.join(self.rec, "2026_CQWW_SSB", "a.wav"), "wb") as f:
            f.write(b"RIFF")
        self.old_rec = main_module.RECORDINGS_DIR
        main_module.RECORDINGS_DIR = self.rec
        main_module.audio_source = None

    def tearDown(self):
        main_module.RECORDINGS_DIR = self.old_rec
        main_module.audio_source = None

    def test_export_writes_temp_zip(self):
        response = main_module.api_export(None)
        try:
            self.assertTrue(os.path.isfile(response.path))
            self.assertGreater(os.path.getsize(response.path), 0)
        finally:
            if os.path.exists(response.path):
                os.remove(response.path)


class DestructiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qsodestructive-")
        self.rec = os.path.join(self.tmp, "recordings")
        os.makedirs(os.path.join(self.rec, "_continuous"), exist_ok=True)
        with open(os.path.join(self.rec, "_continuous", "chunk.wav"), "wb") as f:
            f.write(b"RIFF")
        self.old_rec = main_module.RECORDINGS_DIR
        main_module.RECORDINGS_DIR = self.rec

    def tearDown(self):
        main_module.RECORDINGS_DIR = self.old_rec
        main_module.audio_source = None

    def test_delete_recordings_pauses_and_waits_before_removing_files(self):
        class FakeSource:
            cfg = config_module.AppConfig()

            def __init__(self):
                self._continuous_paused = False
                self.paused = False
                self.waited = False

            def pause_continuous(self):
                self.paused = True
                self._continuous_paused = True

            def wait_continuous_idle(self, timeout=5.0):
                self.waited = True
                return True

            def resume_continuous(self):
                self._continuous_paused = False

        fake = FakeSource()
        main_module.audio_source = fake
        with mock.patch.object(main_module, "_backup_before_destructive"), \
                mock.patch.object(main_module.qso_db, "clear_all"):
            result = main_module.api_delete_recordings()
        self.assertTrue(fake.paused)
        self.assertTrue(fake.waited)
        self.assertFalse(os.path.exists(os.path.join(self.rec, "_continuous", "chunk.wav")))
        self.assertEqual(result.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
