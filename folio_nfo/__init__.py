"""folio_nfo: read and write Kodi-style NFO files for Folio and its metadata editor.

    from folio_nfo import load, MovieNfo
    m = MovieNfo.read("Movies/Heat (1995)/Heat (1995).nfo")
    m.plot = "..."; m.locked_fields.add("plot"); m.write()

Writing keeps every tag this package doesn't model, so editing never loses data.
"""
from .collection import CollectionNfo, Part, collection_art, collection_dir, part_poster, read_collection
from .core import Actor, NfoError, Rating
from .files import (album_art, album_nfo_path, artist_art, artist_nfo_path, episode_nfo_path,
                    episode_thumb, load, movie_art, movie_nfo_path, season_art, season_nfo_path,
                    show_art, tvshow_nfo_path)
from .music import AlbumArtistCredit, AlbumNfo, ArtistNfo
from .video import EpisodeNfo, KnownEpisode, MovieNfo, Related, SeasonNfo, TvShowNfo, read_episodes, write_episodes

__version__ = "0.1.0"
__all__ = [
    "Actor", "AlbumArtistCredit", "CollectionNfo", "Part", "collection_art", "collection_dir", "part_poster",
    "read_collection", "AlbumNfo", "ArtistNfo", "EpisodeNfo", "MovieNfo", "NfoError",
    "KnownEpisode", "Rating", "Related", "SeasonNfo", "TvShowNfo", "album_art", "album_nfo_path", "artist_art", "artist_nfo_path",
    "episode_nfo_path", "episode_thumb", "load", "movie_art", "movie_nfo_path", "read_episodes",
    "season_art", "season_nfo_path", "show_art", "tvshow_nfo_path", "write_episodes",
]
