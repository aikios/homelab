"""Free-text indexer search via Prowlarr: the safety net under the arr-matched search.

Radarr/Sonarr only return releases their parser can tie to the exact movie/series
they searched under its canonical name, so a release posted under a romaji or
alternate title, a different year format, or as an odd pack never shows up — the
owner then finds it on the indexer by hand. This searches every Prowlarr indexer
directly with several spellings at once (title, title + year, original and
alternate titles, `S01` / `Season 1` for shows), keeps the releases that actually
name the title, and ranks live ones first. Results get short ids so the agent can
grab one without copying a magnet link around.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .clients.arr import Prowlarr, Radarr, Sonarr

CATEGORIES = {"movie": [2000], "show": [5000]}  # Newznab: Movies, TV
STOP = {"the", "a", "an", "of", "and", "la", "le"}
RES_RE = re.compile(r"\b(2160p|4k|uhd|1080p|720p|576p|480p)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19[2-9]\d|20[0-4]\d)\b")
SEASON_RE = re.compile(r"\bs(\d{1,2})(?:e\d{1,3})?\b|\bseason[ ._-]?(\d{1,2})\b", re.IGNORECASE)
MAX_NAMES = 4
TTL = 6 * 3600
# Added to a magnet built from a bare info-hash (DHT finds peers too, this is faster).
TRACKERS = ("udp://tracker.opentrackr.org:1337/announce", "udp://open.demonii.com:1337/announce",
            "udp://tracker.torrent.eu.org:451/announce")

_results: dict[str, tuple[float, dict]] = {}  # short id -> (stored at, prowlarr result)
_next_id = 0


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def words(name: str) -> list[str]:
    w = norm(name).split()
    return [x for x in w if x not in STOP] or w


def latinish(s: str) -> bool:
    """Indexers name releases in Latin script; skip CJK/Cyrillic alternate titles."""
    return bool(s) and all(ord(c) < 0x250 for c in s)


def pick_hit(hits: list[dict], title: str, year: int | None = None) -> tuple[dict | None, bool]:
    """Best lookup hit and whether it was an exact title match. Prefers exact title +
    year, then exact title, then the year, then the lookup's own top hit."""
    if not hits:
        return None, False
    t = norm(title)
    exact = [h for h in hits if norm(h.get("title", "")) == t]
    for pool in ([h for h in exact if year and h.get("year") == year], exact,
                 [h for h in hits if year and h.get("year") == year]):
        if pool:
            return pool[0], bool(exact) and pool[0] in exact
    return hits[0], False


def names_for(title: str, kind: str, year: int | None = None) -> tuple[list[str], int | None]:
    """The title plus the canonical, original and alternate titles the arr lookup
    knows (lookup only; nothing is added), deduplicated, Latin-script only."""
    names, yr = [title], year
    try:
        arr = Radarr() if kind == "movie" else Sonarr()
        hit, _ = pick_hit(arr.lookup(title)[:6], title, year)
        alts = [a.get("title", "") for a in (hit or {}).get("alternateTitles") or []]
        # only trust the hit if one of its names contains what was asked for (a
        # loose lookup can return an unrelated show first)
        if hit and any(set(words(title)) <= set(words(n))
                       for n in [hit.get("title", ""), hit.get("originalTitle", ""), *alts]):
            yr = yr or hit.get("year")
            names += [hit.get("title", ""), hit.get("originalTitle", ""), *alts]
    except Exception as e:  # noqa: BLE001 — lookup is a nicety; the plain title still searches
        print(f"indexer_search: {kind} lookup for {title!r} failed: {e}", flush=True)
    out, seen = [], set()
    for n in names:
        k = norm(n)
        if k and k not in seen and latinish(n):
            seen.add(k)
            out.append(n)
    return out[:MAX_NAMES], yr


def queries(names: list[str], kind: str, year: int | None, season: int | None) -> list[str]:
    qs = []
    for i, n in enumerate(names):
        qs.append(n)
        if kind == "movie" and year and i == 0:
            qs.append(f"{n} {year}")
        if kind == "show" and season is not None and i < 2:
            qs += [f"{n} S{season:02d}", f"{n} Season {season}"]
    return list(dict.fromkeys(qs))


def relevant(r: dict, names: list[str], kind: str, year: int | None, season: int | None) -> bool:
    """Keep a release only if its name contains every word of one of the titles, isn't
    a different-year namesake (movies), and covers the season asked for (shows; a
    pack without season markers counts)."""
    t = norm(r.get("title", ""))
    tw = set(t.split())
    if not any(set(words(n)) <= tw for n in names):
        return False
    if kind == "movie" and year:
        years = [int(y) for y in YEAR_RE.findall(r.get("title", ""))]
        if years and all(abs(y - year) > 1 for y in years):
            return False
    if kind == "show" and season is not None:
        marks = [int(a or b) for a, b in SEASON_RE.findall(r.get("title", ""))]
        if marks and season not in marks and "complete" not in t:
            return False
    return True


