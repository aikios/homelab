"""indexer_search tests (no network: Prowlarr/arr lookups are faked).

    uv run --no-sync python -m unittest discover -s services/media-concierge/tests
"""
import unittest
from unittest import mock

from media_concierge import indexer_search as ix


def rel(title, seeders=10, ih=None, **kw):
    return {"title": title, "seeders": seeders, "infoHash": ih or title, "size": 2 * 1024**3,
            "indexer": "1337x", **kw}


class Names(unittest.TestCase):
    def test_pick_hit_prefers_exact_title_and_year(self):
        hits = [{"title": "Dune", "year": 1984}, {"title": "Dune", "year": 2021},
                {"title": "Dune Part Two", "year": 2024}]
        self.assertEqual(ix.pick_hit(hits, "dune", 2021), (hits[1], True))
        self.assertEqual(ix.pick_hit(hits, "dune"), (hits[0], True))

    def test_pick_hit_flags_a_guess(self):
        hits = [{"title": "Lenny Cooke", "year": 2013}]
        self.assertEqual(ix.pick_hit(hits, "lenny cook"), (hits[0], False))

    def test_names_include_original_and_alternates_latin_only(self):
        hit = {"title": "Puella Magi Madoka Magica", "year": 2011,
               "originalTitle": "魔法少女まどか☆マギカ",
               "alternateTitles": [{"title": "Mahou Shoujo Madoka Magica"},
                                   {"title": "Puella Magi Madoka Magica"}, {"title": "Madoka Magica"}]}
        fake = mock.Mock(lookup=mock.Mock(return_value=[hit]))
        with mock.patch.object(ix, "Sonarr", return_value=fake):
            names, yr = ix.names_for("madoka magica", "show")
        self.assertEqual(names, ["madoka magica", "Puella Magi Madoka Magica", "Mahou Shoujo Madoka Magica"])
        self.assertEqual(yr, 2011)

    def test_unrelated_lookup_hit_lends_no_names(self):
        hit = {"title": "La porta magica", "year": 1990, "alternateTitles": [{"title": "Magic Door"}]}
        fake = mock.Mock(lookup=mock.Mock(return_value=[hit]))
        with mock.patch.object(ix, "Sonarr", return_value=fake):
            self.assertEqual(ix.names_for("Madoka Magica", "show"), (["Madoka Magica"], None))

    def test_queries(self):
        self.assertEqual(ix.queries(["Birdman"], "movie", 2014, None), ["Birdman", "Birdman 2014"])
        self.assertEqual(ix.queries(["Veep", "Veep US"], "show", None, 7),
                         ["Veep", "Veep S07", "Veep Season 7", "Veep US", "Veep US S07", "Veep US Season 7"])


class Relevance(unittest.TestCase):
    def test_title_words_required(self):
        names = ["Lenny Cooke"]
        self.assertTrue(ix.relevant(rel("Lenny.Cooke.2013.720p.HDRip"), names, "movie", 2013, None))
        self.assertFalse(ix.relevant(rel("Lenny Kravitz Live 2013"), names, "movie", 2013, None))

    def test_alternate_title_counts(self):
        names = ["Puella Magi Madoka Magica", "Mahou Shoujo Madoka Magica"]
        self.assertTrue(ix.relevant(rel("[Erai] Mahou Shoujo Madoka Magica - S01 1080p"), names, "show", None, 1))

    def test_other_year_namesake_dropped(self):
        names = ["Dune"]
        self.assertFalse(ix.relevant(rel("Dune 1984 1080p BluRay"), names, "movie", 2021, None))
        self.assertTrue(ix.relevant(rel("Dune 2021 1080p"), names, "movie", 2021, None))
        self.assertTrue(ix.relevant(rel("Dune 1080p WEB"), names, "movie", 2021, None))

    def test_season_filter_keeps_packs(self):
        names = ["Veep"]
        self.assertTrue(ix.relevant(rel("Veep S07 1080p"), names, "show", None, 7))
        self.assertTrue(ix.relevant(rel("Veep Season 7 Complete"), names, "show", None, 7))
        self.assertTrue(ix.relevant(rel("Veep Complete Series 1080p"), names, "show", None, 7))
        self.assertFalse(ix.relevant(rel("Veep S03 1080p"), names, "show", None, 7))


