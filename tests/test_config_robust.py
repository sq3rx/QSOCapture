"""Config robustness tests: malformed values, clamping, atomic save, devices."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module


class ConfigRobustnessTest(unittest.TestCase):
    def test_malformed_values_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.cfg")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[audio]\n")
                f.write("sample_rate = not-a-number\n")
                f.write("channels = also-bad\n")
                f.write("pre_roll = nope\n")
                f.write("[web]\n")
                f.write("port = oops\n")
            cfg = config_module.load_config(path)
            defaults = config_module.AppConfig()
            self.assertEqual(cfg.sample_rate, defaults.sample_rate)
            self.assertEqual(cfg.channels, defaults.channels)
            self.assertEqual(cfg.pre_roll, defaults.pre_roll)
            self.assertEqual(cfg.web_port, defaults.web_port)

    def test_out_of_range_values_are_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.cfg")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[audio]\n")
                f.write("sample_rate = 1\n")
                f.write("channels = 9\n")
                f.write("pre_roll = -5\n")
                f.write("post_roll = 999\n")
                f.write("[general]\n")
                f.write("continuous_chunk_minutes = 0\n")
                f.write("max_recordings_gb = -3\n")
            cfg = config_module.load_config(path)
            self.assertEqual(cfg.sample_rate, 8000)
            self.assertEqual(cfg.channels, 2)
            self.assertEqual(cfg.pre_roll, 0.0)
            self.assertEqual(cfg.post_roll, 120.0)
            self.assertEqual(cfg.continuous_chunk_minutes, 1)
            self.assertEqual(cfg.max_recordings_gb, 0.0)

    def test_save_is_atomic_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.cfg")
            cfg = config_module.AppConfig()
            cfg.station_name = "FIRST"
            config_module.save_config(cfg, path)
            cfg.station_name = "SECOND"
            config_module.save_config(cfg, path)
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.isfile(path + ".bak"))
            loaded = config_module.load_config(path)
            self.assertEqual(loaded.station_name, "SECOND")
            with open(path + ".bak", encoding="utf-8") as f:
                self.assertIn("FIRST", f.read())

    def test_device_encoding_roundtrip(self):
        encoded = config_module.encode_device(3, "USB Audio")
        self.assertEqual(encoded, "3|USB Audio")
        self.assertEqual(config_module.decode_device(encoded), (3, "USB Audio"))
        self.assertEqual(config_module.decode_device("Legacy Name"), (None, "Legacy Name"))
        self.assertEqual(config_module.decode_device(""), (None, ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
