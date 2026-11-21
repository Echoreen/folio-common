"""Music library cache (SQLite) and background scanner for Folio. No Qt.

* scan() walks the library folder, reads tags only for new/changed files (in parallel, which
  matters on network storage), parses NFO files, records folder artwork and extracts embedded
  cover art once. Everything goes into a per-library SQLite file in ~/.cache/folio.
* build_library() turns the cache into Artist/Album/Track objects in well under a second,
  without touching the music folder, so the app starts instantly.

Precedence for every field: NFO  >  tags  >  folder / file names.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from folio_common import nfolib

SCHEMA_VERSION = 1


def code_fingerprint(*files) -> str:
    """A short stamp of the code that reads info files (folio_nfo, plus the given files). When an update
    changes that code, the stamp changes, and the next normal scan re-reads every .nfo by itself
    (only the small info files, never the songs or videos): no "Re-read every file" needed."""
    import folio_nfo
    h = hashlib.sha1()
    for p in sorted(Path(folio_nfo.__file__).parent.glob("*.py")) + [Path(f) for f in files]:
        try:
            h.update(p.name.encode() + p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


NFO_PARSER_VERSION = "3." + code_fingerprint(nfolib.__file__)   # changes by itself with the reading code


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- locations

_migrated = False


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
    d = base / "folio"
    global _migrated
    if not _migrated:
        _migrated = True
        _migrate_cache(base / "nfoplayer", d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_cache(old: Path, new: Path):
    """The app used to be called NFO Player: move its cache over instead of rescanning everything."""
    if new.exists() or not old.is_dir():
        return
    try:
        old.rename(new)
    except OSError:
        return
    for db in new.glob("*.sqlite"):
        try:
            con = sqlite3.connect(str(db))
            con.execute("UPDATE embedded SET art = replace(art, ?, ?) WHERE art LIKE ?",
                        (str(old), str(new), str(old) + "%"))
            con.commit()
            con.close()
        except sqlite3.Error:
            pass


def art_dir() -> Path:
    d = cache_dir() / "art"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path_for(root) -> Path:
    key = hashlib.sha1(str(Path(root).expanduser().absolute()).encode()).hexdigest()[:12]
    return cache_dir() / f"music-{key}.sqlite"


def connect(db_file) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_file), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, dir TEXT, mtime REAL, size INTEGER, tags TEXT);
        CREATE TABLE IF NOT EXISTS nfos  (path TEXT PRIMARY KEY, dir TEXT, kind TEXT, mtime REAL, data TEXT);
        CREATE TABLE IF NOT EXISTS dirs  (dir TEXT PRIMARY KEY, cover TEXT, thumb TEXT, fanart TEXT);
        CREATE TABLE IF NOT EXISTS embedded (key TEXT PRIMARY KEY, dir TEXT, source TEXT, source_mtime REAL, art TEXT);
        CREATE TABLE IF NOT EXISTS artistinfo (folder TEXT PRIMARY KEY, name TEXT, thumb TEXT, fanart TEXT,
                                               nfo_mtime REAL, nfo TEXT);
        CREATE TABLE IF NOT EXISTS plays (path TEXT, ts REAL);
        CREATE INDEX IF NOT EXISTS plays_ts ON plays (ts);
        CREATE TABLE IF NOT EXISTS first_seen (path TEXT PRIMARY KEY, ts REAL);
    """)
    row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if row is None:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        con.commit()
    elif int(row["value"]) != SCHEMA_VERSION:   # future migrations: simplest is a clean rescan
        con.executescript("DELETE FROM files; DELETE FROM nfos; DELETE FROM dirs; DELETE FROM embedded; "
                          "DELETE FROM artistinfo;")
        con.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))
        con.commit()
    return con


# ---------------------------------------------------------------- grouping keys (shared by scanner + builder)

def album_key(tags: dict, folder: str) -> tuple:
    """Which album a file belongs to. Picard's release id is the most reliable key."""
    if tags.get("mb_albumid"):
        return ("mb", tags["mb_albumid"])
    if tags.get("album"):
        who = tags.get("albumartist") or ("Various Artists" if tags.get("compilation") else "") \
            or ((tags.get("artists") or [""])[0])
        return ("tag", who.casefold(), tags["album"].casefold())
    return ("dir", folder)


def embedded_key(folder: str, akey: tuple) -> str:
    return hashlib.sha1(json.dumps([folder, list(akey)]).encode()).hexdigest()[:20]


# ---------------------------------------------------------------- artist info folder (Kodi-style)

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def name_key(name: str) -> str:
    """Loose name match for grouping: Unicode-normalised, spaces collapsed, case ignored."""
    return " ".join(unicodedata.normalize("NFKC", name or "").split()).rstrip(".").casefold()


def artist_key(name: str) -> str:
    """Match "AC/DC" in tags with an "AC_DC" folder, ignoring case."""
    return _ILLEGAL.sub("_", name or "").strip().rstrip(".").casefold()


