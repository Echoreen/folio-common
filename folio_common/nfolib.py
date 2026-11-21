"""Data model, NFO parsing and audio tag reading for Folio. No Qt.

Metadata precedence: album.nfo / artist.nfo  >  file tags  >  folder / file names.
"""
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import folio_nfo as fn

AUDIO_EXTS = {".flac", ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac", ".wav", ".wv", ".ape",
              ".alac", ".aiff", ".aif", ".dsf", ".wma"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartsmall")
ARTIST_NAMES = ("thumb", "artist", "folder", "poster")
FANART_NAMES = ("fanart", "backdrop", "background")

try:  # tag reading; without it, titles/numbers come from file names
    import mutagen  # type: ignore
except Exception:  # pragma: no cover
    mutagen = None


# ---------------------------------------------------------------- model

@dataclass
class Track:
    path: Path
    number: int | None          # disc 2 track 3 -> 203
    title: str
    duration: float | None = None
    artists: list[str] = field(default_factory=list)   # empty = album artist
    artist_ids: list[str] = field(default_factory=list)  # their MusicBrainz IDs, same order ("" = unknown)


@dataclass
class Album:
    folder: Path | None
    title: str
    artist: str = ""
    review: str = ""
    release_date: str = ""
    label: str = ""
    moods: list[str] = field(default_factory=list)      # NFO <style>/<mood>
    genres: list[str] = field(default_factory=list)     # NFO <genre>, else the tracks' genre tags
    critic_rating: int | None = None
    community_rating: float | None = None
    community_max: float = 10
    mbid: str = ""                                      # MusicBrainz release group
    mb_release: str = ""                                # MusicBrainz release (a specific edition)
    kind: str = ""                                      # "Album", "EP", "Live Album", …
    ids: dict = field(default_factory=dict)             # other sites: {"spotify": "…", "applemusic": "…"}
    links: list = field(default_factory=list)           # [(name, url)] from <url name="…">
    has_nfo: bool = False
    cover: Path | None = None
    tracks: list[Track] = field(default_factory=list)
    credits: list = field(default_factory=list)          # [(name, MusicBrainz ID)] from album.nfo

    @property
    def year(self) -> str:
        return self.release_date[:4]

    @property
    def duration(self) -> float | None:
        ds = [t.duration for t in self.tracks]
        return sum(ds) if ds and all(d is not None for d in ds) else None


@dataclass
class Artist:
    name: str
    folder: Path | None = None
    biography: str = ""
    moods: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    mbid: str = ""
    ids: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    has_nfo: bool = False
    image: Path | None = None       # thumb.jpg etc.
    fanart: Path | None = None
    albums: list[Album] = field(default_factory=list)
    shared: list[Album] = field(default_factory=list)    # collaborations filed under the first artist named

    @property
    def releases(self) -> list[Album]:
        """Their albums plus collaborations they share ("Anyma & LISA" for LISA), oldest first."""
        if not self.shared:
            return self.albums
        return sorted(self.albums + self.shared, key=lambda al: (al.release_date or "9999", al.title.casefold()))

    @property
    def latest_cover(self) -> Path | None:
        for al in reversed(self.releases):
            if al.cover:
                return al.cover
        return None


def all_moods(artists: list[Artist]) -> list[str]:
    return sorted({m for a in artists for al in a.albums for m in al.moods}
                  | {m for a in artists for m in a.moods}, key=str.casefold)


def all_genres(artists: list[Artist]) -> list[str]:
    return sorted({g for a in artists for al in a.albums for g in al.genres}
                  | {g for a in artists for g in a.genres}, key=str.casefold)


def all_labels(artists: list[Artist]) -> list[str]:
    return sorted({al.label for a in artists for al in a.albums if al.label}, key=str.casefold)


# ---------------------------------------------------------------- NFO parsing (-> plain dicts, cacheable as JSON)

def _load(path: Path, cls):
    """Read an NFO with folio_nfo (tolerates BOMs, odd encodings, trailing URL lines,
    tag-name case); None if it isn't that kind of NFO."""
    try:
        return cls.read(path)
    except (fn.NfoError, OSError):
        return None


def _text(root: ET.Element, tag: str) -> str:
    return (root.findtext(tag) or "").strip()


def _texts(root: ET.Element, tag: str) -> list[str]:
    return [e.text.strip() for e in root.findall(tag) if e.text and e.text.strip()]


def _num(s, cast):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


def _duration(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    if ":" in s:
        try:
            parts = [float(x) for x in s.split(":")]
        except ValueError:
            return None
        total = 0.0
        for x in parts:
            total = total * 60 + x
        return total
    return _num(s, float)


def _ids_links(root: ET.Element) -> tuple[dict, list]:
    """<uniqueid type="spotify">…</uniqueid> and <url name="Bandcamp">https://…</url> (any site)."""
    ids = {}
    for u in root.findall("uniqueid"):
        if u.text and u.text.strip():
            ids[(u.get("type") or "").strip().lower().replace(" ", "").replace("_", "")] = u.text.strip()
    links = []
    for u in root.findall("url"):
        url = (u.text or "").strip()
        if url.startswith(("http://", "https://")):
            links.append([(u.get("name") or "").strip() or re.sub(r"^https?://(www\.)?", "", url).split("/")[0], url])
    return ids, links


def parse_artist_nfo(path: Path) -> dict | None:
    a = _load(path, fn.ArtistNfo)
    if a is None:
        return None
    ids, links = _ids_links(a.element)
    return {
        "name": a.name,
        "biography": a.biography,
        "moods": a.styles + a.moods,
        "genres": a.genres,
        "mbid": a.mbid,
        "ids": ids,
        "links": links,
        "aliases": list(a.aliases),
        "alias_mbids": list(a.alias_mbids),
        "disambiguation": a.disambiguation,
    }


def parse_album_nfo(path: Path) -> dict | None:
    al = _load(path, fn.AlbumNfo)
    if al is None:
        return None
    root = al.element
    r = next((x for x in al.ratings.values() if x.default), next(iter(al.ratings.values()), None))
    community, cmax = (r.value, r.max) if r else (None, 10.0)
    tracks = []
    for t in root.findall("track"):
        pos = _num(_text(t, "position"), int)
        title = _text(t, "title")
        if pos is not None and title:
            tracks.append({"position": pos, "title": title, "duration": _duration(_text(t, "duration"))})
    ids, links = _ids_links(root)
    return {
        "title": al.title,
        "artist": al.text("artist") or al.text("albumartist"),
        "review": al.review,
        "release_date": al.release_date or al.original_release_date or al.text("year"),
        "label": al.text("studio") or al.label,
        "moods": al.styles + al.moods,
        "genres": al.genres,
        "critic_rating": _num(al.text("criticrating"), int),
        "community_rating": community,
        "community_max": cmax,
        "mbid": al.mb_release_group_id,
        "mb_release": al.mb_album_id,
        "ids": ids,
        "links": links,
        "kind": al.release_type or al.type,
        "tracks": tracks,
        "credits": [[c.name, c.mbid] for c in al.credits],
    }


def apply_album_nfo(album: Album, d: dict):
    """NFO is king: every value the NFO provides replaces what came from tags."""
    album.has_nfo = True
    album.ids.update(d.get("ids") or {})
    if d.get("links"):
        album.links = [tuple(x) for x in d["links"]]
    for key in ("title", "artist", "review", "release_date", "label", "mbid", "mb_release"):
        if d.get(key):
            setattr(album, key, d[key])
    if d.get("kind"):
        kind = format_release_type([d["kind"]])
        # album.nfo keeps only the main type ("Album", "EP"); the songs' tags may also say "live" or
        # "compilation" ("Live Album", "Compilation"): keep that fuller answer when it agrees
        tag_kind = album.kind or ""
        if not (tag_kind and tag_kind != kind and (tag_kind.endswith(kind) or kind == "Album")):
            album.kind = kind
    if d.get("credits"):
        album.credits = [tuple(c) for c in d["credits"]]
    if d.get("moods"):
        album.moods = list(d["moods"])
    if d.get("genres"):
        album.genres = list(d["genres"])
    for key in ("critic_rating", "community_rating"):
        if d.get(key) is not None:
            setattr(album, key, d[key])
    if d.get("community_rating") is not None:
        album.community_max = d.get("community_max") or 10.0
    by_pos = {t["position"]: t for t in d.get("tracks") or []}
    if by_pos:
        for t in album.tracks:
            n = t.number % 100 if t.number is not None and t.number >= 100 else t.number
            if t.number is not None and t.number < 200 and n in by_pos:
                t.title = by_pos[n]["title"]
                if by_pos[n]["duration"] and not t.duration:
                    t.duration = by_pos[n]["duration"]


def album_from_nfo(folder: Path, d: dict) -> Album:
    al = Album(folder=folder, title=d.get("title") or folder.name)
    apply_album_nfo(al, d)
    return al


def apply_artist_nfo(artist: Artist, d: dict):
    artist.has_nfo = True
    if d.get("name"):
        artist.name = d["name"]
    if d.get("biography"):
        artist.biography = d["biography"]
    if d.get("moods"):
        artist.moods = list(d["moods"])
    if d.get("genres"):
        artist.genres = list(d["genres"])
    if d.get("mbid"):
        artist.mbid = d["mbid"]
    artist.ids.update(d.get("ids") or {})
    if d.get("links"):
        artist.links = [tuple(x) for x in d["links"]]


# ---------------------------------------------------------------- names / small helpers

_NUM_PREFIX = re.compile(r"^\s*(?:(\d)[-.](?=\d))?(\d{1,3})(?!\d)\s*[-._)\]]*\s*(.*)$")
_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+|\s*;\s*", re.I)

_SECONDARY = {"compilation": "Compilation", "soundtrack": "Soundtrack", "spokenword": "Spoken Word",
              "interview": "Interview", "audiobook": "Audiobook", "live": "Live", "remix": "Remix",
              "dj-mix": "DJ Mix", "mixtape/street": "Mixtape", "demo": "Demo", "audio drama": "Audio Drama"}
_PRIMARY = {"album": "Album", "single": "Single", "ep": "EP", "broadcast": "Broadcast", "other": "Release"}


def title_from_filename(stem: str) -> tuple[int | None, str]:
    m = _NUM_PREFIX.match(stem)
    if m and m.group(3):
        disc, num, rest = m.groups()
        n = int(num) + (int(disc) * 100 if disc else 0)
        return n, rest.strip(" -_") or stem
    return None, stem


def pair_artist_ids(names: list[str], multi: list[str], raw_ids: list[str]) -> tuple[list[str], list[str]]:
    """(names, IDs in the same order). `names` come from the ARTIST line split up, `multi` from the
    ARTISTS tag (one name per value), `raw_ids` from MUSICBRAINZ_ARTISTID. IDs are only kept when they
    line up one-to-one with the names: a wrong pairing is worse than none."""
    ids = [x.strip() for x in raw_ids or [] if x.strip()]
    if len(ids) == 1 and re.search(r"[/;]", ids[0]):
        ids = [x for x in re.split(r"\s*[/;]\s*", ids[0]) if x]
    multi = [x.strip() for x in multi or [] if x.strip()]
    if ids and len(multi) == len(ids):
        return multi, ids
    if ids and len(names) == len(ids):
        return list(names), ids
    return list(names), []


def split_artists(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for part in _FEAT.split(str(v)):
            part = part.strip()
            if part and part.casefold() not in (o.casefold() for o in out):
                out.append(part)
    return out


def split_multi(values) -> list[str]:
    """Genre-style multi-values: ["Metal; Metalcore"] -> ["Metal", "Metalcore"], de-duplicated."""
    out: list[str] = []
    for v in values or []:
        for part in re.split(r"\s*;\s*", str(v)):
            part = part.strip()
            if part and part.casefold() not in (o.casefold() for o in out):
                out.append(part.title() if part.islower() else part)
    return out


def format_release_type(values) -> str:
    """Picard 'releasetype' (e.g. ["album", "live"]) -> "Live Album"."""
    parts = [p.strip().lower() for v in values or [] for p in re.split(r"[;,/]\s*|\s+", str(v)) if p.strip()]
    # keep "mixtape/street" and "dj-mix" intact
    joined = " ".join(str(v).lower() for v in values or [])
    if "mixtape/street" in joined:
        parts.append("mixtape/street")
    primary = next((_PRIMARY[p] for p in parts if p in _PRIMARY), "")
    secondary = next((_SECONDARY[p] for p in parts if p in _SECONDARY), "")
    if secondary and primary and primary != "Album":
        return f"{secondary} {primary}"
    if secondary:
        return secondary if secondary in ("Compilation", "Soundtrack", "Mixtape", "DJ Mix", "Demo", "Remix",
                                          "Interview", "Audiobook", "Spoken Word", "Audio Drama") \
            else f"{secondary} Album"
    return primary


def year_of(s: str) -> str:
    m = re.match(r"\d{4}(-\d\d(-\d\d)?)?", (s or "").strip())
    return m.group(0) if m else ""


# ---------------------------------------------------------------- tag reading

def _first_int(v) -> int | None:
    if v is None:
        return None
    return _num(str(v).split("/")[0].strip(), int)


def read_tags(path) -> dict:
    """Read the tags a music library needs. Never raises.

    Handles Picard's conventions for ID3 (MP3), Vorbis comments (FLAC/Ogg/Opus) and MP4.
    """
    path = Path(path)
    num, ftitle = title_from_filename(path.stem)
    ftrack, fdisc = (num % 100, num // 100) if num is not None and num >= 100 else (num, None)
    out = {"title": ftitle, "artists": [], "album": "", "albumartist": "", "track": ftrack,
           "disc": fdisc, "date": "", "originaldate": "", "genres": [],
           "label": "", "duration": None, "mb_albumid": "", "mb_releasegroupid": "", "mb_albumartistid": "",
           "releasetype": "", "compilation": False, "artist_ids": []}
    if mutagen is None:
        return out
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    if f is None:
        return out
    try:
        length = getattr(f.info, "length", None)
        out["duration"] = float(length) if length else None
    except Exception:
        pass
    tags = f.tags
    if not tags:
        return out

    def get(*keys) -> list[str]:
        for k in keys:
            try:
                v = tags.get(k)
            except Exception:  # EasyID3 raises for unknown keys in some versions
                v = None
            if v:
                return [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
        return []

    def one(*keys) -> str:
        v = get(*keys)
        return v[0].strip() if v else ""

    out["title"] = one("title") or ftitle
    out["artists"] = split_artists(get("artist"))
    out["album"] = one("album")
    out["albumartist"] = one("albumartist", "album artist", "album_artist")
    t = _first_int(one("tracknumber"))
    if t is not None:
        out["track"] = t
    d = _first_int(one("discnumber"))
    if d is not None:
        out["disc"] = d
    out["date"] = year_of(one("date", "year"))
    out["originaldate"] = year_of(one("originaldate", "originalyear"))
    out["genres"] = split_multi(get("genre"))
    out["label"] = one("label", "organization", "publisher")
    out["mb_albumid"] = one("musicbrainz_albumid")
    out["mb_releasegroupid"] = one("musicbrainz_releasegroupid")
    out["mb_albumartistid"] = one("musicbrainz_albumartistid")
    # each credited artist's MusicBrainz ID (Picard and the Importer write them), paired with the names
    # in the same order; when the two lists don't line up, no IDs (a wrong pairing is worse than none)
    out["artists"], out["artist_ids"] = pair_artist_ids(out["artists"], get("artists"), get("musicbrainz_artistid"))
    out["releasetype"] = format_release_type(get("releasetype", "musicbrainz_albumtype"))
    out["compilation"] = one("compilation", "itunescompilation") in ("1", "true", "True")
    return out


def extract_cover(path) -> bytes | None:
    """Embedded front-cover image bytes, if any. Never raises."""
    if mutagen is None:
        return None
    try:
        f = mutagen.File(Path(path))
    except Exception:
        return None
    if f is None:
        return None
    try:
        pics = []
        if hasattr(f, "pictures") and f.pictures:                       # FLAC
            pics = [(p.type, p.data) for p in f.pictures]
        tags = f.tags
        if not pics and tags is not None:
            if hasattr(tags, "getall"):                                 # ID3 (MP3, AIFF, WAV)
                pics = [(getattr(p, "type", 0), p.data) for p in tags.getall("APIC")]
            if not pics and "covr" in tags:                             # MP4
                pics = [(3, bytes(c)) for c in tags["covr"]]
            if not pics and "metadata_block_picture" in tags:           # Ogg Vorbis / Opus
                from mutagen.flac import Picture
                for b64 in tags["metadata_block_picture"]:
                    try:
                        pic = Picture(base64.b64decode(b64))
                        pics.append((pic.type, pic.data))
                    except Exception:
                        continue
        if not pics:
            return None
        front = [d for t, d in pics if t == 3]
        return (front or [d for _t, d in pics])[0] or None
    except Exception:
        return None


def image_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


# ---------------------------------------------------------------- lyrics

_LRC_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_LRC_WORD = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_LRC_OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", re.I)


def parse_lrc(text: str) -> list[tuple[float, str]] | None:
    """LRC -> [(seconds, line)], sorted. Handles several stamps per line, [offset:], and
    word-level <mm:ss.xx> tags (stripped). None if the text has no timestamps."""
    offset = 0.0
    out: list[tuple[float, str]] = []
    for raw in (text or "").splitlines():
        m = _LRC_OFFSET.match(raw)
        if m:
            offset = int(m.group(1)) / 1000.0     # positive offset = lyrics shown earlier
            continue
        raw = raw.strip()
        pos, times = 0, []
        while True:
            m = _LRC_TS.match(raw, pos)
            if not m:
                break
            mins, secs, frac = m.groups()
            t = int(mins) * 60 + int(secs) + (int(frac) / (10 ** len(frac)) if frac else 0.0)
            times.append(t)
            pos = m.end()
        if not times:
            continue
        line = _LRC_WORD.sub("", raw[pos:]).strip()
        for t in times:
            out.append((max(0.0, t - offset), line))
    out.sort(key=lambda x: x[0])
    return out or None


def _embedded_lyrics(path: Path) -> tuple[str | None, list | None]:
    """(plain or LRC text, SYLT synced lines) from the file's tags."""
    if mutagen is None:
        return None, None
    try:
        f = mutagen.File(path)
    except Exception:
        return None, None
    tags = getattr(f, "tags", None) if f is not None else None
    if not tags:
        return None, None
    try:
        if hasattr(tags, "getall"):                                   # ID3
            sylt = tags.getall("SYLT")
            if sylt:
                fr = sylt[0]
                if getattr(fr, "format", 2) == 2:                      # times in milliseconds
                    lines = [(ms / 1000.0, str(txt).strip()) for txt, ms in fr.text]
                    if lines:
                        return None, sorted(lines, key=lambda x: x[0])
            uslt = tags.getall("USLT")
            if uslt:
                return str(uslt[0].text), None
            return None, None
        for key in ("lyrics", "LYRICS", "unsyncedlyrics", "UNSYNCEDLYRICS", "\xa9lyr"):
            try:
                v = tags.get(key)
            except Exception:
                v = None
            if v:
                v = v[0] if isinstance(v, list) else v
                return str(v), None
    except Exception:
        pass
    return None, None


def read_lyrics(path) -> dict | None:
    """{"synced": [(seconds, line)] or None, "plain": str or None, "source": str}, or None.

    Order: song.lrc next to the file, embedded tags, song.txt next to the file.
    Embedded lyrics that contain LRC timestamps count as synced."""
    path = Path(path)
    for ext in (".lrc", ".LRC"):
        side = path.with_suffix(ext)
        try:
            if side.is_file():
                text = side.read_bytes().decode("utf-8-sig", errors="replace")
                synced = parse_lrc(text)
                if synced:
                    return {"synced": synced, "plain": None, "source": side.name}
                if text.strip():
                    return {"synced": None, "plain": text.strip(), "source": side.name}
        except OSError:
            pass
    text, sylt = _embedded_lyrics(path)
    if sylt:
        return {"synced": sylt, "plain": None, "source": "embedded"}
    if text and text.strip():
        synced = parse_lrc(text)
        if synced:
            return {"synced": synced, "plain": None, "source": "embedded"}
        return {"synced": None, "plain": text.strip(), "source": "embedded"}
    for ext in (".txt", ".TXT"):
        side = path.with_suffix(ext)
        try:
            if side.is_file():
                text = side.read_bytes().decode("utf-8-sig", errors="replace").strip()
                if text:
                    return {"synced": None, "plain": text, "source": side.name}
        except OSError:
            pass
    return None
