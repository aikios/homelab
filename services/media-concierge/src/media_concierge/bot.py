"""Discord front-end: owner-gated, per-channel history, background job poller."""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import os
import time
import traceback

import json
import time as _time
from pathlib import Path

import anthropic
import discord

from . import costmeter, ctx, jobs, organize, qbmonitor, subs
from .agent import run_turn
from .clients.arr import Radarr, Sonarr
from .clients.jellyfin import Jellyfin
from .clients.qbit import Qbit
from .config import CFG
from .tools import _attach_subs

MAX_HIST = 40
IDLE_EVICT = 30 * 60
POLL_SECS = 45
TURN_TIMEOUT = 330          # hard ceiling on one agent turn (agent's own cap is 300)
RESET_WORDS = {"reset", "/reset", "forget", "new chat", "clear", "start over"}

DEBUG_CID = 1                              # sentinel channel id for the local debug path
DEBUG_SOCK = CFG.state_dir / "debug.sock"  # unix socket to inject "DMs" without Discord

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# one worker thread: a wedged turn can't starve the default pool or hide
_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent")

# channel_id -> {"messages": [...], "last": ts, "lock": asyncio.Lock, "errors": int}
_sessions: dict[int, dict] = {}


def _sess(cid: int) -> dict:
    s = _sessions.get(cid)
    if not s:
        s = {"messages": [], "last": time.time(), "lock": asyncio.Lock(), "errors": 0}
        _sessions[cid] = s
    return s


def _evict_idle() -> None:
    now = time.time()
    for cid in [c for c, s in _sessions.items() if now - s["last"] > IDLE_EVICT]:
        _sessions.pop(cid, None)


async def _send(channel: discord.abc.Messageable, text: str) -> None:
    for i in range(0, len(text) or 1, 1900):
        await channel.send(text[i:i + 1900] or "​")


@client.event
async def on_ready() -> None:
    print(f"media-concierge online as {client.user} — owner {CFG.owner_discord_id}")
    client.loop.create_task(_poller())
    if CFG.debug_sock:
        client.loop.create_task(_debug_server())


@client.event
async def on_message(msg: discord.Message) -> None:
    # Owner-only server + DMs: respond to every message from the owner. Ignore rest.
    if msg.author.id != CFG.owner_discord_id or msg.author.bot:
        return
    content = msg.content
    if client.user in msg.mentions:
        content = content.replace(f"<@{client.user.id}>", "").replace(
            f"<@!{client.user.id}>", "").strip()
    if not content:
        return

    cid = msg.channel.id
    if content.strip().lower() in RESET_WORDS:
        _sessions.pop(cid, None)
        await _send(msg.channel, "reset.")
        return

    _evict_idle()
    sess = _sess(cid)
    async with sess["lock"]:
        sess["last"] = time.time()
        base = _repair_history(sess["messages"])
        reply, new_msgs, usages, note = await _attempt(cid, base, content, msg.channel)
        if reply is not None:
            sess["messages"] = _repair_history(new_msgs)
            sess["errors"] = 0
            await _send(msg.channel, reply)
            try:
                costmeter.record(CFG.model, usages)
                w = costmeter.check_warn()
                if w:
                    await _send(msg.channel, w)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        else:
            sess["errors"] = sess.get("errors", 0) + 1
            sess["messages"] = []          # circuit-breaker: bad state never persists
            await _send(msg.channel, f"\u26a0\ufe0f {note}")


@client.event
async def on_error(event: str, *args, **kw) -> None:   # noqa: ARG001
    traceback.print_exc()


