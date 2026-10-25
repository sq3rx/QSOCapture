"""Tests for the centralized logging setup."""

import json
import logging
import time
import unittest

import logging_setup


class FormatterTests(unittest.TestCase):
    def test_formatter_renders_utc_level_logger_and_thread(self):
        formatter = logging_setup.build_formatter()
        record = logging.LogRecord(
            "QSOCapture.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        out = formatter.format(record)
        self.assertRegex(
            out,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}Z "
            r"\[INFO\] QSOCapture\.test \[[^\]]+\]: hello world$",
        )

    def test_formatter_uses_utc(self):
        formatter = logging_setup.build_formatter()
        record = logging.LogRecord(
            "QSOCapture.test", logging.INFO, __file__, 1, "x", (), None)
        rendered = formatter.format(record)
        utc_stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(record.created))
        self.assertIn(utc_stamp, rendered)


class DebugToggleTests(unittest.TestCase):
    def tearDown(self):
        logging_setup.set_debug(False)

    def test_set_debug_toggles_qsocapture_logger_and_file_handler(self):
        logging_setup.set_debug(True)
        self.assertEqual(logging.getLogger("QSOCapture").level, logging.DEBUG)
        if logging_setup._file_handler is not None:
            self.assertEqual(logging_setup._file_handler.level, logging.DEBUG)

        logging_setup.set_debug(False)
        self.assertEqual(logging.getLogger("QSOCapture").level, logging.INFO)
        if logging_setup._file_handler is not None:
            self.assertEqual(logging_setup._file_handler.level, logging.INFO)

    def test_is_debug_reflects_state(self):
        logging_setup.set_debug(True)
        self.assertTrue(logging_setup.is_debug())
        logging_setup.set_debug(False)
        self.assertFalse(logging_setup.is_debug())


class BufferTests(unittest.TestCase):
    def test_buffer_captures_and_get_recent_logs_returns_lines(self):
        logging_setup.setup_logging()
        marker = "buffer-marker-%d" % time.time_ns()
        logging.getLogger("QSOCapture.test").warning(marker)
        lines = logging_setup.get_recent_logs(200)
        self.assertTrue(any(marker in ln for ln in lines), lines[-3:])

    def test_clear_buffer_empties_the_ring(self):
        logging_setup.setup_logging()
        logging.getLogger("QSOCapture.test").warning("to-be-cleared")
        logging_setup.clear_buffer()
        self.assertEqual(logging_setup.get_recent_logs(10), [])


class DescribeExceptionTests(unittest.TestCase):
    def test_oserror_uses_numeric_codes_not_localized_text(self):
        exc = OSError(10061, "Connection refused")
        exc.winerror = 1225  # type: ignore[attr-defined]
        text = logging_setup.describe_exception(exc)
        self.assertIn("winerror=1225", text)
        self.assertIn("errno=10061", text)
        self.assertNotIn("Connection refused", text)

    def test_plain_exception_keeps_message(self):
        self.assertEqual(
            logging_setup.describe_exception(ValueError("bad value")),
            "ValueError bad value",
        )


class ExcepthookTests(unittest.TestCase):
    def test_keyboard_interrupt_is_warning_without_traceback(self):
        with self.assertLogs("QSOCapture.main", level="WARNING") as captured:
            logging_setup.handle_uncaught(KeyboardInterrupt, KeyboardInterrupt(), None)
        joined = "\n".join(captured.output)
        self.assertIn("Interrupted", joined)
        self.assertNotIn("CRITICAL", joined)

    def test_real_error_is_critical(self):
        with self.assertLogs("QSOCapture.main", level="CRITICAL") as captured:
            try:
                raise RuntimeError("boom")
            except RuntimeError as exc:
                logging_setup.handle_uncaught(type(exc), exc, exc.__traceback__)
        self.assertTrue(any("CRITICAL" in line for line in captured.output))


class ApiLogFilterTests(unittest.TestCase):
    def test_debug_lines_filtered_unless_requested(self):
        import main as main_module

        main_module.LOG_BUFFER.clear()
        main_module.LOG_BUFFER.append(
            "2026-01-01 00:00:00.000Z [DEBUG] QSOCapture.x [T]: verbose")
        main_module.LOG_BUFFER.append(
            "2026-01-01 00:00:00.000Z [INFO] QSOCapture.x [T]: normal")

        plain = json.loads(main_module.api_log(n=50, debug=False).body)
        self.assertEqual(len(plain["lines"]), 1)
        self.assertIn("normal", plain["lines"][0])

        full = json.loads(main_module.api_log(n=50, debug=True).body)
        self.assertEqual(len(full["lines"]), 2)


if __name__ == "__main__":
    unittest.main()
