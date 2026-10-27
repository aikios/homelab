"""Thin clients for Radarr / Sonarr (v3 API) and Prowlarr (v1 API).

Design: the *arr apps are the download+import engine. We add the title (without an
auto-search), then use the arr's own release search so quality/language parsing and
post-download import/rename come for free. Prowlarr is the indexer backend and also
offers a raw search preview.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..config import CFG


class ArrError(RuntimeError):
    pass


class _Arr:
    api = "v3"

    def __init__(self, base: str, key: str):
        self._c = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"X-Api-Key": key},
            timeout=60.0,
        )

    def _j(self, method: str, path: str, **kw) -> Any:
        r = self._c.request(method, f"/api/{self.api}{path}", **kw)
        if r.status_code >= 400:
            raise ArrError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # ---- shared ----
    def quality_profiles(self) -> list[dict]:
        return self._j("GET", "/qualityprofile")

    def profile_id(self, name: str) -> int:
        for p in self.quality_profiles():
            if p["name"].lower() == name.lower():
                return p["id"]
        raise ArrError(f"no quality profile named {name!r}")

    def root_folder(self) -> str:
        rf = self._j("GET", "/rootfolder")
        return rf[0]["path"]

    def queue(self) -> list[dict]:
        q = self._j("GET", "/queue", params={"pageSize": 200, "includeMovie": True,
                                             "includeEpisode": True, "includeSeries": True})
        return q.get("records", q) if isinstance(q, dict) else q

    def command(self, name: str, **body) -> dict:
        return self._j("POST", "/command", json={"name": name, **body})

    def grab_release(self, guid: str, indexer_id: int) -> dict:
        """POST a release the arr previously returned -> arr grabs via its download client."""
        return self._j("POST", "/release", json={"guid": guid, "indexerId": indexer_id})


class Radarr(_Arr):
    def __init__(self):
        super().__init__(CFG.radarr_url, CFG.radarr_key)

    def lookup(self, term: str) -> list[dict]:
        return self._j("GET", "/movie/lookup", params={"term": term})

    def list(self) -> list[dict]:
        return self._j("GET", "/movie")

    def get_by_tmdb(self, tmdb_id: int) -> dict | None:
        for m in self.list():
            if m.get("tmdbId") == tmdb_id:
                return m
        return None

    def add(self, tmdb_id: int, quality: str = "HD-1080p", monitored: bool = True,
            search: bool = False) -> dict:
        existing = self.get_by_tmdb(tmdb_id)
        if existing:
            return existing
        found = next((x for x in self.lookup(f"tmdb:{tmdb_id}") if x.get("tmdbId") == tmdb_id), None)
        if not found:
            raise ArrError(f"tmdb {tmdb_id} not found via lookup")
        payload = {
            **found,
            "qualityProfileId": self.profile_id(quality),
            "rootFolderPath": self.root_folder(),
            "monitored": monitored,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": search},
        }
        return self._j("POST", "/movie", json=payload)

    def releases(self, movie_id: int) -> list[dict]:
        return self._j("GET", "/release", params={"movieId": movie_id})

    def delete(self, movie_id: int, delete_files: bool = True) -> None:
        self._j("DELETE", f"/movie/{movie_id}",
                params={"deleteFiles": delete_files, "addImportExclusion": False})


class Sonarr(_Arr):
    def __init__(self):
        super().__init__(CFG.sonarr_url, CFG.sonarr_key)

    def lookup(self, term: str) -> list[dict]:
        return self._j("GET", "/series/lookup", params={"term": term})

    def list(self) -> list[dict]:
        return self._j("GET", "/series")

    def get_by_tvdb(self, tvdb_id: int) -> dict | None:
        for s in self.list():
            if s.get("tvdbId") == tvdb_id:
                return s
        return None

    def add(self, tvdb_id: int, quality: str = "HD-1080p", monitored: bool = True,
            search: bool = False, season: int | None = None) -> dict:
        existing = self.get_by_tvdb(tvdb_id)
        if existing:
            return existing
        found = next((x for x in self.lookup(f"tvdb:{tvdb_id}") if x.get("tvdbId") == tvdb_id), None)
        if not found:
            raise ArrError(f"tvdb {tvdb_id} not found via lookup")
        seasons = found.get("seasons", [])
        if season is not None:
            for s in seasons:
                s["monitored"] = s.get("seasonNumber") == season
        payload = {
            **found,
            "qualityProfileId": self.profile_id(quality),
            "rootFolderPath": self.root_folder(),
            "monitored": monitored,
            "seasonFolder": True,
            "seasons": seasons,
            "addOptions": {
                "searchForMissingEpisodes": search,
                "searchForCutoffUnmetEpisodes": False,
                "monitor": "all" if season is None else "none",
            },
        }
        return self._j("POST", "/series", json=payload)

    def releases(self, series_id: int, season: int | None = None,
                 episode_id: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"seriesId": series_id}
        if season is not None:
            params["seasonNumber"] = season
        if episode_id is not None:
            params["episodeId"] = episode_id
        return self._j("GET", "/release", params=params)

    def episodes(self, series_id: int) -> list[dict]:
        return self._j("GET", "/episode", params={"seriesId": series_id})

    def delete(self, series_id: int, delete_files: bool = True) -> None:
        self._j("DELETE", f"/series/{series_id}", params={"deleteFiles": delete_files})


class Prowlarr:
    def __init__(self):
        self._c = httpx.Client(
            base_url=CFG.prowlarr_url.rstrip("/"),
            headers={"X-Api-Key": CFG.prowlarr_key},
            timeout=60.0,
        )

    def _j(self, method: str, path: str, **kw) -> Any:
        r = self._c.request(method, f"/api/v1{path}", **kw)
        if r.status_code >= 400:
            raise ArrError(f"prowlarr {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    def indexers(self) -> list[dict]:
        return self._j("GET", "/indexer")

    def search(self, query: str, categories: list[int] | None = None,
               indexer_ids: list[int] | None = None, limit: int = 25) -> list[dict]:
        params: dict[str, Any] = {"query": query, "type": "search", "limit": limit}
        if categories:
            params["categories"] = categories
        if indexer_ids:
            params["indexerIds"] = indexer_ids
        return self._j("GET", "/search", params=params)


if __name__ == "__main__":
    import json
    r, s, p = Radarr(), Sonarr(), Prowlarr()
    print("radarr profiles:", [x["name"] for x in r.quality_profiles()])
    print("radarr root:", r.root_folder(), "| movies in lib:", len(r.list()))
    print("sonarr root:", s.root_folder(), "| series in lib:", len(s.list()))
    print("prowlarr indexers:", [(i["id"], i["name"], i["enable"]) for i in p.indexers()])
    hit = r.lookup("Dune Part Two")[:1]
    print("lookup sample:", json.dumps(
        {k: hit[0][k] for k in ("title", "year", "tmdbId")} if hit else {}, indent=1))
