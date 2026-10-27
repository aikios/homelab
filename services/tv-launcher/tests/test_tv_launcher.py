"""tv-launcher tests (stdlib unittest; no TV, no network).

    uv run --no-sync python -m unittest discover -s services/tv-launcher/tests
"""
import os
import socket
import tempfile
import unittest
from unittest import mock

from tv_launcher import watcher as w


class ClassifyTest(unittest.TestCase):
    def test_ours(self):
        for page in ("jellyfin.html?v=18", "sports.html"):
            cur = {"APP_ID": "3010", "MESSAGE": f"http://10.0.0.2:{w.PORT}/{page}"}
            self.assertEqual(w.classify(cur), "ours")

    def test_input_is_hands_off(self):
        self.assertEqual(w.classify({"APP_ID": "1", "NAME_SPACE": 0, "MESSAGE": ""}), "input")

    def test_other(self):
        self.assertEqual(w.classify({}), "other")
        self.assertEqual(w.classify(None), "other")
        self.assertEqual(w.classify({"APP_ID": "3", "MESSAGE": "http://x:1234/jellyfin.html"}), "other")


class WatchdogTest(unittest.TestCase):
    def test_noop_outside_systemd(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            w.beat()  # must not raise

    def test_pings_notify_socket(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notify")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
                srv.bind(path)
                with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                    w.beat()
                self.assertEqual(srv.recv(64), b"WATCHDOG=1")


if __name__ == "__main__":
    unittest.main()
