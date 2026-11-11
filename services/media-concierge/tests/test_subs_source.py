"""subs.pick_source: which subtitle file a video gets (ffprobe and online fetch faked).

    uv run --no-sync python -m unittest discover -s services/media-concierge/tests
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from media_concierge import subs


class PickSourceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, fn in (("probe", self.fake_probe), ("_fetch_online", lambda v, lang="en": None)):
            p = mock.patch.object(subs, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def fake_probe(self, video):
        p = Path(video)
        side = [str(f) for f in sorted(p.parent.iterdir()) if f.suffix.lower() in subs.SUB_EXT]
        return {"video": video, "embedded": [], "sidecar": side}

    def touch(self, name, text="1\n00:00:01,000 --> 00:00:02,000\nhi\n"):
        f = self.dir / name
        f.write_text(text)
        return str(f)

    def test_other_episodes_lone_sub_is_not_taken(self):
        self.touch("Show - S01E01.mp4")
        e2 = self.touch("Show - S01E02.mp4")
        self.touch("Show - S01E01.en.default.ass", "[Script Info]\n")
        self.assertIsNone(subs.pick_source(e2))  # falls through to online (faked: nothing)

    def test_own_untagged_sub_is_taken(self):
        self.touch("Show - S01E01.mp4")
        e2 = self.touch("Show - S01E02.mp4")
        self.touch("Show - S01E01.en.default.ass", "[Script Info]\n")
        own = self.touch("Show - S01E02.und.srt")
        self.assertEqual(subs.pick_source(e2), ("sidecar", own))

    def test_lone_sub_beside_a_lone_movie_is_taken(self):
        mv = self.touch("Movie (2001).mp4")
        sub = self.touch("release.srt")
        self.assertEqual(subs.pick_source(mv), ("sidecar", sub))

    def test_lone_sub_in_another_language_is_not_taken(self):
        mv = self.touch("Movie (2001).mp4")
        self.touch("Movie (2001).es.srt")
        self.assertIsNone(subs.pick_source(mv))


if __name__ == "__main__":
    unittest.main()