def resolution(title: str) -> str:
    m = RES_RE.search(title or "")
    if not m:
        return "?"
    v = m.group(1).lower()
    return "2160p" if v in ("4k", "uhd") else v


def rank_key(r: dict, want: str, kind: str = "movie", season: int | None = None) -> tuple:
    """Live first; for shows, real season packs before movies / other seasons; then
    the wanted resolution; then seeders."""
    title = r.get("title", "")
    seeds = r.get("seeders") or 0
    off_kind = False
    if kind == "show":
        marks = [int(a or b) for a, b in SEASON_RE.findall(title)]
        off_kind = bool(re.search(r"\bmovie\b|\bfilm\b", title, re.IGNORECASE)) or (
            season is not None and bool(marks) and season not in marks)
    return (seeds <= 0, off_kind, resolution(title) != want, -seeds)


def dedupe(results: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in results:
        k = (r.get("infoHash") or "").lower() or r.get("guid") or r.get("title")
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def search(title: str, kind: str, year: int | None = None, season: int | None = None,
           want_resolution: str = "1080p") -> tuple[list[dict], list[str], int | None]:
    """All relevant releases across indexers, best first; plus the names searched."""
    names, yr = names_for(title, kind, year)
    qs = queries(names, kind, yr, season)
    cats = CATEGORIES.get(kind)

    def one(q: str) -> list[dict]:
        try:
            return Prowlarr().search(q, categories=cats, limit=100) or []
        except Exception:  # noqa: BLE001 — one failing query must not sink the rest
            return []

    with ThreadPoolExecutor(max_workers=min(6, len(qs))) as ex:
        found = [r for batch in ex.map(one, qs) for r in batch]
    rels = [r for r in dedupe(found) if relevant(r, names, kind, yr, season)]
    return sorted(rels, key=lambda r: rank_key(r, want_resolution, kind, season)), names, yr


def remember(r: dict) -> str:
    global _next_id
    now = time.time()
    for k in [k for k, (t, _) in _results.items() if now - t > TTL]:
        del _results[k]
    _next_id += 1
    rid = f"i{_next_id}"
    _results[rid] = (now, r)
    return rid


def recall(rid: str) -> dict | None:
    hit = _results.get(rid.strip().lower())
    return hit[1] if hit and time.time() - hit[0] <= TTL else None


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def render(rels: list[dict], names: list[str], limit: int = 8) -> str:
    if not rels:
        return f"free-text search ({' / '.join(names)}): nothing that names the title."
    alive = sum(1 for r in rels if (r.get("seeders") or 0) > 0)
    lines = [f"free-text search ({' / '.join(names)}): {len(rels)} releases, {alive} with seeders"]
    for r in rels[:limit]:
        seeds = r.get("seeders") or 0
        lines.append(f"[{remember(r)}] {r.get('title', '?')}\n"
                     f"    {resolution(r.get('title', ''))} · {fmt_size(r.get('size') or 0)} · "
                     f"{'DEAD (0 seeders)' if seeds <= 0 else f'{seeds} seed'} · {r.get('indexer', '?')}")
    return "\n".join(lines)


def source_for(r: dict, fetch: Any) -> tuple[str, Any]:
    """('magnet', uri) or ('torrent', bytes) for a result. Prowlarr often only gives a
    proxied downloadUrl (qBittorrent, in Docker, may not reach it), so resolve it
    here: it either redirects to a magnet or serves the .torrent itself."""
    for k in ("magnetUrl", "guid"):  # some indexers put the magnet itself here
        if (r.get(k) or "").startswith("magnet:"):
            return "magnet", r[k]
    url = r.get("downloadUrl") or r.get("magnetUrl")
    problem = "no download link"
    if url:
        try:
            resp = fetch(url)
            loc = resp.headers.get("location", "")
            if loc.startswith("magnet:"):
                return "magnet", loc
            if resp.status_code == 200 and resp.content[:1] == b"d":  # bencoded .torrent
                return "torrent", resp.content
            problem = f"download link gave HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001 — fall back to the info-hash below
            problem = f"download link failed: {e}"
    ih = (r.get("infoHash") or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}|[A-Z2-7]{32}", ih):
        q = urllib.parse.urlencode([("dn", r.get("title", ""))] + [("tr", t) for t in TRACKERS])
        return "magnet", f"magnet:?xt=urn:btih:{ih}&{q}"
    raise ValueError(f"{problem}, and no info-hash to fall back on")
