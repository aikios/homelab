#!/usr/bin/env python3
# sports_api.py -- vega-side (:8098) clean-stream extractor + HLS proxy.
# Headless Chromium (playwright) loads each third-party stream page,
# executes its (often obfuscated) player JS, and we intercept the real
# m3u8/mp4 requests. Ad domains are route-blocked so the browser never
# even loads them. Candidates are validated (referer probe) before the
# TV ever sees them, then served through the referer-spoofing proxy.
#
#   GET /schedule                 today's games from SOURCE_SITE
#   GET /event?path=/nba/123     raw stream anchors for an event
#   GET /prepare?path=/nba/123   resolve+validate ALL sources in parallel
#   GET /pxy/<sid>/<quoted-abs-url>                  HLS proxy (CORS)

import asyncio
import base64
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.async_api import async_playwright

PORT = int(os.environ.get("SPORTS_API_PORT", "8098"))
# LAN host/IP the TV reaches this API at — baked into the proxy URLs we hand the
# TV, so it must be this host's address on the TV's network. Set per host in the
# gitignored .env (see .env.example); the loopback default is just a safe fallback.
PXY_HOST = os.environ.get("SPORTS_API_HOST", "127.0.0.1")
BASE = "https://SOURCE_SITE"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
AD_HOSTS = ("histats.com", "google-analytics", "googletagmanager",
            "googlesyndication", "reliedhounder", "lauansaltire", "popads",
            "doubleclick", "adservice", "livelog.site", "cloudflareinsights",
            "aclib", "s10.histats", "amazon-adsystem", "adsrv", "adcash",
            "propellerads", "adsterra", "onclckds", "toponad")
SESSIONS = {}
SESS_TTL = 6 * 3600
_cache = {"schedule": (0, None), "event": {}, "prepare": {}}
SCHED_TTL = 60
PREPARE_TTL = 600
MAX_CTX = 8

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


