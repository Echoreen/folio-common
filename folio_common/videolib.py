"""Movie libraries for Folio: scanning, Kodi movie NFOs, artwork, technical info, watch state.
No Qt.

Layout (the same one Kodi, Jellyfin and tinyMediaManager use):

    Movies/
      Title (Year)/
        anything.mkv              the movie (a detailed release name is fine)
        movie.nfo                 or <video name>.nfo; NFO values win over everything else
        poster.jpg  fanart.jpg  clearlogo.png  landscape.jpg
        extras/ trailers/ ...     extras, listed on the movie page
        .actors/Actor_Name.jpg    optional cast photos (Kodi style)

Loose videos directly in a folder that holds several movies work too (each uses
<video name>.nfo and <video name>-poster.jpg). Title and year fall back to the folder name
"Title (Year)", then to the file name ("Ghost.in.the.Shell.1995.1080p..." -> Ghost in the Shell, 1995).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import folio_nfo as fn
from folio_common import library   # cache_dir(), shared conventions

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".ogv"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
EXTRA_DIRS = {"extras", "extra", "trailers", "trailer", "featurettes", "behind the scenes", "deleted scenes",
              "interviews", "scenes", "shorts", "other", "samples", "sample", "specials",
              "nc", "ncop", "nced", "creditless", "nc op & ed", "menus", "menu"}   # anime packs' openings/endings
EXTRA_SUFFIX = re.compile(r"[-_. ](trailer|sample|featurette|behindthescenes|deleted|interview|scene|short|extra)"
                          r"\d*$", re.I)
CREDITLESS = re.compile(r"(?i)(?<![a-z])(nc ?(op|ed)|creditless|clean ?(opening|ending))(?![a-z])")
SCHEMA_VERSION = 1
NFO_PARSER_VERSION = "6." + library.code_fingerprint(__file__)   # changes by itself when the reading code does

_RATING_NAMES = {"themoviedb": "tmdb", "tmdb": "tmdb", "imdb": "imdb", "trakt": "trakt", "metacritic": "metacritic",
                 "tomatometerallcritics": "rottentomatoes", "rottentomatoes": "rottentomatoes",
                 "tomatometerallaudience": "rt_audience", "letterboxd": "letterboxd", "anidb": "anidb",
                 "myanimelist": "myanimelist", "mal": "myanimelist"}


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- model

@dataclass
class Person:
    name: str
    role: str = ""
    order: int = 999
    thumb: str = ""        # local image path, if any


@dataclass
class Movie:
    folder: Path
    video: Path                                  # main file (largest version)
    title: str
    year: str = ""
    original_title: str = ""
    sort_title: str = ""
    premiered: str = ""
    runtime: int | None = None                   # minutes
    plot: str = ""
    outline: str = ""
    tagline: str = ""
    mpaa: str = ""
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    writers: list[str] = field(default_factory=list)
    cast: list[Person] = field(default_factory=list)
    collection: str = ""
    tags: list[str] = field(default_factory=list)
    rating: float | None = None
    rating_max: float = 10
    ratings: dict = field(default_factory=dict)  # source -> {"value", "max", "votes"}
    ids: dict = field(default_factory=dict)
    related: list = field(default_factory=list)   # [{"title", "anilist", "tmdb", "kind", "relation", ...}]
    studio_logos: dict = field(default_factory=dict)   # studio name -> logo web address (TMDB)
    crew_photos: dict = field(default_factory=dict)    # director / writer name (casefolded) -> photo in .actors
    has_nfo: bool = False
    poster: Path | None = None
    fanart: Path | None = None
    logo: Path | None = None
    landscape: Path | None = None
    versions: list[Path] = field(default_factory=list)
    extras: list[Path] = field(default_factory=list)
    info: dict = field(default_factory=dict)     # from ffprobe: resolution, hdr, codecs, audio, subs
    size: int = 0
    added: float = 0.0
    # watch state (filled from the cache)
    position: float = 0.0                        # seconds; resume point
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


# ---------------------------------------------------------------- names

_FOLDER_RE = re.compile(r"^(?P<title>.+?)[\s._]*[(\[](?P<year>(19|20)\d\d)[)\]]")
_YEAR_TOKEN = re.compile(r"^(19|20)\d\d$")
_JUNK = re.compile(r"^(\d{3,4}p|4k|uhd|hdr|hdr10|dv|bluray|blu-ray|bdrip|brrip|web|webrip|web-dl|webdl|dvdrip|"
                   r"remux|x264|x265|h264|h265|hevc|av1|aac|ac3|dts|truehd|atmos|10bit|proper|repack|extended|"
                   r"unrated|remastered|imax)$", re.I)


def parse_name(name: str) -> tuple[str, str]:
    """"Title (Year)" or "Title.Year.1080p.Junk" -> (title, year)."""
    m = _FOLDER_RE.match(name)
    if m:
        return m.group("title").strip(" .-_"), m.group("year")
    tokens = re.split(r"[ ._]+", name)
    out, year = [], ""
    for i, tok in enumerate(tokens):
        if i > 0 and _YEAR_TOKEN.match(tok):
            year = tok
            break
        if i > 0 and _JUNK.match(tok):
            break
        out.append(tok)
    title = " ".join(out).strip(" -") or name
    return title, year


def _is_extra(path: Path, movie_dir: Path) -> bool:
    try:
        rel_parts = path.relative_to(movie_dir).parts[:-1]
    except ValueError:
        rel_parts = ()
    if any(p.lower() in EXTRA_DIRS for p in rel_parts):
        return True
    return bool(EXTRA_SUFFIX.search(path.stem) or CREDITLESS.search(path.stem))


# ---------------------------------------------------------------- NFO

def _text(root, tag) -> str:
    return (root.findtext(tag) or "").strip()


def _texts(root, tag) -> list[str]:
    out = []
    for e in root.findall(tag):
        for part in re.split(r"\s*/\s*", (e.text or "").strip()):
            if part and part not in out:
                out.append(part)
    return out


def _num(s, cast):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


def ratings_from(nfo) -> tuple[float | None, float, dict]:
    """(default rating, its max, {source: {"value", "max", "votes"}}) from a folio_nfo video NFO.
    A lone legacy <rating> gives the default rating but no named sources."""
    all_ratings: dict[str, dict] = {}
    if nfo.element is not None and nfo.element.find("ratings") is not None:
        for name, r in nfo.ratings.items():
            src = _RATING_NAMES.get(name, "rating" if name == "default" else name)
            all_ratings[src] = {"value": r.value, "max": r.max, "votes": r.votes}
    if getattr(nfo, "mal_rank", None) and "myanimelist" in all_ratings:
        all_ratings["myanimelist"]["rank"] = nfo.mal_rank
    r = nfo.rating
    return (r.value if r else None), (r.max if r else 10.0), all_ratings


def cast_from(nfo) -> list[dict]:
    return [{"name": a.name, "role": a.role, "order": a.order if a.order is not None else i, "thumb": a.thumb}
            for i, a in enumerate(nfo.actors)]


def clean_mpaa(s: str) -> str:
    return re.sub(r"^(Rated|US:)\s*", "", s or "", flags=re.I)


def parse_movie_nfo(path: Path) -> dict | None:
    try:
        m = fn.MovieNfo.read(path)
    except (fn.NfoError, OSError):
        return None
    rating, rmax, all_ratings = ratings_from(m)
    if m.critic_rating is not None and "rottentomatoes" not in all_ratings:
        all_ratings["rottentomatoes"] = {"value": m.critic_rating, "max": 100.0, "votes": None}
    return {
        "title": m.title, "original_title": m.original_title,
        "sort_title": m.sort_title, "year": m.text("year")[:4],
        "premiered": m.premiered or m.text("releasedate") or m.text("aired"),
        "runtime": m.runtime, "plot": m.plot,
        "outline": m.outline, "tagline": m.tagline, "mpaa": clean_mpaa(m.mpaa),
        "genres": m.genres, "studios": m.studios, "countries": m.countries,
        "directors": m.directors, "writers": m.writers or _texts(m.element, "writer"),
        "cast": cast_from(m), "collection": m.collection, "tags": m.tags, "rating": rating,
        "rating_max": rmax, "ratings": all_ratings, "ids": dict(m.ids), "studio_logos": dict(m.studio_logos),
        "related": [{k: getattr(r, k) for k in ("title", "anilist", "tmdb", "kind", "relation", "format", "year", "manual", "cover")}
                    for r in m.related],
        "watched": m.watched or (m.play_count or 0) > 0,
    }


# ---------------------------------------------------------------- technical info (ffprobe)

def _lang(s) -> str:
    s = (s or "").lower()
    names = {"eng": "English", "en": "English", "jpn": "Japanese", "ja": "Japanese", "kor": "Korean",
             "ko": "Korean", "spa": "Spanish", "es": "Spanish", "fre": "French", "fra": "French", "fr": "French",
             "ger": "German", "deu": "German", "de": "German", "ita": "Italian", "it": "Italian",
             "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "por": "Portuguese", "pt": "Portuguese",
             "rus": "Russian", "ru": "Russian"}
    return names.get(s, s.upper() if s and s not in ("und", "unk") else "")


def probe(path) -> dict:
    """Resolution / HDR / codecs / audio / subtitles via ffprobe. {} if ffprobe is missing or fails."""
    exe = shutil.which("ffprobe")
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format",
                              str(path)], capture_output=True, text=True, timeout=60).stdout
        j = json.loads(out or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    info: dict = {"audio": [], "subtitles": []}
    try:
        info["duration"] = float(j.get("format", {}).get("duration") or 0) or None
    except ValueError:
        info["duration"] = None
    for st in j.get("streams", []):
        kind = st.get("codec_type")
        disp = st.get("disposition") or {}
        if kind == "video" and not disp.get("attached_pic") and "video" not in info:
            w, h = st.get("width") or 0, st.get("height") or 0
            res = ("4K" if w >= 3200 or h >= 2000 else "1440p" if h >= 1400 else "1080p" if w >= 1800 or h >= 1000
                   else "720p" if w >= 1200 or h >= 700 else "SD") if w or h else ""
            trc = (st.get("color_transfer") or "").lower()
            hdr = ""
            side = " ".join(json.dumps(sd) for sd in st.get("side_data_list") or [])
            if "DOVI" in side or "dolby vision" in side.lower():
                hdr = "Dolby Vision"
            elif trc == "smpte2084":
                hdr = "HDR10+" if "HDR10+" in side or "SMPTE2094-40" in side else "HDR10"
            elif trc == "arib-std-b67":
                hdr = "HLG"
            codec = {"av1": "AV1", "hevc": "HEVC", "h264": "H.264", "vp9": "VP9", "mpeg2video": "MPEG-2",
                     "mpeg4": "MPEG-4", "vc1": "VC-1"}.get(st.get("codec_name", ""), (st.get("codec_name") or "").upper())
            info["video"] = {"width": w, "height": h, "resolution": res, "hdr": hdr, "codec": codec,
                             "bit_depth": int(st.get("bits_per_raw_sample") or 0) or
                             (10 if "10" in (st.get("pix_fmt") or "") else 8)}
        elif kind == "audio":
            ch = st.get("channels") or 0
            layout = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(ch, f"{ch} ch" if ch else "")
            codec = {"truehd": "TrueHD", "eac3": "E-AC-3", "ac3": "AC-3", "dts": "DTS", "aac": "AAC", "flac": "FLAC",
                     "opus": "Opus", "mp3": "MP3", "vorbis": "Vorbis", "pcm_s16le": "PCM", "pcm_s24le": "PCM"
                     }.get(st.get("codec_name", ""), (st.get("codec_name") or "").upper())
            if codec == "DTS" and "MA" in (st.get("profile") or ""):
                codec = "DTS-HD MA"
            tags = st.get("tags") or {}
            info["audio"].append({"codec": codec, "layout": layout, "language": _lang(tags.get("language")),
                                  "title": tags.get("title", ""), "default": bool(disp.get("default"))})
        elif kind == "subtitle":
            tags = st.get("tags") or {}
            info["subtitles"].append({"language": _lang(tags.get("language")), "title": tags.get("title", ""),
                                      "forced": bool(disp.get("forced")), "codec": st.get("codec_name", "")})
    return info


# ---------------------------------------------------------------- cache

def db_path_for(root) -> Path:
    key = hashlib.sha1(str(Path(root).expanduser().absolute()).encode()).hexdigest()[:12]
    return library.cache_dir() / f"movies-{key}.sqlite"


def connect(db_file) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_file), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta   (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS movies (video TEXT PRIMARY KEY, folder TEXT, mtime REAL, size INTEGER,
                                           nfo TEXT, nfo_mtime REAL, data TEXT, probe TEXT, added REAL);
        CREATE TABLE IF NOT EXISTS watch  (video TEXT PRIMARY KEY, position REAL, duration REAL,
                                           watched INTEGER, play_count INTEGER, last_played REAL);
    """)
    con.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    con.commit()
    return con