EXACT = "="       # load_artist_info keys for names written exactly (capitals and all)
BY_ID = "mb:"     # load_artist_info keys for MusicBrainz IDs


def exact_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name or "").split())


def info_for_name(artist_info: dict, name: str, mbid: str = "") -> dict | None:
    """An artist's Artist Info page: by MusicBrainz ID when known, then a name written exactly the same
    ("LISA" finds LISA's page even when "LiSA" has one too), then ignoring capitals and the like."""
    return (artist_info.get(BY_ID + mbid.lower()) if mbid else None) or \
        artist_info.get(EXACT + exact_name(name)) or artist_info.get(artist_key(name))


def alias_map(artist_info: dict) -> dict[str, str]:
    """name_key(other spelling) -> the name kept, from the "Same artist as…" lists in Artist Info."""
    out: dict[str, str] = {}
    for info in {id(i): i for i in artist_info.values()}.values():   # each page once
        nfo = info.get("nfo") or {}
        keep = nfo.get("name") or info.get("name") or ""
        for other in nfo.get("aliases") or []:
            if keep and name_key(other) != name_key(keep):
                out.setdefault(name_key(other), keep)
    return out


_JOINERS = re.compile(r"\s+(?:&|x|×|feat\.?|ft\.?|featuring)\s+|\s*,\s+|\s+&\s*|\s*&\s+", re.I)


def artist_pages(db_file) -> list[dict]:
    """Every artist.nfo in Artist Info, one per folder (load_artist_info keeps only one per name)."""
    if not Path(db_file).exists():
        return []
    con = connect(db_file)
    try:
        return [json.loads(r["nfo"]) | {"_folder": r["folder"]} if r["nfo"] else {"name": r["name"], "_folder": r["folder"]}
                for r in con.execute("SELECT folder, name, nfo FROM artistinfo")]
    finally:
        con.close()


def collaboration_splitter(albums, pages: list[dict], aliases: dict[str, str]):
    """Who an album belongs to, as a list (first one files it). An album artist like "Anyma & LISA"
    becomes ["Anyma", "LISA"]:
      - from album.nfo's MusicBrainz credits when there are two or more (exact, IDs and all), or
      - by name, only when every part is already an artist you have (an album artist or an Artist Info
        page) and that name means just one artist, and the whole name isn't an artist of its own on
        MusicBrainz ("Simon & Garfunkel", "Earth, Wind & Fire" stay one).
    Anything else stays one artist."""
    known: dict[str, set[str]] = {}                       # name_key -> the spellings / IDs seen with it
    def note(name, who):
        if name:
            known.setdefault(name_key(name), set()).add(who)
    for nfo in pages:
        n = nfo.get("name") or ""
        note(n, (nfo.get("mbid") or "").lower() or "page:" + n)
    for al in albums:
        if al.artist and not _JOINERS.search(al.artist):
            note(al.artist, "album:" + al.artist)
    bands = {name_key(nfo.get("name") or "") for nfo in pages if nfo.get("mbid")}

    def one_artist(part: str) -> bool:
        seen = known.get(name_key(part))
        if not seen:
            return False
        ids = {x for x in seen if not x.startswith(("page:", "album:"))}
        return len(ids) <= 1                               # two MusicBrainz artists with one name: unsure

    def split(al) -> list[str]:
        credits = [(n, m) for n, m in (getattr(al, "credits", None) or []) if n]
        whole = al.artist or ""
        if len(credits) >= 2:
            return [aliases.get(name_key(n), n) for n, _m in credits]
        if len(credits) == 1:                              # MusicBrainz says one artist: it is one
            return [whole] if whole else []
        if not _JOINERS.search(whole) or name_key(whole) in bands:
            return [whole] if whole else []
        parts = [p.strip() for p in _JOINERS.split(whole) if p and p.strip()]
        parts = [aliases.get(name_key(p), p) for p in parts]
        if len(parts) >= 2 and all(one_artist(p) for p in parts):
            return parts
        return [whole]
    return split


def find_artist_info_dir(root) -> Path | None:
    """Default location: "Artist Info" inside the library folder, or next to it."""
    root = Path(root).expanduser()
    for cand in (root / "Artist Info", root.parent / "Artist Info"):
        if cand.is_dir():
            return cand
    return None