class RankAndRender(unittest.TestCase):
    def test_live_and_wanted_resolution_first(self):
        rs = [rel("A 720p", 50), rel("B 1080p", 5), rel("C 1080p", 0), rel("D 1080p", 30)]
        got = [r["title"] for r in sorted(rs, key=lambda r: ix.rank_key(r, "1080p"))]
        self.assertEqual(got, ["D 1080p", "B 1080p", "A 720p", "C 1080p"])

    def test_show_ranking_puts_season_packs_before_movies(self):
        rs = [rel("Madoka Magica the Movie Rebellion 1080p", 170), rel("Madoka Magica S02 1080p", 150),
              rel("Puella Magi Madoka Magica (BD 1080p)", 120)]
        got = [r["title"] for r in sorted(rs, key=lambda r: ix.rank_key(r, "1080p", "show", 1))]
        self.assertEqual(got[0], "Puella Magi Madoka Magica (BD 1080p)")

    def test_dedupe_by_infohash(self):
        rs = [rel("X", ih="ABC"), rel("X again", ih="abc"), rel("Y", ih="def")]
        self.assertEqual([r["title"] for r in ix.dedupe(rs)], ["X", "Y"])

    def test_render_labels_dead_and_ids_resolve(self):
        out = ix.render([rel("Lenny Cooke 720p", 0)], ["Lenny Cooke"])
        self.assertIn("1 releases, 0 with seeders", out)
        self.assertIn("DEAD (0 seeders)", out)
        rid = out.split("[", 1)[1].split("]", 1)[0]
        got = ix.recall(rid)
        assert got is not None
        self.assertEqual(got["title"], "Lenny Cooke 720p")
        self.assertIsNone(ix.recall("i99999"))


class Source(unittest.TestCase):
    def resp(self, status, headers=None, content=b""):
        return mock.Mock(status_code=status, headers=headers or {}, content=content)

    def test_magnet_used_directly(self):
        self.assertEqual(ix.source_for({"magnetUrl": "magnet:?xt=urn:btih:AB"}, None),
                         ("magnet", "magnet:?xt=urn:btih:AB"))

    def test_download_link_redirecting_to_magnet(self):
        f = mock.Mock(return_value=self.resp(301, {"location": "magnet:?xt=urn:btih:CD"}))
        self.assertEqual(ix.source_for({"downloadUrl": "http://p/1"}, f), ("magnet", "magnet:?xt=urn:btih:CD"))

    def test_download_link_serving_torrent_file(self):
        f = mock.Mock(return_value=self.resp(200, content=b"d8:announce..."))
        self.assertEqual(ix.source_for({"downloadUrl": "http://p/1"}, f), ("torrent", b"d8:announce..."))

    def test_magnet_in_guid(self):
        self.assertEqual(ix.source_for({"guid": "magnet:?xt=urn:btih:EF", "downloadUrl": "http://p"}, None),
                         ("magnet", "magnet:?xt=urn:btih:EF"))

    def test_broken_link_falls_back_to_infohash(self):
        f = mock.Mock(return_value=self.resp(500))
        how, uri = ix.source_for({"downloadUrl": "http://p/1", "title": "X 1080p",
                                  "infoHash": "C251069146A2F1A1EF7C71F0C1A78AFF7AB7C185"}, f)
        self.assertEqual(how, "magnet")
        self.assertTrue(uri.startswith("magnet:?xt=urn:btih:C251069146A2F1A1EF7C71F0C1A78AFF7AB7C185&dn=X+1080p&tr="))

    def test_neither_is_an_error(self):
        f = mock.Mock(return_value=self.resp(404))
        with self.assertRaises(ValueError):
            ix.source_for({"downloadUrl": "http://p/1"}, f)


if __name__ == "__main__":
    unittest.main()
