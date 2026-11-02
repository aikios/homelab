"""organize tests: wrong-part matches and library overwrites (Radarr is faked).

    uv run --no-sync python -m unittest discover -s services/media-concierge/tests
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from media_concierge import organize

PARTS = [{"title": "BOCCHI THE ROCK! Recap Part 2", "year": 2024, "tmdbId": 2},
         {"title": "BOCCHI THE ROCK! Recap Part 1", "year": 2024, "tmdbId": 1}]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.movies = self.root / "movies"
        self.shows = self.root / "shows"
        self.movies.mkdir()
        self.lookup = PARTS
        cfg = mock.patch.object(organize, "CFG", SimpleNamespace(
            media_movies=str(self.movies), media_shows=str(self.shows)))
        radarr = mock.patch.object(organize, "Radarr")
        cfg.start()
        self.addCleanup(cfg.stop)
        radarr.start().return_value.lookup.side_effect = lambda _term: self.lookup
        self.addCleanup(radarr.stop)

    def download(self, name):
        d = self.root / "dl" / name
        d.mkdir(parents=True)
        (d / (name + ".mkv")).write_bytes(b"new")
        return d


class PickHit(Base):
    def test_named_part_wins_over_lookup_order(self):
        ident = organize.identify("x.mkv", "BOCCHI THE ROCK! Recap Part 1")
        self.assertEqual((ident.title, ident.tmdb_id), ("BOCCHI THE ROCK! Recap Part 1", 1))

    def test_word_part_numbers_match(self):
        self.lookup = [{"title": "Dune: Part Two", "year": 2024}, {"title": "Dune: Part One", "year": 2021}]
        self.assertEqual(organize.identify("x.mkv", "Dune Part 1").year, 2021)

    def test_no_part_named_among_several_parts_is_ambiguous(self):
        self.assertEqual(organize.identify("Bocchi Re Re 2024.mkv").confidence, "ambiguous")

    def test_plain_movie_keeps_first_hit(self):
        self.lookup = [{"title": "Dune", "year": 2021}, {"title": "Dune: Part Two", "year": 2024}]
        self.assertEqual(organize.identify("Dune.2021.mkv").title, "Dune")

    def test_named_part_of_a_single_title_falls_back_to_it(self):
        self.lookup = [{"title": "Heat", "year": 1995}]
        self.assertEqual(organize.identify("x.mkv", "Heat Part 1").title, "Heat")


class ImportDownload(Base):
    def test_files_the_part_it_was_asked_for(self):
        res = organize.import_download(str(self.download("a")), "BOCCHI THE ROCK! Recap Part 1", "movie")
        self.assertIn("Recap Part 1 (2024)", res["library_path"])

    def test_refuses_overwrite_even_across_extensions(self):
        slot = self.movies / "BOCCHI THE ROCK! Recap Part 1 (2024)"
        slot.mkdir()
        kept = slot / "BOCCHI THE ROCK! Recap Part 1 (2024).mp4"
        kept.write_bytes(b"old")
        src = self.download("a")
        with self.assertRaises(FileExistsError):
            organize.import_download(str(src), "BOCCHI THE ROCK! Recap Part 1", "movie")
        self.assertEqual(kept.read_bytes(), b"old")
        self.assertTrue((src / "a.mkv").exists())

    def test_ambiguous_title_leaves_the_download(self):
        src = self.download("Bocchi Re Re 2024")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            organize.import_download(str(src), None, "movie")
        self.assertTrue((src / "Bocchi Re Re 2024.mkv").exists())

    def test_existing_episode_is_skipped_not_overwritten(self):
        self.lookup = []
        sonarr = mock.patch.object(organize, "Sonarr")
        sonarr.start().return_value.lookup.return_value = [{"title": "Show", "tvdbId": 9}]
        self.addCleanup(sonarr.stop)
        season = self.shows / "Show" / "Season 01"
        season.mkdir(parents=True)
        kept = season / "Show - S01E01.mp4"
        kept.write_bytes(b"old")
        src = self.root / "dl" / "Show.S01"
        src.mkdir(parents=True)
        (src / "Show.S01E01.mkv").write_bytes(b"new")
        (src / "Show.S01E02.mkv").write_bytes(b"new")
        res = organize.import_download(str(src), None, "show")
        self.assertEqual(res["count"], 1)
        self.assertEqual(kept.read_bytes(), b"old")
        self.assertIn("already in library", res["skipped"][0])


if __name__ == "__main__":
    unittest.main()
