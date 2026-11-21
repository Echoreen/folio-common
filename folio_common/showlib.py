"""TV show libraries for Folio: scanning, Kodi tvshow.nfo / episode NFOs, seasons, artwork,
technical info and watch state. No Qt.

Layout (the one Kodi, Jellyfin, Sonarr and tinyMediaManager use):

    Shows/
      Show Name (Year)/
        tvshow.nfo                  the show; NFO values win over everything else
        poster.jpg  fanart.jpg  banner.jpg  clearlogo.png  landscape.jpg
        season01-poster.jpg  season-specials-poster.jpg
        Season 01/                  or "Season 1", "S01"; "Specials" is season 0
          Show - S01E01 - Title.mkv
          Show - S01E01 - Title.nfo       <episodedetails>
          Show - S01E01 - Title-thumb.jpg
          season.nfo  poster.jpg          optional: season name/plot and poster

Episodes can also sit straight in the show folder. Numbers come from the episode NFO, else the
file name: S01E02, s1e2, S01E02E03 / S01E02-E03 (two in one file), 1x02, and anime-style
"[Group] Show - 12 [1080p]" (season from the folder, else 1).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import folio_nfo as fn
from folio_common import library
from folio_common import videolib
from folio_common.videolib import IMAGE_EXTS, Cancelled, Person, _num, _text, _texts, probe

SCHEMA_VERSION = 1
NFO_PARSER_VERSION = "5." + library.code_fingerprint(__file__, videolib.__file__)   # changes with the reading code
EXTRA_DIRS = videolib.EXTRA_DIRS - {"specials"}
SPECIALS = {"specials", "special", "season 0", "season 00", "extras specials"}


# ---------------------------------------------------------------- model

@dataclass
class Episode:
    video: Path
    show_folder: str
    season: int
    episode: int | None
    episodes: list[int] = field(default_factory=list)     # more than one: a double episode
    title: str = ""
    plot: str = ""
    aired: str = ""
    runtime: int | None = None                            # minutes
    rating: float | None = None
    directors: list[str] = field(default_factory=list)
    writers: list[str] = field(default_factory=list)
    ids: dict = field(default_factory=dict)
    thumb: Path | None = None
    info: dict = field(default_factory=dict)
    size: int = 0
    added: float = 0.0
    has_nfo: bool = False
    position: float = 0.0
    duration: float = 0.0
    watched: bool = False
    play_count: int = 0
    last_played: float = 0.0

    @property
    def key(self) -> str:
        return str(self.video)

    @property
    def progress(self) -> float:
        if self.watched or not self.duration or self.position < 60:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))

    @property
    def versions(self) -> list[Path]:
        return [self.video]

    @property
    def code(self) -> str:
        """S01E02, S01E02–E03, or "Special 3"."""
        if self.episode is None:
            return f"S{self.season:02d}" if self.season else "Special"
        if self.season == 0:
            return f"Special {self.episode}"
        tail = f"–E{self.episodes[-1]:02d}" if len(self.episodes) > 1 else ""
        return f"S{self.season:02d}E{self.episode:02d}{tail}"

    @property
    def label(self) -> str:
        return self.title or (f"Episode {self.episode}" if self.episode is not None else self.video.stem)


@dataclass
class Season:
    number: int
    name: str = ""
    plot: str = ""
    poster: Path | None = None
    episodes: list[Episode] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.name or ("Specials" if self.number == 0 else f"Season {self.number}")


@dataclass
class Show:
    folder: Path
    title: str
    year: str = ""
    original_title: str = ""
    sort_title: str = ""
    premiered: str = ""
    status: str = ""
    plot: str = ""
    outline: str = ""
    tagline: str = ""
    mpaa: str = ""
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    cast: list[Person] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    rating: float | None = None
    rating_max: float = 10
    ratings: dict = field(default_factory=dict)
    ids: dict = field(default_factory=dict)
    related: list = field(default_factory=list)   # [{"title", "anilist", "tmdb", "kind", "relation", ...}]
    known: list = field(default_factory=list)     # every aired episode TMDB lists: [season, episode, title, aired]
    has_nfo: bool = False
    poster: Path | None = None
    fanart: Path | None = None
    banner: Path | None = None
    logo: Path | None = None
    landscape: Path | None = None
    seasons: list[Season] = field(default_factory=list)

    @property
    def key(self) -> str:
        return str(self.folder)

    @property
    def episodes(self) -> list[Episode]:
        """Every episode in watching order: season 1 onwards, specials last."""
        regular = [e for s in self.seasons if s.number > 0 for e in s.episodes]
        return regular + [e for s in self.seasons if s.number == 0 for e in s.episodes]

    @property
    def regular(self) -> list[Episode]:
        eps = [e for s in self.seasons if s.number > 0 for e in s.episodes]
        return eps or self.episodes

    @property
    def added(self) -> float:
        return max((e.added for e in self.episodes), default=0.0)

    @property
    def last_played(self) -> float:
        return max((e.last_played for e in self.episodes), default=0.0)

    @property
    def watched_count(self) -> int:
        return sum(1 for e in self.regular if e.watched)

    @property
    def watched(self) -> bool:
        eps = self.regular
        return bool(eps) and all(e.watched for e in eps)

    @property
    def started(self) -> bool:
        return any(e.watched or e.progress > 0 for e in self.episodes)

    @property
    def progress(self) -> float:
        """Share of episodes watched (for the bar under a poster); 0 when not started or done."""
        eps = self.regular
        if not eps or self.watched or not self.started:
            return 0.0
        return max(0.02, self.watched_count / len(eps))

    def next_episode(self) -> Episode | None:
        """What Play / Next Up means: an episode you stopped partway through, else the one after
        the last you watched, else the first you haven't seen."""
        eps = self.regular
        if not eps:
            return None
        going = [e for e in self.episodes if e.progress > 0]
        if going:
            return max(going, key=lambda e: e.last_played)
        watched = [i for i, e in enumerate(eps) if e.watched]
        if watched:
            latest = max(watched, key=lambda i: (eps[i].last_played, i))
            for e in eps[latest + 1:]:
                if not e.watched:
                    return e
        return next((e for e in eps if not e.watched), eps[0])

    def season(self, number: int) -> Season | None:
        return next((s for s in self.seasons if s.number == number), None)

    def year_span(self) -> str:
        years = sorted({e.aired[:4] for e in self.episodes if e.aired[:4].isdigit()})
        first = self.year or (years[0] if years else "")
        last = years[-1] if years else ""
        if self.status.lower() in ("continuing", "returning series", "airing", "ongoing") and first:
            return f"{first}–"
        return f"{first}–{last}" if first and last and last > first else first


