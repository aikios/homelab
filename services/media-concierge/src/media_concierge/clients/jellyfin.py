"""Jellyfin API client — library inventory, targeted/full rescan, delete, played state."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import CFG


class JellyfinError(RuntimeError):
    pass


class Jellyfin:
    def __init__(self):
        self._c = httpx.Client(
            base_url=CFG.jellyfin_url.rstrip("/"),
            headers={"X-Emby-Token": CFG.jellyfin_key},
            timeout=60.0,
        )
        self._uid: str | None = None

    def _j(self, method: str, path: str, **kw) -> Any:
        r = self._c.request(method, path, **kw)
        if r.status_code >= 400:
            raise JellyfinError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    def info(self) -> dict:
        return self._j("GET", "/System/Info")

    def admin_user_id(self) -> str:
        if self._uid:
            return self._uid
        users = self._j("GET", "/Users")
        admin = next((u for u in users if u.get("Policy", {}).get("IsAdministrator")), users[0])
        self._uid = admin["Id"]
        return self._uid

    def items(self, kinds: tuple[str, ...] = ("Movie", "Series")) -> list[dict]:
        uid = self.admin_user_id()
        out = self._j("GET", "/Items", params={
            "userId": uid,
            "Recursive": "true",
            "IncludeItemTypes": ",".join(kinds),
            "Fields": "Path,Genres,ProductionYear,MediaSources,People,DateCreated,"
                      "RunTimeTicks,ProviderIds",
            "SortBy": "SortName",
        })
        return out.get("Items", [])

    def played_map(self) -> dict[str, bool]:
        """item id -> whether the admin user has played it."""
        uid = self.admin_user_id()
        out = self._j("GET", "/Items", params={
            "userId": uid, "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "UserData",
        })
        m: dict[str, bool] = {}
        for it in out.get("Items", []):
            m[it["Id"]] = bool(it.get("UserData", {}).get("Played"))
        return m

    def refresh(self, item_id: str | None = None) -> None:
        if item_id:
            self._j("POST", f"/Items/{item_id}/Refresh",
                    params={"Recursive": "true", "MetadataRefreshMode": "FullRefresh",
                            "ImageRefreshMode": "FullRefresh",
                            "ReplaceAllMetadata": "false"})
        else:
            self._j("POST", "/Library/Refresh")

    def delete_item(self, item_id: str) -> None:
        self._j("DELETE", f"/Items/{item_id}")


if __name__ == "__main__":
    j = Jellyfin()
    info = j.info()
    print("jellyfin", info.get("Version"), "on", info.get("ServerName"))
    its = j.items()
    print(f"{len(its)} top-level items")
    for it in its[:12]:
        print(f"  {it['Type']:7} {it.get('ProductionYear','????')}  {it['Name']}")