def _scan_artist_info(con: sqlite3.Connection, info_dir, full: bool) -> bool:
    """Index <info_dir>/<Artist>/{thumb,fanart}.jpg + artist.nfo. Returns True if anything changed."""
    old = {r["folder"]: dict(r) for r in con.execute("SELECT * FROM artistinfo")}
    rows = {}
    if info_dir:
        try:
            with os.scandir(info_dir) as it:
                subdirs = [e.path for e in it if not e.name.startswith(".") and e.is_dir(follow_symlinks=True)]
        except OSError:
            return False                      # folder unreachable: keep what we had
        for d in subdirs:
            images, nfo_path = {}, None
            try:
                with os.scandir(d) as it:
                    for e in it:
                        low = e.name.lower()
                        stem, ext = os.path.splitext(low)
                        if low == "artist.nfo":
                            nfo_path = e.path
                        elif ext in nfolib.IMAGE_EXTS and stem not in images:
                            images[stem] = e.path
            except OSError:
                if d in old:
                    rows[d] = old[d]
                continue
            nfo_mtime, nfo = None, None
            if nfo_path:
                try:
                    nfo_mtime = os.stat(nfo_path).st_mtime
                except OSError:
                    nfo_mtime = None
                prev = old.get(d)
                if not full and prev and prev["nfo_mtime"] == nfo_mtime and prev["nfo"]:
                    nfo = prev["nfo"]
                else:
                    data = nfolib.parse_artist_nfo(Path(nfo_path))
                    nfo = json.dumps(data) if data else None
            rows[d] = {"folder": d, "name": os.path.basename(d), "thumb": _pick(images, nfolib.ARTIST_NAMES),
                       "fanart": _pick(images, nfolib.FANART_NAMES), "nfo_mtime": nfo_mtime, "nfo": nfo}
    changed = rows != old
    if changed:
        con.execute("DELETE FROM artistinfo")
        con.executemany("INSERT INTO artistinfo VALUES (:folder, :name, :thumb, :fanart, :nfo_mtime, :nfo)",
                        list(rows.values()))
        con.commit()
    return changed


def load_artist_info(db_file) -> dict[str, dict]:
    """artist_key(name) -> {"name", "folder", "thumb", "fanart", "nfo"} from the cache."""
    if not Path(db_file).exists():
        return {}
    con = connect(db_file)
    try:
        out: dict[str, dict] = {}
        for r in con.execute("SELECT * FROM artistinfo"):
            nfo = json.loads(r["nfo"]) if r["nfo"] else None
            info = {"name": (nfo or {}).get("name") or r["name"], "folder": r["folder"],
                    "thumb": r["thumb"], "fanart": r["fanart"], "nfo": nfo}
            out.setdefault(artist_key(r["name"]), info)
            if nfo and nfo.get("name"):
                out.setdefault(artist_key(nfo["name"]), info)
            for n in (r["name"], (nfo or {}).get("name") or ""):     # written exactly: "LISA" is not "LiSA"
                if n:
                    out.setdefault(EXACT + exact_name(n), info)
            for m in [(nfo or {}).get("mbid") or ""] + list((nfo or {}).get("alias_mbids") or []):
                if m:
                    out.setdefault(BY_ID + m.lower(), info)          # by MusicBrainz ID: the surest
        for info in list(out.values()):                  # other spellings ("Same artist as…") find it too
            for other in (info.get("nfo") or {}).get("aliases") or []:
                out.setdefault(artist_key(other), info)
                out.setdefault(EXACT + exact_name(other), info)
        return out
    finally:
        con.close()


# ---------------------------------------------------------------- scanning

def _pick(names: dict, wanted) -> str | None:
    for n in wanted:
        if n in names:
            return names[n]
    return None


def _walk(root: str, cancelled, skip=()):
    audio: dict[str, tuple] = {}     # path -> (dir, mtime, size)
    nfos: dict[str, tuple] = {}      # path -> (dir, kind, mtime)
    dirs: dict[str, tuple] = {}      # dir -> (cover, thumb, fanart)
    walked: set[str] = set()
    failed: list[str] = []
    seen: set[tuple] = set()
    for s_ in skip:                     # e.g. the Artist Info folder: indexed separately
        try:
            st_ = os.stat(s_)
            seen.add((st_.st_dev, st_.st_ino))
        except OSError:
            pass
    stack = [root]
    while stack:
        if cancelled():
            raise Cancelled
        d = stack.pop()
        try:
            st = os.stat(d)
            ident = (st.st_dev, st.st_ino)
            if ident in seen:            # symlink loop / duplicate mount
                continue
            seen.add(ident)
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            if d == root:
                raise
            failed.append(d)
            continue
        walked.add(d)
        images: dict[str, str] = {}
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=True):
                    stack.append(e.path)
                    continue
                if not e.is_file(follow_symlinks=True):
                    continue
                low = e.name.lower()
                stem, ext = os.path.splitext(low)
                if ext in nfolib.AUDIO_EXTS:
                    s = e.stat()
                    audio[e.path] = (d, s.st_mtime, s.st_size)
                elif low in ("album.nfo", "artist.nfo"):
                    s = e.stat()
                    nfos[e.path] = (d, low[:-4], s.st_mtime)
                elif ext in nfolib.IMAGE_EXTS and stem not in images:
                    images[stem] = e.path
            except OSError:
                continue
        if images:
            row = (_pick(images, nfolib.COVER_NAMES), _pick(images, nfolib.ARTIST_NAMES),
                   _pick(images, nfolib.FANART_NAMES))
            if any(row):
                dirs[d] = row
    return audio, nfos, dirs, walked, failed