# ---------------------------------------------------------------- names

_SXE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})[ ._-]*e(\d{1,4})((?:[ ._-]*-?[ ._-]*e\d{1,4})*)")
_NXN = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![0-9])")
_SEASON_DIR = re.compile(r"(?i)^(?:season|series|s)[ ._-]*(\d{1,3})\b")
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_JUNK = re.compile(r"(?i)\b(\d{3,4}p|4k|uhd|hdr\d*|x26[45]|h\.?26[45]|hevc|av1|10bit|8bit|bd|bluray|web[- ]?dl|"
                   r"webrip|dual[- ]audio|aac\d?(\.\d)?|flac|opus|ddp?\d(\.\d)?)\b")
_ABS = re.compile(r"(?i)(?:^|[ ._-])(?:ep(?:isode)?[ ._]*)?(\d{1,4})(?:v\d)?(?:[ ._-]|$)")


def season_from_dir(name: str) -> int | None:
    low = name.strip().lower()
    if low in SPECIALS:
        return 0
    m = _SEASON_DIR.match(low)
    return int(m.group(1)) if m else None


_SXE_ABS = re.compile(r"(?i)(?<![a-z0-9])s\d{1,3}[ ._-]*e\d{1,4}\s*[(\[](\d{1,4})[)\]]")


def absolute_number(filename: str) -> int | None:
    """The absolute episode number some groups add after SxxEyy: "Naruto - S03E01 (084)" -> 84.
    Groups disagree about where seasons start (Judas' season 3 starts at 84, TMDB's at 101), so when a
    file gives both, this number is the one to trust."""
    m = _SXE_ABS.search(os.path.splitext(filename)[0])
    return int(m.group(1)) if m else None


