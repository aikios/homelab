"""homelab_core.notify transport tests (no network: HTTPSConnection is faked).

    uv run --no-sync python -m unittest discover -s libs/core/tests
"""
import http.client
import json
import unittest
from typing import ClassVar
from unittest import mock

from homelab_core import notify


class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body


class FakeConn:
    """Scripted HTTPSConnection: each item is a (status, body) or an exception."""
    made: ClassVar[list] = []
    script: ClassVar[list] = []

    def __init__(self, host, timeout=None):
        self.host, self.sent, self.closed = host, [], False
        FakeConn.made.append(self)

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body, headers))
        step = FakeConn.script.pop(0)
        if isinstance(step, Exception):
            raise step
        self._next = step

    def getresponse(self):
        return FakeResp(*self._next)

    def close(self):
        self.closed = True


class Transport(unittest.TestCase):
    def setUp(self):
        FakeConn.made, FakeConn.script = [], []
        notify._local.conn = None
        self.conn_patch = mock.patch.object(notify.http.client, "HTTPSConnection", FakeConn)
        self.sleep_patch = mock.patch.object(notify.time, "sleep")
        self.conn_patch.start()
        self.sleep = self.sleep_patch.start()

    def tearDown(self):
        self.conn_patch.stop()
        self.sleep_patch.stop()
        notify._local.conn = None

    def test_reuses_one_connection_and_sends_auth_in_process(self):
        FakeConn.script = [(200, b"[]"), (200, b"[]")]
        notify._request("GET", notify.API + "/channels/42/messages?limit=100", "tok")
        notify._request("GET", notify.API + "/channels/42/messages?limit=100&after=7", "tok")
        self.assertEqual(len(FakeConn.made), 1)
        c = FakeConn.made[0]
        self.assertEqual(c.host, "discord.com")
        method, path, body, hdrs = c.sent[1]
        self.assertEqual((method, path, body), ("GET", "/api/v10/channels/42/messages?limit=100&after=7", None))
        self.assertEqual(hdrs["Authorization"], "Bot tok")
        self.assertEqual(hdrs["User-Agent"], notify.USER_AGENT)

    def test_post_json_body(self):
        FakeConn.script = [(200, b'{"id": "1"}')]
        notify.send_channel_message("tok", "42", "x" * 3000)
        _, _, body, hdrs = FakeConn.made[0].sent[0]
        self.assertEqual(hdrs["Content-Type"], "application/json")
        self.assertEqual(len(json.loads(body)["content"]), notify.MAX_MESSAGE_CHARS)

    def test_stale_keepalive_reconnects(self):
        FakeConn.script = [(200, b"[]"), http.client.RemoteDisconnected("idle close"), (200, b"[]")]
        notify._request("GET", notify.API + "/channels/42/messages?limit=100", "tok")
        notify._request("GET", notify.API + "/channels/42/messages?limit=100", "tok")
        self.assertEqual(len(FakeConn.made), 2)
        self.assertTrue(FakeConn.made[0].closed)

    def test_rate_limit_waits_then_retries(self):
        FakeConn.script = [(429, b'{"message": "slow down", "retry_after": 0.4}'), (200, b'[{"id": "5"}]')]
        self.assertEqual(notify._request("GET", notify.API + "/channels/42/messages?limit=100", "tok"), [{"id": "5"}])
        self.sleep.assert_called_once_with(0.4)

    def test_long_rate_limit_is_an_error(self):
        FakeConn.script = [(429, b'{"retry_after": 600}')]
        with self.assertRaises(RuntimeError):
            notify._request("GET", notify.API + "/channels/42/messages?limit=100", "tok")

    def test_discord_error_payload_raises(self):
        FakeConn.script = [(403, b'{"code": 50007, "message": "Cannot send messages to this user"}')]
        with self.assertRaises(RuntimeError):
            notify.send_channel_message("tok", "42", "hi")

    def test_network_down_raises_after_retries(self):
        FakeConn.script = [OSError("down")] * 3
        with self.assertRaises(RuntimeError):
            notify._request("GET", notify.API + "/channels/42/messages?limit=100", "tok")


if __name__ == "__main__":
    unittest.main()
