"""homelab_core.subtitles tests. Run: uv run --no-sync python -m unittest discover -s libs/core/tests"""
import unittest

from homelab_core import subtitles as s


class PickPrimary(unittest.TestCase):
    def pick(self, tracks):
        got = s.pick_primary(tracks)
        assert got is not None
        return got

    def test_full_track_beats_forced_and_signs(self):
        tracks = [{"title": "Forced", "cues": 40},
                  {"title": "Signs & Songs", "cues": 90},
                  {"title": "English", "cues": 1200}]
        self.assertEqual(self.pick(tracks)["title"], "English")

    def test_forced_flag_excluded_even_untitled(self):
        tracks = [{"forced": True, "cues": 2000}, {"cues": 900}]
        self.assertEqual(self.pick(tracks)["cues"], 900)

    def test_most_cues_wins_over_order(self):
        tracks = [{"title": "", "cues": 300}, {"title": "", "cues": 1100}]
        self.assertEqual(self.pick(tracks)["cues"], 1100)

    def test_sdh_loses_ties_and_unknowns(self):
        tracks = [{"title": "English SDH"}, {"title": "English"}]
        self.assertEqual(self.pick(tracks)["title"], "English")
        tracks = [{"title": "English [CC]", "cues": 800}, {"title": "English", "cues": 800}]
        self.assertEqual(self.pick(tracks)["title"], "English")

    def test_sdh_with_more_dialogue_still_wins(self):
        tracks = [{"title": "English", "cues": 150}, {"title": "English SDH", "cues": 1400}]
        self.assertEqual(self.pick(tracks)["title"], "English SDH")

    def test_order_breaks_full_ties(self):
        tracks = [{"title": "a"}, {"title": "b"}]
        self.assertEqual(self.pick(tracks)["title"], "a")

    def test_only_secondary_is_none(self):
        self.assertIsNone(s.pick_primary([{"forced": True}, {"title": "Commentary"}]))
        self.assertIsNone(s.pick_primary([]))


class Helpers(unittest.TestCase):
    def test_cue_count(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\nhi\n\n2\n00:00:03,000 --> 00:00:04,000\nyo\n"
        self.assertEqual(s.cue_count(srt), 2)
        ass = "[Events]\nFormat: x\nDialogue: 0,a\nComment: 0,b\nDialogue: 0,c\n"
        self.assertEqual(s.cue_count(ass), 2)
        self.assertEqual(s.cue_count(""), 0)

    def test_lang_key(self):
        for a, b in (("eng", "en"), ("EN", "en"), ("en-US", "en"), ("spa", "es"),
                     ("ger", "deu"), ("chi", "zho")):
            self.assertEqual(s.lang_key(a), s.lang_key(b))
        self.assertNotEqual(s.lang_key("eng"), s.lang_key("spa"))
        self.assertEqual(s.lang_key(""), "und")

    def test_frames_tag(self):
        self.assertEqual(s.frames_tag({"NUMBER_OF_FRAMES-eng": "1234"}), 1234)
        self.assertEqual(s.frames_tag({"NUMBER_OF_FRAMES": "7"}), 7)
        self.assertIsNone(s.frames_tag({"title": "x"}))
        self.assertIsNone(s.frames_tag({"NUMBER_OF_FRAMES": "n/a"}))


if __name__ == "__main__":
    unittest.main()