def parse_episode(filename: str, folder_season: int | None = None) -> tuple[int | None, list[int], str]:
    """File name -> (season, [episode numbers], title guess). Season None when the name doesn't say."""
    stem = os.path.splitext(filename)[0]
    m = _SXE.search(stem)
    if m:
        season = int(m.group(1))
        eps = [int(m.group(2))] + [int(x) for x in re.findall(r"(?i)e(\d{1,4})", m.group(3) or "")]
        title = stem[m.end():]
    else:
        m = _NXN.search(stem)
        if m:
            season, eps, title = int(m.group(1)), [int(m.group(2))], stem[m.end():]
        else:
            # anime style: "[Group] Show - 12 [1080p]" / "Show Episode 12" / "Show 12v2"
            clean = _JUNK.sub(" ", _BRACKETS.sub(" ", stem))
            dash = re.search(r"\s-\s*(\d{1,4})(?:v\d)?(?:\s|$|-)", clean)
            if dash:
                eps, title = [int(dash.group(1))], clean[dash.end():]
            else:
                nums = [x for x in _ABS.finditer(clean) if not re.fullmatch(r"(19|20)\d\d", x.group(1))]
                if not nums:
                    return folder_season, [], ""
                last = nums[-1]
                eps, title = [int(last.group(1))], clean[last.end():]
            season = folder_season if folder_season is not None else None
    title = re.sub(r"[._]+", " ", _JUNK.sub(" ", _BRACKETS.sub(" ", title))).strip(" -–")
    return season, eps, re.sub(r"\s{2,}", " ", title)


def parse_show_name(name: str) -> tuple[str, str]:
    return videolib.parse_name(_BRACKETS.sub(" ", name).strip() if not re.search(r"\((19|20)\d\d\)", name)
                               else name)


# ---------------------------------------------------------------- NFO

def parse_show_nfo(path: Path) -> dict | None:
    try:
        s = fn.TvShowNfo.read(path)
    except (fn.NfoError, OSError):
        return None
    rating, rmax, ratings = videolib.ratings_from(s)
    premiered = s.premiered or s.text("aired")
    return {
        "title": s.title, "original_title": s.original_title,
        "sort_title": s.sort_title, "year": (s.text("year") or premiered)[:4],
        "premiered": premiered, "status": s.status, "plot": s.plot,
        "outline": s.outline, "tagline": s.tagline, "mpaa": videolib.clean_mpaa(s.mpaa),
        "genres": s.genres, "studios": s.studios, "countries": s.countries,
        "tags": s.tags, "cast": videolib.cast_from(s), "rating": rating, "rating_max": rmax, "ratings": ratings,
        "ids": dict(s.ids), "named_seasons": {str(n): name for n, name in s.named_seasons.items()},
        "known": [[k.season, k.episode, k.title, k.aired] for k in s.known_episodes],
        "related": [{k: getattr(r, k) for k in ("title", "anilist", "tmdb", "kind", "relation", "format", "year", "manual", "cover")}
                    for r in s.related],
    }


def parse_episode_nfo(path: Path) -> dict | None:
    """<episodedetails> (several of them in one file for a double episode)."""
    try:
        items = fn.read_episodes(path)
    except (fn.NfoError, OSError):
        return None
    if not items:
        return None
    first = items[0]
    eps = [e.episode for e in items if e.episode is not None]
    thumb = first.text("thumb")
    return {
        "title": " / ".join(e.title for e in items if e.title) if len(items) > 1 else first.title,
        "season": first.season, "episodes": eps,
        "plot": "\n\n".join(e.plot for e in items if e.plot),
        "aired": first.aired or first.text("premiered"),
        "runtime": first.runtime, "rating": videolib.ratings_from(first)[0],
        "directors": first.directors, "writers": first.writers or _texts(first.element, "writer"),
        "ids": dict(first.ids), "watched": first.watched or (first.play_count or 0) > 0,
        "thumb": thumb if thumb and not thumb.startswith(("http://", "https://")) else "",
    }


def parse_season_nfo(path: Path) -> dict | None:
    try:
        s = fn.SeasonNfo.read(path)
    except (fn.NfoError, OSError):
        return None
    return {"title": s.title, "plot": s.plot, "season": s.season_number}


# ---------------------------------------------------------------- cache

