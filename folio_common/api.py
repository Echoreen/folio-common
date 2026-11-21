"""Small stdlib HTTP clients for TMDB and fanart.tv, with an on-disk JSON cache so a dry run
followed by --apply doesn't ask the same questions twice."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "Folio-Editor/0.1 (personal media library)"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "folio" / "api"
TMDB_IMG = "https://image.tmdb.org/t/p/"


class ApiError(Exception):
    pass


def _error_detail(e: urllib.error.HTTPError) -> str:
    """The reason a server gives in its error body (GraphQL puts it in errors[].message)."""
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    try:
        j = json.loads(body)
        msgs = [x.get("message", "") for x in j.get("errors") or [] if isinstance(x, dict)]
        msgs = [m for m in msgs if m] or [j.get("status_message") or j.get("message") or ""]
        text = "; ".join(m for m in msgs if m)
    except ValueError:
        text = body.strip()
    return f" ({text[:300]})" if text else ""


class Http:
    def __init__(self, cache_dir: Path | None = CACHE_DIR, ttl: float = 24 * 3600, min_interval: float = 0.05):
        self.cache_dir, self.ttl, self.min_interval = cache_dir, ttl, min_interval
        self._last = 0.0

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                 secret_params: dict | None = None) -> dict | None:
        """GET JSON. `secret_params` (API keys) are sent but left out of the cache key.
        Returns None for 404."""
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        full = url + ("?" + q if q else "")
        key = hashlib.sha1(full.encode()).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return cached.get("data")
        sq = urllib.parse.urlencode(secret_params or {})
        send = full + (("&" if q else "?") + sq if sq else "")
        req = urllib.request.Request(send, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                    **(headers or {})})
        for attempt in range(4):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._cache_put(key, {"data": data})
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._cache_put(key, {"data": None})
                    return None
                if e.code == 429 or e.code >= 500:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                if e.code == 401:
                    raise ApiError(f"{urllib.parse.urlparse(url).netloc}: the API key was refused (401)") from None
                raise ApiError(f"{url}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    raise ApiError(f"{urllib.parse.urlparse(url).netloc}: {getattr(e, 'reason', e)}") from None
                time.sleep(2 ** attempt)
        raise ApiError(f"{url}: gave up after retries")

    def post_json(self, url: str, body: dict) -> dict | None:
        """POST JSON (GraphQL), cached by the request body."""
        raw = json.dumps(body, sort_keys=True)
        key = hashlib.sha1((url + raw).encode()).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return cached.get("data")
        req = urllib.request.Request(url, data=raw.encode(), method="POST",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                              "Content-Type": "application/json"})
        for attempt in range(4):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self._cache_put(key, {"data": data})
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self._cache_put(key, {"data": None})
                    return None
                if e.code == 429 or e.code >= 500:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt * 5))
                    continue
                raise ApiError(f"{urllib.parse.urlparse(url).netloc}: HTTP {e.code}{_error_detail(e)}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    raise ApiError(f"{urllib.parse.urlparse(url).netloc}: {getattr(e, 'reason', e)}") from None
                time.sleep(2 ** attempt)
        raise ApiError(f"{url}: gave up after retries")

    def download(self, url: str, dest: Path) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        self._throttle()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            raise ApiError(f"download failed: {url} ({getattr(e, 'reason', e)})") from None
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".folio-", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            from folio_nfo.core import FILE_MODE
            os.chmod(tmp, FILE_MODE)        # a temporary file is owner-only: give it a normal file's rights
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _throttle(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _cache_get(self, key):
        if not self.cache_dir:
            return None
        p = self.cache_dir / f"{key}.json"
        try:
            if time.time() - p.stat().st_mtime < self.ttl:
                return json.loads(p.read_text())
        except (OSError, ValueError):
            pass
        return None

    def _cache_put(self, key, value):
        if not self.cache_dir:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(json.dumps(value))
        except OSError:
            pass


class TMDB:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, keys: dict, http: Http | None = None, language: str = "en-US"):
        tk = keys.get("tmdb") or {}
        self.token, self.api_key = tk.get("read_token", ""), tk.get("api_key", "")
        if not (self.token or self.api_key):
            raise ApiError("No TMDB key: add [tmdb] api_key or read_token to ~/.config/folio/keys.toml")
        self.http, self.language = http or Http(), language

    def _get(self, path, **params):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        secret = {} if self.token else {"api_key": self.api_key}
        return self.http.get_json(self.BASE + path, {"language": self.language, **params}, headers, secret)

    def search_tv(self, query, year=None):
        return (self._get("/search/tv", query=query, first_air_date_year=year) or {}).get("results", [])

    def search_movie(self, query, year=None):
        return (self._get("/search/movie", query=query, year=year) or {}).get("results", [])

    def tv(self, tmdb_id):
        return self._get(f"/tv/{tmdb_id}", append_to_response="external_ids,content_ratings,aggregate_credits,images",
                         include_image_language="en,ja,null")

    def season(self, tmdb_id, number):
        return self._get(f"/tv/{tmdb_id}/season/{number}")

    def season_images(self, tmdb_id, season):
        """Every poster TMDB has for one season (for choosing a season poster)."""
        return self._get(f"/tv/{tmdb_id}/season/{season}/images", include_image_language="en,ja,null")

    def episode_images(self, tmdb_id, season, episode):
        """Every still TMDB has for one episode (for choosing a thumbnail)."""
        return self._get(f"/tv/{tmdb_id}/season/{season}/episode/{episode}/images")

    def movie(self, tmdb_id):
        return self._get(f"/movie/{tmdb_id}", append_to_response="external_ids,release_dates,credits,images",
                         include_image_language="en,ja,null")

    def collection(self, collection_id):
        """A movie collection: name, overview, poster/backdrop and every movie in it (parts)."""
        return self._get(f"/collection/{collection_id}")

    def find(self, external_id, source):          # source: imdb_id / tvdb_id
        return self._get(f"/find/{external_id}", external_source=source) or {}

    @staticmethod
    def image(path, size="original"):
        return f"{TMDB_IMG}{size}{path}" if path else ""


class Fanart:
    BASE = "https://webservice.fanart.tv/v3"

    def __init__(self, keys: dict, http: Http | None = None):
        fk = keys.get("fanart") or {}
        self.api_key, self.client_key = fk.get("api_key", ""), fk.get("client_key", "")
        self.http = http or Http()

    @property
    def enabled(self):
        return bool(self.api_key)

    def _get(self, path):
        if not self.enabled:
            return None
        secret = {"api_key": self.api_key, **({"client_key": self.client_key} if self.client_key else {})}
        try:
            return self.http.get_json(self.BASE + path, secret_params=secret)
        except ApiError:
            return None                      # artwork extras are optional; never fail a run over them

    def tv(self, tvdb_id):
        return self._get(f"/tv/{tvdb_id}") if tvdb_id else None

    def movie(self, tmdb_id):
        return self._get(f"/movies/{tmdb_id}") if tmdb_id else None

    def music(self, mbid):
        """Artist photos, backgrounds, logos and banners by MusicBrainz artist ID."""
        return self._get(f"/music/{mbid}") if mbid else None

    @staticmethod
    def best(images, lang="en"):
        """Highest-liked image in `lang`, else language-neutral, else anything."""
        if not images:
            return ""
        def rank(i):
            l = (i.get("lang") or "")
            return (l == lang, l in ("", "00"), int(i.get("likes") or 0))
        return max(images, key=rank).get("url", "")


class MyAnimeList:
    """MyAnimeList's own score, rank and number of voters, by MAL ID (AniList gives Folio the ID).
    Needs only a Client ID from a free registered app: [mal] client_id in keys.toml."""
    BASE = "https://api.myanimelist.net/v2/anime/"

    def __init__(self, keys: dict, http: Http | None = None):
        sec = keys.get("mal") or keys.get("myanimelist") or {}
        self.client_id = sec.get("client_id", "")
        self.http = http or Http()

    @property
    def enabled(self):
        return bool(self.client_id)

    def scores(self, mal_id: str) -> dict:
        """{"value": 8.4, "votes": 123456, "rank": 87}; {} if unknown or MAL can't be reached."""
        if not self.enabled or not str(mal_id).isdigit():
            return {}
        try:
            d = self.http.get_json(self.BASE + str(mal_id), {"fields": "mean,rank,num_scoring_users"},
                                   {"X-MAL-CLIENT-ID": self.client_id}) or {}
        except ApiError:
            return {}                        # extra scores are optional; never fail a run over them
        if not d.get("mean"):
            return {}
        return {"value": float(d["mean"]), "votes": d.get("num_scoring_users") or None, "rank": d.get("rank") or None}


