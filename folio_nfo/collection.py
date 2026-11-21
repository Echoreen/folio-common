"""collection.nfo: one movie collection ("Ghost in the Shell Collection") in the Collection Info folder.

    Collection Info/
        Ghost in the Shell Collection/
            collection.nfo      title, overview, TMDB id, and every movie TMDB lists for it
            poster.jpg
            fanart.jpg
            parts/603.jpg       small posters, so Folio can show movies you don't have yet

The folder is named after the collection name your movie.nfo files use (<set><name>), so Folio finds
it without asking TMDB. Kodi keeps collection artwork the same way ("Movie set information folder").

<collection>
    <title>Ghost in the Shell Collection</title>
    <overview>...</overview>
    <uniqueid type="tmdb" default="true">1234</uniqueid>
    <part tmdb="9323" premiered="1995-11-18">Ghost in the Shell</part>
    ...
</collection>
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .core import TEXT, NfoBase, _replace_block, read_ids, write_ids

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


@dataclass
class Part:
    title: str
    tmdb: str = ""
    premiered: str = ""            # YYYY-MM-DD, or "" if TMDB has no date yet

    @property
    def year(self) -> str:
        return self.premiered[:4] if self.premiered[:4].isdigit() else ""


@dataclass
class CollectionNfo(NfoBase):
    ROOT = "collection"
    FIELDS = {"title": ("title", TEXT), "overview": ("overview", TEXT)}
    title: str = ""
    overview: str = ""
    ids: dict[str, str] = field(default_factory=dict)
    default_id: str = ""
    parts: list[Part] = field(default_factory=list)

    def _read_extra(self, el):
        self.ids, self.default_id = read_ids(el)
        self.parts = [Part((p.text or "").strip(), (p.get("tmdb") or "").strip(), (p.get("premiered") or "").strip())
                      for p in el.findall("part") if (p.text or "").strip()]

    def _write_extra(self, el):
        write_ids(el, self.ids, self.default_id)
        old = [((p.text or "").strip(), p.get("tmdb") or "", p.get("premiered") or "") for p in el.findall("part")]
        if old != [(p.title, p.tmdb, p.premiered) for p in self.parts]:
            new = []
            for p in self.parts:
                e = ET.Element("part", {k: v for k, v in (("tmdb", p.tmdb), ("premiered", p.premiered)) if v})
                e.text = p.title
                new.append(e)
            _replace_block(el, "part", new)


def folder_name(name: str) -> str:
    """A collection name as a folder name, by the same rules as movie folders: ':' -> ' - ', '/' -> '-'."""
    s = re.sub(r"\s*:\s*", " - ", name.strip())
    s = re.sub(r'[/\\]', "-", s)
    s = re.sub(r'[<>"|?*\x00-\x1f]', "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" .") or "Collection"


def collection_dir(info_root: Path, name: str) -> Path:
    return Path(info_root) / folder_name(name)


def _image(folder: Path, stem: str) -> Path | None:
    for ext in IMAGE_EXTS:
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def collection_art(folder: Path) -> dict[str, Path]:
    """{"poster": ..., "fanart": ...} for whatever exists."""
    folder = Path(folder)
    found = {"poster": _image(folder, "poster"), "fanart": _image(folder, "fanart") or _image(folder, "backdrop")}
    return {k: v for k, v in found.items() if v}


def part_poster(folder: Path, tmdb_id: str) -> Path | None:
    return _image(Path(folder) / "parts", str(tmdb_id)) if tmdb_id else None


def read_collection(info_root: Path | None, name: str) -> CollectionNfo | None:
    """The collection.nfo for a collection name, or None."""
    if not info_root or not name:
        return None
    p = collection_dir(info_root, name) / "collection.nfo"
    try:
        return CollectionNfo.read(p) if p.is_file() else None
    except Exception:
        return None
