#!/usr/bin/env python3
# sports_api.py -- clean-stream extractor + HLS proxy (blacklodge :8100, vega :8098).
# Headless Chromium (playwright) loads each third-party stream page,
# executes its (often obfuscated) player JS, and we intercept the real
# m3u8/mp4 requests. Ad domains are route-blocked so the browser never
# even loads them. Candidates are validated (referer probe) before the
# TV ever sees them, then served through the referer-spoofing proxy.
#
#   GET /schedule                 today's games from the source site
#   GET /event?path=/nba/123     raw stream anchors for an event
#   GET /prepare?path=/nba/123   progressive resolve+validate of ALL sources
#        [&since=<version>&wait=<s>] long-poll: returns once the job's version
#        passes `since` (or, without `since`, once any source works) or it ends
#   GET /pxy/<sid>/<quoted-abs-url>                  HLS proxy (CORS)

import asyncio
import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

PORT = int(os.environ.get("SPORTS_API_PORT", "8098"))
# LAN host/IP the TV reaches this API at — baked into the proxy URLs we hand the
# TV, so it must be this host's address on the TV's network. Set per host in the
# gitignored .env (see .env.example); the loopback default is just a safe fallback.
PXY_HOST = os.environ.get("SPORTS_API_HOST", "127.0.0.1")
# Schedule/event site. Deliberately kept out of the repo: set it in the
# gitignored .env (see .env.example).
BASE = os.environ.get("SPORTS_SOURCE_URL", "").rstrip("/")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
AD_HOSTS = ("histats.com", "google-analytics", "googletagmanager",
            "googlesyndication", "reliedhounder", "lauansaltire", "popads",
            "doubleclick", "adservice", "livelog.site", "cloudflareinsights",
            "aclib", "s10.histats", "amazon-adsystem", "adsrv", "adcash",
            "propellerads", "adsterra", "onclckds", "toponad")
SESSIONS = {}
SESS_TTL = 6 * 3600
_sess_lock = threading.Lock()
_cache = {"schedule": (0, None), "event": {}}
SCHED_TTL = 60
PREPARE_TTL = 600     # reuse a finished job that found working streams
DEAD_TTL = 60         # ...or one that found nothing, but retry sooner
# Concurrent headless-browser contexts, and per-job source workers (more than
# MAX_CTX so static fallback + validation overlap with browser work).
MAX_CTX = int(os.environ.get("SPORTS_MAX_CTX", "8"))
WORKERS = MAX_CTX + 4
RESOLVE_BUDGET = 16   # s per source page load, goto included
FOUND_GRACE = 1.5     # s to keep listening after the first stream shows up
# Optional faster peer (e.g. vega's instance) to hand /prepare to. Browser
# extraction is CPU-bound; when the peer answers, the TV gets its (proxy-
# rewritten) result, otherwise we resolve locally — the TV never depends on it.
DELEGATE = os.environ.get("SPORTS_DELEGATE", "").rstrip("/")
DELEGATE_BACKOFF = 60  # s to stop trying the peer after it fails
_delegate_down_until = 0.0
_delegate_seen_ok = 0.0  # last successful contact; re-ping after 30s
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state")
# Event paths are /<sport>/<id>; the id is usually numeric but can be a slug
# (e.g. the always-on /nfl/redzone channel).
PATH_RE = re.compile(r"^/[a-z0-9-]+/[a-z0-9-]+$")

M3U8_RE = re.compile(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*')
MP4_RE = re.compile(r'https?://[^\s"\'<>\\]+\.mp4[^\s"\'<>\\]*')
REL_M3U8_RE = re.compile(r'["\'](/[^"\']{2,}\.m3u8[^"\']*)["\']')
IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
ATOB_RE = re.compile(r'atob\(["\']([A-Za-z0-9+/=]{16,})["\']')

_loop = None
_pw = None
_browser = None
_ctx_sem = None


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg, flush=True)


