"""Tests for dashboard event fan-out (SSE broadcasting)."""

import os
import queue
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as main_module


class EventBroadcastTest(unittest.TestCase):
    def setUp(self):
        with main_module._EVENT_LOCK:
            main_module._EVENT_SUBSCRIBERS.clear()
        main_module._EVENT_DROPPED = 0

    def tearDown(self):
        with main_module._EVENT_LOCK:
            main_module._EVENT_SUBSCRIBERS.clear()

    def _subscribe(self, maxsize=100):
        sub = queue.Queue(maxsize=maxsize)
        with main_module._EVENT_LOCK:
            main_module._EVENT_SUBSCRIBERS.add(sub)
        return sub

    def test_event_reaches_every_subscriber(self):
        first = self._subscribe()
        second = self._subscribe()

        main_module.push_event("qso_saved", {"id": 1})

        self.assertEqual(first.get_nowait()["event"], "qso_saved")
        self.assertEqual(second.get_nowait()["data"], {"id": 1})

    def test_full_subscriber_does_not_block_others(self):
        full = self._subscribe(maxsize=1)
        full.put_nowait({"event": "old", "data": {}})
        healthy = self._subscribe()

        main_module.push_event("qso_saved")

        self.assertEqual(healthy.get_nowait()["event"], "qso_saved")
        self.assertGreaterEqual(main_module._EVENT_DROPPED, 1)

    def test_no_subscribers_is_noop(self):
        main_module.push_event("qso_saved")
        self.assertEqual(main_module._EVENT_DROPPED, 0)

    def test_unsubscribe_stops_delivery(self):
        sub = self._subscribe()
        with main_module._EVENT_LOCK:
            main_module._EVENT_SUBSCRIBERS.discard(sub)

        main_module.push_event("qso_saved")

        with self.assertRaises(queue.Empty):
            sub.get_nowait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