# ---------------------------------------------------------------- scanning

def _pick_image(images: dict[str, str], stem: str, names: tuple[str, ...]) -> str | None:
    s = stem.lower()
    for n in names:
        for cand in (f"{s}-{n}", n):
            if cand in images:
                return images[cand]
    return None


def _walk(root: str, cancelled) -> tuple[dict, list[str]]:
    """dir -> {"videos": [(path, mtime, size)], "images": {stem: path}, "nfos": {stem: (path, mtime)},
    "actors": {name: path}, "subdirs": [...]} for every readable folder."""
    dirs: dict[str, dict] = {}
    failed: list[str] = []
    seen: set[tuple] = set()
    stack = [root]
    while stack:
        if cancelled():
            raise Cancelled
        d = stack.pop()
        try:
            st = os.stat(d)
            ident = (st.st_dev, st.st_ino)
            if ident in seen:
                continue
            seen.add(ident)
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            if d == root:
                raise
            failed.append(d)
            continue
        info = {"videos": [], "images": {}, "nfos": {}, "actors": {}, "subdirs": []}
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=True):
                    if e.name == ".actors":
                        try:
                            with os.scandir(e.path) as it2:
                                for a in it2:
                                    stem, ext = os.path.splitext(a.name)
                                    if ext.lower() in IMAGE_EXTS:
                                        info["actors"][stem.replace("_", " ").casefold()] = a.path
                        except OSError:
                            pass
                    elif not e.name.startswith("."):
                        stack.append(e.path)
                        info["subdirs"].append(e.path)
                    continue
                if e.name.startswith("."):
                    continue
                stem, ext = os.path.splitext(e.name)
                ext = ext.lower()
                if ext in VIDEO_EXTS:
                    s = e.stat()
                    info["videos"].append((e.path, s.st_mtime, s.st_size))
                elif ext == ".nfo":
                    info["nfos"][stem.lower()] = (e.path, e.stat().st_mtime)
                elif ext in IMAGE_EXTS:
                    info["images"].setdefault(stem.lower(), e.path)
            except OSError:
                continue
        dirs[d] = info
    return dirs, failed