def fetch(url, referer=None, timeout=10, headers=None, limit=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    if referer:
        req.add_header("Referer", referer)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(limit) if limit else r.read()


def unjs(s):
    return s.replace("\\u0026", "&").replace("\\/", "/").replace("\\", "")


def is_ad(url):
    u = url.lower()
    return any(h in u for h in AD_HOSTS)


def absolutize(u, page_url):
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    if not re.match(r'^https?://', u):
        u = urllib.parse.urljoin(page_url, u)
    return u


# ---------------- headless browser resolver ----------------

async def _get_browser():
    global _browser
    if _browser is None or not _browser.is_connected():
        _browser = await _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--mute-audio",
                  "--disable-gpu", "--blink-settings=imagesEnabled=false"])
        log("browser (re)launched")
    return _browser


async def _router(route):
    u = route.request.url.lower()
    if is_ad(u) or "/ads/" in u or "vast" in u or "banner" in u or "popunder" in u:
        await route.abort()
    else:
        await route.continue_()


async def _aresolve(url, referer, budget):
    tq = time.time()
    async with _ctx_sem:
        tw = time.time() - tq
        browser = await _get_browser()
        ctx = await browser.new_context(
            user_agent=UA, locale="en-US",
            viewport={"width": 1280, "height": 720})
        found = []

        async def on_response(resp):
            try:
                u = resp.url
                base = u.split("?")[0]
                if base.endswith(".m3u8") or base.endswith(".mp4"):
                    if not is_ad(u) and all(f[0] != u for f in found):
                        ref = resp.request.headers.get("referer") or url
                        found.append((u, ref))
                else:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct or "javascript" in ct:
                        body = await resp.text()
                        body = unjs(body)
                        for m in M3U8_RE.findall(body):
                            m2 = absolutize(m, u)
                            if not is_ad(m2) and all(f[0] != m2 for f in found):
                                found.append((m2, u))
            except Exception:
                pass

        try:
            await ctx.add_init_script(
                "window.open=function(){return null;}")
            await ctx.route("**/*", _router)
            page = await ctx.new_page()
            page.on("response", on_response)
            page.on("popup", lambda p: asyncio.ensure_future(p.close()))
            t0 = time.time()
            try:
                await page.goto(url, referer=referer,
                                timeout=int(min(12, budget) * 1000),
                                wait_until="domcontentloaded")
            except Exception:
                pass
            tg = time.time() - t0
            try:
                await page.mouse.click(640, 360)
            except Exception:
                pass
            first = None
            while time.time() - t0 < budget:
                if found:
                    first = first or time.time()
                    if time.time() - first >= FOUND_GRACE:
                        break
                await asyncio.sleep(0.25)
            log("    browse %-28s queued %.1fs goto %.1fs first %s total %.1fs" % (
                host_of(url)[:28], tw, tg,
                "%.1fs" % (first - t0) if first else "-", time.time() - t0))
            return found
        finally:
            await ctx.close()


def bresolve(url, referer, budget=18):
    """Run one page-load extraction; returns [(stream_url, referer_full)]."""
    if _loop is None:
        return []
    fut = asyncio.run_coroutine_threadsafe(
        _aresolve(url, referer, budget), _loop)
    return fut.result(budget + 25)


def start_browser():
    global _loop, _pw, _ctx_sem
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True).start()
    fut = asyncio.run_coroutine_threadsafe(_init(), _loop)
    fut.result(60)


async def _init():
    global _pw, _ctx_sem
    _pw = await async_playwright().start()
    _ctx_sem = asyncio.Semaphore(MAX_CTX)
    await _get_browser()


# ---------------- validation + sessions ----------------

def register(headers, alts):
    """New proxy session. `headers` is the set that validated; `alts` are the
    other plausible sets, tried by the proxy if the CDN starts refusing."""
    sid = secrets.token_hex(4)
    now = time.time()
    with _sess_lock:
        SESSIONS[sid] = {"headers": headers, "alts": alts, "ts": now}
        for k in [k for k, v in SESSIONS.items() if now - v["ts"] > SESS_TTL]:
            del SESSIONS[k]
    return sid


# Some CDNs disguise TS segments as images (a real PNG/WebP header, then the TS
# packets) and some "streams" are pure decoys whose segments are only an image.
IMAGE_MAGIC = (b"\x89PNG", b"RIFF", b"\xff\xd8\xff", b"GIF8")
MP4_BOXES = (b"ftyp", b"styp", b"moof", b"sidx")


def ts_offset(b, scan=65536):
    """Offset of the first of 3 consecutive 188-byte TS packets, or None."""
    for i in range(min(len(b) - 376, scan)):
        if b[i] == 0x47 and b[i + 188] == 0x47 and b[i + 376] == 0x47:
            return i
    return None


