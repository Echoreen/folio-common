"""Shared machinery for reading and writing Kodi-style NFO files.

Every NFO kind (artist, album, tvshow, season, episode, movie) is a dataclass
with a FIELDS table mapping attribute -> (xml tag, kind). Reading fills the
attributes; writing puts them back into the *original* XML tree, so tags this
package doesn't know about (and their order) survive a round trip untouched.
"""
from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, fields as dc_fields
from pathlib import Path
from typing import ClassVar

# kinds: text, int, float, list (repeated tag), bool
TEXT, INT, FLOAT, LIST, BOOL = "text", "int", "float", "list", "bool"


class NfoError(Exception):
    """The file isn't an NFO of the expected kind (or isn't XML at all)."""


# ---------------------------------------------------------------- small value types
@dataclass
class Rating:
    value: float
    max: float = 10.0
    votes: int | None = None
    default: bool = False


@dataclass
class Actor:
    name: str
    role: str = ""
    order: int | None = None
    thumb: str = ""


# Legacy single-id tags some writers (Radarr, older Kodi/Jellyfin) add next to <uniqueid>.
_LEGACY_ID_TAGS = {"tmdbid": "tmdb", "imdbid": "imdb", "imdb_id": "imdb", "tvdbid": "tvdb"}
_IMDB = re.compile(r"^tt\d+$")


# ---------------------------------------------------------------- parsing helpers
def _num(s: str | None, cast):
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return cast(float(s)) if cast is int else cast(s)
    except ValueError:
        return None


def _split_roots(raw: str) -> tuple[list[ET.Element], str, str]:
    """Parse text that may hold several root elements (multi-episode files) and
    trailing junk (Sonarr/Radarr 'metadata URL' lines). Returns (roots, root_tag, trailing)."""
    text = raw.lstrip("﻿")
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)
    m = re.search(r"<([A-Za-z_][\w.-]*)", body)
    if not m:
        raise NfoError("no XML element found")
    tag = m.group(1)
    end = body.rfind(f"</{tag}>")
    if end == -1:
        # self-closing or broken; let the parser complain properly
        end_idx = len(body)
    else:
        end_idx = end + len(f"</{tag}>")
    xml_part, trailing = body[:end_idx], body[end_idx:].strip()
    try:
        wrapper = ET.fromstring(f"<folio_wrap>{xml_part}</folio_wrap>")
    except ET.ParseError as e:
        raise NfoError(f"not valid XML: {e}") from None
    roots = [el for el in wrapper if el.tag == tag]
    if not roots:
        raise NfoError("no root element")
    return roots, tag, trailing