def _under(path: str, folders: list[str]) -> bool:
    return any(path == f or path.startswith(f.rstrip(os.sep) + os.sep) for f in folders)


def _extract_to_cache(job) -> tuple[str, str]:
    key, source = job
    data = nfolib.extract_cover(source)
    if not data:
        return key, ""
    out = art_dir() / f"{key}{nfolib.image_ext(data)}"
    try:
        out.write_bytes(data)
    except OSError:
        return key, ""
    return key, str(out)


def scan(root, db_file=None, progress: Callable[[int, int, str], None] | None = None,
         cancelled: Callable[[], bool] | None = None, full: bool = False, workers: int = 8,
         artist_info=None) -> dict:
    """Bring the cache up to date with the music folder. Returns stats.

    Safe on network shares: if the folder can't be read (share not mounted) nothing is deleted,
    and files under sub-folders that failed to list are kept.
    """
    root = str(Path(root).expanduser())
    db_file = db_file or db_path_for(root)
    progress = progress or (lambda *_: None)
    cancelled = cancelled or (lambda: False)
    t0 = time.time()

    progress(0, 0, "Looking for music")
    try:
        audio, nfos, dirs, walked, failed = _walk(root, cancelled, skip=[str(artist_info)] if artist_info else [])
    except Cancelled:
        return {"cancelled": True, "changed": False}
    except OSError as e:
        return {"error": f"Can't read {root} ({e.strerror or e}). Is the network share connected?",
                "changed": False}

    con = connect(db_file)
    try:
        known = {r["path"]: (r["mtime"], r["size"]) for r in con.execute("SELECT path, mtime, size FROM files")}
        if not audio and not nfos and known:
            return {"error": f"No music found in {root}. Is the network share connected?", "changed": False}

        removed = [p for p in known if p not in audio and not _under(p, failed)]
        todo = [p for p, (_d, m, s) in audio.items() if full or known.get(p) != (m, s)]
        added = sum(1 for p in todo if p not in known)

        # ---- tags (parallel: network latency, not CPU, is the bottleneck)
        total = len(todo)
        done = 0
        if todo:
            progress(0, total, "Reading tags")
            batch = []
            ex = ThreadPoolExecutor(max_workers=workers)
            try:
                for path, tags in zip(todo, ex.map(nfolib.read_tags, todo)):
                    if cancelled():
                        raise Cancelled
                    d, m, s = audio[path]
                    batch.append((path, d, m, s, json.dumps(tags)))
                    done += 1
                    if len(batch) >= 200:
                        con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", batch)
                        con.commit()
                        batch.clear()
                    if done % 20 == 0 or done == total:
                        progress(done, total, "Reading tags")
                if batch:
                    con.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", batch)
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        if removed:
            con.executemany("DELETE FROM files WHERE path=?", [(p,) for p in removed])
            con.executemany("DELETE FROM first_seen WHERE path=?", [(p,) for p in removed])
        # when each file joined the library ("Recently added"). Files already known when this was
        # introduced, and everything on a first scan, use the file's own date instead of "now".
        seen = {r[0] for r in con.execute("SELECT path FROM first_seen")}
        now = time.time()
        fresh = [(p, audio[p][1] if (not seen or p in known) else now) for p in audio if p not in seen]
        if fresh:
            con.executemany("INSERT OR IGNORE INTO first_seen VALUES (?, ?)", fresh)
        con.commit()

        # ---- NFO files
        known_nfo = {r["path"]: r["mtime"] for r in con.execute("SELECT path, mtime FROM nfos")}
        parser = con.execute("SELECT value FROM meta WHERE key='nfo_parser'").fetchone()
        reparse = parser is None or parser["value"] != NFO_PARSER_VERSION
        if reparse:
            known_nfo = {}                       # re-read them all once (Artist Info too, below)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('nfo_parser', ?)", (NFO_PARSER_VERSION,))
        nfo_changed = 0
        for path, (d, kind, m) in nfos.items():
            if not full and known_nfo.get(path) == m:
                continue
            data = nfolib.parse_album_nfo(Path(path)) if kind == "album" else nfolib.parse_artist_nfo(Path(path))
            con.execute("INSERT OR REPLACE INTO nfos VALUES (?,?,?,?,?)",
                        (path, d, kind, m, json.dumps(data) if data else None))
            nfo_changed += 1
        gone_nfo = [p for p in known_nfo if p not in nfos and not _under(p, failed)]
        con.executemany("DELETE FROM nfos WHERE path=?", [(p,) for p in gone_nfo])

        # ---- folder artwork
        old_dirs = {r["dir"]: (r["cover"], r["thumb"], r["fanart"]) for r in con.execute("SELECT * FROM dirs")}
        dirs_changed = sum(1 for d, row in dirs.items() if old_dirs.get(d) != row) + \
            sum(1 for d in old_dirs if d not in dirs and d in walked)
        con.executemany("INSERT OR REPLACE INTO dirs VALUES (?,?,?,?)", [(d, *row) for d, row in dirs.items()])
        con.executemany("DELETE FROM dirs WHERE dir=?", [(d,) for d in old_dirs if d not in dirs and d in walked])
        con.commit()

        # ---- embedded cover art: once per album per folder, only where there's no single-album cover file
        rows = con.execute("SELECT path, dir, mtime, tags FROM files").fetchall()
        groups: dict[tuple, list] = {}
        for r in rows:
            tags = json.loads(r["tags"])
            groups.setdefault((r["dir"], album_key(tags, r["dir"])), []).append(r)
        albums_per_dir = Counter(d for d, _k in groups)
        old_emb = {r["key"]: (r["source"], r["source_mtime"], r["art"]) for r in con.execute("SELECT * FROM embedded")}
        jobs, keep = [], set()
        for (d, akey), rs in groups.items():
            if dirs.get(d, (None,))[0] and albums_per_dir[d] == 1:
                continue                          # a cover.jpg/folder.jpg covers it
            key = embedded_key(d, akey)
            keep.add(key)
            src = min(rs, key=lambda r: r["path"])
            old = old_emb.get(key)
            if not full and old and old[0] == src["path"] and old[1] == src["mtime"] \
                    and (not old[2] or Path(old[2]).exists()):
                continue
            jobs.append((key, src["path"], src["mtime"], d))
        if jobs:
            progress(0, len(jobs), "Collecting cover art")
            ex = ThreadPoolExecutor(max_workers=workers)
            try:
                results = ex.map(_extract_to_cache, [(k, p) for k, p, _m, _d in jobs])
                for n, ((key, src, m, d), (_k, art)) in enumerate(zip(jobs, results), 1):
                    if cancelled():
                        raise Cancelled
                    con.execute("INSERT OR REPLACE INTO embedded VALUES (?,?,?,?,?)", (key, d, src, m, art))
                    if n % 10 == 0 or n == len(jobs):
                        progress(n, len(jobs), "Collecting cover art")
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        stale = [k for k in old_emb if k not in keep and not _under(old_emb[k][0] or "", failed)]
        for k in stale:
            art = old_emb[k][2]
            if art:
                try:
                    Path(art).unlink()
                except OSError:
                    pass
        con.executemany("DELETE FROM embedded WHERE key=?", [(k,) for k in stale])
        info_changed = _scan_artist_info(con, str(artist_info) if artist_info else None, full or reparse)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)", (str(time.time()),))
        con.commit()
    except Cancelled:
        con.commit()
        return {"cancelled": True, "changed": True}
    finally:
        con.close()

    changed = bool(todo or removed or nfo_changed or gone_nfo or dirs_changed or jobs or stale or info_changed)
    return {"changed": changed, "added": added, "updated": len(todo) - added, "removed": len(removed),
            "files": len(audio), "unreadable_folders": len(failed), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- building the model

def _most_common(values) -> str:
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else ""


_ARTICLES = ("the ", "a ", "an ")


def sort_name(name: str) -> str:
    """Sort key like Jellyfin's: case-insensitive, ignoring leading punctuation and a leading
    "The", "A" or "An" ("...Baby One More Time" -> b, "The Pretty Reckless" -> p)."""
    n = (name or "").casefold().strip()
    i = 0
    while i < len(n) and not n[i].isalnum():
        i += 1
    n = n[i:] or n
    for a in _ARTICLES:
        if n.startswith(a) and len(n) > len(a):
            n = n[len(a):].lstrip()
            break
    return n


def index_letter(name: str) -> str:
    """A–Z bucket for the sidebar jump bar ("The Beatles" -> B, "Angèle" -> A, "방찬" -> #)."""
    import unicodedata
    n = sort_name(name).lstrip(" '\"([{.-_")
    if not n:
        return "#"
    c = unicodedata.normalize("NFKD", n[0])[0].upper()
    return c if "A" <= c <= "Z" else "#"


def build_library(db_file, root=None, artist_info: dict | None = None) -> list[nfolib.Artist]:
    """Cache -> Artist/Album/Track objects. Reads only the database."""
    if not Path(db_file).exists():
        return []
    if artist_info is None:
        artist_info = load_artist_info(db_file)
    con = connect(db_file)
    try:
        files = [(r["path"], r["dir"], json.loads(r["tags"])) for r in con.execute("SELECT path, dir, tags FROM files")]
        dirs = {r["dir"]: (r["cover"], r["thumb"], r["fanart"]) for r in con.execute("SELECT * FROM dirs")}
        embedded = {r["key"]: r["art"] for r in con.execute("SELECT key, art FROM embedded")}
        album_nfos, artist_nfos = {}, {}
        for r in con.execute("SELECT dir, kind, data FROM nfos WHERE data IS NOT NULL"):
            (album_nfos if r["kind"] == "album" else artist_nfos)[r["dir"]] = json.loads(r["data"])
    finally:
        con.close()
    root = str(Path(root).expanduser()) if root else None

    # ---- tracks -> albums (by tags)
    groups: dict[tuple, list] = {}
    for path, d, tags in files:
        groups.setdefault(album_key(tags, d), []).append((path, d, tags))

    albums: list[nfolib.Album] = []
    album_mb_artist: dict[int, str] = {}
    dir_album_count = Counter()
    for rs in groups.values():
        for d in {d for _p, d, _t in rs}:
            dir_album_count[d] += 1
    used_nfo_dirs = set()

    for akey, rs in groups.items():
        track_dirs = sorted({d for _p, d, _t in rs})
        folder = os.path.commonpath(track_dirs) if len(track_dirs) > 1 else track_dirs[0]
        tags_list = [t for _p, _d, t in rs]
        discs = {t.get("disc") or 1 for t in tags_list}
        tracks = []
        for path, _d, t in rs:
            n = t.get("track")
            disc = t.get("disc") or 1
            if n is not None and len(discs) > 1 and disc > 1:
                n = disc * 100 + n
            tracks.append(nfolib.Track(path=Path(path), number=n, title=t.get("title") or Path(path).stem,
                                       duration=t.get("duration"), artists=list(t.get("artists") or []),
                                       artist_ids=list(t.get("artist_ids") or [])))
        tracks.sort(key=lambda tr: (tr.number if tr.number is not None else 10**6, tr.path.name.casefold()))

        title = _most_common(t.get("album") for t in tags_list) or Path(folder).name
        artist = (_most_common(t.get("albumartist") for t in tags_list)
                  or ("Various Artists" if any(t.get("compilation") for t in tags_list) else "")
                  or _most_common((t.get("artists") or [""])[0] for t in tags_list)
                  or (Path(folder).parent.name if root and Path(folder).parent != Path(root) else "")
                  or "Unknown Artist")
        genres: list[str] = []
        for t in tags_list:
            for g in t.get("genres") or []:
                if g.casefold() not in (x.casefold() for x in genres):
                    genres.append(g)
        album_artist_mbid = _most_common(t.get("mb_albumartistid") for t in tags_list)
        album = nfolib.Album(
            folder=Path(folder), title=title, artist=artist,
            release_date=min((t.get("originaldate") or t.get("date") or "" for t in tags_list), key=lambda s: s or "9999"),
            label=_most_common(t.get("label") for t in tags_list),
            genres=genres[:8],
            mbid=_most_common(t.get("mb_releasegroupid") for t in tags_list),
            mb_release=_most_common(t.get("mb_albumid") for t in tags_list),
            kind=_most_common(t.get("releasetype") for t in tags_list),
            tracks=tracks,
        )
        # cover: folder image (if the folder holds only this album) > embedded art
        for d in [folder] + track_dirs:
            if dirs.get(d, (None,))[0] and dir_album_count.get(d, 0) <= 1:
                album.cover = Path(dirs[d][0])
                break
        if album.cover is None:
            for d in track_dirs:
                art = embedded.get(embedded_key(d, akey))
                if art:
                    album.cover = Path(art)
                    break
        # NFO is king
        nfo_dir = next((d for d in [folder] + track_dirs if d in album_nfos), None)
        if nfo_dir is not None:
            data = album_nfos[nfo_dir]
            single = dir_album_count.get(nfo_dir, 0) <= 1
            ids = {t.get("mb_albumid") for t in tags_list} | {album.mbid}
            if single or (data.get("mbid") and data["mbid"] in ids) or \
                    (data.get("title") or "").casefold() == album.title.casefold():
                nfolib.apply_album_nfo(album, data)
                used_nfo_dirs.add(nfo_dir)
        album_mb_artist[id(album)] = album_artist_mbid
        albums.append(album)

    # album.nfo folders without audio (still worth showing: review, cover)
    for d, data in album_nfos.items():
        if d in used_nfo_dirs or dir_album_count.get(d):
            continue
        al = nfolib.album_from_nfo(Path(d), data)
        if dirs.get(d, (None,))[0]:
            al.cover = Path(dirs[d][0])
        if not al.artist:
            parent = Path(d).parent
            al.artist = (artist_nfos.get(str(parent)) or {}).get("name") or parent.name
        albums.append(al)

    # ---- other spellings of the same artist ("Same artist as…" in the Importer) use the kept name
    aliases = alias_map(artist_info)
    if aliases:
        for al in albums:
            al.artist = aliases.get(name_key(al.artist), al.artist)
            for t in al.tracks:
                t.artists = [aliases.get(name_key(n), n) for n in t.artists]

    # ---- collaborations ("Anyma & LISA"): filed under the first artist, shared with the others
    owner: dict[int, str] = {}                            # id(album) -> the artist it's filed under
    shared_with: dict[str, list[nfolib.Album]] = {}       # name_key -> albums they share
    shared_name: dict[str, str] = {}
    split = collaboration_splitter(albums, artist_pages(db_file), aliases)
    for al in albums:
        names = split(al)
        owner[id(al)] = names[0] if names else (al.artist or "Unknown Artist")
        for n in names[1:]:
            shared_with.setdefault(name_key(n), []).append(al)
            shared_name.setdefault(name_key(n), n)

    # ---- albums -> artists
    by_artist: dict[str, list[nfolib.Album]] = {}
    for al in albums:
        by_artist.setdefault(name_key(owner[id(al)]), []).append(al)
    for k in shared_with:                                 # someone with only shared albums still gets a page
        by_artist.setdefault(k, [])
    nfo_by_name = {name_key(d.get("name") or ""): (folder, d) for folder, d in artist_nfos.items() if d.get("name")}

    artists: list[nfolib.Artist] = []
    for key, als in by_artist.items():
        name = _most_common(owner[id(al)] for al in als) or shared_name.get(key) or "Unknown Artist"
        # a collaboration's folder ("Anyma & LISA/…") isn't the first artist's own folder
        parents = sorted({str(al.folder.parent) for al in als if al.folder is not None
                          and name_key(owner[id(al)]) == name_key(al.artist or "")})
        nfo_folder = next((p for p in parents if p in artist_nfos), None)
        data = artist_nfos.get(nfo_folder) if nfo_folder else None
        if data is None and key in nfo_by_name:
            nfo_folder, data = nfo_by_name[key]
        folder = nfo_folder or (parents[0] if len(parents) == 1 and parents[0] != root else None)
        a = nfolib.Artist(name=name, folder=Path(folder) if folder else None,
                          albums=sorted(als, key=lambda al: (al.release_date or "9999", al.title.casefold())))
        a.mbid = _most_common(album_mb_artist.get(id(al), "") for al in als)   # from Picard's tags
        info = info_for_name(artist_info, name) or (
            info_for_name(artist_info, data["name"]) if data and data.get("name") else None)
        # artist.nfo: every artist's page lives in the Artist Info folder, so that copy wins; one
        # still left in a music folder only fills in what it leaves out
        if data:
            nfolib.apply_artist_nfo(a, data)
        if info and info.get("nfo"):
            nfolib.apply_artist_nfo(a, info["nfo"])
        if folder and folder in dirs:
            _cover, thumb, fanart = dirs[folder]
            # in an artist folder, folder.jpg is the artist photo
            a.image = Path(thumb) if thumb else None
            a.fanart = Path(fanart) if fanart else None
        # Artist Info pictures win over the music folder's
        if info:
            if info.get("thumb"):
                a.image = Path(info["thumb"])
            if info.get("fanart"):
                a.fanart = Path(info["fanart"])
        a.shared = list(shared_with.get(key, []))
        artists.append(a)
    artists = merge_artists(artists)
    for a in artists:
        # genres, the Apple Music way: the ones most of their albums have (up to three, most first);
        # the artist.nfo's own list only when no album has a genre
        count: Counter = Counter()
        spelled: dict[str, str] = {}                 # casefolded -> how it's written (first seen)
        for al in reversed(a.releases):               # newest first, so ties go to the newest album's genre
            if al.genres:
                k = al.genres[0].casefold()
                count[k] += 1
                spelled.setdefault(k, al.genres[0])
        if count:
            order = list(spelled)
            a.genres = [spelled[k] for k in sorted(count, key=lambda k: (-count[k], order.index(k)))][:3]
        else:
            a.genres = list(a.genres[:3])
        # no artist.nfo values: gather them from the albums
        for attr, cap in (("moods", 6),):
            if not getattr(a, attr):
                seen: list[str] = []
                for al in a.releases:
                    for m in getattr(al, attr):
                        if m.casefold() not in (x.casefold() for x in seen):
                            seen.append(m)
                setattr(a, attr, seen[:cap])
    artists.sort(key=lambda a: sort_name(a.name))
    canonical_credits(albums, artist_pages(db_file), artists)
    return artists


def canonical_credits(albums, pages: list[dict], artists) -> None:
    """The names under songs, decided by MusicBrainz IDs first:
      - a credited artist with an ID shows the name on their Artist Info page (or their album-artist
        name), so a tag like "LiSA (Japanese singer)" shows as her page's name;
      - spellings that are one artist (one ID, or no IDs and only capitals / spaces differ) become one;
      - two artists whose names only differ in capitals ("LISA", "LiSA") stay two, by their IDs, and
        if they'd be spelled exactly alike, the page's note tells them apart ("LISA (Thai rapper)").
    Changes track.artists in place."""
    by_id: dict[str, str] = {}
    note: dict[str, str] = {}
    for nfo in pages:
        n, m = nfo.get("name") or "", (nfo.get("mbid") or "").lower()
        if n and m:
            by_id.setdefault(m, n)
            note.setdefault(m, nfo.get("disambiguation") or "")
        for x in nfo.get("alias_mbids") or []:
            if n and x:
                by_id.setdefault(x.lower(), n)
    for a in artists:
        if a.mbid and a.name:
            by_id.setdefault(a.mbid.lower(), a.name)
    credits = []                                          # (track, position, name, id)
    for al in albums:
        for t in al.tracks:
            ids = t.artist_ids if len(t.artist_ids) == len(t.artists) else [""] * len(t.artists)
            for i, (n, m) in enumerate(zip(t.artists, ids)):
                m = (m or "").lower()
                credits.append((t, i, by_id.get(m, n) if m else n, m))
    groups: dict[str, list] = {}
    for c in credits:
        groups.setdefault(name_key(c[2]), []).append(c)
    for cs in groups.values():
        ids = {m for *_x, m in cs if m}
        if len(ids) <= 1:                                 # one artist: one spelling
            m = next(iter(ids), "")
            name = by_id.get(m) if m in by_id else Counter(c[2] for c in cs).most_common(1)[0][0]
            for t, i, _n, _m in cs:
                t.artists[i] = name
            continue
        per_id = {m: by_id.get(m) or Counter(c[2] for c in cs if c[3] == m).most_common(1)[0][0] for m in ids}
        same = Counter(exact_name(v) for v in per_id.values())
        for m, v in list(per_id.items()):
            if same[exact_name(v)] > 1:
                per_id[m] = f"{v} ({note.get(m) or m[:8]})"
        for t, i, n, m in cs:
            t.artists[i] = per_id[m] if m else n


def merge_artists(artists: list[nfolib.Artist]) -> list[nfolib.Artist]:
    """One entry per artist. Album-artist tags can spell a name several ways ("DPR LIVE",
    "DPR LIVE " , full-width letters) that the same artist.nfo / Artist Info folder then
    names identically; those become one artist. Same MusicBrainz ID: same artist. Same name
    but different MusicBrainz IDs: kept apart (two different artists)."""
    out: list[nfolib.Artist] = []
    by_mbid: dict[str, nfolib.Artist] = {}
    by_name: dict[str, list[nfolib.Artist]] = {}
    for a in artists:
        target = by_mbid.get(a.mbid) if a.mbid else None
        if target is None:
            for b in by_name.get(name_key(a.name), []):
                if not a.mbid or not b.mbid or a.mbid == b.mbid:
                    target = b
                    break
        if target is None:
            out.append(a)
            by_name.setdefault(name_key(a.name), []).append(a)
            if a.mbid:
                by_mbid[a.mbid] = a
            continue
        # fold a into target: keep target's details, fill what it lacks
        target.albums = sorted(target.albums + a.albums,
                               key=lambda al: (al.release_date or "9999", al.title.casefold()))
        target.shared = target.shared + [al for al in a.shared if all(al is not x for x in target.shared)]
        for f in ("mbid", "biography", "image", "fanart", "folder"):
            if not getattr(target, f) and getattr(a, f):
                setattr(target, f, getattr(a, f))
        for f in ("genres", "moods"):
            if not getattr(target, f) and getattr(a, f):
                setattr(target, f, list(getattr(a, f)))
        target.ids = {**a.ids, **target.ids}
        target.links = target.links + [l for l in a.links if l not in target.links]
        target.has_nfo = target.has_nfo or a.has_nfo
        if target.mbid:
            by_mbid[target.mbid] = target
    return out


# ---------------------------------------------------------------- listening activity

def record_play(db_file, path, ts: float | None = None):
    """Count a play (the app calls this once a song has been listened to for long enough)."""
    con = connect(db_file)
    try:
        con.execute("INSERT INTO plays VALUES (?, ?)", (str(path), ts or time.time()))
        con.commit()
    finally:
        con.close()


def load_activity(db_file, recent_limit: int = 3000) -> dict:
    """{"recent": [(path, ts)] newest first, "counts": {path: plays}, "first_seen": {path: ts}}"""
    if not Path(db_file).exists():
        return {"recent": [], "counts": {}, "first_seen": {}}
    con = connect(db_file)
    try:
        recent = [(r[0], r[1]) for r in
                  con.execute("SELECT path, ts FROM plays ORDER BY ts DESC LIMIT ?", (recent_limit,))]
        counts = {r[0]: r[1] for r in con.execute("SELECT path, COUNT(*) FROM plays GROUP BY path")}
        first = {r[0]: r[1] for r in con.execute("SELECT path, ts FROM first_seen")}
        return {"recent": recent, "counts": counts, "first_seen": first}
    finally:
        con.close()