def unwrap_segment(b):
    """Strip a fake image header from a disguised TS segment."""
    if b[:4].startswith(IMAGE_MAGIC):
        off = ts_offset(b)
        if off:
            return b[off:]
    return b


def segment_ok(b):
    """False only for image decoys; unknown formats get the benefit of doubt."""
    if not b[:4].startswith(IMAGE_MAGIC):
        return True
    return ts_offset(b) is not None


def _first_media(stream_url, body, h):
    """From a validated playlist, fetch the head of a real segment (following
    one master->variant hop) and check it isn't a decoy."""
    text = body.decode("utf-8", "replace")
    for _ in range(2):
        if "#EXT-X-KEY:METHOD=AES" in text or "#EXT-X-KEY:METHOD=SAMPLE" in text:
            return True  # encrypted: bytes are opaque, can't judge
        uris = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")]
        if not uris:
            return False
        is_master = "#EXT-X-STREAM-INF" in text
        nxt = urllib.parse.urljoin(stream_url, uris[0] if is_master else uris[-1])
        _, b = fetch(nxt, timeout=6, headers=h, limit=65536)
        if b[:7] == b"#EXTM3U":
            stream_url, text = nxt, b.decode("utf-8", "replace")
            continue
        return segment_ok(b)
    return False


def header_sets(found_on):
    """Request headers a stream CDN plausibly expects, most likely first."""
    origin = urllib.parse.urljoin(found_on, "/")
    return [{"Referer": origin},
            {"Referer": found_on},
            {"Referer": origin, "Origin": origin.rstrip("/")}]


def validate(stream_url, found_on):
    """Probe the stream with likely headers; return the working set or None."""
    is_m3u8 = ".m3u8" in stream_url
    for h in header_sets(found_on):
        try:
            if is_m3u8:
                st, body = fetch(stream_url, timeout=6, headers=h)
                if body[:7] == b"#EXTM3U":
                    return h if _first_media(stream_url, body, h) else None
            else:
                st, body = fetch(stream_url, timeout=6,
                                 headers=dict(h, Range="bytes=0-2048"))
                if st in (200, 206) and len(body) > 0:
                    return h
        except Exception:
            continue
    return None


def cand_json(stream_url, headers, found_on):
    sid = register(headers, [h for h in header_sets(found_on) if h != headers])
    return {"url": "http://%s:%d/pxy/%s/%s"
            % (PXY_HOST, PORT, sid, urllib.parse.quote(stream_url, safe="")),
            "type": "hls" if ".m3u8" in stream_url else "mp4",
            "direct": stream_url}


# ---------------- source-site scraping ----------------

def schedule():
    ts, cached = _cache["schedule"]
    if cached and time.time() - ts < SCHED_TTL:
        return cached
    status, body = fetch(BASE)
    text = body.decode("utf-8", "replace")
    games = []
    for m in re.finditer(
            r'<a href="(/[a-z0-9-]+/[a-z0-9-]+)"[^>]*class="([^"]*game-row[^"]*)"[^>]*aria-label="([^"]*)"', text):
        path, cls, label = m.group(1), m.group(2), m.group(3)
        parts = [p.strip() for p in label.split(" - ")]
        games.append({
            "path": path,
            "sport": path.strip("/").split("/")[0],
            "title": parts[0] if parts else label,
            "status": parts[1] if len(parts) > 1 else "",
            "detail": " - ".join(parts[2:]) if len(parts) > 2 else "",
            "live": "is-live" in cls,
            "label": label,
        })
    _cache["schedule"] = (time.time(), games)
    return games


def event(path):
    ts, cached = _cache["event"].get(path, (0, None))
    if cached and time.time() - ts < SCHED_TTL:
        return cached
    status, body = fetch(BASE + path)
    text = body.decode("utf-8", "replace")
    t = re.search(r"<title>([^<]*)</title>", text)
    title = t.group(1).strip() if t else path
    title = re.split(r" - Watch | \| ", title)[0]  # drop the site-name suffix
    title = title.replace(" Live Stream Free", "").strip()
    sources = []
    seen = set()
    for m in re.finditer(
            r'<a [^>]*href="(https?://[^"]+)"[^>]*class="stream-link"[^>]*>(.*?)</a>', text, re.S):
        url = unjs(m.group(1))
        if url in seen or is_ad(url):
            continue
        seen.add(url)
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        label = " ".join(label.split()).replace("(opens in new tab)", "")
        label = label or "Server %d" % (len(sources) + 1)
        sources.append({"url": url, "label": label})
    data = {"title": title, "sources": sources}
    _cache["event"][path] = (time.time(), data)
    return data


