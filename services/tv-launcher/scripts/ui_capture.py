#!/usr/bin/env python3
"""Capture the TV client (static/jellyfin.html + sports.html) the way the Vizio
TV renders it: Chromium 72 (the TV's Conjure webview major), 3840x2160 CSS px
(the TV reports that width, so the pages take their real `zoom` path), driven
with remote-control keys over the DevTools protocol.

    python3 scripts/ui_capture.py --chrome ~/ui-harness/chromium/r609904/chrome-linux/chrome \
        --out ~/ui-harness/shots/before --env ~/ui-harness/jf.env

  --env      file with JF_SRV (Jellyfin base URL), JF_U, JF_K (from jf_url.txt) and
             SR (a stream-resolver base URL, e.g. http://127.0.0.1:8098). Never
             committed; keys never reach the output.
  --cache    record/replay dir for API responses (default ~/ui-harness/cache):
             the first run records real responses, later runs replay them, so
             before/after captures see identical data.
  --only     comma-separated scenario names.

Writes <out>/<scenario>.png (downscaled to 1920x1080) and <out>/metrics.json:
per scenario, visible text elements that are clipped, outside the 5% TV safe
area, or under WCAG 4.5:1 contrast against their effective background.
Stdlib + websocket-client (`uv run --with websocket-client ...`).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import ClassVar, cast

import websocket  # ty: ignore[unresolved-import]  # websocket-client, via `uv run --with`

STATIC = Path(__file__).resolve().parents[1] / "static"
W, H = 3840, 2160


# ---------------------------------------------------------------- server ---

class Proxy(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, env: dict, cache: Path):
        super().__init__(("127.0.0.1", 0), Handler)
        self.env, self.cache = env, cache
        cache.mkdir(parents=True, exist_ok=True)


class Handler(http.server.BaseHTTPRequestHandler):
    @property
    def proxy(self) -> Proxy:
        return cast(Proxy, self.server)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path
        if path.startswith("/jf/"):
            return self.proxied(self.proxy.env["JF_SRV"].rstrip("/") + path[3:], "jf")
        if re.match(r"^/(schedule|event|prepare|ping)\b", path):
            return self.proxied(self.proxy.env["SR"].rstrip("/") + path, "sr")
        f = STATIC / urllib.parse.unquote(path.split("?")[0].lstrip("/"))
        if not f.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".js": "application/javascript"}.get(f.suffix, "application/octet-stream")
        self.reply(200, ctype, f.read_bytes())

    def reply(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.reply(204, "text/plain", b"")

    def proxied(self, url, kind):
        # cache key without credentials; prepare's long-poll params vary per call
        key_url = re.sub(r"([?&])(api_key|PlaySessionId|since|jid|wait|cb)=[^&]*", r"\1", url)
        key = hashlib.sha1(key_url.encode()).hexdigest()
        meta, body = self.proxy.cache / f"{key}.json", self.proxy.cache / f"{key}.bin"
        if meta.exists() and body.exists():
            m = json.loads(meta.read_text())
            return self.reply(m["code"], m["ctype"], body.read_bytes())
        hdrs = {}
        if kind == "jf":
            hdrs["X-Emby-Token"] = self.proxy.env["JF_K"]
            sep = "&" if "?" in url else "?"
            if "/Images/" in url and "api_key=" not in url:
                url += sep + "api_key=" + self.proxy.env["JF_K"]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=90) as r:
                code, ctype, data = r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            code, ctype, data = e.code, "text/plain", e.read()
        except OSError as e:
            return self.reply(502, "text/plain", str(e).encode())
        if kind == "sr" and "/prepare" in url and not json.loads(data or b"{}").get("done", True):
            return self.reply(code, ctype, data)  # only cache a finished prepare
        if code < 500:
            body.write_bytes(data)
            meta.write_text(json.dumps({"code": code, "ctype": ctype}))
        self.reply(code, ctype, data)


# ---------------------------------------------------------------- chrome ---

class Chrome:
    def __init__(self, binary: str):
        self.tmp = tempfile.mkdtemp(prefix="uicap-")
        self.proc = subprocess.Popen(
            [binary, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             "--remote-debugging-port=0", f"--user-data-dir={self.tmp}",
             f"--window-size={W},{H}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.tmp) / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.1)
        port = port_file.read_text().split()[0]
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read())
        page = next(t for t in tabs if t["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120)
        self.n = 0
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Emulation.setDeviceMetricsOverride",
                  width=W, height=H, deviceScaleFactor=1, mobile=False)

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr, wait=True):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=wait, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError("js: " + json.dumps(r["exceptionDetails"])[:400])
        return r.get("result", {}).get("value")

    def goto(self, url, settle=2.0):
        self.call("Page.navigate", url=url)
        self.wait_for("document.readyState === 'complete'")
        time.sleep(settle)

    def wait_for(self, cond, timeout=60.0):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(f"!!({cond})", wait=False):
                return
            time.sleep(0.25)
        raise TimeoutError(cond)

    KEYS: ClassVar[dict[str, int]] = {"ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
            "Enter": 13, "Escape": 27}

    def key(self, k, settle=0.6):
        vk = self.KEYS[k]
        for t in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=t, key=k, code=k,
                      windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
        time.sleep(settle)

    def shot(self, path: Path):
        r = self.call("Page.captureScreenshot", format="png",
                      clip={"x": 0, "y": 0, "width": W, "height": H, "scale": 0.5})
        path.write_bytes(base64.b64decode(r["data"]))

    def close(self):
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------- metrics ---

METRICS_JS = r"""
(function () {
  function rgb(s) { var m = (s || "").match(/[\d.]+/g); return m ? m.map(Number) : null; }
  function lum(c) { var a = c.slice(0, 3).map(function (v) { v /= 255;
    return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); });
    return .2126 * a[0] + .7152 * a[1] + .0722 * a[2]; }
  function bg(el) { // first opaque-ish background up the tree (gradients/images: unknown)
    for (var e = el; e; e = e.parentElement) {
      var cs = getComputedStyle(e);
      if (cs.backgroundImage && cs.backgroundImage !== "none") return {img: true};
      var c = rgb(cs.backgroundColor);
      if (c && (c.length < 4 || c[3] > .5)) return {c: c};
    }
    return {c: [255, 255, 255]};
  }
  var out = [], W = innerWidth, H = innerHeight, sx = W * .05, sy = H * .05;
  var els = document.querySelectorAll("body *");
  for (var i = 0; i < els.length; i++) {
    var el = els[i], cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || +cs.opacity === 0) continue;
    var own = "";
    for (var n = el.firstChild; n; n = n.nextSibling) if (n.nodeType === 3) own += n.nodeValue;
    own = own.trim();
    if (!own) continue;
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > H) continue;
    var issues = [];
    if (el.scrollWidth > el.clientWidth + 1 && cs.overflow !== "visible") issues.push("clipped-x");
    if (el.scrollHeight > el.clientHeight + 1 && cs.overflow !== "visible") issues.push("clipped-y");
    if (r.left < sx || r.right > W - sx || r.top < sy || r.bottom > H - sy) issues.push("outside-safe-area");
    var b = bg(el), fg = rgb(cs.color), ratio = null;
    if (b.c && fg) { var l1 = lum(fg), l2 = lum(b.c);
      ratio = Math.round((Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05) * 10) / 10;
      if (ratio < 4.5) issues.push("contrast " + ratio); }
    else if (b.img) issues.push("over-image (check visually)");
    out.push({text: own.slice(0, 60), tag: el.tagName.toLowerCase(), id: el.id, cls: el.className,
              font: cs.fontSize, rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
              issues: issues});
  }
  return out;
})()
"""


# ------------------------------------------------------------- scenarios ---

def jf_url(base, env):
    return (f"{base}/jellyfin.html?srv={urllib.parse.quote(base + '/jf', safe='')}"
            f"&u={env['JF_U']}&k={env['JF_K']}&v=cap")


def open_view(c, kind):
    """From home, open the first library view of a collection type."""
    c.js(f"""(function () {{ var i = items.findIndex(function (x) {{ return x.CollectionType === '{kind}'; }});
        sel = i; draw(); openItem(items[i]); }})()""", wait=False)
    c.wait_for("stack.length > 0 && grid.querySelectorAll('img').length > 0")
    time.sleep(2.5)


def fake_frame(c, which):
    """Chromium snapshots can't decode h264, so put the item's backdrop behind the
    player as a stand-in frame, then show the controls as playback would."""
    c.js(f"""(function () {{
      pstate.style.display = 'none';
      var it = items[sel] || {{}};  // episodes have no backdrop of their own: use the series'
      video.poster = API + '/Items/' + (it.SeriesId || curItemId) + '/Images/Backdrop?fillWidth=1920&quality=90';
      pfill.style.width = '37%'; pbuf.style.width = '52%';
      ptime.textContent = '16:42 / 44:51';
      ppp.innerHTML = {'ICON_PLAY' if which == 'paused' else 'ICON_PAUSE'};
      showCtrl(false);
    }})()""", wait=False)
    time.sleep(2.0)


def scenarios(c: Chrome, base: str, env: dict):
    def home():
        c.goto(jf_url(base, env), settle=1.0)
        c.wait_for("items.length > 0")
        time.sleep(1.0)
    yield "jf-home", home

    def movies():
        home()
        open_view(c, "movies")
        c.key("ArrowRight")
    yield "jf-movies", movies

    def episodes():
        home()
        open_view(c, "tvshows")
        c.js("openItem(items[sel])", wait=False)  # first series -> seasons
        c.wait_for("stack.length > 1 && grid.querySelectorAll('img').length > 0")
        time.sleep(1.5)
        c.js("openItem(items[items.length - 1])", wait=False)  # last season -> episodes
        c.wait_for("stack.length > 2 && grid.querySelectorAll('img').length > 0")
        time.sleep(2.5)
    yield "jf-episodes", episodes

    def player(state):
        def run():
            episodes()
            # the episode with the longest title stresses the title line
            c.js("""(function () { var i = 0; items.forEach(function (x, j) {
                if ((x.Name || '').length > (items[i].Name || '').length) i = j; });
                sel = i; draw(); openItem(items[i]); })()""", wait=False)
            c.wait_for("inPlayer")
            time.sleep(3.0)
            fake_frame(c, state)
        return run
    yield "jf-player-playing", player("playing")
    yield "jf-player-paused", player("paused")

    def movie_paused():
        home()
        open_view(c, "movies")
        c.js("openItem(items[sel])", wait=False)
        c.wait_for("inPlayer")
        time.sleep(3.0)
        fake_frame(c, "paused")
    yield "jf-player-movie-paused", movie_paused

    def sports(tab_moves=0, down=0):
        def run():
            c.goto(f"{base}/sports.html?api={base.rsplit(':', 1)[1]}&back=x", settle=1.0)
            c.wait_for("games.length > 0", timeout=90)
            time.sleep(1.0)
            for _ in range(tab_moves):
                c.key("ArrowRight")
            for _ in range(down):
                c.key("ArrowDown")
        return run
    yield "sports-live", sports()
    yield "sports-tab", sports(tab_moves=1, down=2)

    def sources():
        sports()()
        c.key("Enter", settle=1.0)
        c.wait_for("view === 'src' && prep && prep.done", timeout=120)
        c.key("ArrowDown")
        time.sleep(1.0)
    yield "sports-sources", sources


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--cache", default=str(Path.home() / "ui-harness" / "cache"))
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    env = dict(ln.split("=", 1) for ln in Path(a.env).read_text().split() if "=" in ln)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    srv = Proxy(env, Path(a.cache))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    only = set(filter(None, a.only.split(",")))
    c = Chrome(a.chrome)
    metrics = {}
    try:
        for name, run in scenarios(c, base, env):
            if only and name not in only:
                continue
            t0 = time.time()
            try:
                run()
                c.shot(out / f"{name}.png")
                metrics[name] = [m for m in c.js(METRICS_JS, wait=False) if m["issues"]]
                print(f"{name}: ok ({time.time() - t0:.0f}s, {len(metrics[name])} flagged)")
            except Exception as e:  # noqa: BLE001 — one broken scenario must not stop the rest
                print(f"{name}: FAILED {str(e)[:200]}")
    finally:
        c.close()
        srv.shutdown()
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    main()