_TRANSIENT = (
    asyncio.TimeoutError,
    anthropic.RateLimitError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", 0) >= 500  # 500/503/529 overloaded etc.
    return False


async def _attempt(cid: int, base: list[dict], content: str,
                   channel: "discord.abc.Messageable"):
    """Run the turn. On a transient error retry once with the same context; on a
    structural/unknown error retry once with NO context. Returns
    (reply|None, new_msgs|None, usages, note)."""
    loop = asyncio.get_running_loop()

    async def _once(msgs: list[dict]):
        return await asyncio.wait_for(
            loop.run_in_executor(_EXEC, functools.partial(_run, cid, msgs)),
            timeout=TURN_TIMEOUT,
        )

    full = base + [{"role": "user", "content": content}]
    err: BaseException | None = None
    try:
        async with channel.typing():
            r, nm, u = await _once(list(full))
        return r, nm, u, None
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        err = exc

    if _is_transient(err):
        await asyncio.sleep(4)
        try:
            async with channel.typing():
                r, nm, u = await _once(list(full))
            return r, nm, u, None
        except Exception as e2:  # noqa: BLE001
            traceback.print_exc()
            return None, None, [], (f"still failing ({type(e2).__name__}). "
                                    f"try again in a minute.")

    # structural / unknown -> drop history, try the bare message
    try:
        async with channel.typing():
            r, nm, u = await _once([{"role": "user", "content": content}])
        await _send(channel, "(our earlier context was malformed — dropped it)")
        return r, nm, u, None
    except Exception as e3:  # noqa: BLE001
        traceback.print_exc()
        return None, None, [], f"error: {e3}"


# ─────────────────────────  history repair  ─────────────────────────

def _block_type(b: object) -> "str | None":
    return b.get("type") if isinstance(b, dict) else getattr(b, "type", None)


def _block_id(b: object, key: str) -> "str | None":
    return b.get(key) if isinstance(b, dict) else getattr(b, key, None)


def _tool_use_ids(m: dict) -> set:
    c = m.get("content")
    if not isinstance(c, list):
        return set()
    return {i for b in c if _block_type(b) == "tool_use" and (i := _block_id(b, "id"))}


def _has_tool_result(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(_block_type(b) == "tool_result" for b in c)


def _clean_start(m: dict) -> bool:
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, list):
        return not any(_block_type(b) == "tool_result" for b in c)
    return True


def _repair_history(msgs: list[dict]) -> list[dict]:
    """Guarantee a well-formed transcript: every tool_use answered by the very next
    message, no orphan tool_result, no trailing unanswered tool_use. Fixes the
    damage a blind tail-slice through a tool turn causes."""
    clean: list[dict] = []
    for m in msgs:
        role = m.get("role")
        prev = clean[-1] if clean else None
        prev_unanswered = bool(prev and prev.get("role") == "assistant"
                               and _tool_use_ids(prev))
        if role == "user" and _has_tool_result(m):
            if prev_unanswered:
                clean.append(m)          # valid tool_use -> tool_result pair
            # else: orphan tool_result -> drop
        elif role == "assistant":
            if prev_unanswered:
                clean.pop()              # previous tool_use never got a result
            clean.append(m)
        elif role == "user":
            if prev_unanswered:
                clean.pop()              # plain user msg can't follow a pending tool_use
            clean.append(m)
        # any other role: ignore
    if clean and clean[-1].get("role") == "assistant" and _tool_use_ids(clean[-1]):
        clean.pop()
    clean = clean[-MAX_HIST:]
    while clean and not _clean_start(clean[0]):
        clean.pop(0)
    return clean


def _run(cid: int, messages: list[dict]) -> tuple:
    ctx.set_channel(cid)
    return run_turn(messages)


# ─────────────────────────────  job poller  ─────────────────────────────

async def _poller() -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await asyncio.get_running_loop().run_in_executor(None, _poll_once)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        await asyncio.sleep(POLL_SECS)


_RECONCILED = CFG.state_dir / "reconciled.json"


def _seen_reconciled() -> set[str]:
    try:
        return set(json.loads(_RECONCILED.read_text()))
    except Exception:  # noqa: BLE001
        return set()


def _mark_reconciled(path: str) -> None:
    s = _seen_reconciled()
    s.add(path)
    _RECONCILED.write_text(json.dumps(sorted(s)[-500:]))


def _recent_media_files() -> list[tuple[str, str, str]]:
    """(title, path, dateAdded) for movie + episode files imported in the last 6h."""
    from datetime import datetime
    now = _time.time()
    out: list[tuple[str, str, str]] = []
    try:
        for m in Radarr().list():
            mf = m.get("movieFile") or {}
            if m.get("hasFile") and mf.get("path"):
                out.append((m["title"], mf["path"], mf.get("dateAdded", "")))
    except Exception:  # noqa: BLE001
        pass
    try:
        s = Sonarr()
        for sr in s.list():
            if not sr.get("statistics", {}).get("episodeFileCount"):
                continue
            for ef in s._j("GET", "/episodefile", params={"seriesId": sr["id"]}) or []:  # noqa: SLF001
                if ef.get("path"):
                    out.append((sr["title"], ef["path"], ef.get("dateAdded", "")))
    except Exception:  # noqa: BLE001
        pass
    recent = []
    for title, path, added in out:
        try:
            age = now - datetime.fromisoformat(added.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            age = 0
        if age <= 6 * 3600:
            recent.append((title, path, added))
    return recent


def _reconcile_subs() -> None:
    """Self-heal: any movie or episode imported recently that lacks the current
    styled English track gets one — regardless of how it was downloaded."""
    seen = _seen_reconciled()
    for title, path, _added in _recent_media_files():
        if path in seen:
            continue
        if Path(path).suffix.lower() not in (".mkv", ".mp4", ".m4v"):
            _mark_reconciled(path)
            continue
        src = subs.pick_source(path, "en")
        if src and src[0] == "done" and not subs.needs_restyle(path):
            _mark_reconciled(path)
            continue
        try:
            r = _attach_subs(path, "en", "soft", 0)
            Jellyfin().refresh()
            _notify_owner(f"🎬 **{title}** — {r}")
        except Exception as e:  # noqa: BLE001
            _notify_owner(f"styled-subs step failed for {title}: {e}")
        _mark_reconciled(path)


def _poll_once() -> None:
    try:
        _reconcile_subs()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    try:
        qbmonitor.tick(_notify_owner)      # qBittorrent-wide status (all torrents)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    open_jobs = jobs.open_jobs()
    if not open_jobs:
        return
    try:
        torrents = Qbit().torrents()
    except Exception:  # noqa: BLE001
        torrents = []
    try:
        rq = {r.get("title", "") for r in Radarr().queue()}
        sq = {r.get("title", "") for r in Sonarr().queue()}
    except Exception:  # noqa: BLE001
        rq = sq = set()

    for j in open_jobs:
        if j.kind == "burn":
            fresh = jobs.get(j.id)
            if fresh and fresh.state != "open":
                _notify(j.channel_id, f"✅ subtitle burn done: {j.title}")
            continue
        if j.kind == "magnet":
            _handle_magnet(j, torrents)
            continue
        if j.kind != "download":
            continue
        t = _match_torrent(torrents, j.title)
        in_arr_q = any(j.title.split(" (")[0].lower() in q.lower() for q in (rq | sq))
        if t and t["progress"] < 1.0:
            pct = t["progress"] * 100
            # progress pings come from the qBittorrent-wide monitor now; keep the
            # job's last_report fresh for job_status.
            if abs(pct - j.detail.get("last_pct", -9)) >= 10:
                jobs.update(j.id, last_pct=pct,
                            last_report=f"downloading {pct:.0f}%")
            continue
        # download complete (or gone) and not stuck in arr import queue -> imported
        if not in_arr_q:
            _finish_download(j)


def _match_torrent(torrents: list[dict], title: str) -> dict | None:
    key = title.split(" (")[0].lower()
    for t in torrents:
        if key in t["name"].lower():
            return t
    return None


def _finish_download(j) -> None:  # noqa: ANN001
    path = _imported_path(j)
    msg = f"✅ **{j.title}** imported."
    lang = j.detail.get("subs_lang", "en")
    mode = j.detail.get("subs_mode", "soft")
    if path:
        try:
            r = _attach_subs(path, lang, mode, j.channel_id)
            msg += f"\n{r}"
        except Exception as e:  # noqa: BLE001
            msg += f"\n(subtitle step failed: {e})"
    try:
        Jellyfin().refresh()
        msg += "\nJellyfin scan triggered."
    except Exception:  # noqa: BLE001
        pass
    jobs.update(j.id, state="done", last_report="imported")
    _notify(j.channel_id, msg)


def _imported_path(j) -> str | None:  # noqa: ANN001
    key = j.title.split(" (")[0].lower()
    try:
        if j.detail.get("media_kind") == "movie":
            for m in Radarr().list():
                if key in m["title"].lower() and m.get("hasFile"):
                    mf = m.get("movieFile") or {}
                    return mf.get("path")
        else:
            for s in Sonarr().list():
                if key in s["title"].lower():
                    eps = Sonarr()._j("GET", "/episodefile",  # noqa: SLF001
                                      params={"seriesId": s["id"]})
                    if eps:
                        return sorted(eps, key=lambda e: e.get("dateAdded", ""))[-1].get("path")
    except Exception:  # noqa: BLE001
        pass
    return None


def _match_magnet(torrents: list[dict], j: jobs.Job) -> dict | None:
    tag = j.detail.get("tag")
    ih = (j.detail.get("infohash") or "").lower()
    for t in torrents:
        tags = [x.strip() for x in (t.get("tags") or "").split(",")]
        if tag and tag in tags:
            return t
        if ih and t.get("hash", "").lower() == ih:
            return t
    return None


def _handle_magnet(j: jobs.Job, torrents: list[dict]) -> None:
    t = _match_magnet(torrents, j)
    if t is None:
        if time.time() - j.created > 2 * 3600:
            jobs.update(j.id, state="error", last_report="torrent not found")
            _notify(j.channel_id, f"⚠️ {j.title}: never showed up in qBittorrent.")
        return
    prog = t.get("progress", 0) or 0
    if prog < 1.0:
        pct = prog * 100
        if abs(pct - j.detail.get("last_pct", -9)) >= 10:
            jobs.update(j.id, last_pct=pct, last_report=f"downloading {pct:.0f}%")
        return
    _finish_magnet(j, t)


def _finish_magnet(j: jobs.Job, t: dict) -> None:
    content = t.get("content_path") or t.get("save_path") or ""
    lang = j.detail.get("subs_lang", "en")
    mode = j.detail.get("subs_mode", "soft")
    try:
        res = organize.import_download(content, title_hint=j.detail.get("title_hint"),
                                       kind_hint=j.detail.get("media_kind", "auto"))
    except Exception as e:  # noqa: BLE001
        jobs.update(j.id, state="error", last_report=f"import failed: {e}")
        _notify(j.channel_id, f"⚠️ {j.title}: import failed — {e}\n(files left at {content})")
        return
    try:
        Qbit().delete([t["hash"]], delete_files=False)   # files already moved out
    except Exception:  # noqa: BLE001
        pass
    lines = [f"✅ **{j.title}** imported → {res['library_path']} ({res['count']} file(s))"]
    for p in res["imported"]:
        try:
            r = _attach_subs(p, lang, mode, j.channel_id)
            lines.append(f"  • {Path(p).name}: {r}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  • {Path(p).name}: subtitle step failed — {e}")
    if res.get("skipped"):
        lines.append("  skipped (couldn't place): " + ", ".join(res["skipped"]))
    try:
        Jellyfin().refresh()
        lines.append("Jellyfin scan triggered.")
    except Exception:  # noqa: BLE001
        pass
    jobs.update(j.id, state="done", last_report=f"imported {res['count']} file(s)")
    _notify(j.channel_id, "\n".join(lines))


def _notify(channel_id: int, text: str) -> None:
    ch = client.get_channel(channel_id) if channel_id else None
    if ch is None:
        return _notify_owner(text)
    fut = asyncio.run_coroutine_threadsafe(_send(ch, text), client.loop)
    try:
        fut.result(timeout=20)
    except Exception:  # noqa: BLE001
        pass


def _notify_owner(text: str) -> None:
    async def _go() -> None:
        u = client.get_user(CFG.owner_discord_id) or await client.fetch_user(CFG.owner_discord_id)
        if u:
            await _send(u, text)
    try:
        asyncio.run_coroutine_threadsafe(_go(), client.loop).result(timeout=20)
    except Exception:  # noqa: BLE001
        pass


# ───────────────────  local debug channel ("DM" without Discord)  ───────────────────
# A unix socket on the LIVE bot that runs one real agent turn per injected prompt,
# reusing the exact _attempt path (same tools, cost meter, job poller). Local-only
# (0600); see debug.py for the client (media-concierge-dm). Background notifications from
# debug-initiated jobs fall through to the owner's Discord DM via _notify.

class _DebugChannel:
    """Stand-in for a discord channel so the debug turn reuses _attempt/_send;
    collects everything 'sent' so the socket client gets the full reply."""

    def __init__(self) -> None:
        self.id = DEBUG_CID
        self._out: list[str] = []

    def typing(self):
        @contextlib.asynccontextmanager
        async def _noop():
            yield
        return _noop()

    async def send(self, text: str) -> None:
        self._out.append(text)

    def collected(self) -> str:
        return "\n".join(self._out)


async def _debug_handle(content: str) -> str:
    """Run one agent turn for an injected prompt, as if it were an owner DM."""
    _evict_idle()
    sess = _sess(DEBUG_CID)
    async with sess["lock"]:
        sess["last"] = time.time()
        base = _repair_history(sess["messages"])
        ch = _DebugChannel()
        reply, new_msgs, usages, note = await _attempt(DEBUG_CID, base, content, ch)
        if reply is None:
            sess["errors"] = sess.get("errors", 0) + 1
            sess["messages"] = []
            return f"⚠️ {note}"
        sess["messages"] = _repair_history(new_msgs)
        sess["errors"] = 0
        try:
            costmeter.record(CFG.model, usages)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        extra = ch.collected()
        return f"{extra}\n{reply}".strip() if extra else reply


async def _debug_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await reader.read(16384)
        prompt = data.decode(errors="replace").strip()
        if prompt:
            try:
                reply = await _debug_handle(prompt)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                reply = f"debug turn error: {e}"
            writer.write(reply.encode())
            await writer.drain()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _debug_server() -> None:
    with contextlib.suppress(FileNotFoundError):
        DEBUG_SOCK.unlink()
    server = await asyncio.start_unix_server(_debug_conn, path=str(DEBUG_SOCK))
    with contextlib.suppress(OSError):
        os.chmod(DEBUG_SOCK, 0o600)
    print(f"debug DM socket listening at {DEBUG_SOCK}")
    async with server:
        await server.serve_forever()


def main() -> None:
    missing = [k for k, v in (("DISCORD_BOT_TOKEN", CFG.discord_bot_token),
                              ("OWNER_DISCORD_ID", CFG.owner_discord_id),
                              ("JELLYFIN_API_KEY", CFG.jellyfin_key)) if not v]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)} — shared creds go in "
                         f"~/homelab/secrets/shared.env, service keys in this "
                         f"service's .env")
    client.run(CFG.discord_bot_token, log_handler=None)


if __name__ == "__main__":
    main()