# ---------------- host priority ----------------
# The same ~25 stream hosts show up on every event and the same ones tend to
# work, so remember per-host outcomes (exponentially decayed) and resolve the
# historically good hosts first: the first working stream lands in wave one.

STATS_DECAY = 0.85
_stats_lock = threading.Lock()
_stats = None


def host_of(url):
    h = urllib.parse.urlparse(url).netloc.lower()
    return h.removeprefix("www.")


def _stats_file():
    return os.path.join(STATE_DIR, "host_stats.json")


def _load_stats():
    global _stats
    if _stats is None:
        try:
            with open(_stats_file()) as f:
                _stats = json.load(f)
        except Exception:
            _stats = {}
    return _stats


def host_score(host):
    with _stats_lock:
        ok, tries = _load_stats().get(host, (0.0, 0.0))
    return (ok + 1) / (tries + 2)


def record_host(host, ok):
    with _stats_lock:
        st = _load_stats()
        o, t = st.get(host, (0.0, 0.0))
        st[host] = (o * STATS_DECAY + ok, t * STATS_DECAY + 1)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = _stats_file() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, _stats_file())
        except Exception as e:
            log("stats save failed: %r" % e)


# ---------------- progressive prepare jobs ----------------
# One background job per event resolves every source (best hosts first) and
# publishes each verdict as soon as it lands, so the TV can start playing the
# first working stream instead of waiting for all ~27 to finish.

JOBS = {}
_jobs_lock = threading.Lock()


class Job:
    def __init__(self, path):
        self.path = path
        self.jid = secrets.token_hex(4)
        self.t0 = time.time()
        self.t_done = 0.0
        self.t_first_ok = None
        self.cond = threading.Condition()
        self.version = 0
        self.done = False
        self.error = None
        self.title = path
        self.sources = []

    def ok_count(self):
        return sum(1 for x in self.sources if x["state"] == "ok")

    def update(self, fn=None):
        with self.cond:
            if fn:
                fn()
            self.version += 1
            self.cond.notify_all()

    def wait(self, pred, timeout):
        with self.cond:
            self.cond.wait_for(pred, timeout)

    def snapshot(self):
        with self.cond:
            srcs = [{"label": x["label"], "url": x["url"], "state": x["state"],
                     "ok": x["state"] == "ok", "candidates": list(x["candidates"])}
                    for x in self.sources]
            return {"title": self.title, "sources": srcs,
                    "ok_count": self.ok_count(),
                    "checked": sum(1 for x in srcs if x["state"] != "pending"),
                    "done": self.done, "version": self.version,
                    "jid": self.jid,
                    "error": self.error,
                    "elapsed": round(time.time() - self.t0, 1)}

    def fresh(self):
        if not self.done:
            return True
        ttl = PREPARE_TTL if self.ok_count() else DEAD_TTL
        return time.time() - self.t_done < ttl


def _check_source(job, i):
    src = job.sources[i]
    t = time.time()
    via = "browser"
    raw = []
    try:
        raw = bresolve(src["url"], BASE + job.path, budget=RESOLVE_BUDGET)
    except Exception:
        pass
    if not raw:
        via = "static"
        try:
            raw = static_resolve(src["url"], job.path)
        except Exception:
            raw = []
    cands = []
    if raw:
        raw = raw[:4]
        with ThreadPoolExecutor(len(raw)) as vx:
            verdicts = list(vx.map(lambda r: validate(r[0], r[1]), raw))
        for (stream_url, found_on), h in zip(raw, verdicts):
            if h:
                cands.append(cand_json(stream_url, h, found_on))

    def apply():
        src["candidates"] = cands
        src["state"] = "ok" if cands else "dead"
        if cands and job.t_first_ok is None:
            job.t_first_ok = time.time()
    job.update(apply)
    record_host(host_of(src["url"]), bool(cands))
    log("  %s %-28s %s %.1fs (%s: %d raw, %d ok)" % (
        job.path, host_of(src["url"])[:28], "OK  " if cands else "dead",
        time.time() - t, via, len(raw), len(cands)))


