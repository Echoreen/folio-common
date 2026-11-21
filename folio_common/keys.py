"""API keys from ~/.config/folio/keys.toml, overridable with environment variables.

    [tmdb]    api_key / read_token        FOLIO_TMDB_KEY / FOLIO_TMDB_TOKEN
    [fanart]  api_key / client_key        FOLIO_FANART_KEY / FOLIO_FANART_CLIENT_KEY
    [omdb]    api_key                     FOLIO_OMDB_KEY
    [anilist] client_id / client_secret   (not needed for public data)
    [mal]     client_id                   FOLIO_MAL_CLIENT_ID  (MyAnimeList scores and ranks)
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

KEYS_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "folio" / "keys.toml"

_ENV = {("tmdb", "api_key"): "FOLIO_TMDB_KEY", ("tmdb", "read_token"): "FOLIO_TMDB_TOKEN",
        ("fanart", "api_key"): "FOLIO_FANART_KEY", ("fanart", "client_key"): "FOLIO_FANART_CLIENT_KEY",
        ("omdb", "api_key"): "FOLIO_OMDB_KEY", ("mal", "client_id"): "FOLIO_MAL_CLIENT_ID"}


def load_keys(path: Path | None = None) -> dict[str, dict[str, str]]:
    path = Path(path or KEYS_FILE)
    data: dict = {}
    if path.is_file():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    out: dict[str, dict[str, str]] = {}
    for section, vals in data.items():
        if isinstance(vals, dict):
            out[section] = {k: str(v).strip() for k, v in vals.items()
                            if str(v).strip() and str(v).strip() != "..."}
    for (section, key), env in _ENV.items():
        if os.environ.get(env):
            out.setdefault(section, {})[key] = os.environ[env].strip()
    return out
