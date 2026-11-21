# folio_nfo

Shared NFO reading and writing for Folio (read-only) and the metadata editor (read/write).
Uses only the Python standard library.

| Class        | File                                | Written by            |
|--------------|-------------------------------------|-----------------------|
| `ArtistNfo`  | `artist.nfo`                        | you / Kodi            |
| `AlbumNfo`   | `album.nfo`                         | you / Kodi            |
| `TvShowNfo`  | `tvshow.nfo`                        | Sonarr                |
| `SeasonNfo`  | `Season 01/season.nfo`              | the editor (Sonarr doesn't) |
| `EpisodeNfo` | `<episode file>.nfo` (multi-episode files supported) | Sonarr |
| `MovieNfo`   | `movie.nfo` or `<movie file>.nfo`   | Radarr                |

```python
import folio_nfo as fn

item = fn.load(path)                 # picks the class from the root tag; episodes -> list
m = fn.MovieNfo.read(path)
m.plot = "Better plot"
m.locked_fields.add("plot")          # <lockedfields>plot</lockedfields>, for the editor's locks
m.write()                            # atomic; unknown tags (fileinfo, tracks, …) are kept
```

Notes
- Tags the package doesn't model are preserved in place on write, and unchanged fields are not rewritten.
- Text after the XML (Sonarr/Radarr "metadata URL" lines) is ignored when reading and kept when writing.
- IDs come from `<uniqueid>`, plus legacy `<tmdbid>`, `<imdbid>`, `<tvdbid>` and an `<id>tt…</id>`; legacy tags are kept in sync on write.
- Album ratings are written in Kodi's music form (`<rating max="10">`); video uses `<ratings>`.
- Artwork helpers (`show_art`, `season_art`, `episode_thumb`, `movie_art`, `album_art`, `artist_art`) follow Kodi/Sonarr/Radarr names.

Run the tests from the repo root: `python3 -m unittest discover -s tests -v`
