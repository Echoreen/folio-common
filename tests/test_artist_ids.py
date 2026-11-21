"""MusicBrainz IDs decide which artist a song credits: "LiSA (Japanese singer)" in a tag shows as her
page's name, BLACKPINK's LISA and the Japanese singer LiSA stay two artists, and spellings of one
artist become one."""
import unittest
from pathlib import Path

from folio_common import library
from folio_common import nfolib


def track(names, ids=()):
    return nfolib.Track(path=Path("x.opus"), number=1, title="song", artists=list(names), artist_ids=list(ids))


def album(*tracks):
    return nfolib.Album(folder=None, title="a", artist="someone", tracks=list(tracks))


class TestPairing(unittest.TestCase):
    def test_pairs_only_when_they_line_up(self):
        P = nfolib.pair_artist_ids
        self.assertEqual(P(["Stray Kids", "LiSA"], [], ["skz", "lisa-jp"]), (["Stray Kids", "LiSA"], ["skz", "lisa-jp"]))
        self.assertEqual(P(["Stray Kids feat. LiSA"], ["Stray Kids", "LiSA"], ["skz", "lisa-jp"]),
                         (["Stray Kids", "LiSA"], ["skz", "lisa-jp"]))          # the ARTISTS tag wins
        self.assertEqual(P(["A", "B"], [], ["a-id/b-id"]), (["A", "B"], ["a-id", "b-id"]))
        self.assertEqual(P(["A", "B", "C"], [], ["a-id", "b-id"]), (["A", "B", "C"], []))   # unsure: none
        self.assertEqual(P(["A"], [], []), (["A"], []))


class TestCanonicalCredits(unittest.TestCase):
    PAGES = [{"name": "LISA", "mbid": "lisa-bp"},
             {"name": "LiSA", "mbid": "lisa-jp", "disambiguation": "Japanese singer"},
             {"name": "Takase Toya", "mbid": "toya-id", "alias_mbids": ["old-id"]}]

    def run_it(self, *tracks, pages=None):
        library.canonical_credits([album(*tracks)], self.PAGES if pages is None else pages, [])
        return [t.artists for t in tracks]

    def test_the_page_name_replaces_the_tag(self):
        self.assertEqual(self.run_it(track(["Stray Kids", "LiSA (Japanese singer)"], ["skz", "lisa-jp"])),
                         [["Stray Kids", "LiSA"]])

    def test_two_lisas_stay_two(self):
        out = self.run_it(track(["LISA"], ["lisa-bp"]), track(["LiSA"], ["lisa-jp"]))
        self.assertEqual(out, [["LISA"], ["LiSA"]])

    def test_same_spelling_for_two_artists_is_told_apart(self):
        pages = [{"name": "LISA", "mbid": "lisa-bp"},
                 {"name": "LISA", "mbid": "lisa-jp", "disambiguation": "Japanese singer"}]
        out = self.run_it(track(["LISA"], ["lisa-bp"]), track(["LISA"], ["lisa-jp"]), pages=pages)
        self.assertEqual(sorted(x[0] for x in out), ["LISA (Japanese singer)", "LISA (lisa-bp)"])

    def test_one_artist_one_spelling(self):
        out = self.run_it(track(["Lady Gaga"]), track(["lady gaga"]), track(["Lady Gaga"]))
        self.assertEqual(out, [["Lady Gaga"]] * 3)                                # no IDs, capitals only
        out = self.run_it(track(["Takasetoya"], ["old-id"]))
        self.assertEqual(out, [["Takase Toya"]])                                  # an ID from "Same artist as…"


if __name__ == "__main__":
    unittest.main()
