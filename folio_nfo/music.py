"""artist.nfo and album.nfo (Kodi music format)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .core import (BOOL, INT, LIST, TEXT, NfoBase, Rating, _replace_block, _sub, read_ratings,
                   write_ratings)


@dataclass
class ArtistNfo(NfoBase):
    ROOT = "artist"
    FIELDS = {
        "name": ("name", TEXT), "sort_name": ("sortname", TEXT), "mbid": ("musicBrainzArtistID", TEXT),
        "type": ("type", TEXT), "gender": ("gender", TEXT), "disambiguation": ("disambiguation", TEXT),
        "genres": ("genre", LIST), "styles": ("style", LIST), "moods": ("mood", LIST),
        "years_active": ("yearsactive", LIST), "born": ("born", TEXT), "formed": ("formed", TEXT),
        "died": ("died", TEXT), "disbanded": ("disbanded", TEXT), "biography": ("biography", TEXT),
        # Folio's own: other spellings of this artist ("Same artist as…"), their MusicBrainz IDs, and
        # names you said are someone else (so Folio stops suggesting them)
        "aliases": ("alias", LIST), "alias_mbids": ("aliasmusicbrainzartistid", LIST),
        "not_same": ("notsameas", LIST),
    }
    name: str = ""
    sort_name: str = ""
    mbid: str = ""
    type: str = ""
    gender: str = ""
    disambiguation: str = ""
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    moods: list[str] = field(default_factory=list)
    years_active: list[str] = field(default_factory=list)
    born: str = ""
    formed: str = ""
    died: str = ""
    disbanded: str = ""
    biography: str = ""
    aliases: list[str] = field(default_factory=list)
    alias_mbids: list[str] = field(default_factory=list)
    not_same: list[str] = field(default_factory=list)


@dataclass
class AlbumArtistCredit:
    name: str
    mbid: str = ""


@dataclass
class AlbumNfo(NfoBase):
    ROOT = "album"
    FIELDS = {
        "title": ("title", TEXT), "artists": ("artist", LIST),
        "mb_album_id": ("musicbrainzalbumid", TEXT),
        "mb_release_group_id": ("musicbrainzreleasegroupid", TEXT),
        "genres": ("genre", LIST), "styles": ("style", LIST), "moods": ("mood", LIST),
        "themes": ("theme", LIST), "review": ("review", TEXT), "type": ("type", TEXT),
        "release_type": ("releasetype", TEXT), "release_date": ("releasedate", TEXT),
        "original_release_date": ("originalreleasedate", TEXT), "year": ("year", INT),
        "label": ("label", TEXT), "compilation": ("compilation", BOOL),
    }
    title: str = ""
    artists: list[str] = field(default_factory=list)
    mb_album_id: str = ""
    mb_release_group_id: str = ""
    genres: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    moods: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    review: str = ""
    type: str = ""
    release_type: str = ""
    release_date: str = ""
    original_release_date: str = ""
    year: int | None = None
    label: str = ""
    compilation: bool = False
    credits: list[AlbumArtistCredit] = field(default_factory=list)   # <albumArtistCredits>
    ratings: dict[str, Rating] = field(default_factory=dict)

    def _read_extra(self, el):
        self.credits = [AlbumArtistCredit((c.findtext("artist") or "").strip(),
                                          (c.findtext("musicBrainzArtistID") or "").strip().lower())
                        for c in el.findall("albumArtistCredits") if (c.findtext("artist") or "").strip()]
        self.ratings = read_ratings(el)

    def _write_extra(self, el):
        old = [AlbumArtistCredit((c.findtext("artist") or "").strip(),
                                 (c.findtext("musicBrainzArtistID") or "").strip().lower())
               for c in el.findall("albumArtistCredits") if (c.findtext("artist") or "").strip()]
        if old != self.credits:
            new = []
            for c in self.credits:
                e = ET.Element("albumArtistCredits")
                _sub(e, "artist", c.name)
                if c.mbid:
                    _sub(e, "musicBrainzArtistID", c.mbid)
                new.append(e)
            _replace_block(el, "albumArtistCredits", new)
        write_ratings(el, self.ratings, legacy=el.find("ratings") is None)