def _run_job(job):
    try:
        ev = event(job.path)
        order = sorted(ev["sources"], key=lambda x: -host_score(host_of(x["url"])))

        def init():
            job.title = ev["title"]
            job.sources = [{"label": x["label"], "url": x["url"],
                            "state": "pending", "candidates": []} for x in order]
        job.update(init)

        def safe(i):
            try:
                _check_source(job, i)
            except Exception as e:
                log("check err %s: %r" % (job.sources[i]["url"], e))
                job.update(lambda: job.sources[i].update(state="dead"))
        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(safe, range(len(job.sources))))
    except Exception as e:
        job.error = repr(e)[:120]
        log("prepare %s failed: %r" % (job.path, e))

    def fin():
        job.done = True
        job.t_done = time.time()
    job.update(fin)
    first = ("%.1fs" % (job.t_first_ok - job.t0)) if job.t_first_ok else "never"
    log("prepare %s: %d/%d sources ok, first ok %s, all done %.1fs" % (
        job.path, job.ok_count(), len(job.sources), first, job.t_done - job.t0))


def get_job(path):
    with _jobs_lock:
        job = JOBS.get(path)
        if job is None or not job.fresh():
            job = Job(path)
            JOBS[path] = job
            threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        now = time.time()
        for k in [k for k, j in JOBS.items() if j.done and now - j.t_done > 3600]:
            del JOBS[k]
        return job


def prepare(path, since=None, wait=40.0, jid=None):
    """Start/reuse the event's job and long-poll it. Without `since` (legacy
    clients), return as soon as any source works or the job ends; with it,
    as soon as anything changes past that version. `since` is only meaningful
    for the job it came from (`jid`), so a new job answers immediately."""
    job = get_job(path)
    if since is not None and jid and jid != job.jid:
        since = -1
    if since is None:
        job.wait(lambda: job.done or job.ok_count() > 0, wait)
    else:
        job.wait(lambda: job.done or job.version > since, wait)
    return job.snapshot()


def delegated_prepare(query, wait):
    """Forward a /prepare query to DELEGATE; None if it's unavailable."""
    global _delegate_down_until, _delegate_seen_ok
    if not DELEGATE or time.time() < _delegate_down_until:
        return None
    try:
        # cheap liveness check first so a sleeping peer costs ~1.5s, not the
        # whole long-poll timeout
        if time.time() - _delegate_seen_ok > 30:
            fetch(DELEGATE + "/ping", timeout=1.5)
        _, body = fetch(DELEGATE + "/prepare?" + query, timeout=wait + 10)
        _delegate_seen_ok = time.time()
        data = json.loads(body)
        data["via"] = "delegate"
        return data
    except Exception as e:
        _delegate_down_until = time.time() + DELEGATE_BACKOFF
        log("delegate %s unavailable (%r); resolving locally" % (DELEGATE, e))
        return None


def static_resolve(start_url, event_ref, budget=12):
    """Static BFS fallback (iframe/script/atob scraping)."""
    m3u8s = []
    seen = set()
    scripts_done = set()
    queue = [(start_url, BASE + event_ref if event_ref else BASE, 0)]
    pages = 0
    t0 = time.time()
    while queue and pages < 10 and time.time() - t0 < budget:
        url, ref, depth = queue.pop(0)
        url = unjs(url.strip())
        if not re.match(r'^https?://', url) or url in seen or is_ad(url):
            continue
        seen.add(url)
        try:
            status, body = fetch(url, ref)
        except Exception:
            continue
        pages += 1
        text = unjs(body.decode("utf-8", "replace"))
        for u in M3U8_RE.findall(text):
            u = absolutize(u, url)
            if not is_ad(u) and all(f[0] != u for f in m3u8s):
                m3u8s.append((u, url))
        for u in REL_M3U8_RE.findall(text):
            u = absolutize(u, url)
            if not is_ad(u) and all(f[0] != u for f in m3u8s):
                m3u8s.append((u, url))
        for b64 in ATOB_RE.findall(text):
            try:
                dec = base64.b64decode(b64).decode("utf-8", "replace")
            except Exception:
                continue
            for u in M3U8_RE.findall(dec):
                u = absolutize(u, url)
                if not is_ad(u) and all(f[0] != u for f in m3u8s):
                    m3u8s.append((u, url))
        if depth < 3:
            own = urllib.parse.urlparse(url)
            n = 0
            for src in SCRIPT_RE.findall(text):
                a = absolutize(unjs(src), url)
                p = urllib.parse.urlparse(a)
                if p.netloc != own.netloc or a in scripts_done or is_ad(a):
                    continue
                scripts_done.add(a)
                n += 1
                if n > 5:
                    break
                try:
                    _, sb = fetch(a, url)
                    for u in M3U8_RE.findall(unjs(sb.decode("utf-8", "replace"))):
                        u = absolutize(u, a)
                        if not is_ad(u) and all(f[0] != u for f in m3u8s):
                            m3u8s.append((u, a))
                except Exception:
                    pass
            for src in IFRAME_RE.findall(text):
                a = absolutize(unjs(src), url)
                if a not in seen:
                    queue.append((a, url, depth + 1))
    return m3u8s


