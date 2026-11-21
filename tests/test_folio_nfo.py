import shutil
import tempfile
import unittest
from pathlib import Path

import folio_nfo as fn

SONARR_TVSHOW = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow>
  <title>Severance</title>
  <rating>8.7</rating>
  <year>2022</year>
  <plot>Mark leads a team of office workers.</plot>
  <mpaa>TV-MA</mpaa>
  <id>371980</id>
  <uniqueid type="tvdb" default="true">371980</uniqueid>
  <uniqueid type="imdb">tt11280740</uniqueid>
  <uniqueid type="tmdb">95396</uniqueid>
  <genre>Drama</genre>
  <genre>Mystery</genre>
  <premiered>2022-02-18</premiered>
  <studio>Apple TV+</studio>
  <actor><name>Adam Scott</name><role>Mark Scout</role><thumb>https://x/a.jpg</thumb></actor>
  <actor><name>Britt Lower</name><role>Helly R.</role></actor>
  <somethingSonarrAdds attr="1">keep me</somethingSonarrAdds>
</tvshow>
https://www.thetvdb.com/?tab=series&id=371980
"""

SONARR_EPISODE_MULTI = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <title>Pilot (1)</title><season>1</season><episode>1</episode>
  <aired>2020-01-01</aired><plot>Part one.</plot>
  <uniqueid type="tvdb" default="true">111</uniqueid>
  <watched>false</watched>
</episodedetails>
<episodedetails>
  <title>Pilot (2)</title><season>1</season><episode>2</episode>
  <aired>2020-01-01</aired><plot>Part two.</plot>
  <uniqueid type="tvdb" default="true">112</uniqueid>
</episodedetails>
"""

RADARR_MOVIE = """﻿<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>Heat</title>
  <originaltitle>Heat</originaltitle>
  <sorttitle>Heat</sorttitle>
  <ratings>
    <rating name="themoviedb" max="10" default="true"><value>7.9</value><votes>7000</votes></rating>
    <rating name="imdb" max="10"><value>8.3</value></rating>
  </ratings>
  <userrating />
  <plot>A group of high-end professional thieves...</plot>
  <runtime>170</runtime>
  <mpaa>R</mpaa>
  <id>tt0113277</id>
  <tmdbid>949</tmdbid>
  <uniqueid type="tmdb" default="true">949</uniqueid>
  <uniqueid type="imdb">tt0113277</uniqueid>
  <genre>Crime</genre>
  <genre>Thriller</genre>
  <country>United States of America</country>
  <set><name>Some Collection</name><overview>Films.</overview></set>
  <credits>Michael Mann</credits>
  <director>Michael Mann</director>
  <premiered>1995-12-15</premiered>
  <year>1995</year>
  <studio>Warner Bros. Pictures</studio>
  <fileinfo><streamdetails><video><codec>h264</codec></video></streamdetails></fileinfo>
  <actor><name>Al Pacino</name><role>Vincent Hanna</role><order>0</order></actor>
</movie>
"""

KODI_ALBUM = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<album>
  <title>Amigos</title>
  <musicbrainzalbumid>abc</musicbrainzalbumid>
  <artist>BIBI</artist>
  <artist>Becky G</artist>
  <genre>K-Pop / Pop</genre>
  <style>Playful</style>
  <rating max="10">7.5</rating>
  <albumArtistCredits><artist>BIBI</artist><musicBrainzArtistID>21C93D2D-DC10-4F8F-AE91-7285EFF37C2F</musicBrainzArtistID></albumArtistCredits>
  <albumArtistCredits><artist>Becky G</artist><musicBrainzArtistID>eb90c8b8-d733-4c14-a293-105c3d1ba9b3</musicBrainzArtistID></albumArtistCredits>
  <track><position>1</position><title>Amigos</title></track>
