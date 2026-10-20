"""Unit tests for config self-healing and dual_card normalization."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module


def write_cfg(path, so2r_mode, channels, device2):
    with open(path, "w", encoding="utf-8") as f:
        f.write("[audio]\n")
        f.write("mode = soundcard\n")
        f.write("channels = %d\n" % channels)
        f.write("so2r_mode = %s\n" % so2r_mode)
        f.write("soundcard_device2 = %s\n" % device2)


class DualCardHealTest(unittest.TestCase):
    def test_load_config_normalizes_dual_card_for_so1r(self):
        """dual_card with channels=1 (the bug report) must load as stereo."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.cfg")
            write_cfg(path, "dual_card", 1, "")
            cfg = config_module.load_config(path)
            self.assertEqual(cfg.so2r_mode, "stereo")

    def test_load_config_normalizes_dual_card_without_second_device(self):
        """dual_card with channels=2 but no RX2 device must load as stereo."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.cfg")
            write_cfg(path, "dual_card", 2, "")
            cfg = config_module.load_config(path)
            self.assertEqual(cfg.so2r_mode, "stereo")

    def test_load_config_keeps_valid_dual_card(self):
        """dual_card with channels=2 AND a second device stays as-is."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.cfg")
            write_cfg(path, "dual_card", 2, "USB Device")
            cfg = config_module.load_config(path)
            self.assertEqual(cfg.so2r_mode, "dual_card")

    def test_reject_invalid_config_is_noop_for_stereo(self):
        cfg = config_module.AppConfig()
        cfg.so2r_mode = "stereo"
        cfg.channels = 1
        cfg.soundcard_device2 = ""
        config_module.reject_invalid_config(cfg)
        self.assertEqual(cfg.so2r_mode, "stereo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