# ---------------- HLS proxy ----------------

def rewrite_playlist(text, playlist_url, sid):
    out = []
    base = playlist_url

    def px(u):
        return "/pxy/%s/%s" % (sid, urllib.parse.quote(u, safe=""))

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if "URI=" in s:
                def key(m):
                    return 'URI="%s"' % px(urllib.parse.urljoin(base, m.group(1)))
                s = re.sub(r'URI="([^"]+)"', key, s)
            out.append(s)
        elif s:
            out.append(px(urllib.parse.urljoin(base, s)))
        else:
            out.append("")
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data, ctype="application/json"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/ping":
                return self.reply(200, {"ok": True})
            if u.path == "/schedule":
                return self.reply(200, schedule())
            if u.path == "/event":
                path = (q.get("path") or [""])[0]
                if not PATH_RE.match(path):
                    return self.reply(400, {"error": "bad path"})
                return self.reply(200, event(path))
            if u.path == "/prepare":
                path = (q.get("path") or [""])[0]
                if not PATH_RE.match(path):
                    return self.reply(400, {"error": "bad path"})
                since = q.get("since")
                try:
                    wait = min(float((q.get("wait") or ["40"])[0]), 40.0)
                except ValueError:
                    wait = 40.0
                d = delegated_prepare(u.query, wait)
                if d is None:
                    d = prepare(path, int(since[0]) if since else None, wait,
                                (q.get("jid") or [None])[0])
                return self.reply(200, d)
            m = re.match(r"^/pxy/([0-9a-f]+)/(.+)$", u.path)
            if m:
                return self.proxy(m.group(1), urllib.parse.unquote(m.group(2)))
            return self.reply(404, {"error": "not found"})
        except Exception as e:
            log("handler err: " + repr(e)[:200])
            return self.reply(502, {"error": repr(e)[:120]})

    def proxy(self, sid, url):
        sess = SESSIONS.get(sid)
        if not sess:
            return self.reply(404, {"error": "stale session"})
        # Retry once with the working headers (CDN hiccup), then the other
        # plausible header sets; whichever works becomes the session default.
        tries = [sess["headers"], sess["headers"]] + sess["alts"]
        err = None
        for n, h in enumerate(tries):
            try:
                _, body = fetch(url, timeout=12, headers=h)
            except Exception as e:
                err = e
                continue
            if n > 1:
                sess["alts"] = [x for x in tries[2:] if x is not h] + [sess["headers"]]
                sess["headers"] = h
                log("pxy %s: switched headers to %s" % (sid, h))
            break
        else:
            raise err or RuntimeError("proxy fetch failed")
        if body[:7] == b"#EXTM3U" or url.split("?")[0].endswith(".m3u8"):
            text = rewrite_playlist(body.decode("utf-8", "replace"), url, sid)
            return self.reply(200, text.encode(), "application/vnd.apple.mpegurl")
        body = unwrap_segment(body)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not BASE:
        raise SystemExit("SPORTS_SOURCE_URL is not set (see .env.example)")
    threading.Thread(target=lambda: schedule(), daemon=True).start()
    start_browser()
    log("sports_api listening on :%d (browser up, schedule warming)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()