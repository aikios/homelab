"""stream-resolver unit tests (stdlib unittest; no network, no browser).

    uv run --no-sync python -m unittest discover -s services/stream-resolver/tests
"""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from stream_resolver import server as sa

HERE = os.path.dirname(os.path.abspath(__file__))


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), "rb") as f:
        return f.read()


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        sa._cache["schedule"] = (0, None)

    def test_parses_slug_and_numeric_events(self):
        with mock.patch.object(sa, "fetch", return_value=(200, fixture("home.html"))):
            games = sa.schedule()
        paths = [g["path"] for g in games]
        self.assertEqual(paths[0], "/nfl/redzone")
        self.assertIn("/nfl/401873012", paths)
        self.assertNotIn("/about", paths)
        rz = games[0]
        self.assertEqual((rz["sport"], rz["title"], rz["status"], rz["live"]),
                         ("nfl", "NFL RedZone", "Live", True))

    def test_path_re(self):
        for ok in ("/nfl/redzone", "/nba/401909861", "/f1/us-gp-2026"):
            self.assertTrue(sa.PATH_RE.match(ok), ok)
        for bad in ("/nfl", "/nfl/../x", "/NFL/1", "/nfl/1/2", "nfl/1"):
            self.assertFalse(sa.PATH_RE.match(bad), bad)


class SessionTest(unittest.TestCase):
    def test_parallel_registration_never_collides(self):
        # The old ms-timestamp sids collided under parallel validation and
        # silently swapped referers between streams.
        sids = []
        lock = threading.Lock()

        def reg(n):
            sid = sa.register({"Referer": "https://h%d/" % n}, [])
            with lock:
                sids.append((sid, n))
        ts = [threading.Thread(target=reg, args=(n,)) for n in range(200)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len({s for s, _ in sids}), 200)
        for sid, n in sids:
            self.assertEqual(sa.SESSIONS[sid]["headers"]["Referer"], "https://h%d/" % n)

    def test_header_sets_and_alts(self):
        sets = sa.header_sets("https://player.example/embed/1")
        self.assertEqual(sets[0], {"Referer": "https://player.example/"})
        self.assertEqual(sets[2]["Origin"], "https://player.example")
        c = sa.cand_json("https://cdn.x/live.m3u8", sets[1], "https://player.example/embed/1")
        sid = c["url"].split("/pxy/")[1].split("/")[0]
        self.assertEqual(sa.SESSIONS[sid]["alts"], [sets[0], sets[2]])
        self.assertEqual(c["type"], "hls")


class PlaylistTest(unittest.TestCase):
    def test_rewrite_segments_and_keys(self):
        pl = ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
              "#EXTINF:6,\nseg1.ts\nhttps://other.cdn/seg2.ts\n")
        out = sa.rewrite_playlist(pl, "https://cdn.x/a/live.m3u8", "abcd")
        self.assertIn('URI="/pxy/abcd/https%3A%2F%2Fcdn.x%2Fa%2Fkey.bin"', out)
        self.assertIn("/pxy/abcd/https%3A%2F%2Fcdn.x%2Fa%2Fseg1.ts", out)
        self.assertIn("/pxy/abcd/https%3A%2F%2Fother.cdn%2Fseg2.ts", out)


class SegmentTest(unittest.TestCase):
    TS = (b"\x47" + b"\x00" * 187) * 8

    def test_png_wrapped_ts_is_unwrapped(self):
        wrapped = b"\x89PNG\r\n\x1a\n" + b"x" * 2188 + self.TS
        self.assertEqual(sa.unwrap_segment(wrapped), self.TS)
        self.assertTrue(sa.segment_ok(wrapped))

    def test_plain_and_unknown_segments_untouched(self):
        self.assertEqual(sa.unwrap_segment(self.TS), self.TS)
        enc = bytes(range(256)) * 8  # e.g. AES-encrypted: no magic, no sync
        self.assertEqual(sa.unwrap_segment(enc), enc)
        self.assertTrue(sa.segment_ok(enc))

    def test_pure_image_decoy_rejected(self):
        self.assertFalse(sa.segment_ok(b"\x89PNG\r\n\x1a\n" + b"\x00" * 5000))

    def test_validate_follows_master_and_rejects_decoy(self):
        bodies = {
            "https://c/m.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n",
            "https://c/v.m3u8": b"#EXTM3U\n#EXTINF:6,\ns1.ts\n#EXTINF:6,\ns2.ts\n",
            "https://c/s2.ts": b"\x89PNG" + b"\x00" * 4000,
        }
        with mock.patch.object(sa, "fetch",
                               side_effect=lambda u, **k: (200, bodies[u])):
            self.assertIsNone(sa.validate("https://c/m.m3u8", "https://p/e"))
        bodies["https://c/s2.ts"] = self.TS
        with mock.patch.object(sa, "fetch",
                               side_effect=lambda u, **k: (200, bodies[u])):
            self.assertEqual(sa.validate("https://c/m.m3u8", "https://p/e"),
                             {"Referer": "https://p/"})


class HostStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = [mock.patch.object(sa, "STATE_DIR", self.tmp.name),
                  mock.patch.object(sa, "_stats", None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def test_good_hosts_rank_first_and_persist(self):
        for _ in range(3):
            sa.record_host("good.example", True)
            sa.record_host("bad.example", False)
        self.assertGreater(sa.host_score("good.example"), sa.host_score("new.example"))
        self.assertGreater(sa.host_score("new.example"), sa.host_score("bad.example"))
        sa._stats = None  # reload from disk
        self.assertGreater(sa.host_score("good.example"), 0.5)

    def test_host_of(self):
        self.assertEqual(sa.host_of("https://WWW.Foo.example/x?y"), "foo.example")


class JobTest(unittest.TestCase):
    """The progressive job, with the browser/validation step faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        srcs = [{"url": "https://h%d.example/e" % i, "label": "S%d" % i} for i in range(6)]
        self.patches = [
            mock.patch.object(sa, "STATE_DIR", self.tmp.name),
            mock.patch.object(sa, "_stats", None),
            mock.patch.object(sa, "JOBS", {}),
            mock.patch.object(sa, "event", return_value={"title": "T", "sources": srcs}),
            mock.patch.object(sa, "_check_source", side_effect=self.fake_check),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    @staticmethod
    def fake_check(job, i):
        n = int(sa.host_of(job.sources[i]["url"])[1])
        time.sleep(0.05 * (n + 1))
        ok = n in (2, 4)

        def apply():
            job.sources[i]["state"] = "ok" if ok else "dead"
            job.sources[i]["candidates"] = [{"url": "u%d" % n}] if ok else []
        job.update(apply)

    def test_legacy_call_returns_at_first_ok(self):
        d = sa.prepare("/nfl/1", wait=5)
        self.assertGreaterEqual(d["ok_count"], 1)
        self.assertFalse(d["done"])
        self.assertTrue(any(s["ok"] for s in d["sources"]))

    def test_long_poll_progresses_to_done(self):
        since, jid, seen = -1, None, []
        for _ in range(50):
            d = sa.prepare("/nfl/1", since=since, wait=5, jid=jid)
            self.assertGreater(d["version"], since)
            since, jid = d["version"], d["jid"]
            seen.append(d["checked"])
            if d["done"]:
                break
        self.assertTrue(d["done"])
        self.assertEqual((d["checked"], d["ok_count"]), (6, 2))
        self.assertEqual(seen, sorted(seen))
        # the finished job is reused (cache), not re-run
        self.assertEqual(sa.prepare("/nfl/1", wait=0)["jid"], jid)

    def test_foreign_jid_answers_immediately(self):
        sa.prepare("/nfl/1", wait=5)
        t = time.time()
        d = sa.prepare("/nfl/1", since=10**6, wait=5, jid="deadbeef")
        self.assertLess(time.time() - t, 1)
        self.assertNotEqual(d["jid"], "deadbeef")


class DelegateTest(unittest.TestCase):
    def test_unreachable_peer_falls_back_and_backs_off(self):
        with mock.patch.object(sa, "DELEGATE", "http://127.0.0.1:9"), \
                mock.patch.object(sa, "_delegate_down_until", 0.0):
            self.assertIsNone(sa.delegated_prepare("path=/nfl/1", 1))
            self.assertGreater(sa._delegate_down_until, time.time())
            with mock.patch.object(sa, "fetch") as f:
                self.assertIsNone(sa.delegated_prepare("path=/nfl/1", 1))
                f.assert_not_called()

    def test_dead_peer_costs_only_the_ping(self):
        calls = []

        def fake(url, **k):
            calls.append((url, k.get("timeout")))
            raise TimeoutError()
        with mock.patch.object(sa, "DELEGATE", "http://peer"), \
                mock.patch.object(sa, "_delegate_down_until", 0.0), \
                mock.patch.object(sa, "_delegate_seen_ok", 0.0), \
                mock.patch.object(sa, "fetch", side_effect=fake):
            self.assertIsNone(sa.delegated_prepare("path=/nfl/1", 15))
        self.assertEqual(calls, [("http://peer/ping", 1.5)])

    def test_no_peer_configured(self):
        with mock.patch.object(sa, "DELEGATE", ""):
            self.assertIsNone(sa.delegated_prepare("path=/nfl/1", 1))


if __name__ == "__main__":
    unittest.main()