def fetch(url, referer=None, timeout=10, headers=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    if referer:
        req.add_header("Referer", referer)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


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
    async with _ctx_sem:
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
            try:
                await page.goto(url, referer=referer, timeout=12000,
                                wait_until="domcontentloaded")
            except Exception:
                pass
            try:
                await page.mouse.click(640, 360)
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < budget:
                if found and time.time() - t0 > 5:
                    break
                await asyncio.sleep(0.4)
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

def register(referer):
    sid = ("%x" % (int(time.time() * 1000) ^ id(SESSIONS)))[:8]
    SESSIONS[sid] = {"referer": referer, "ts": time.time()}
    now = time.time()
    for s in [k for k, v in SESSIONS.items() if now - v["ts"] > SESS_TTL]:
        del SESSIONS[s]
    return sid


def validate(stream_url, found_on):
    """Probe the stream with likely referers; return working referer or None."""
    origin = urllib.parse.urljoin(found_on, "/")
    is_m3u8 = stream_url.split("?")[0].endswith(".m3u8") or ".m3u8" in stream_url
    for ref in (origin, found_on):
        try:
            if is_m3u8:
                st, body = fetch(stream_url, ref, timeout=8)
                if body[:7] == b"#EXTM3U":
                    return ref
            else:
                st, body = fetch(stream_url, ref, timeout=8,
                                 headers={"Range": "bytes=0-2048"})
                if st in (200, 206) and len(body) > 0:
                    return ref
        except Exception:
            continue
    return None


def cand_json(stream_url, working_ref):
    sid = register(working_ref)
    return {"url": "http://%s:%d/pxy/%s/%s"
            % (PXY_HOST, PORT, sid, urllib.parse.quote(stream_url, safe="")),
            "type": "hls" if ".m3u8" in stream_url else "mp4",
            "direct": stream_url}


# ---------------- SourceSite scraping ----------------

def schedule():
    ts, cached = _cache["schedule"]
    if cached and time.time() - ts < SCHED_TTL:
        return cached
    status, body = fetch(BASE)
    text = body.decode("utf-8", "replace")
    games = []
    for m in re.finditer(
            r'<a href="(/[a-z0-9-]+/[0-9]+)"[^>]*class="([^"]*game-row[^"]*)"[^>]*aria-label="([^"]*)"', text):
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
    title = title.split(" - Watch ")[0].replace(" Live Stream Free", "").strip()
    sources = []
    seen = set()
    for m in re.finditer(
            r'<a [^>]*href="(https?://[^"]+)"[^>]*class="stream-link"[^>]*>(.*?)</a>', text, re.S):
        url = unjs(m.group(1))
        if url in seen or is_ad(url):
            continue
        seen.add(url)
        label = re.sub(r"<[^>]+>", "", m.group(2))
        label = " ".join(label.split()).replace("(opens in new tab)", "")
        label = label or "Server %d" % (len(sources) + 1)
        sources.append({"url": url, "label": label})
    data = {"title": title, "sources": sources}
    _cache["event"][path] = (time.time(), data)
    return data


def prepare(path, deadline=50):
    """Browser-resolve every source in parallel, validate, cache."""
    ts, cached = _cache["prepare"].get(path, (0, None))
    if cached and time.time() - ts < PREPARE_TTL:
        return cached
    ev = event(path)
    srcs = ev["sources"]
    raw = [None] * len(srcs)

    def work(i):
        try:
            raw[i] = bresolve(srcs[i]["url"], BASE + path, budget=16)
        except Exception:
            raw[i] = []
        if not raw[i]:
            # static fallback for pages where the browser found nothing
            try:
                raw[i] = static_resolve(srcs[i]["url"], path)
            except Exception:
                raw[i] = []

    threads = []
    for i in range(len(srcs)):
        t = threading.Thread(target=work, args=(i,))
        t.daemon = True
        t.start()
        threads.append(t)
    t_end = time.time() + deadline
    for t in threads:
        t.join(max(0, t_end - time.time()))

    # validate all candidates in parallel
    jobs = []
    for i in range(len(srcs)):
        if raw[i]:
            for stream_url, found_on in raw[i][:4]:
                jobs.append((i, stream_url, found_on))
    verdicts = [None] * len(jobs)

    def vwork(j):
        i, stream_url, found_on = jobs[j]
        verdicts[j] = (i, stream_url, validate(stream_url, found_on))

    threads = []
    for j in range(len(jobs)):
        t = threading.Thread(target=vwork, args=(j,))
        t.daemon = True
        t.start()
        threads.append(t)
    t_end = time.time() + 15
    for t in threads:
        t.join(max(0, t_end - time.time()))

    per_src = {i: [] for i in range(len(srcs))}
    for j in range(len(jobs)):
        if verdicts[j] and verdicts[j][2]:
            i, stream_url, ref = verdicts[j]
            per_src[i].append(cand_json(stream_url, ref))

    out = []
    ok_count = 0
    for i, s in enumerate(srcs):
        cands = per_src[i]
        ok = len(cands) > 0
        ok_count += ok
        out.append({"label": s["label"], "url": s["url"],
                    "ok": ok, "candidates": cands})
    data = {"title": ev["title"], "sources": out, "ok_count": ok_count}
    _cache["prepare"][path] = (time.time(), data)
    log("prepare %s: %d/%d sources ok" % (path, ok_count, len(srcs)))
    return data


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
                if not re.match(r"^/[a-z0-9-]+/[0-9]+$", path):
                    return self.reply(400, {"error": "bad path"})
                return self.reply(200, event(path))
            if u.path == "/prepare":
                path = (q.get("path") or [""])[0]
                if not re.match(r"^/[a-z0-9-]+/[0-9]+$", path):
                    return self.reply(400, {"error": "bad path"})
                return self.reply(200, prepare(path))
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
        status, body = fetch(url, sess["referer"])
        if body[:7] == b"#EXTM3U" or url.split("?")[0].endswith(".m3u8"):
            text = rewrite_playlist(body.decode("utf-8", "replace"), url, sid)
            return self.reply(200, text.encode(), "application/vnd.apple.mpegurl")
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
    threading.Thread(target=lambda: schedule(), daemon=True).start()
    start_browser()
    log("sports_api listening on :%d (browser up, schedule warming)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()