def _group(root: str, dirs: dict) -> list[dict]:
    """Decide what the movies are. Returns [{"folder", "videos": [...], "extras": [...], "loose": bool}]."""
    movies = []
    for d, info in dirs.items():
        name = os.path.basename(d).lower()
        if name in EXTRA_DIRS:
            continue                                     # handled by their movie folder
        main = [v for v in info["videos"] if not _is_extra(Path(v[0]), Path(d))]
        if not main:
            continue
        extras = [v[0] for v in info["videos"] if _is_extra(Path(v[0]), Path(d))]
        for sub in info["subdirs"]:
            if os.path.basename(sub).lower() in EXTRA_DIRS and sub in dirs:
                stack = [sub]
                while stack:
                    s = stack.pop()
                    extras += [v[0] for v in dirs.get(s, {}).get("videos", [])]
                    stack += dirs.get(s, {}).get("subdirs", [])
        ftitle, fyear = parse_name(os.path.basename(d))
        # one movie per folder when the folder is named like a movie or holds a single film;
        # otherwise every video is its own movie (a folder of loose files)
        has_folder_nfo = "movie" in info["nfos"]
        same_film = len(main) == 1 or has_folder_nfo or (fyear and d != root) or \
            all(re.sub(r"(?i)[ ._-]*(cd|disc|disk|part|pt)[ ._-]*\d+$", "", Path(v[0]).stem) ==
                re.sub(r"(?i)[ ._-]*(cd|disc|disk|part|pt)[ ._-]*\d+$", "", Path(main[0][0]).stem) for v in main)
        if same_film and d != root:
            movies.append({"folder": d, "videos": main, "extras": sorted(extras), "loose": False})
        else:
            for v in main:
                movies.append({"folder": d, "videos": [v], "extras": [], "loose": True})
    return movies