class OMDb:
    """IMDb, Rotten Tomatoes and Metacritic scores by IMDb ID (free key: 1,000 lookups a day;
    answers are cached for a day like everything else)."""
    BASE = "https://www.omdbapi.com/"

    def __init__(self, keys: dict, http: Http | None = None):
        self.api_key = (keys.get("omdb") or {}).get("api_key", "")
        self.http = http or Http()

    @property
    def enabled(self):
        return bool(self.api_key)

    def ratings(self, imdb_id: str) -> dict:
        """{"imdb": (value, max, votes), "rottentomatoes": (...), "metacritic": (...)}; {} if unknown."""
        if not self.enabled or not imdb_id:
            return {}
        try:
            d = self.http.get_json(self.BASE, {"i": imdb_id}, secret_params={"apikey": self.api_key}) or {}
        except ApiError:
            return {}                        # extra scores are optional; never fail a run over them
        if d.get("Response") != "True":
            return {}
        out = {}
        votes = d.get("imdbVotes", "").replace(",", "")
        for r in d.get("Ratings") or []:
            src, val = r.get("Source", ""), (r.get("Value") or "").strip()
            try:
                if src == "Internet Movie Database" and "/" in val:
                    out["imdb"] = (float(val.split("/")[0]), 10.0, int(votes) if votes.isdigit() else None)
                elif src == "Rotten Tomatoes" and val.endswith("%"):
                    out["rottentomatoes"] = (float(val[:-1]), 100.0, None)
                elif src == "Metacritic" and "/" in val:
                    out["metacritic"] = (float(val.split("/")[0]), 100.0, None)
            except ValueError:
                continue
        if "imdb" not in out and d.get("imdbRating", "N/A") not in ("N/A", ""):
            try:
                out["imdb"] = (float(d["imdbRating"]), 10.0, int(votes) if votes.isdigit() else None)
            except ValueError:
                pass
        return out
