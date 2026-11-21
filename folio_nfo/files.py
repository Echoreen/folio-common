"""Where NFOs and artwork live on disk (Kodi naming, as Sonarr/Radarr write it)."""
from __future__ import annotations

from pathlib import Path

from .core import NfoError, _split_roots, read_text
from .music import AlbumNfo, ArtistNfo
from .video import EpisodeNfo, MovieNfo, SeasonNfo, TvShowNfo

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_KINDS = {c.ROOT: c for c in (ArtistNfo, AlbumNfo, TvShowNfo, SeasonNfo, EpisodeNfo, MovieNfo)}


def load(path: str | Path):
    """Read any NFO, picking the class from its root tag. Episode files return a list."""
    path = Path(path)
    roots, tag, trailing = _split_roots(read_text(path))
    cls = _KINDS.get(tag.lower())
    if cls is None:
        raise NfoError(f"unknown NFO kind <{tag}>")
    if cls is EpisodeNfo:
        return [EpisodeNfo.from_element(r, path, trailing) for r in roots]
    return cls.from_element(roots[0], path, trailing)


def _first(folder: Path, *stems: str) -> Path | None:
    for stem in stems:
        for ext in IMAGE_EXTS:
            p = folder / f"{stem}{ext}"
            if p.is_file():
                return p
    return None


# ---------------------------------------------------------------- NFO paths
def movie_nfo_path(video: Path, prefer_movie_nfo: bool = False) -> Path:
    """Existing movie.nfo or <file>.nfo; for a new file, whichever you prefer."""
    video = Path(video)
    a, b = video.parent / "movie.nfo", video.with_suffix(".nfo")
    for p in ((a, b) if prefer_movie_nfo else (b, a)):
        if p.is_file():
            return p
    return a if prefer_movie_nfo else b


def episode_nfo_path(video: Path) -> Path:
    return Path(video).with_suffix(".nfo")


def tvshow_nfo_path(show_dir: Path) -> Path:
    return Path(show_dir) / "tvshow.nfo"


def season_nfo_path(season_dir: Path) -> Path:
    return Path(season_dir) / "season.nfo"


def artist_nfo_path(artist_dir: Path) -> Path:
    return Path(artist_dir) / "artist.nfo"


def album_nfo_path(album_dir: Path) -> Path:
    return Path(album_dir) / "album.nfo"


# ---------------------------------------------------------------- artwork
def show_art(show_dir: Path) -> dict[str, Path]:
    d = Path(show_dir)
    found = {k: _first(d, *names) for k, names in {
        "poster": ("poster", "folder"), "fanart": ("fanart", "backdrop"), "banner": ("banner",),
        "clearlogo": ("clearlogo", "logo"), "landscape": ("landscape", "thumb"),
    }.items()}
    return {k: v for k, v in found.items() if v}


def season_art(show_dir: Path, season: int, season_dir: Path | None = None) -> dict[str, Path]:
    """Sonarr: season01-poster.jpg / season-specials-poster.jpg in the show folder.
    Others put poster.jpg inside the season folder."""
    d = Path(show_dir)
    tag = "season-specials" if season == 0 else f"season{season:02d}"
    out = {}
    for kind in ("poster", "banner", "fanart", "landscape"):
        p = _first(d, f"{tag}-{kind}")
        if p is None and season_dir is not None:
            p = _first(Path(season_dir), *((kind, "folder") if kind == "poster" else (kind,)))
        if p:
            out[kind] = p
    return out


def episode_thumb(video: Path) -> Path | None:
    v = Path(video)
    return _first(v.parent, f"{v.stem}-thumb", v.stem)


def movie_art(video: Path) -> dict[str, Path]:
    v = Path(video)
    d, s = v.parent, v.stem
    found = {
        "poster": _first(d, f"{s}-poster", "poster", "folder", "cover"),
        "fanart": _first(d, f"{s}-fanart", "fanart", "backdrop"),
        "clearlogo": _first(d, f"{s}-clearlogo", "clearlogo", "logo"),
        "banner": _first(d, f"{s}-banner", "banner"),
        "landscape": _first(d, f"{s}-landscape", "landscape"),
    }
    return {k: p for k, p in found.items() if p}


def album_art(album_dir: Path) -> Path | None:
    return _first(Path(album_dir), "cover", "folder", "front", "album")


def artist_art(artist_dir: Path) -> dict[str, Path]:
    d = Path(artist_dir)
    found = {"thumb": _first(d, "thumb", "folder", "artist"), "fanart": _first(d, "fanart", "backdrop"),
             "banner": _first(d, "banner"), "clearlogo": _first(d, "clearlogo", "logo")}
    return {k: p for k, p in found.items() if p}
