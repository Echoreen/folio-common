"""tvshow.nfo, season.nfo, episode NFOs and movie NFOs (Kodi format, as written by
Sonarr, Radarr, Jellyfin, tinyMediaManager and Kodi itself)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .core import (BOOL, FLOAT, INT, LIST, TEXT, Actor, NfoBase, Rating, _replace_block, _split_roots,
                   _sub, read_actors, read_ids, read_ratings, read_text, write_actors, write_elements,
                   write_ids, write_ratings)


@dataclass
class Related:
    """Another show or movie linked to this one (a Folio tag; other players ignore it).

    From AniList:  <related anilist="128546" relation="SEQUEL" format="MOVIE" year="2025">Title</related>
    Made by hand:  <related kind="movie" tmdb="1218925" manual="true">Title</related>"""
    title: str
    anilist: str = ""
    tmdb: str = ""
    kind: str = ""              # "show" / "movie" (links made by hand; AniList ones go by format)
    relation: str = ""          # AniList's word: SEQUEL, PREQUEL, SIDE_STORY, SPIN_OFF, PARENT, ALTERNATIVE...
    format: str = ""            # AniList's format: TV, MOVIE, OVA, ONA, SPECIAL...
    year: str = ""
    manual: bool = False
    cover: str = ""             # web address of a poster, for showing it when you don't have it

    _ATTRS = ("anilist", "tmdb", "kind", "relation", "format", "year", "cover")

    @classmethod
    def from_element(cls, e: ET.Element) -> "Related":
        a = {k: (e.get(k) or "").strip() for k in cls._ATTRS}
        return cls((e.text or "").strip(), manual=(e.get("manual") or "").lower() in ("true", "1", "yes"), **a)

    def element(self) -> ET.Element:
        e = ET.Element("related", {a: getattr(self, a) for a in self._ATTRS if getattr(self, a)})
        if self.manual:
            e.set("manual", "true")
        e.text = self.title
        return e

    def key(self) -> tuple:
        return (self.anilist, self.kind, self.tmdb)


@dataclass
class KnownEpisode:
    """An episode TMDB lists for a show, whether or not you have it (Folio greys out the ones you don't):
    <knownepisode season="1" episode="5" aired="2022-11-08">Title</knownepisode>"""
    season: int
    episode: int
    title: str = ""
    aired: str = ""


@dataclass
class _VideoBase(NfoBase):
    ids: dict[str, str] = field(default_factory=dict)       # {"tmdb": "123", "imdb": "tt..."}
    default_id: str = ""
    ratings: dict[str, Rating] = field(default_factory=dict)
    actors: list[Actor] = field(default_factory=list)
    related: list[Related] = field(default_factory=list)

    def _read_extra(self, el):
        self.ids, self.default_id = read_ids(el)
        self.ratings = read_ratings(el)
        self.actors = read_actors(el)
        self.related = [Related.from_element(r) for r in el.findall("related") if (r.text or "").strip()]

    def _write_extra(self, el):
        write_ids(el, self.ids, self.default_id)
        write_ratings(el, self.ratings)
        write_actors(el, self.actors)
        old = [Related.from_element(r) for r in el.findall("related") if (r.text or "").strip()]
        if old != self.related:
            _replace_block(el, "related", [r.element() for r in self.related])

    @property
    def rating(self) -> Rating | None:
        """The default rating, or the first one."""
        for r in self.ratings.values():
            if r.default:
                return r
        return next(iter(self.ratings.values()), None)


@dataclass
class TvShowNfo(_VideoBase):
    ROOT = "tvshow"
    FIELDS = {
        "title": ("title", TEXT), "original_title": ("originaltitle", TEXT),
        "sort_title": ("sorttitle", TEXT), "plot": ("plot", TEXT), "outline": ("outline", TEXT),
        "tagline": ("tagline", TEXT), "mpaa": ("mpaa", TEXT), "premiered": ("premiered", TEXT),
        "year": ("year", INT), "status": ("status", TEXT), "runtime": ("runtime", INT),
        "studios": ("studio", LIST), "genres": ("genre", LIST), "tags": ("tag", LIST),
        "countries": ("country", LIST), "critic_rating": ("criticrating", FLOAT),
        "date_added": ("dateadded", TEXT), "mal_rank": ("malrank", INT),
    }
    title: str = ""
    original_title: str = ""
    sort_title: str = ""
    plot: str = ""
    outline: str = ""
    tagline: str = ""
    mpaa: str = ""
    premiered: str = ""
    year: int | None = None
    status: str = ""
    runtime: int | None = None
    studios: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    critic_rating: float | None = None
    date_added: str = ""
    mal_rank: int | None = None         # MyAnimeList rank (#87); Folio shows it next to the MAL score
    named_seasons: dict[int, str] = field(default_factory=dict)   # <namedseason number="1">Name</namedseason>
    known_episodes: list[KnownEpisode] = field(default_factory=list)   # every aired episode TMDB lists

    def _read_extra(self, el):
        super()._read_extra(el)
        self.known_episodes = []
        for k in el.findall("knownepisode"):
            try:
                self.known_episodes.append(KnownEpisode(int(k.get("season", "")), int(k.get("episode", "")),
                                                        (k.text or "").strip(), (k.get("aired") or "").strip()))
            except ValueError:
                continue
        self.named_seasons = {}
        for ns in el.findall("namedseason"):
            try:
                n = int(ns.get("number", ""))
            except ValueError:
                continue
            if (ns.text or "").strip():
                self.named_seasons[n] = ns.text.strip()

    def _write_extra(self, el):
        super()._write_extra(el)
        old = {int(n.get("number")): (n.text or "").strip() for n in el.findall("namedseason")
               if (n.get("number") or "").lstrip("-").isdigit()}
        if old != self.named_seasons:
            new = []
            for n, name in sorted(self.named_seasons.items()):
                e = ET.Element("namedseason", {"number": str(n)})
                e.text = name
                new.append(e)
            _replace_block(el, "namedseason", new)
        old_known = []
        for k in el.findall("knownepisode"):
            try:
                old_known.append(KnownEpisode(int(k.get("season", "")), int(k.get("episode", "")),
                                              (k.text or "").strip(), (k.get("aired") or "").strip()))
            except ValueError:
                pass
        if old_known != self.known_episodes:
            new = []
            for k in self.known_episodes:
                e = ET.Element("knownepisode", {"season": str(k.season), "episode": str(k.episode),
                                                **({"aired": k.aired} if k.aired else {})})
                e.text = k.title
                new.append(e)
            _replace_block(el, "knownepisode", new)


@dataclass
class SeasonNfo(_VideoBase):
    ROOT = "season"
    FIELDS = {
        "season_number": ("seasonnumber", INT), "title": ("title", TEXT), "plot": ("plot", TEXT),
        "outline": ("outline", TEXT), "premiered": ("premiered", TEXT), "year": ("year", INT),
        "date_added": ("dateadded", TEXT),
    }
    season_number: int | None = None
    title: str = ""
    plot: str = ""
    outline: str = ""
    premiered: str = ""
    year: int | None = None
    date_added: str = ""


@dataclass
class EpisodeNfo(_VideoBase):
    ROOT = "episodedetails"
    FIELDS = {
        "title": ("title", TEXT), "show_title": ("showtitle", TEXT), "season": ("season", INT),
        "episode": ("episode", INT), "display_season": ("displayseason", INT),
        "display_episode": ("displayepisode", INT), "plot": ("plot", TEXT),
        "outline": ("outline", TEXT), "aired": ("aired", TEXT), "runtime": ("runtime", INT),
        "mpaa": ("mpaa", TEXT), "writers": ("credits", LIST), "directors": ("director", LIST),
        "studios": ("studio", LIST), "user_rating": ("userrating", FLOAT),
        "watched": ("watched", BOOL), "play_count": ("playcount", INT), "date_added": ("dateadded", TEXT),
    }
    title: str = ""
    show_title: str = ""
    season: int | None = None
    episode: int | None = None
    display_season: int | None = None
    display_episode: int | None = None
    plot: str = ""
    outline: str = ""
    aired: str = ""
    runtime: int | None = None
    mpaa: str = ""
    writers: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    user_rating: float | None = None
    watched: bool = False
    play_count: int | None = None
    date_added: str = ""


def read_episodes(path: str | Path) -> list[EpisodeNfo]:
    """An episode NFO can hold several <episodedetails> (multi-episode files like S01E01-E02)."""
    path = Path(path)
    roots, _tag, trailing = _split_roots(read_text(path))
    return [EpisodeNfo.from_element(r, path, trailing) for r in roots]


def write_episodes(episodes: list[EpisodeNfo], path: str | Path | None = None) -> Path:
    if not episodes:
        raise ValueError("no episodes to write")
    path = Path(path or episodes[0].path)
    write_elements(path, [e.to_element() for e in episodes], episodes[0].trailing)
    for e in episodes:
        e.path = path
    return path


@dataclass
class MovieNfo(_VideoBase):
    ROOT = "movie"
    FIELDS = {
        "title": ("title", TEXT), "original_title": ("originaltitle", TEXT),
        "sort_title": ("sorttitle", TEXT), "plot": ("plot", TEXT), "outline": ("outline", TEXT),
        "tagline": ("tagline", TEXT), "mpaa": ("mpaa", TEXT), "premiered": ("premiered", TEXT),
        "year": ("year", INT), "runtime": ("runtime", INT), "studios": ("studio", LIST),
        "genres": ("genre", LIST), "tags": ("tag", LIST), "countries": ("country", LIST),
        "writers": ("credits", LIST), "directors": ("director", LIST), "trailer": ("trailer", TEXT),
        "critic_rating": ("criticrating", FLOAT), "user_rating": ("userrating", FLOAT),
        "watched": ("watched", BOOL), "play_count": ("playcount", INT), "date_added": ("dateadded", TEXT),
        "mal_rank": ("malrank", INT),
    }
    title: str = ""
    original_title: str = ""
    sort_title: str = ""
    plot: str = ""
    outline: str = ""
    tagline: str = ""
    mpaa: str = ""
    premiered: str = ""
    year: int | None = None
    runtime: int | None = None
    studios: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    writers: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    trailer: str = ""
    critic_rating: float | None = None
    user_rating: float | None = None
    watched: bool = False
    play_count: int | None = None
    date_added: str = ""
    mal_rank: int | None = None
    collection: str = ""            # <set><name>…</name></set>  (or old-style <set>Name</set>)
    collection_overview: str = ""
    studio_logos: dict[str, str] = field(default_factory=dict)   # <studiologo name="X">web address</studiologo>

    def _read_extra(self, el):
        super()._read_extra(el)
        self.studio_logos = {(e.get("name") or "").strip(): (e.text or "").strip() for e in el.findall("studiologo")
                             if (e.get("name") or "").strip() and (e.text or "").strip()}
        s = el.find("set")
        if s is not None:
            name = (s.findtext("name") or "").strip()
            self.collection = name or (s.text or "").strip()
            self.collection_overview = (s.findtext("overview") or "").strip()

    def _write_extra(self, el):
        super()._write_extra(el)
        old_logos = {(e.get("name") or "").strip(): (e.text or "").strip() for e in el.findall("studiologo")}
        if old_logos != self.studio_logos:
            new_logos = []
            for name, url in self.studio_logos.items():
                e = ET.Element("studiologo", {"name": name})
                e.text = url
                new_logos.append(e)
            _replace_block(el, "studiologo", new_logos)
        old = el.find("set")
        old_name = ((old.findtext("name") or old.text or "").strip()) if old is not None else ""
        old_ov = (old.findtext("overview") or "").strip() if old is not None else ""
        if (old_name, old_ov) == (self.collection, self.collection_overview):
            return
        new = []
        if self.collection:
            s = ET.Element("set")
            _sub(s, "name", self.collection)
            if self.collection_overview:
                _sub(s, "overview", self.collection_overview)
            new.append(s)
        _replace_block(el, "set", new)
