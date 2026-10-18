"""Minimal qBittorrent WebUI client (login cookie + the handful of calls we need)."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import CFG


class QbitError(RuntimeError):
    pass


class Qbit:
    def __init__(self):
        self._c = httpx.Client(base_url=CFG.qbit_url.rstrip("/"), timeout=30.0)
        self._logged_in = False

    def login(self) -> None:
        # From a whitelisted subnet qBittorrent bypasses auth and returns 204 (no
        # session needed). Otherwise a valid login returns 200 "Ok.".
        r = self._c.post("/api/v2/auth/login",
                         data={"username": CFG.qbit_user, "password": CFG.qbit_pass},
                         headers={"Referer": CFG.qbit_url})
        if r.status_code == 204 or (r.status_code == 200 and r.text.strip() == "Ok."):
            self._logged_in = True
            return
        raise QbitError(f"qbit login failed: {r.status_code} {r.text[:120]}")

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        r = self._c.request(method, path, **kw)
        if r.status_code in (401, 403):
            self.login()
            r = self._c.request(method, path, **kw)
        if r.status_code >= 400:
            raise QbitError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r

    def version(self) -> str:
        return self._call("GET", "/api/v2/app/version").text

    def torrents(self, hashes: list[str] | None = None,
                 tag: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if hashes:
            params["hashes"] = "|".join(hashes)
        if tag is not None:
            params["tag"] = tag
        return self._call("GET", "/api/v2/torrents/info", params=params).json()

    def transfer_info(self) -> dict[str, Any]:
        return self._call("GET", "/api/v2/transfer/info").json()

    def categories(self) -> dict[str, Any]:
        return self._call("GET", "/api/v2/torrents/categories").json()

    def add_category(self, name: str, save_path: str = "") -> None:
        try:
            self._call("POST", "/api/v2/torrents/createCategory",
                       data={"category": name, "savePath": save_path})
        except QbitError as e:
            if "409" not in str(e):
                raise

    def delete(self, hashes: list[str], delete_files: bool = True) -> None:
        self._call("POST", "/api/v2/torrents/delete",
                   data={"hashes": "|".join(hashes), "deleteFiles": str(delete_files).lower()})

    def add_url(self, url: str, category: str, save_path: str = "",
                tags: str = "") -> None:
        data = {"urls": url, "category": category, "savepath": save_path}
        if tags:
            data["tags"] = tags
        self._call("POST", "/api/v2/torrents/add", data=data)


if __name__ == "__main__":
    q = Qbit()
    print("qbit version:", q.version())
    print("categories:", list(q.categories().keys()))
    for t in q.torrents():
        print(f"  {t['progress']*100:5.1f}%  {t['state']:12}  {t['name'][:70]}")
    ti = q.transfer_info()
    print(f"dl {ti['dl_info_speed']/1024:.0f} KB/s  up {ti['up_info_speed']/1024:.0f} KB/s")