</album>
"""

KODI_ARTIST = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<artist>
  <name>Spiritbox</name>
  <musicBrainzArtistID>c87a5e0b-0b26-45a7-a9d8-4d5f4ec2f1f6</musicBrainzArtistID>
  <genre>Metalcore</genre><genre>Progressive Metal</genre>
  <formed>2017</formed>
  <biography>Canadian band.</biography>
  <thumb aspect="thumb">https://x/thumb.jpg</thumb>
</artist>
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir)

    def put(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class TestShows(Base):
    def test_sonarr_tvshow_with_trailing_url(self):
        p = self.put("Severance/tvshow.nfo", SONARR_TVSHOW)
        s = fn.TvShowNfo.read(p)
        self.assertEqual(s.title, "Severance")
        self.assertEqual(s.year, 2022)
        self.assertEqual(s.genres, ["Drama", "Mystery"])
        self.assertEqual(s.ids, {"tvdb": "371980", "imdb": "tt11280740", "tmdb": "95396"})
        self.assertEqual(s.default_id, "tvdb")
        self.assertEqual(s.rating.value, 8.7)
        self.assertEqual([a.name for a in s.actors], ["Adam Scott", "Britt Lower"])
        self.assertIn("thetvdb.com", s.trailing)

    def test_tvshow_edit_keeps_unknown_tags_and_url(self):
        p = self.put("Severance/tvshow.nfo", SONARR_TVSHOW)
        s = fn.TvShowNfo.read(p)
        s.plot = "Edited plot & more <tags>"
        s.genres.append("Sci-Fi")
        s.named_seasons[1] = "Season One"
        s.locked_fields.add("plot")
        s.write()
        out = p.read_text()
        self.assertIn('<somethingSonarrAdds attr="1">keep me</somethingSonarrAdds>', out)
        self.assertTrue(out.rstrip().endswith("id=371980"))
        again = fn.TvShowNfo.read(p)
        self.assertEqual(again.plot, "Edited plot & more <tags>")
        self.assertEqual(again.genres, ["Drama", "Mystery", "Sci-Fi"])
        self.assertEqual(again.named_seasons, {1: "Season One"})
        self.assertEqual(again.locked_fields, {"plot"})
        self.assertEqual(again.actors[0].thumb, "https://x/a.jpg")
        # untouched fields stay where they were: <title> still first
        self.assertLess(out.index("<title>"), out.index("<plot>"))

    def test_multi_episode_file(self):
        p = self.put("Show/Season 01/Show - S01E01-E02.nfo", SONARR_EPISODE_MULTI)
        eps = fn.load(p)
        self.assertEqual([(e.season, e.episode, e.title) for e in eps],
                         [(1, 1, "Pilot (1)"), (1, 2, "Pilot (2)")])
        eps[1].title = "Pilot, Part 2"
        fn.write_episodes(eps)
        again = fn.read_episodes(p)
        self.assertEqual(len(again), 2)
        self.assertEqual(again[1].title, "Pilot, Part 2")
        self.assertEqual(again[0].ids, {"tvdb": "111"})

    def test_new_season_nfo_from_scratch(self):
        p = self.dir / "Show/Season 01/season.nfo"
        s = fn.SeasonNfo(season_number=1, title="The Beginning", plot="Where it starts.")
        s.write(p)
        again = fn.load(p)
        self.assertIsInstance(again, fn.SeasonNfo)
        self.assertEqual((again.season_number, again.title), (1, "The Beginning"))
        self.assertTrue(p.read_text().startswith('<?xml version="1.0" encoding="UTF-8"'))

    def test_artwork_lookup(self):
        show = self.dir / "Show"
        for f in ("poster.jpg", "fanart.jpg", "season01-poster.jpg", "season-specials-poster.jpg",
                  "Season 01/Show - S01E01-thumb.jpg"):
            self.put(f"Show/{f}", "x")
        self.assertEqual(set(fn.show_art(show)), {"poster", "fanart"})
        self.assertEqual(fn.season_art(show, 1)["poster"].name, "season01-poster.jpg")
        self.assertEqual(fn.season_art(show, 0)["poster"].name, "season-specials-poster.jpg")
        self.assertEqual(fn.episode_thumb(show / "Season 01/Show - S01E01.mkv").name, "Show - S01E01-thumb.jpg")


class TestMovies(Base):
    def test_radarr_movie(self):
        p = self.put("Heat (1995)/Heat (1995).nfo", RADARR_MOVIE)
        m = fn.load(p)
        self.assertIsInstance(m, fn.MovieNfo)
        self.assertEqual((m.title, m.year, m.runtime), ("Heat", 1995, 170))
        self.assertEqual(m.ids, {"tmdb": "949", "imdb": "tt0113277"})
        self.assertEqual(m.rating.value, 7.9)
        self.assertEqual(m.ratings["imdb"].value, 8.3)
        self.assertEqual(m.ratings["themoviedb"].votes, 7000)
        self.assertEqual(m.collection, "Some Collection")
        self.assertEqual(m.directors, ["Michael Mann"])

    def test_movie_edit_keeps_fileinfo_and_syncs_legacy_ids(self):
        p = self.put("Heat (1995)/Heat (1995).nfo", RADARR_MOVIE)
        m = fn.MovieNfo.read(p)
        m.ids["tmdb"] = "950"
        m.ratings["rottentomatoes"] = fn.Rating(87, max=100)
        m.collection = ""
        m.write()
        out = p.read_text()
        self.assertIn("<codec>h264</codec>", out)
        self.assertIn("<tmdbid>950</tmdbid>", out)
        self.assertNotIn("<set>", out)
        again = fn.MovieNfo.read(p)
        self.assertEqual(again.ids["tmdb"], "950")
        self.assertEqual(again.ratings["rottentomatoes"].value, 87)
        self.assertEqual(again.ratings["rottentomatoes"].max, 100)

    def test_movie_nfo_path_preference(self):
        v = self.dir / "Heat (1995)/Heat (1995).mkv"
        self.assertEqual(fn.movie_nfo_path(v).name, "Heat (1995).nfo")
        self.assertEqual(fn.movie_nfo_path(v, prefer_movie_nfo=True).name, "movie.nfo")
        self.put("Heat (1995)/movie.nfo", RADARR_MOVIE)
        self.assertEqual(fn.movie_nfo_path(v).name, "movie.nfo")   # an existing file always wins

    def test_noop_write_changes_nothing_meaningful(self):
        p = self.put("Heat (1995)/movie.nfo", RADARR_MOVIE)
        before = fn.MovieNfo.read(p)
        before.write()
        after = fn.MovieNfo.read(p)
        for f in ("title", "plot", "genres", "ids", "ratings", "actors", "collection", "year"):
            self.assertEqual(getattr(before, f), getattr(after, f), f)


class TestMusic(Base):
    def test_album(self):
        p = self.put("BIBI/Amigos/album.nfo", KODI_ALBUM)
        a = fn.load(p)
        self.assertEqual(a.artists, ["BIBI", "Becky G"])
        self.assertEqual(a.genres, ["K-Pop", "Pop"])
        self.assertEqual([c.mbid[:8] for c in a.credits], ["21c93d2d", "eb90c8b8"])
        self.assertEqual(a.ratings["default"].value, 7.5)

    def test_album_edit_keeps_tracks_and_legacy_rating(self):
        p = self.put("BIBI/Amigos/album.nfo", KODI_ALBUM)
        a = fn.AlbumNfo.read(p)
        a.moods = ["Bright"]
        a.ratings["default"].value = 8
        a.write()
        out = p.read_text()
        self.assertIn("<track>", out)
        self.assertIn('<rating max="10">8</rating>', out)
        self.assertNotIn("<ratings>", out)
        self.assertEqual(fn.AlbumNfo.read(p).moods, ["Bright"])

    def test_artist(self):
        p = self.put("Spiritbox/artist.nfo", KODI_ARTIST)
        a = fn.ArtistNfo.read(p)
        self.assertEqual(a.name, "Spiritbox")
        self.assertTrue(a.mbid.startswith("c87a5e0b"))   # tag match is case-insensitive
        self.assertEqual(a.genres, ["Metalcore", "Progressive Metal"])
        a.biography = "Canadian metal band from Victoria, BC."
        a.write()
        out = p.read_text()
        self.assertIn('<thumb aspect="thumb">', out)
        self.assertEqual(fn.ArtistNfo.read(p).biography, "Canadian metal band from Victoria, BC.")


class TestErrors(Base):
    def test_wrong_kind_and_garbage(self):
        p = self.put("x/movie.nfo", RADARR_MOVIE)
        with self.assertRaises(fn.NfoError):
            fn.TvShowNfo.read(p)
        g = self.put("x/scene.nfo", "  ▄▄▄ RELEASE GROUP ▄▄▄\n Artist....: Foo\n")
        with self.assertRaises(fn.NfoError):
            fn.load(g)


if __name__ == "__main__":
    unittest.main()