def _build_record(m: dict, info: dict) -> dict:
    """Everything about one movie that doesn't need ffprobe."""
    main = max(m["videos"], key=lambda v: v[2])           # largest file = main version
    video = Path(main[0])
    stem = video.stem
    folder = Path(m["folder"])
    if m["loose"]:
        title, year = parse_name(stem)
        nfo = info["nfos"].get(stem.lower())
    else:
        title, year = parse_name(folder.name)      # a movie's own folder names it
        if not year:
            year = parse_name(stem)[1]
        nfo = info["nfos"].get("movie") or info["nfos"].get(stem.lower())
        if nfo is None:
            others = [v for k, v in info["nfos"].items() if k not in ("tvshow",)]
            nfo = others[0] if len(others) == 1 else None
    images = info["images"]

    def img(names):
        if m["loose"]:          # several movies share this folder: only <video>-poster.jpg etc. are theirs
            s_ = stem.lower()
            return next((images[f"{s_}-{n}"] for n in names if f"{s_}-{n}" in images), None)
        return _pick_image(images, stem, names)
    return {
        "video": str(video), "folder": str(folder), "title": title, "year": year,
        "versions": [v[0] for v in sorted(m["videos"], key=lambda v: -v[2])],
        "extras": m["extras"], "nfo": nfo[0] if nfo else None, "nfo_mtime": nfo[1] if nfo else None,
        "poster": img(("poster", "folder", "cover", "movie")),
        "fanart": img(("fanart", "backdrop", "background")),
        "logo": img(("clearlogo", "logo")),
        "landscape": img(("landscape", "thumb")),
        "actors": info["actors"], "size": main[2], "mtime": main[1],
    }