def read_text(path: Path) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- base class
@dataclass
class NfoBase:
    ROOT: ClassVar[str] = ""
    FIELDS: ClassVar[dict[str, tuple[str, str]]] = {}

    path: Path | None = None
    trailing: str = ""                     # text after the XML (kept, never parsed)
    locked_fields: set[str] = field(default_factory=set)   # Jellyfin-style <lockedfields>a|b</lockedfields>
    _element: ET.Element | None = field(default=None, repr=False, compare=False)

    # ---- reading
    @classmethod
    def from_element(cls, el: ET.Element, path: Path | None = None, trailing: str = ""):
        if el.tag.lower() != cls.ROOT:
            raise NfoError(f"expected <{cls.ROOT}>, found <{el.tag}>")
        obj = cls(path=path, trailing=trailing)
        obj._element = el
        for attr, (tag, kind) in cls.FIELDS.items():
            found = [c for c in el if c.tag.lower() == tag.lower()]
            if kind == LIST:
                vals = []
                for c in found:
                    vals += [v.strip() for v in (c.text or "").split(" / ") if v.strip()]
                setattr(obj, attr, list(dict.fromkeys(vals)))
                continue
            txt = (found[0].text or "").strip() if found else None
            if kind == TEXT:
                setattr(obj, attr, txt or "")
            elif kind == INT:
                setattr(obj, attr, _num(txt, int))
            elif kind == FLOAT:
                setattr(obj, attr, _num(txt, float))
            elif kind == BOOL:
                setattr(obj, attr, (txt or "").lower() in ("true", "1", "yes"))
        lf = el.findtext("lockedfields") or ""
        obj.locked_fields = {x.strip() for x in lf.split("|") if x.strip()}
        obj._read_extra(el)
        return obj

    def _read_extra(self, el: ET.Element) -> None:  # subclasses add special fields
        pass

    @property
    def element(self) -> ET.Element | None:
        """The parsed XML, for reading tags this package doesn't model (read-only use)."""
        return self._element

    def text(self, tag: str) -> str:
        """First <tag> text, case-insensitive, stripped ('' if absent)."""
        el = self._element
        if el is None:
            return ""
        for c in el:
            if c.tag.lower() == tag.lower():
                return (c.text or "").strip()
        return ""

    @classmethod
    def read(cls, path: str | Path):
        path = Path(path)
        roots, _tag, trailing = _split_roots(read_text(path))
        return cls.from_element(roots[0], path, trailing)

    # ---- writing
    def to_element(self) -> ET.Element:
        el = self._element if self._element is not None else ET.Element(self.ROOT)
        for attr, (tag, kind) in self.FIELDS.items():
            val = getattr(self, attr)
            if kind == LIST:
                _set_repeated(el, tag, [str(v) for v in val if str(v).strip()])
            elif val is None or val == "" or (kind == BOOL and val is False):
                _set_repeated(el, tag, [])
            elif kind == BOOL:
                _set_repeated(el, tag, ["true"])
            elif kind == FLOAT:
                _set_repeated(el, tag, [_fmt_float(val)])
            else:
                _set_repeated(el, tag, [str(val)])
        _set_repeated(el, "lockedfields", ["|".join(sorted(self.locked_fields))] if self.locked_fields else [])
        self._write_extra(el)
        self._element = el
        return el

    def _write_extra(self, el: ET.Element) -> None:
        pass

    def write(self, path: str | Path | None = None, keep_trailing: bool = True) -> Path:
        path = Path(path or self.path)
        if path is None:
            raise ValueError("no path to write to")
        write_elements(path, [self.to_element()], self.trailing if keep_trailing else "")
        self.path = path
        return path