def db_path_for(root) -> Path:
    key = hashlib.sha1(str(Path(root).expanduser().absolute()).encode()).hexdigest()[:12]
    return library.cache_dir() / f"shows-{key}.sqlite"


def connect(db_file) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_file), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta     (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS shows    (folder TEXT PRIMARY KEY, data TEXT, nfo TEXT, nfo_mtime REAL);
        CREATE TABLE IF NOT EXISTS episodes (video TEXT PRIMARY KEY, show TEXT, mtime REAL, size INTEGER,
                                             nfo TEXT, nfo_mtime REAL, data TEXT, probe TEXT, added REAL);
        CREATE TABLE IF NOT EXISTS watch    (video TEXT PRIMARY KEY, position REAL, duration REAL,
                                             watched INTEGER, play_count INTEGER, last_played REAL);
    """)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


# watch state lives in the same "watch" table the movie library uses
save_progress = videolib.save_progress
set_watched = videolib.set_watched
finish_session = videolib.finish_session


def set_many_watched(db_file, videos, watched: bool):
    for v in videos:
        videolib.set_watched(db_file, v, watched)


# ---------------------------------------------------------------- scanning

def _img(images: dict, names) -> str | None:
    return next((images[n] for n in names if n in images), None)


def _collect(root: str, dirs: dict) -> list[dict]:
    """Shows (one per folder directly under the library folder; loose files in the library folder
    are grouped by the show name in their file names) with their episodes and artwork."""
    shows: list[dict] = []
    top = dirs.get(root, {"subdirs": [], "videos": [], "images": {}, "nfos": {}})
    for sd in sorted(top["subdirs"]):
        if os.path.basename(sd).lower() in EXTRA_DIRS or sd not in dirs:
            continue
        info = dirs[sd]
        episodes, season_info = [], {}
        stack = [(sd, None)]
        while stack:
            d, inherited = stack.pop()
            di = dirs.get(d)
            if di is None:
                continue
            base = os.path.basename(d)
            fs = inherited if d != sd else None
            if d != sd:
                s_ = season_from_dir(base)
                if s_ is not None:
                    fs = s_
                    season_info.setdefault(s_, {"dir": d, "images": di["images"], "nfos": di["nfos"]})
            for v in di["videos"]:
                episodes.append({"path": v[0], "mtime": v[1], "size": v[2], "dir": d, "folder_season": fs,
                                 "images": di["images"], "nfos": di["nfos"]})
            for sub in di["subdirs"]:
                if os.path.basename(sub).lower() not in EXTRA_DIRS:
                    stack.append((sub, fs))
        if not episodes:
            continue
        title, year = parse_show_name(os.path.basename(sd))
        nfo = info["nfos"].get("tvshow")
        im = info["images"]
        seasons = {}
        for n in {e["folder_season"] for e in episodes if e["folder_season"] is not None} | set(season_info):
            names = ([f"season{n:02d}-poster", f"season{n}-poster"] if n else
                     ["season-specials-poster", "season00-poster", "season0-poster"])
            poster = _img(im, names)
            si = season_info.get(n)
            if poster is None and si:
                poster = _img(si["images"], ("poster", "folder", "cover"))
            snfo = si["nfos"].get("season") if si else None
            seasons[str(n)] = {"poster": poster, "nfo": snfo[0] if snfo else None}
        shows.append({"folder": sd, "title": title, "year": year, "nfo": nfo, "episodes": episodes,
                      "poster": _img(im, ("poster", "folder", "cover")), "fanart": _img(im, ("fanart", "backdrop",
                                                                                         "background")),
                      "banner": _img(im, ("banner",)), "logo": _img(im, ("clearlogo", "logo")),
                      "landscape": _img(im, ("landscape", "thumb")), "seasons": seasons,
                      "actors": info["actors"]})
    # loose episodes in the library folder itself
    loose: dict[str, list] = {}
    for v in top["videos"]:
        stem = os.path.splitext(os.path.basename(v[0]))[0]
        m = _SXE.search(stem) or _NXN.search(stem)
        name = re.sub(r"[._]+", " ", _BRACKETS.sub(" ", stem[:m.start()] if m else stem)).strip(" -") or "Unsorted"
        loose.setdefault(name, []).append({"path": v[0], "mtime": v[1], "size": v[2], "dir": root,
                                           "folder_season": None, "images": top["images"], "nfos": top["nfos"]})
    for name, eps in loose.items():
        title, year = videolib.parse_name(name)
        shows.append({"folder": os.path.join(root, f"[{name}]"), "title": title, "year": year, "nfo": None,
                      "episodes": eps, "poster": None, "fanart": None, "banner": None, "logo": None,
                      "landscape": None, "seasons": {}, "actors": {}})
    return shows


def _episode_record(e: dict, show_folder: str) -> dict:
    name = os.path.basename(e["path"])
    stem = os.path.splitext(name)[0].lower()
    season, eps, title = parse_episode(name, e["folder_season"])
    nfo = e["nfos"].get(stem)
    thumb = _img(e["images"], (f"{stem}-thumb", stem))
    return {"video": e["path"], "show": show_folder, "season": season, "episodes": eps, "title": title,
            "thumb": thumb, "nfo": nfo[0] if nfo else None, "nfo_mtime": nfo[1] if nfo else None,
            "mtime": e["mtime"], "size": e["size"]}


def scan(root, db_file=None, progress: Callable[[int, int, str], None] | None = None,
         cancelled: Callable[[], bool] | None = None, full: bool = False, workers: int = 4, **_ignored) -> dict:
    root = str(Path(root).expanduser())
    db_file = db_file or db_path_for(root)
    progress = progress or (lambda *_: None)
    cancelled = cancelled or (lambda: False)
    t0 = time.time()
    progress(0, 0, "Looking for shows")
    try:
        dirs, failed = videolib._walk(root, cancelled)
    except Cancelled:
        return {"cancelled": True, "changed": False}
    except OSError as e:
        return {"error": f"Can't read {root} ({e.strerror or e}). Is the drive or share connected?", "changed": False}
    shows = _collect(root, dirs)

    con = connect(db_file)
    gone_eps: list[str] = []
    try:
        old_shows = {r["folder"]: dict(r) for r in con.execute("SELECT * FROM shows")}
        old_eps = {r["video"]: dict(r) for r in con.execute("SELECT * FROM episodes")}
        parser = con.execute("SELECT value FROM meta WHERE key='nfo_parser'").fetchone()
        full_nfo = parser is None or parser["value"] != NFO_PARSER_VERSION
        if full_nfo:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('nfo_parser', ?)", (NFO_PARSER_VERSION,))
        if not shows and old_shows:
            return {"error": f"No shows found in {root}. Is the drive or share connected?", "changed": False}
        under_failed = lambda p: any(p == f or p.startswith(f.rstrip(os.sep) + os.sep) for f in failed)
        changed = False
        now = time.time()
        show_rows, ep_rows, need_probe = [], [], []
        for sh in shows:
            if cancelled():
                raise Cancelled
            prev = old_shows.get(sh["folder"])
            nfo_path, nfo_mtime = (sh["nfo"] if sh["nfo"] else (None, None))
            nfo_data = prev["nfo"] if prev else None
            if nfo_path and (full or full_nfo or not prev or prev["nfo_mtime"] != nfo_mtime or not prev["nfo"]):
                parsed = parse_show_nfo(Path(nfo_path))
                nfo_data = json.dumps(parsed) if parsed else None
            elif not nfo_path:
                nfo_data = None
            seasons = {}
            for n, s_ in sh["seasons"].items():
                sn = parse_season_nfo(Path(s_["nfo"])) if s_["nfo"] else None
                seasons[n] = {"poster": s_["poster"], "title": (sn or {}).get("title", ""),
                              "plot": (sn or {}).get("plot", "")}
            data = json.dumps({k: sh[k] for k in ("title", "year", "poster", "fanart", "banner", "logo", "landscape",
                                                  "actors")}
                              | {"seasons": seasons})
            row = (sh["folder"], data, nfo_data, nfo_mtime)
            if not prev or tuple(prev[k] for k in ("folder", "data", "nfo", "nfo_mtime")) != row:
                changed = True
            show_rows.append(row)
            for ef in sh["episodes"]:
                r = _episode_record(ef, sh["folder"])
                pe = old_eps.get(r["video"])
                enfo = pe["nfo"] if pe else None
                if r["nfo"] and (full or full_nfo or not pe or pe["nfo_mtime"] != r["nfo_mtime"] or not pe["nfo"]):
                    parsed = parse_episode_nfo(Path(r["nfo"]))
                    enfo = json.dumps(parsed) if parsed else None
                elif not r["nfo"]:
                    enfo = None
                probe_data = pe["probe"] if pe and pe["mtime"] == r["mtime"] and pe["size"] == r["size"] else None
                if full or probe_data is None:
                    need_probe.append(r["video"])
                added = pe["added"] if pe and pe["added"] else (r["mtime"] if not old_eps else now)
                edata = json.dumps({k: r[k] for k in ("season", "episodes", "title", "thumb")})
                erow = (r["video"], r["show"], r["mtime"], r["size"], enfo, r["nfo_mtime"], edata, probe_data, added)
                if not pe or tuple(pe[k] for k in ("video", "show", "mtime", "size", "nfo", "nfo_mtime", "data",
                                                   "probe", "added")) != erow:
                    changed = True
                ep_rows.append(erow)
        seen_shows = {r[0] for r in show_rows}
        seen_eps = {r[0] for r in ep_rows}
        gone_shows = [f for f in old_shows if f not in seen_shows and not under_failed(f)]
        gone_eps = [v for v in old_eps if v not in seen_eps and not under_failed(v)]
        con.executemany("INSERT OR REPLACE INTO shows VALUES (?,?,?,?)", show_rows)
        con.executemany("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?)", ep_rows)
        con.executemany("DELETE FROM shows WHERE folder=?", [(f,) for f in gone_shows])
        con.executemany("DELETE FROM episodes WHERE video=?", [(v,) for v in gone_eps])
        con.commit()
        changed = changed or bool(gone_shows or gone_eps)

        if need_probe:
            progress(0, len(need_probe), "Reading video details")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for n, (video, info) in enumerate(zip(need_probe, ex.map(probe, need_probe)), 1):
                    if cancelled():
                        raise Cancelled
                    con.execute("UPDATE episodes SET probe=? WHERE video=?", (json.dumps(info), video))
                    if n % 25 == 0:
                        con.commit()
                    progress(n, len(need_probe), "Reading video details")
            con.commit()
            changed = True
    except Cancelled:
        con.commit()
        return {"cancelled": True, "changed": True}
    finally:
        con.close()
    return {"changed": changed, "files": len(ep_rows), "shows": len(show_rows), "removed": len(gone_eps),
            "unreadable_folders": len(failed), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- loading

def load_shows(db_file) -> list[Show]:
    if not Path(db_file).exists():
        return []
    con = connect(db_file)
    try:
        show_rows = con.execute("SELECT * FROM shows").fetchall()
        ep_rows = con.execute("SELECT * FROM episodes").fetchall()
        watch = {r["video"]: dict(r) for r in con.execute("SELECT * FROM watch")}
    finally:
        con.close()
    by_show: dict[str, list] = {}
    for r in ep_rows:
        by_show.setdefault(r["show"], []).append(r)
    out = []
    for r in show_rows:
        d = json.loads(r["data"])
        nfo = json.loads(r["nfo"]) if r["nfo"] else None
        p = lambda k: Path(d[k]) if d.get(k) else None
        sh = Show(folder=Path(r["folder"]), title=d["title"], year=d.get("year") or "", poster=p("poster"),
                  fanart=p("fanart"), banner=p("banner"), logo=p("logo"), landscape=p("landscape"))
        named = {}
        actors = d.get("actors") or {}                   # .actors/Actor_Name.jpg in the show folder
        if nfo:
            sh.has_nfo = True
            for k in ("title", "original_title", "sort_title", "premiered", "status", "plot", "outline", "tagline",
                      "mpaa"):
                if nfo.get(k):
                    setattr(sh, k, nfo[k])
            if nfo.get("year"):
                sh.year = nfo["year"]
            for k in ("genres", "studios", "countries", "tags"):
                if nfo.get(k):
                    setattr(sh, k, list(nfo[k]))
            if nfo.get("rating") is not None:
                sh.rating, sh.rating_max = nfo["rating"], nfo.get("rating_max") or 10
            sh.ids = dict(nfo.get("ids") or {})
            sh.related = list(nfo.get("related") or [])
            sh.known = [list(k) for k in nfo.get("known") or []]
            sh.ratings = dict(nfo.get("ratings") or {})
            for c in sorted(nfo.get("cast") or [], key=lambda c: c.get("order") if c.get("order") is not None else 999):
                thumb = c.get("thumb") or ""
                if thumb.startswith(("http://", "https://")) or (thumb and not Path(thumb).is_absolute()):
                    thumb = ""
                thumb = thumb or actors.get(c["name"].casefold(), "")
                sh.cast.append(Person(c["name"], c.get("role", ""), c.get("order") or 999, thumb))
            named = nfo.get("named_seasons") or {}
        seasons: dict[int, Season] = {}
        for n, sd in (d.get("seasons") or {}).items():
            num = int(n)
            seasons[num] = Season(num, name=named.get(n) or sd.get("title") or "", plot=sd.get("plot") or "",
                                  poster=Path(sd["poster"]) if sd.get("poster") else None)
        for er in by_show.get(r["folder"], []):
            ed = json.loads(er["data"])
            en = json.loads(er["nfo"]) if er["nfo"] else None
            info = json.loads(er["probe"]) if er["probe"] else {}
            season = ed.get("season")
            eps = list(ed.get("episodes") or [])
            ep = Episode(video=Path(er["video"]), show_folder=r["folder"], season=1, episode=None,
                         title=ed.get("title") or "", thumb=Path(ed["thumb"]) if ed.get("thumb") else None,
                         info=info, size=er["size"] or 0, added=er["added"] or 0.0)
            if en:                                        # NFO is king
                ep.has_nfo = True
                if en.get("season") is not None:
                    season = en["season"]
                if en.get("episodes"):
                    eps = list(en["episodes"])
                for k in ("title", "plot", "aired"):
                    if en.get(k):
                        setattr(ep, k, en[k])
                for k in ("directors", "writers"):
                    if en.get(k):
                        setattr(ep, k, list(en[k]))
                ep.runtime, ep.rating, ep.ids = en.get("runtime"), en.get("rating"), dict(en.get("ids") or {})
                if en.get("thumb"):
                    t = Path(en["thumb"])
                    t = t if t.is_absolute() else ep.video.parent / t
                    if t.exists():
                        ep.thumb = t
                ep.watched = bool(en.get("watched"))
            ep.season = season if season is not None else 1
            ep.episodes = eps
            ep.episode = eps[0] if eps else None
            if not ep.runtime and info.get("duration"):
                ep.runtime = int(round(info["duration"] / 60))
            w = watch.get(er["video"])
            if w:
                ep.position, ep.duration = w["position"] or 0.0, w["duration"] or 0.0
                ep.watched, ep.play_count, ep.last_played = bool(w["watched"]), w["play_count"] or 0, \
                    w["last_played"] or 0.0
            if not ep.duration and info.get("duration"):
                ep.duration = info["duration"]
            seasons.setdefault(ep.season, Season(ep.season, name=named.get(str(ep.season), ""))).episodes.append(ep)
        for s_ in seasons.values():
            s_.episodes.sort(key=lambda e: (e.episode if e.episode is not None else 10 ** 6, e.video.name.casefold()))
        sh.seasons = [seasons[n] for n in sorted(seasons, key=lambda n: (n == 0, n)) if seasons[n].episodes]
        if not sh.year:
            aired = sorted(e.aired[:4] for e in sh.episodes if e.aired[:4].isdigit())
            sh.year = aired[0] if aired else ""
        if sh.seasons:
            out.append(sh)
    out.sort(key=sort_key)
    return out


def sort_key(sh: Show) -> str:
    return library.sort_name(sh.sort_title or sh.title)


def up_next(show: Show) -> Episode | None:
    """Next Up for a show you've started (None when you haven't, or you've seen it all)."""
    if not show.started or show.watched:
        going = [e for e in show.episodes if e.progress > 0]
        return max(going, key=lambda e: e.last_played) if going else None
    return show.next_episode()


def playlist_from(show: Show, start: Episode, limit: int = 60) -> list[Episode]:
    """`start` and the episodes after it, in order (specials only when you start in them)."""
    eps = show.episodes if start.season == 0 else show.regular
    try:
        i = next(i for i, e in enumerate(eps) if e is start or e.key == start.key)
    except StopIteration:
        return [start]
    return eps[i:i + limit]
