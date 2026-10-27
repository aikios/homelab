"""media-transcoder tests (stdlib unittest; Discord and ssh are mocked).

    uv run --no-sync python -m unittest discover -s services/media-transcoder/tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from media_transcoder import notify as fn


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for d in ("queue", "claimed", "done", "failed"):
            os.makedirs(os.path.join(self.tmp.name, d))
        self.patches: list = [mock.patch.object(fn, "BASE", self.tmp.name),
                        mock.patch.object(fn, "STATE", os.path.join(self.tmp.name, "notified.json"))]
        for p in self.patches:
            p.start()
        self.sent = []
        self.send_ok = True

        def fake_send(text):
            self.sent.append(text)
            return self.send_ok
        self.patches.append(mock.patch.object(fn, "send", side_effect=fake_send))
        self.patches[-1].start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def file(self, state, jid, **job):
        job.setdefault("source", f"/media/Movies/{jid}.mkv")
        with open(os.path.join(self.tmp.name, state, jid + ".json"), "w") as f:
            json.dump(dict(job, id=jid), f)


class ResultsTest(Base):
    def test_first_run_seeds_without_replaying_backlog(self):
        self.file("done", "old1")
        self.file("failed", "old2")
        self.assertEqual(fn.notify_results(), 0)
        self.assertEqual(self.sent, [])
        self.file("done", "new1", source_bytes=9_100_000_000,
                  result={"status": "done", "detail": "ok via NVIDIA (533s)", "mode": "NVIDIA",
                          "out_bytes": 2_074_000_000, "secs": 533, "subs": 1, "replaced": True})
        self.assertEqual(fn.notify_results(), 1)
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertTrue(msg.startswith("✅ Re-encoded and replaced:"))
        self.assertIn("**new1** — 9.1 GB → 2.1 GB mp4 · NVIDIA · 8m53s · 1 sub", msg)
        self.assertEqual(fn.notify_results(), 0)  # not reported twice

    def test_failures_and_skips_grouped(self):
        fn.notify_results()  # seed (empty)
        self.file("failed", "f1", result={"status": "failed", "error": "encode failed (both modes)"})
        self.file("failed", "f2", result={"status": "failed", "error": "HTTP 500",
                                          "replaced": True})
        self.file("done", "s1", result={"status": "done", "detail": "already-friendly"})
        self.file("done", "legacy")  # filed by an old worker: no result block
        fn.notify_results()
        text = "\n".join(self.sent)
        self.assertIn("**f1** — encode failed (both modes) (original untouched)", text)
        self.assertIn("**f2** — replaced, but a later step failed: HTTP 500", text)
        self.assertIn("ℹ️ Skipped:\n• **s1** — already direct-playable", text)
        self.assertIn("• **legacy**", text)

    def test_undelivered_reports_retry(self):
        fn.notify_results()
        self.file("done", "j1", result={"status": "done", "detail": "ok"})
        self.send_ok = False
        fn.notify_results()
        self.send_ok = True
        fn.notify_results()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(fn.notify_results(), 0)

    def test_long_batches_truncate(self):
        fn.notify_results()
        for i in range(fn.MAX_LINES + 5):
            self.file("done", f"j{i:02d}", result={"status": "done", "detail": "ok"})
        fn.notify_results()
        self.assertIn("…and 5 more", self.sent[0])


class SubmitTest(Base):
    def test_submit_batches_one_dm(self):
        fn.notify_submitted([
            {"source": "/m/A (2001)/A (2001).mkv", "why": "mkv · hevc/eac3",
             "source_bytes": 4_200_000_000},
            {"source": "/m/B.avi"}])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0],
                         "🎞️ Queued 2 files for re-encoding:\n"
                         "• **A (2001)** — mkv · hevc/eac3 · 4.2 GB\n• **B**")

    def test_nothing_queued_sends_nothing(self):
        fn.notify_submitted([])
        self.assertEqual(self.sent, [])

    def test_send_swallows_errors(self):
        self.patches[-1].stop()
        self.patches.pop()
        with mock.patch("homelab_core.send_discord_dm", side_effect=SystemExit("no token")):
            self.assertFalse(fn.send("x"))


class WorkerFilingTest(unittest.TestCase):
    def test_file_job_records_result_then_moves(self):
        with mock.patch.dict(os.environ, {"MEDIA_TRANSCODER_STAGE": tempfile.mkdtemp()}):
            from media_transcoder import worker as w
        job = {"id": "j", "source": "/m/x.mkv",
               "result_info": {"mode": "NVIDIA", "out_bytes": 5, "replaced": True}}
        with mock.patch.object(w.subprocess, "run") as run, \
                mock.patch.object(w, "move") as move:
            run.return_value = mock.Mock(returncode=0)
            w.file_job("j.json", job, "done", {"status": "done", "detail": "ok via NVIDIA"})
        written = json.loads(run.call_args.kwargs["input"])
        self.assertNotIn("result_info", written)
        r = written["result"]
        self.assertEqual((r["status"], r["mode"], r["replaced"], r["detail"]),
                         ("done", "NVIDIA", True, "ok via NVIDIA"))
        self.assertIn("claimed/j.json", run.call_args.args[0][-1])
        move.assert_called_once_with("j.json", "done")


if __name__ == "__main__":
    unittest.main()