# ---------------------------------------------------------------- writing helpers
def _fmt_float(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _set_repeated(parent: ET.Element, tag: str, values: list[str]) -> None:
    """Replace every <tag> child with one per value, at the position of the first old one."""
    kids = list(parent)
    idx = next((i for i, c in enumerate(kids) if c.tag.lower() == tag.lower()), None)
    existing = [c for c in kids if c.tag.lower() == tag.lower()]
    # unchanged? leave the XML alone (keeps attributes and original formatting)
    if [(c.text or "").strip() for c in existing] == values:
        return
    for c in existing:
        parent.remove(c)
    if not values:
        return
    if idx is None:
        idx = len(list(parent))
    for i, v in enumerate(values):
        new = ET.Element(tag)
        new.text = v
        parent.insert(idx + i, new)


def _replace_block(parent: ET.Element, tag: str, new_children: list[ET.Element]) -> None:
    """Like _set_repeated but for complex children (actors, ratings, uniqueid)."""
    kids = list(parent)
    idx = next((i for i, c in enumerate(kids) if c.tag == tag), None)
    for c in [c for c in kids if c.tag == tag]:
        parent.remove(c)
    if idx is None:
        idx = len(list(parent))
    for i, c in enumerate(new_children):
        parent.insert(idx + i, c)


def _sub(parent: ET.Element, tag: str, text) -> ET.Element:
    e = ET.SubElement(parent, tag)
    e.text = str(text)
    return e


_UMASK = os.umask(0)            # read once when loaded (setting it is the only way to read it)
os.umask(_UMASK)
FILE_MODE = 0o666 & ~_UMASK     # what a normally saved file gets (usually rw-r--r--)


def write_elements(path: Path, elements: list[ET.Element], trailing: str = "") -> None:
    """Write one or more root elements as a Kodi-style NFO, atomically."""
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>']
    for el in elements:
        _strip_ws(el)
        ET.indent(el, space="  ")
        parts.append(ET.tostring(el, encoding="unicode"))
    if trailing:
        parts.append(trailing)
    data = "\n".join(parts) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".folio-", suffix=".nfo", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        os.chmod(tmp, FILE_MODE)            # a temporary file is owner-only: give it a normal file's rights
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _strip_ws(el: ET.Element) -> None:
    """Drop old indentation so ET.indent produces clean output."""
    if len(el):
        if el.text is not None and not el.text.strip():
            el.text = None
        for c in el:
            if c.tail is not None and not c.tail.strip():
                c.tail = None
            _strip_ws(c)


# ---------------------------------------------------------------- shared special fields
def read_ids(el: ET.Element) -> tuple[dict[str, str], str]:
    ids: dict[str, str] = {}
    default = ""
    for u in el.findall("uniqueid"):
        v = (u.text or "").strip()
        if not v:
            continue
        t = (u.get("type") or "unknown").strip().lower()
        ids.setdefault(t, v)
        if (u.get("default") or "").lower() == "true":
            default = t
    for tag, t in _LEGACY_ID_TAGS.items():
        v = (el.findtext(tag) or "").strip()
        if v and v != "0":
            ids.setdefault(t, v)
    v = (el.findtext("id") or "").strip()
    if v and _IMDB.match(v):
        ids.setdefault("imdb", v)
    return ids, default


def write_ids(el: ET.Element, ids: dict[str, str], default: str) -> None:
    old = {(u.get("type") or "").lower(): (u.text or "").strip() for u in el.findall("uniqueid")}
    if old == {k: v for k, v in ids.items() if v} and not _legacy_stale(el, ids):
        return
    new = []
    for t, v in ids.items():
        if not v:
            continue
        u = ET.Element("uniqueid", {"type": t})
        if t == default:
            u.set("default", "true")
        u.text = v
        new.append(u)
    _replace_block(el, "uniqueid", new)
    for tag, t in _LEGACY_ID_TAGS.items():        # keep legacy copies in step if present
        e = el.find(tag)
        if e is not None:
            e.text = ids.get(t, "")


def _legacy_stale(el, ids) -> bool:
    return any(el.find(tag) is not None and (el.findtext(tag) or "").strip() != ids.get(t, "")
               for tag, t in _LEGACY_ID_TAGS.items())


def read_ratings(el: ET.Element) -> dict[str, Rating]:
    out: dict[str, Rating] = {}
    block = el.find("ratings")
    if block is not None:
        for r in block.findall("rating"):
            val = _num(r.findtext("value") or r.text, float)
            if val is None:
                continue
            name = (r.get("name") or "default").strip().lower()
            out[name] = Rating(val, _num(r.get("max"), float) or 10.0,
                               _num(r.findtext("votes"), int),
                               (r.get("default") or "").lower() == "true")
    else:
        r = el.find("rating")
        if r is not None:
            val = _num(r.text, float)
            if val is not None:
                out["default"] = Rating(val, _num(r.get("max"), float) or 10.0,
                                        _num(el.findtext("votes"), int), True)
    return out


def write_ratings(el: ET.Element, ratings: dict[str, Rating], legacy: bool = False) -> None:
    """legacy=True writes Kodi's music form, a single <rating max="10">7.5</rating>."""
    if read_ratings(el) == ratings:
        return
    if legacy:
        r = next((r for r in ratings.values() if r.default), next(iter(ratings.values()), None))
        new = []
        if r is not None:
            e = ET.Element("rating", {"max": _fmt_float(r.max)})
            e.text = _fmt_float(r.value)
            new.append(e)
        _replace_block(el, "rating", new)
        for old in el.findall("ratings"):
            el.remove(old)
        return
    block = ET.Element("ratings")
    for name, r in ratings.items():
        attrs = {"name": name, "max": _fmt_float(r.max)}
        if r.default:
            attrs["default"] = "true"
        e = ET.SubElement(block, "rating", attrs)
        _sub(e, "value", _fmt_float(r.value))
        if r.votes is not None:
            _sub(e, "votes", r.votes)
    _replace_block(el, "ratings", [block] if ratings else [])
    # a lone legacy <rating> would now disagree with <ratings>; drop it
    for old in el.findall("rating"):
        el.remove(old)


def read_actors(el: ET.Element) -> list[Actor]:
    out = []
    for a in el.findall("actor"):
        name = (a.findtext("name") or "").strip()
        if name:
            out.append(Actor(name, (a.findtext("role") or "").strip(),
                             _num(a.findtext("order"), int), (a.findtext("thumb") or "").strip()))
    return out


def write_actors(el: ET.Element, actors: list[Actor]) -> None:
    if read_actors(el) == actors:
        return
    new = []
    for a in actors:
        e = ET.Element("actor")
        _sub(e, "name", a.name)
        if a.role:
            _sub(e, "role", a.role)
        if a.order is not None:
            _sub(e, "order", a.order)
        if a.thumb:
            _sub(e, "thumb", a.thumb)
        new.append(e)
    _replace_block(el, "actor", new)
