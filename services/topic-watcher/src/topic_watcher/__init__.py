"""topic-watcher — checks Google News' RSS search for new items matching
configured topics and DMs new hits to the owner. Also reads that same DM channel
for plain-language "watch for X" / "stop watching X" replies and updates the
watchlist accordingly.

Design constraints (deliberate):
- No LLM calls anywhere. The "search the web" work is delegated entirely to
  Google News' own search/ranking/dedup (free, no API key), and "understand what
  the owner typed" is a small regex command grammar, not a model call. This runs
  unattended forever; keeping it Claude-free keeps the recurring cost at zero.
- Delivery reuses the shared Discord bot via ``homelab_core.notify`` and DMs the
  same owner the bot already talks to — not a second bot identity or webhook.
- Stdlib only (plus homelab-core, itself stdlib-only).

Two independent entrypoints, run by two independent systemd units:
1. ``topic-watcher`` (no args) — one-shot: fetch news for every topic, notify on
   anything new. Run once a day by ``topic-watcher.timer``.
2. ``topic-watcher --listen`` — long-running: poll the DM every 2s for owner
   commands (add/remove/list) and apply them. Run forever by
   ``topic-watcher-listen.service`` (Restart=always), so a chat command is picked
   up in ~2s instead of waiting for the next daily news-check.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from homelab_core import Discord

HERE = Path(__file__).resolve().parent
SERVICE_DIR = HERE.parents[1]
TOPICS_FILE = SERVICE_DIR / "topics.json"
# Runtime state (seen-history, DM cursor, log) lives under state/, gitignored.
STATE_DIR = SERVICE_DIR / "state"
LOG_FILE = STATE_DIR / "watch.log"

MAX_ITEMS_FETCHED = 10     # per topic, per news-check
MAX_ITEMS_PER_MESSAGE = 5  # don't dump a wall of text if a lot showed up at once
SEEN_HISTORY_CAP = 500     # bound each per-topic state file


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{ts} {msg}\n")


def fetch_news(query: str, limit: int = MAX_ITEMS_FETCHED) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if title and guid:
            items.append({"guid": guid, "title": title, "link": link})
    return items


# ---------------------------------------------------------------- state ---

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, value) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def load_topics() -> list[dict]:
    return json.loads(TOPICS_FILE.read_text())


def save_topics(topics: list[dict]) -> None:
    TOPICS_FILE.write_text(json.dumps(topics, indent=2) + "\n")


def load_seen(topic_id: str) -> set[str]:
    return set(load_json(STATE_DIR / f"{topic_id}.json", []))


def save_seen(topic_id: str, seen: set[str]) -> None:
    save_json(STATE_DIR / f"{topic_id}.json", sorted(seen)[-SEEN_HISTORY_CAP:])


# ------------------------------------------------------- owner commands ---

_TRAILING_NOISE_RE = re.compile(r"\s+in (?:the )?headlines\.?\s*$", re.IGNORECASE)
ADD_RE = re.compile(r"^\s*(?:please\s+)?(?:watch(?: for)?|add)\s*[:\-]?\s+(.+?)\s*[.!]?\s*$", re.IGNORECASE)
REMOVE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:stop watching(?: for)?|unwatch|remove)\s*[:\-]?\s+(.+?)\s*[.!]?\s*$", re.IGNORECASE
)
LIST_RE = re.compile(
    r"^\s*(?:list|show)\s+watches\s*\??\s*$|^\s*what are you watching\??\s*$|^\s*watches\??\s*$", re.IGNORECASE
)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "watch"


def format_topic_list(topics: list[dict]) -> str:
    if not topics:
        return "Not watching anything right now."
    return "\n".join(f"• {t.get('label', t['id'])}" for t in topics)


def handle_command(text: str, topics: list[dict]) -> tuple[list[dict], str | None]:
    """Returns (possibly-updated topics, reply text or None if not a command)."""
    text = text.strip()

    if LIST_RE.match(text):
        return topics, format_topic_list(topics)

    m = ADD_RE.match(text)
    if m:
        phrase = _TRAILING_NOISE_RE.sub("", m.group(1)).strip()
        tid = slugify(phrase)
        if any(t["id"] == tid for t in topics):
            return topics, f"Already watching: {phrase}"
        topics = [*topics, {"id": tid, "label": phrase, "query": phrase}]
        return topics, f"✅ Added: {phrase} — will check daily."

    m = REMOVE_RE.match(text)
    if m:
        phrase = _TRAILING_NOISE_RE.sub("", m.group(1)).strip().lower()
        matches = [
            t for t in topics
            if phrase in t.get("label", "").lower()
            or phrase in t.get("query", "").lower()
            or phrase in t["id"].replace("-", " ")
        ]
        if not matches:
            return topics, f"🤷 No watch matching '{phrase}'.\n{format_topic_list(topics)}"
        if len(matches) > 1:
            names = ", ".join(t.get("label", t["id"]) for t in matches)
            return topics, f"Matched more than one ({names}) — be more specific."
        removed = matches[0]
        topics = [t for t in topics if t["id"] != removed["id"]]
        state_file = STATE_DIR / f"{removed['id']}.json"
        state_file.unlink(missing_ok=True)
        return topics, f"🗑️ Stopped watching: {removed.get('label', removed['id'])}"

    return topics, None


def check_owner_commands(d: Discord) -> None:
    channel_id = d.dm_channel()
    cursor_file = STATE_DIR / "_dm_cursor.json"
    cursor = load_json(cursor_file, {}).get("last_message_id")

    try:
        messages = d.fetch(channel_id, cursor)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        log(f"ERROR polling DM channel: {e}")
        return

    if not messages:
        return

    topics = load_topics()
    replies = []
    for msg in messages:
        if msg.get("author", {}).get("id") != d.owner_id:
            continue
        topics, reply = handle_command(msg.get("content", ""), topics)
        if reply:
            replies.append(reply)

    save_topics(topics)
    save_json(cursor_file, {"last_message_id": messages[-1]["id"]})

    if replies:
        try:
            d.send(channel_id, "\n\n".join(replies))
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"ERROR replying to owner commands: {e}")
        else:
            log(f"processed {len(replies)} owner command(s)")


# -------------------------------------------------------------- news ---

def run_news_check(d: Discord) -> int:
    channel_id = d.dm_channel()
    exit_code = 0
    for topic in load_topics():
        tid = topic["id"]
        query = topic["query"]
        label = topic.get("label", tid)

        try:
            items = fetch_news(query)
        except (urllib.error.URLError, ET.ParseError) as e:
            log(f"ERROR fetch '{tid}': {e}")
            exit_code = 1
            continue

        seen = load_seen(tid)

        if not seen:
            # First run for this topic: baseline silently. Otherwise every topic
            # you add dumps its entire current result set on you once.
            save_seen(tid, {it["guid"] for it in items})
            log(f"seeded '{tid}' with {len(items)} items (first run, no notify)")
            continue

        new_items = [it for it in items if it["guid"] not in seen]
        if not new_items:
            save_seen(tid, seen | {it["guid"] for it in items})
            continue

        lines = [f"🔔 **{label}** — {len(new_items)} new:"]
        for it in new_items[:MAX_ITEMS_PER_MESSAGE]:
            lines.append(f"• {it['title']}\n  {it['link']}")
        if len(new_items) > MAX_ITEMS_PER_MESSAGE:
            lines.append(f"…and {len(new_items) - MAX_ITEMS_PER_MESSAGE} more.")

        try:
            d.send(channel_id, "\n".join(lines))
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"ERROR notify '{tid}': {e}")
            exit_code = 1
            continue  # don't mark seen — retry next run rather than lose the alert

        save_seen(tid, seen | {it["guid"] for it in items})
        log(f"notified '{tid}': {len(new_items)} new item(s)")

    return exit_code


# ------------------------------------------------------------ entrypoints ---

LISTEN_POLL_SECONDS = 2.0


def listen_forever() -> None:
    """Long-running loop for the always-on service: check the DM for owner
    commands every LISTEN_POLL_SECONDS, forever.

    Deliberately a resident loop, not a process re-spawned every 2s by a timer —
    that would mean a fresh ``uv run`` + Python startup + DM-channel lookup every
    single poll, thousands of times a day, for a check that's almost always a
    no-op. One process, one DM-channel lookup, a cheap GET in a sleep loop. Still
    plain REST polling (no Gateway/websocket, no discord.py) — at 0.5 req/s to one
    DM's message-history route that's nowhere near Discord's rate limits.

    A single unhandled error would otherwise kill the loop silently; systemd's
    Restart=always is the outer net, but we also swallow and log per-iteration
    errors so one transient failure doesn't even trigger a restart.
    """
    d = Discord.from_env()
    log(f"listening for owner commands in channel {d.dm_channel()}, every {LISTEN_POLL_SECONDS}s")

    while True:
        try:
            check_owner_commands(d)
        except Exception as e:  # noqa: BLE001 - keep the loop alive no matter what
            log(f"ERROR in listen loop (continuing): {e}")
        time.sleep(LISTEN_POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--listen" in argv:
        listen_forever()
        return 0  # unreachable — listen_forever() runs until killed

    return run_news_check(Discord.from_env())


if __name__ == "__main__":
    sys.exit(main())