def scan(root, db_file=None, progress: Callable[[int, int, str], None] | None = None,
         cancelled: Callable[[], bool] | None = None, full: bool = False, workers: int = 4, **_ignored) -> dict:
    root = str(Path(root).expanduser())
    db_file = db_file or db_path_for(root)
    progress = progress or (lambda *_: None)
    cancelled = cancelled or (lambda: False)
    t0 = time.time()
    progress(0, 0, "Looking for movies")
    try:
        dirs, failed = _walk(root, cancelled)
    except Cancelled:
        return {"cancelled": True, "changed": False}
    except OSError as e:
        return {"error": f"Can't read {root} ({e.strerror or e}). Is the drive or share connected?", "changed": False}
    records = [_build_record(m, dirs[m["folder"]]) for m in _group(root, dirs)]

    con = connect(db_file)
    try:
        old = {r["video"]: dict(r) for r in con.execute("SELECT * FROM movies")}
        parser = con.execute("SELECT value FROM meta WHERE key='nfo_parser'").fetchone()
        if parser is None or parser["value"] != NFO_PARSER_VERSION:
            full_nfo = True
            con.execute("INSERT OR REPLACE INTO meta VALUES ('nfo_parser', ?)", (NFO_PARSER_VERSION,))
        else:
            full_nfo = False
        if not records and old:
            return {"error": f"No movies found in {root}. Is the drive or share connected?", "changed": False}
        under_failed = lambda p: any(p == f or p.startswith(f.rstrip(os.sep) + os.sep) for f in failed)
        changed = False
        need_probe = []
        rows = []
        now = time.time()
        for r in records:
            prev = old.get(r["video"])
            nfo_data = prev["nfo"] if prev else None
            if r["nfo"] and (full or full_nfo or not prev or prev["nfo_mtime"] != r["nfo_mtime"] or not prev["nfo"]):
                parsed = parse_movie_nfo(Path(r["nfo"]))
                nfo_data = json.dumps(parsed) if parsed else None
            elif not r["nfo"]:
                nfo_data = None
            probe_data = prev["probe"] if prev and prev["mtime"] == r["mtime"] and prev["size"] == r["size"] else None
            if full or probe_data is None:
                need_probe.append(r["video"])
            added = prev["added"] if prev and prev["added"] else (r["mtime"] if not old else now)
            data = json.dumps({k: r[k] for k in ("title", "year", "versions", "extras", "poster", "fanart", "logo",
                                                 "landscape", "actors", "nfo")})
            row = (r["video"], r["folder"], r["mtime"], r["size"], nfo_data, r["nfo_mtime"], data, probe_data, added)
            if not prev or tuple(prev[k] for k in ("video", "folder", "mtime", "size", "nfo", "nfo_mtime", "data",
                                                   "probe", "added")) != row:
                changed = True
            rows.append(row)
        gone = [v for v in old if v not in {r["video"] for r in records} and not under_failed(v)]
        con.executemany("INSERT OR REPLACE INTO movies VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("DELETE FROM movies WHERE video=?", [(v,) for v in gone])
        con.commit()
        changed = changed or bool(gone)

        if need_probe:
            progress(0, len(need_probe), "Reading video details")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for n, (video, info) in enumerate(zip(need_probe, ex.map(probe, need_probe)), 1):
                    if cancelled():
                        raise Cancelled
                    con.execute("UPDATE movies SET probe=? WHERE video=?", (json.dumps(info), video))
                    progress(n, len(need_probe), "Reading video details")
            con.commit()
            changed = True
    except Cancelled:
        con.commit()
        return {"cancelled": True, "changed": True}
    finally:
        con.close()
    return {"changed": changed, "files": len(records), "removed": len(gone),
            "unreadable_folders": len(failed), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- loading

def load_movies(db_file) -> list[Movie]:
    if not Path(db_file).exists():
        return []
    con = connect(db_file)
    try:
        rows = con.execute("SELECT * FROM movies").fetchall()
        watch = {r["video"]: dict(r) for r in con.execute("SELECT * FROM watch")}
    finally:
        con.close()
    out = []
    for r in rows:
        d = json.loads(r["data"])
        nfo = json.loads(r["nfo"]) if r["nfo"] else None
        info = json.loads(r["probe"]) if r["probe"] else {}
        p = lambda k: Path(d[k]) if d.get(k) else None
        m = Movie(folder=Path(r["folder"]), video=Path(r["video"]), title=d["title"], year=d.get("year") or "",
                  versions=[Path(v) for v in d.get("versions", [])], extras=[Path(v) for v in d.get("extras", [])],
                  poster=p("poster"), fanart=p("fanart"), logo=p("logo"), landscape=p("landscape"),
                  info=info, size=r["size"] or 0, added=r["added"] or 0.0)
        actors = d.get("actors") or {}
        if nfo:                                          # NFO is king
            m.has_nfo = True
            for k in ("title", "original_title", "sort_title", "premiered", "plot", "outline", "tagline", "mpaa",
                      "collection"):
                if nfo.get(k):
                    setattr(m, k, nfo[k])
            if nfo.get("year"):
                m.year = nfo["year"]
            elif nfo.get("premiered"):
                m.year = nfo["premiered"][:4]
            for k in ("genres", "studios", "countries", "directors", "writers", "tags"):
                if nfo.get(k):
                    setattr(m, k, list(nfo[k]))
            if nfo.get("runtime"):
                m.runtime = nfo["runtime"]
            if nfo.get("rating") is not None:
                m.rating, m.rating_max = nfo["rating"], nfo.get("rating_max") or 10
            m.ids = dict(nfo.get("ids") or {})
            m.related = list(nfo.get("related") or [])
            m.studio_logos = dict(nfo.get("studio_logos") or {})
            m.crew_photos = {n.casefold(): actors[n.casefold()] for n in m.directors + m.writers
                             if actors.get(n.casefold())}
            m.ratings = dict(nfo.get("ratings") or {})
            for c in sorted(nfo.get("cast") or [], key=lambda c: c.get("order") if c.get("order") is not None else 999):
                thumb = c.get("thumb") or ""
                if thumb.startswith(("http://", "https://")) or (thumb and not Path(thumb).is_absolute()):
                    thumb = ""
                thumb = thumb or actors.get(c["name"].casefold(), "")
                m.cast.append(Person(c["name"], c.get("role", ""), c.get("order") or 999, thumb))
            m.watched = bool(nfo.get("watched"))
        if not m.runtime and info.get("duration"):
            m.runtime = int(round(info["duration"] / 60))
        w = watch.get(r["video"])
        if w:
            m.position, m.duration = w["position"] or 0.0, w["duration"] or 0.0
            m.watched, m.play_count, m.last_played = bool(w["watched"]), w["play_count"] or 0, w["last_played"] or 0.0
        if not m.duration and info.get("duration"):
            m.duration = info["duration"]
        out.append(m)
    out.sort(key=lambda m: sort_key(m))
    return out


def sort_key(m: Movie) -> str:
    return library.sort_name(m.sort_title or m.title)


# ---------------------------------------------------------------- watch state

def save_progress(db_file, video, position: float, duration: float):
    con = connect(db_file)
    try:
        row = con.execute("SELECT watched, play_count FROM watch WHERE video=?", (str(video),)).fetchone()
        con.execute("INSERT OR REPLACE INTO watch VALUES (?,?,?,?,?,?)",
                    (str(video), float(position), float(duration or 0), row["watched"] if row else 0,
                     row["play_count"] if row else 0, time.time()))
        con.commit()
    finally:
        con.close()


def set_watched(db_file, video, watched: bool, duration: float = 0.0, count_play: bool = False):
    con = connect(db_file)
    try:
        row = con.execute("SELECT * FROM watch WHERE video=?", (str(video),)).fetchone()
        plays = (row["play_count"] if row else 0) + (1 if count_play else 0)
        con.execute("INSERT OR REPLACE INTO watch VALUES (?,?,?,?,?,?)",
                    (str(video), 0.0, float(duration or (row["duration"] if row else 0) or 0), 1 if watched else 0,
                     plays, time.time() if count_play else (row["last_played"] if row else 0)))
        con.commit()
    finally:
        con.close()


def finish_session(db_file, video, position: float, duration: float, reached_end: bool) -> str:
    """Record what happened when the player closed: "watched", "resume" or "none"."""
    if reached_end or (duration and position / duration >= 0.9):
        set_watched(db_file, video, True, duration, count_play=True)
        return "watched"
    if position >= 60:
        save_progress(db_file, video, position, duration)
        return "resume"
    return "none"
