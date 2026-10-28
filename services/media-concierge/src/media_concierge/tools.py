"""@beta_tool functions exposed to the agent. Thin glue over the clients.

Every mutating call appends a line to state/logs/actions.jsonl. Read tools don't.
All tools return strings (human-readable, sometimes compact JSON) so the model can
relay them straight to Discord.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
from anthropic import beta_tool

from . import costmeter, ctx, disk, indexer_search, jobs, organize, subs
from .clients.arr import Radarr, Sonarr
from .clients.jellyfin import Jellyfin
from .clients.qbit import Qbit
from .config import CFG

_ACTIONS = CFG.log_dir / "actions.jsonl"


def _log(tool: str, **kw) -> None:
    _ACTIONS.open("a").write(json.dumps({"ts": time.time(), "tool": tool, **kw}) + "\n")


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ─────────────────────────────  READ  ─────────────────────────────

@beta_tool
def library_report(kind: str = "all", detail: str = "summary") -> str:
    """Report what's in the Jellyfin library.

    Args:
        kind: "all", "movies", or "shows".
        detail: "summary" (counts + disk) or "full" (every title with year/size).
    """
    jf = Jellyfin()
    kinds = {"movies": ("Movie",), "shows": ("Series",)}.get(kind, ("Movie", "Series"))
    items = jf.items(kinds)
    d = disk.df()
    lines = [f"Library: {len(items)} titles  |  disk {d['free_gb']} GB free of "
             f"{d['total_gb']} GB ({d['pct_used']}% used)"]
    if detail == "full":
        sizes = {t.path: t for t in disk.inventory_sizes()}
        for it in items:
            ts = sizes.get(it.get("Path", ""))
            sz = _fmt_bytes(ts.bytes) if ts else "?"
            pl = " ✓watched" if ts and ts.played else ""
            lines.append(f"  [{it['Type'][:5]}] {it['Name']} ({it.get('ProductionYear','?')}) "
                         f"— {sz}{pl}")
    else:
        mv = sum(1 for it in items if it["Type"] == "Movie")
        sh = sum(1 for it in items if it["Type"] == "Series")
        lines.append(f"  {mv} movies, {sh} shows")
    return "\n".join(lines)


@beta_tool
def queue_status() -> str:
    """Current downloads and their progress (qBittorrent + Radarr/Sonarr import queue)."""
    q = Qbit()
    rows = []
    for t in q.torrents():
        eta = t.get("eta", 0)
        eta_s = f"{eta // 60}m" if 0 < eta < 8640000 else "—"
        rows.append(f"  {t['progress']*100:5.1f}%  {t['state']:11}  {eta_s:>5}  "
                    f"{_fmt_bytes(t.get('dlspeed',0))}/s  {t['name'][:64]}")
    if not rows:
        rows = ["  (nothing downloading)"]
    arrq = []
    for app, name in ((Radarr(), "R"), (Sonarr(), "S")):
        try:
            for rec in app.queue():
                arrq.append(f"  [{name}] {rec.get('title','?')[:60]} — {rec.get('status','?')}")
        except Exception:  # noqa: BLE001
            pass
    return "Downloads:\n" + "\n".join(rows) + ("\nImport queue:\n" + "\n".join(arrq) if arrq else "")


@beta_tool
def disk_report() -> str:
    """Free space plus the largest titles and a reclaim plan to the safety floor."""
    plan = disk.reclaim_plan()
    d = disk.df()
    big = sorted(disk.inventory_sizes(), key=lambda t: -t.bytes)[:8]
    out = [f"{d['free_gb']} GB free / {d['total_gb']} GB ({d['pct_used']}% used). "
           f"Floor is {CFG.min_free_gb} GB."]
    out.append("Biggest titles:")
    for t in big:
        out.append(f"  {_fmt_bytes(t.bytes):>9}  {t.title} ({t.year})"
                   f"{'  ✓watched' if t.played else ''}")
    if not plan["enough"]:
        out.append(f"⚠️ below floor. To free {plan['need_gb']} GB I'd remove:")
    elif plan["need_gb"] > 0:
        out.append(f"To reach the floor I'd remove:")
    else:
        out.append("Above the floor — nothing needs removing.")
    for p in plan["plan"]:
        out.append(f"  − {p['title']} ({p['year']}) {p['gb']} GB"
                   f"{'  watched' if p['played'] else '  UNWATCHED'}")
    return "\n".join(out)


@beta_tool
def find(query: str, kind: str = "auto") -> str:
    """Resolve a title to a canonical entry (id, year, plot) and say if it's in the library.

    Args:
        query: what the user called it, e.g. "dune part 2" or "the bear season 3".
        kind: "movie", "show", or "auto".
    """
    out = []
    if kind in ("movie", "auto"):
        for h in Radarr().lookup(query)[:4]:
            inlib = " [in library]" if h.get("id") else ""
            out.append(f"MOVIE  {h['title']} ({h.get('year','?')})  tmdb:{h.get('tmdbId')}"
                       f"{inlib}\n    {(h.get('overview') or '')[:180]}")
    if kind in ("show", "auto"):
        for h in Sonarr().lookup(query)[:4]:
            inlib = " [in library]" if h.get("id") else ""
            out.append(f"SHOW   {h['title']} ({h.get('year','?')})  tvdb:{h.get('tvdbId')}"
                       f"{inlib}\n    {(h.get('overview') or '')[:180]}")
    return "\n".join(out) or f"nothing found for {query!r}"


def _release_line(r: dict) -> str:
    q = (r.get("quality") or {}).get("quality") or {}
    langs = ",".join(l.get("name", "?") for l in r.get("languages", [])) or "?"
    seeders = r.get("seeders", "?")
    rej = r.get("rejections") or []
    flag = "  ⚠ " + "; ".join(rej[:2]) if rej else ""
    return (f"{r.get('title','?')}\n"
            f"    {q.get('name','?')} · {q.get('resolution','?')}p · audio:{langs} · "
            f"{_fmt_bytes(r.get('size',0))} · {seeders} seed · {r.get('indexer','?')}"
            f"{flag}\n    guid={r.get('guid')}  indexerId={r.get('indexerId')}")


@beta_tool
def search_releases(title: str, kind: str, want_resolution: str = "1080p",
                    want_audio: str = "en", season: int | None = None,
                    episode: int | None = None, limit: int = 8,
                    year: int | None = None) -> str:
    """Search indexers (incl. 1337x via Prowlarr) for downloadable releases. READ-ONLY.

    Adds the title to Radarr/Sonarr *unmonitored, no auto-search* so their parser can
    rank releases; nothing is downloaded. Present the results to the user and WAIT for
    them to confirm which release to grab.

    Args:
        title: canonical title (use `find` first).
        kind: "movie" or "show".
        want_resolution: "2160p" | "1080p" | "720p".
        want_audio: ISO language for audio, e.g. "en".
        season / episode: for shows.
        limit: max candidates to return.
        year: release year, to pick the right title when several share a name.

    When this route has nothing live at the wanted resolution, a free-text search of
    every indexer (title + year + original/alternate titles) is appended; those
    results have ids like i3 and are grabbed with `grab_indexer_result`.
    """
    profile = {"2160p": "Ultra-HD", "1080p": "HD-1080p", "720p": "HD-720p"}.get(
        want_resolution, "HD-1080p")
    if kind == "movie":
        r = Radarr()
        hit, exact = indexer_search.pick_hit(r.lookup(title), title, year)
        if not hit:
            return f"no movie called {title!r} in the metadata lookup."
        m = r.add(hit["tmdbId"], quality=profile, monitored=False, search=False)
        rels = r.releases(m["id"])
    else:
        s = Sonarr()
        hit, exact = indexer_search.pick_hit(s.lookup(title), title, year)
        if not hit:
            return f"no show called {title!r} in the metadata lookup."
        sr = s.add(hit["tvdbId"], quality=profile, monitored=False, search=False,
                   season=season)
        ep_id = None
        if season is not None and episode is not None:
            ep = next((e for e in s.episodes(sr["id"])
                       if e["seasonNumber"] == season and e["episodeNumber"] == episode), None)
            ep_id = ep["id"] if ep else None
        rels = s.releases(sr["id"], season=season, episode_id=ep_id)

    def score(x: dict) -> tuple:
        q = (x.get("quality") or {}).get("quality") or {}
        res_ok = str(q.get("resolution")) in want_resolution
        aud_ok = any(want_audio.lower() in l.get("name", "").lower()
                     or l.get("name", "").lower().startswith("eng") and want_audio == "en"
                     for l in x.get("languages", []))
        return (not res_ok, not aud_ok, bool(x.get("rejections")), -x.get("seeders", 0))

    rels = sorted(rels, key=score)[:limit]
    assumed = "" if exact else f"(no exact title match; assumed {hit['title']} ({hit.get('year', '?')}))\n"
    usable = [x for x in rels if not x.get("rejections") and (x.get("seeders") or 0) > 0
              and str(((x.get("quality") or {}).get("quality") or {}).get("resolution")) in want_resolution]
    out = assumed
    if rels:
        out += (f"Candidates for {hit['title']} ({want_resolution}, {want_audio} audio) — "
                f"confirm which to grab:\n\n" +
                "\n\n".join(f"[{i+1}] " + _release_line(r) for i, r in enumerate(rels)))
    else:
        out += f"Radarr/Sonarr-matched search: nothing for {hit['title']!r} at {want_resolution}."
    if not usable:  # the arr route came up short: search every indexer by name
        try:
            free, names, _ = indexer_search.search(hit["title"], kind, year or hit.get("year"),
                                                   season, want_resolution)
            out += "\n\n" + indexer_search.render(free, names, limit)
        except Exception as e:  # noqa: BLE001 — the arr results above still stand
            out += f"\n\n(free-text search failed: {e})"
    return out


@beta_tool
def get(title: str, kind: str = "auto", quality: str = "1080p", audio: str = "en",
        subs_lang: str = "en", subs_mode: str = "soft", season: int | None = None,
        episode: int | None = None) -> str:
    """Convenience: run `find` then `search_releases` and return candidates for the user
    to confirm. Does NOT download. After the user picks one, call `grab`.
    """
    k = kind
    if k == "auto":
        k = "show" if (season is not None or "season" in title.lower()) else "movie"
    resolved = find(title, k)
    cands = search_releases(title, k, want_resolution=quality, want_audio=audio,
                            season=season, episode=episode)
    return (f"{resolved}\n\n{cands}\n\n"
            f"(defaults: {quality}, {audio} audio, {subs_lang} subs {subs_mode}) — "
            f"reply which number to grab.")


@beta_tool
def search_indexers(title: str, kind: str = "auto", year: int | None = None,
                    season: int | None = None, want_resolution: str = "1080p",
                    limit: int = 8) -> str:
    """Free-text search across EVERY indexer (Prowlarr), the way he'd search the site
    by hand: the title plus its year, original and alternate titles (romaji, regional
    names), and `S01` / `Season 1` for shows. Finds releases the Radarr/Sonarr-matched
    `search_releases` misses (alternate naming, odd packs). READ-ONLY.

    Results get ids like i3; after he confirms one, call `grab_indexer_result`.
    0-seeder releases are listed as DEAD: report "it exists but nobody is seeding",
    never "not found", when that's the situation.

    Args:
        title: what to look for (canonical or as he said it).
        kind: "movie", "show", or "auto" (show when a season is given).
        year: release year (sharpens movies with common names).
        season: for shows.
        want_resolution: ranked first: "2160p" | "1080p" | "720p".
        limit: max results to list.
    """
    k = kind if kind != "auto" else ("show" if season is not None else "movie")
    rels, names, _ = indexer_search.search(title, k, year, season, want_resolution)
    return indexer_search.render(rels, names, limit)


def _prowlarr_fetch(url: str) -> httpx.Response:
    return httpx.get(url, headers={"X-Api-Key": CFG.prowlarr_key}, timeout=60.0,
                     follow_redirects=False)


@beta_tool
def grab_indexer_result(result_id: str, kind: str = "auto", title_hint: str | None = None,
                        subs_lang: str = "en", subs_mode: str = "soft") -> str:
    """Download a `search_indexers` / `search_releases` free-text result (id like i3)
    and FULL-import it, exactly like `add_magnet` (download -> identify -> file ->
    styled subs -> scan). THE GRAB GATE APPLIES: only after he confirmed this release.

    Args:
        result_id: the id from the search output, e.g. "i3".
        kind: "movie", "show", or "auto".
        title_hint: override the parsed title only if the release name is junk.
        subs_lang / subs_mode: as for add_magnet.
    """
    r = indexer_search.recall(result_id)
    if not r:
        return f"no search result {result_id!r} (they expire after 6h) — search again."
    try:
        how, src = indexer_search.source_for(r, _prowlarr_fetch)
    except (ValueError, httpx.HTTPError) as e:
        return f"couldn't get a download for {r.get('title', result_id)}: {e}"
    label = title_hint or r.get("title") or result_id
    if how == "magnet":
        ih, _ = _magnet_info(src)
        return _start_manual(lambda q, tag: q.add_url(src, category=MANUAL_CATEGORY,
                                                     save_path=CFG.media_downloads, tags=tag),
                             label, ih, kind, title_hint, subs_lang, subs_mode, source="indexer")
    return _start_manual(lambda q, tag: q.add_file(src, category=MANUAL_CATEGORY,
                                                  save_path=CFG.media_downloads, tags=tag),
                         label, (r.get("infoHash") or "").lower() or None, kind, title_hint,
                         subs_lang, subs_mode, source="indexer")


# ─────────────────────────────  MUTATE  ─────────────────────────────

MANUAL_CATEGORY = "manual"
_BTIH = re.compile(r"xt=urn:btih:([0-9a-zA-Z]+)", re.I)


def _magnet_info(magnet: str) -> tuple[str | None, str | None]:
    """(infohash hex lowercase, display-name) from a magnet link, else (None, None)."""
    m = _BTIH.search(magnet)
    if not m:
        return None, None
    ih = m.group(1)
    if len(ih) == 32:                       # base32 infohash -> hex
        try:
            ih = base64.b32decode(ih.upper()).hex()
        except Exception:  # noqa: BLE001
            return None, None
    ih = ih.lower()
    dn = None
    d = re.search(r"[?&]dn=([^&]+)", magnet)
    if d:
        dn = unquote_plus(d.group(1))
    return ih, dn


@beta_tool
def add_magnet(magnet: str, title_hint: str | None = None, kind: str = "auto",
               subs_lang: str = "en", subs_mode: str = "soft") -> str:
    """Download a magnet link (or http[s] .torrent URL) the owner found himself (e.g. on
    1337x, when the indexer search can't find it) and FULL-import it into the library:
    download -> identify from the torrent title -> file as Title (Year) / Season NN with
    SxxExx names (handles season packs) -> mux styled subs per file -> Jellyfin scan.

    Him handing you the link IS the go-ahead: the grab confirmation gate does not apply.
    The poller posts progress and the final result; do not poll it yourself.

    Args:
        magnet: a magnet:?xt=urn:btih:... link, or an http(s) URL to a .torrent file.
        title_hint: override the parsed title only when the torrent name is garbage.
        kind: "movie", "show", or "auto" (infer from the torrent title).
        subs_lang: ISO language for the styled subtitle track (default en).
        subs_mode: "soft" (mux, default) or "burn".
    """
    magnet = magnet.strip()
    is_url = magnet.lower().startswith(("http://", "https://"))
    if not is_url and not magnet.lower().startswith("magnet:"):
        return "that's not a magnet link or a .torrent URL."
    ih, dn = (None, None) if is_url else _magnet_info(magnet)
    if not is_url and not ih:
        return "couldn't parse an infohash out of that magnet."
    label = title_hint or dn or (ih[:12] if ih else "torrent")
    return _start_manual(lambda q, tag: q.add_url(magnet, category=MANUAL_CATEGORY,
                                                 save_path=CFG.media_downloads, tags=tag),
                         label, ih, kind, title_hint, subs_lang, subs_mode, source="magnet")


def _start_manual(add, label: str, ih: str | None, kind: str, title_hint: str | None,
                  subs_lang: str, subs_mode: str, source: str) -> str:
    """Hand a torrent to qBittorrent under a job tag; the poller then downloads,
    identifies, files it into the library, adds subs and scans (the "magnet" job)."""
    tag = "mb-" + uuid.uuid4().hex[:8]
    q = Qbit()
    q.add_category(MANUAL_CATEGORY, CFG.media_downloads)
    add(q, tag)
    j = jobs.create("magnet", label, ctx.channel_id(), tag=tag, infohash=ih,
                    media_kind=kind, title_hint=title_hint, subs_lang=subs_lang,
                    subs_mode=subs_mode)
    _log("add_magnet", infohash=ih, tag=tag, label=label, kind=kind, job=j.id, source=source)
    return (f"Added **{label}** -> qBittorrent (job {j.id}). I'll download it, file it "
            f"into the library with {subs_lang} subs, and post here when it's in Jellyfin.")


@beta_tool
def grab(kind: str, guid: str, indexer_id: int, target_title: str,
         subs_lang: str = "en", subs_mode: str = "soft") -> str:
    """Download a specific release the user just confirmed. Sends it to qBittorrent via
    Radarr/Sonarr, which will import + rename into the library on completion.

    NEVER call this without the user confirming the exact release in this conversation.

    Args:
        kind: "movie" or "show".
        guid: the chosen candidate's guid from `search_releases`.
        indexer_id: the chosen candidate's indexerId from `search_releases`.
        target_title: canonical title, for the job label.
        subs_lang: ISO language for the subtitle track to add after import.
        subs_mode: "soft" or "burn".
    """
    channel_id = ctx.channel_id()
    app = Radarr() if kind == "movie" else Sonarr()
    # flip the title to monitored so import isn't rejected
    if kind == "movie":
        m = next((x for x in Radarr().list()
                  if x["title"].lower() == target_title.lower()), None)
        if m and not m["monitored"]:
            m["monitored"] = True
            app._j("PUT", f"/movie/{m['id']}", json=m)  # noqa: SLF001
    app.grab_release(guid, indexer_id)
    j = jobs.create("download", target_title, channel_id, media_kind=kind, guid=guid,
                    subs_lang=subs_lang, subs_mode=subs_mode)
    _log("grab", title=target_title, guid=guid, indexer_id=indexer_id, job=j.id)
    return (f"Grabbed **{target_title}** → qBittorrent (job {j.id}). "
            f"I'll post here when it's imported and subtitled.")


@beta_tool
def import_file(path: str, kind: str = "auto", title_hint: str | None = None,
                confirmed: bool = False) -> str:
    """Identify a loose file/folder already on disk and file it into the Jellyfin library
    with the correct Title (Year) / SxxExx layout, then trigger a scan.

    First call with confirmed=False to see the identification; call again with
    confirmed=True once the user agrees.

    Args:
        path: absolute path to the file or folder (often under /media/downloads).
        kind: "movie", "show", or "auto".
        title_hint: override if the filename is garbage.
        confirmed: set True only after the user confirms the identification.
    """
    ident = organize.identify(path, title_hint)
    if kind == "movie" and ident.kind != "movie":
        ident.kind = "movie"
    if kind == "show" and ident.kind != "episode":
        ident.kind = "episode"
    if not confirmed:
        cand = "\n".join(f"    - {c}" for c in ident.candidates[:4])
        return (f"Identified as {ident.kind}: **{ident.title} ({ident.year})** "
                f"[{ident.confidence}]"
                + (f" S{ident.season:02d}E{ident.episode:02d}"
                   if ident.kind == "episode" and ident.season else "")
                + (f"\n  other matches:\n{cand}" if cand else "")
                + "\n  confirm to file it into the library.")
    result = organize.import_file(path, ident)
    Jellyfin().refresh()
    _log("import_file", path=path, dest=result["library_path"])
    return (f"Filed into {result['library_path']}\n  moved: "
            + ", ".join(Path(f).name for f in result["files"])
            + (f"\n  cleaned {len(result['cleaned'])} junk files" if result["cleaned"] else "")
            + "\n  Jellyfin scan triggered.")


@beta_tool
def attach_subs(video_path: str, lang: str = "en", mode: str = "soft") -> str:
    """Add a subtitle track in `lang` to a library file, styled to the house look
    (yellow / black outline / Helvetica / italic — configurable via
    MEDIA_CONCIERGE_SUBS_* in .env).

    Source priority (so timing always matches the video):
      1. an embedded text track already in the file (came with the release)
      2. a sidecar .srt/.ass from the same torrent
      3. only then, an online fetch (and it says so, so you can sanity-check timing)
    When there are several tracks in the language, the primary one (most dialogue;
    never a forced / signs & songs track; plain over SDH on a tie) is used.

    Args:
        video_path: absolute path to the video in the library (.mkv/.mp4/.m4v).
        lang: ISO language, e.g. "en", "es".
        mode: "soft" (default: a styled .ass sidecar flagged default; the video file
              is left untouched, so the TV-friendly mp4 stays direct-playable) or
              "burn" (baked into the video, slow).
    """
    return _attach_subs(video_path, lang, mode, ctx.channel_id())


def _attach_subs(video_path: str, lang: str, mode: str, channel_id: int) -> str:
    p = Path(video_path)
    if not p.is_file():
        return f"no such file: {video_path}"
    src = subs.pick_source(video_path, lang)
    if src is None:
        info = subs.probe(video_path)
        return (f"no {lang} subtitle found for {p.name} — not embedded, no release "
                f"sidecar, nothing online. embedded tracks: "
                f"{[e['lang'] for e in info['embedded']] or 'none'}.")
    kind, ref = src
    if kind == "done":
        if not subs.needs_restyle(video_path, lang):
            return f"{p.name} already has the current styled {lang} track."
        # style is stale (e.g. font changed): restyle from our existing styled track
        side = subs.styled_sidecar(video_path, lang)
        kind, ref = ("sidecar", str(side)) if side.exists() else ("embedded", "0")
    note = {"embedded": "embedded track (release-synced)",
            "sidecar": "release sidecar (synced)",
            "online": "fetched online — check timing"}[kind]
    try:
        if kind == "embedded":
            ass = subs.to_styled_ass(video_path, embedded_rel_index=ref)
        else:
            ass = subs.to_styled_ass(ref)
    except Exception as e:  # noqa: BLE001
        return f"subtitle conversion failed: {e}"

    if mode == "soft":
        final = subs.write_sidecar(video_path, ass, lang)
        if kind == "online":
            subs.cleanup_online(ref)
        try:
            Jellyfin().refresh()
        except Exception as e:  # noqa: BLE001 — the next library scan picks it up anyway
            print(f"attach_subs: jellyfin refresh failed: {e}", flush=True)
        _log("attach_subs", video=video_path, sidecar=final, lang=lang, mode="soft",
             source=kind)
        return (f"Added styled {lang} subs as {Path(final).name} (default track; "
                f"video untouched). source: {note}.")

    j = jobs.create("burn", p.name, channel_id, video=video_path, lang=lang)
    subs.burn_async(video_path, ass, j.id)
    _log("attach_subs", video=video_path, lang=lang, mode="burn", job=j.id, source=kind)
    return (f"Burning styled {lang} subs into {p.name} (job {j.id}). source: {note}. "
            f"Slow on this box — I'll report when done.")


@beta_tool
def reclaim_space(target_gb: float | None = None, execute: bool = False) -> str:
    """Show (or run) a plan to free disk space: watched titles first, then oldest-added.

    Args:
        target_gb: free-space target; defaults to the safety floor.
        execute: False = just show the plan; True = actually delete (via Radarr/Sonarr,
                 which trash the files).
    """
    plan = disk.reclaim_plan(target_gb)
    if not execute:
        body = "\n".join(f"  − {p['title']} ({p['year']}) {p['gb']} GB"
                         f"{'  watched' if p['played'] else '  UNWATCHED'}"
                         for p in plan["plan"]) or "  (nothing to remove)"
        return (f"free {plan['free_gb']} GB, target {plan['target_gb']} GB. Plan frees "
                f"~{plan['would_free_gb']} GB:\n{body}\n(call again with execute=true to do it)")
    log = disk.execute_reclaim(plan["plan"])
    _log("reclaim_space", target_gb=plan["target_gb"], removed=len(plan["plan"]))
    Jellyfin().refresh()
    return "Reclaimed:\n" + "\n".join("  " + l for l in log) + \
           f"\nNow {disk.free_gb():.1f} GB free."


@beta_tool
def jellyfin_scan(path: str | None = None) -> str:
    """Trigger a Jellyfin library scan (full, or targeted if a path is given)."""
    Jellyfin().refresh()
    _log("jellyfin_scan", path=path)
    return "Jellyfin library scan started."


@beta_tool
def remove(title: str, kind: str, delete_files: bool = True) -> str:
    """Remove a movie or show from the library (and its files) via Radarr/Sonarr.

    Args:
        title: exact library title.
        kind: "movie" or "show".
        delete_files: also delete media files (default True).
    """
    if kind == "movie":
        m = next((x for x in Radarr().list() if x["title"].lower() == title.lower()), None)
        if not m:
            return f"no movie titled {title!r} in Radarr."
        Radarr().delete(m["id"], delete_files)
    else:
        sh = next((x for x in Sonarr().list() if x["title"].lower() == title.lower()), None)
        if not sh:
            return f"no show titled {title!r} in Sonarr."
        Sonarr().delete(sh["id"], delete_files)
    _log("remove", title=title, kind=kind, delete_files=delete_files)
    Jellyfin().refresh()
    return f"Removed {title} ({'files deleted' if delete_files else 'kept files'})."


@beta_tool
def job_status(job_id: str | None = None) -> str:
    """List tracked long-running jobs (downloads, subtitle burns) and their last status."""
    js = [jobs.get(job_id)] if job_id else jobs.all_jobs()[:12]
    js = [j for j in js if j]
    if not js:
        return "no jobs."
    return "\n".join(f"  {j.id} [{j.state}] {j.kind}: {j.title} — {j.last_report or '…'}"
                     for j in js)


@beta_tool
def cost_status() -> str:
    """Estimated Anthropic API credit remaining and spend so far (local estimate from
    token usage; the API key can't read the real balance)."""
    d = costmeter.status()
    if not d.get("configured"):
        return ("cost tracking not set. Tell me your current credit balance "
                "(e.g. \"credit is $40\") and I'll warn you near "
                f"${CFG.cost_warn_usd:.0f}.")
    return (f"~${d['remaining_usd']:.2f} left · spent ~${d['spent_usd']:.2f} over "
            f"{d['turns']} turns · warn at ${CFG.cost_warn_usd:.0f}")


@beta_tool
def set_credit(usd: float, mode: str = "set") -> str:
    """Update the tracked API credit balance.

    Args:
        usd: dollar amount.
        mode: "set" (this is the balance now) or "add" (I just topped up by this much).
    """
    d = costmeter.add_credit(usd) if mode == "add" else costmeter.set_remaining(usd)
    _log("set_credit", usd=usd, mode=mode)
    return f"credit {'+' if mode == 'add' else '='}${usd:.2f} → ~${d['remaining_usd']:.2f} tracked."


@beta_tool
def run_shell(cmd: str, why: str) -> str:
    """Run an arbitrary shell command on the host as the deploy user (passwordless sudo
    available). Use for anything the other tools don't cover. Prefer `trash` over `rm`.

    Args:
        cmd: the command line.
        why: one-line reason (logged).
    """
    _log("run_shell", cmd=cmd, why=why)
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(Path.home()), capture_output=True,
                           text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return "timed out after 300s"
    out = (p.stdout or "")[-4000:]
    err = (p.stderr or "")[-1500:]
    return f"exit {p.returncode}\n--- stdout ---\n{out}" + (f"\n--- stderr ---\n{err}" if err else "")


@beta_tool
def empty_trash(older_than_days: int | None = None, dry_run: bool = False) -> str:
    """Permanently purge trash. Deletes (mine or the arrs') go to trash — recoverable,
    not erased — until this runs. It also runs automatically once a week; call this to
    empty it right now.

    Args:
        older_than_days: only purge items trashed more than this many days ago (keeps a
            safety window); omit to empty everything now.
        dry_run: just list what would be purged; delete nothing.
    """
    cmd = ["trash-empty", "-f"]
    if dry_run:
        cmd.append("--dry-run")
    if older_than_days is not None:
        cmd.append(str(older_than_days))
    before = disk.free_gb()
    try:
        p = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "trash-empty timed out after 600s"
    if dry_run:
        listing = (p.stdout or "").strip() or "(nothing to purge)"
        return "dry run — would purge:\n" + listing[:3500]
    _log("empty_trash", older_than_days=older_than_days)
    freed = disk.free_gb() - before
    scope = f" older than {older_than_days}d" if older_than_days is not None else ""
    return f"Emptied trash{scope}. Freed ~{freed:.1f} GB · now {disk.free_gb():.1f} GB free."


ALL_TOOLS = [
    library_report, queue_status, disk_report, find, search_releases, search_indexers, get,
    grab, grab_indexer_result, add_magnet, import_file, attach_subs, reclaim_space, jellyfin_scan, remove,
    empty_trash, job_status, cost_status, set_credit, run_shell,
]
