# topic-watcher

Checks Google News' RSS search once a day for anything new matching a configured
topic (e.g. "has the new Madoka movie gotten a US release date yet") and DMs the
owner when something new shows up. Silent when nothing's new. The watchlist is
managed by replying to that same DM in plain language.

## Why this shape
- **No LLM calls, anywhere, by design.** It runs unattended forever; the
  web-search work is fully delegated to Google News' own search/ranking/dedup
  (free, no API key), and "understand what the owner typed" is a small regex
  command grammar, not a model call. Zero recurring Claude cost.
- **Delivery reuses the shared Discord bot** via `homelab_core.notify`, reading
  `DISCORD_BOT_TOKEN` / `OWNER_DISCORD_ID` live from the shared `.env` (named by
  `HOMELAB_SHARED_ENV`, set in the units to mediabot's `.env`). Messages arrive
  as, and commands are read from, the same DM the owner already uses.
- **Stdlib only** (plus homelab-core, itself stdlib-only).

## Two cadences, two units
- `topic-watcher-listen.service` — resident loop (`--listen`), `Restart=always`.
  Polls the owner DM every **2s** for add/remove/list commands and applies them,
  so a chat command lands in ~2s. One process, one DM-channel lookup, a cheap GET
  per poll — plain REST, no Gateway/websocket.
- `topic-watcher.timer` → `topic-watcher.service` (oneshot) — the daily
  **09:00** news check (`±300s` jitter, `Persistent=true`). Fetches news for every
  topic and DMs anything new.

## Managing the watchlist by chat
In the DM: `watch for <topic>` / `add <topic>` adds it (and silently baselines —
no dump of existing coverage); `stop watching <topic>` / `unwatch` / `remove`
removes it and its history (asks you to be specific if the phrase matches more
than one); `list watches` / `watches?` lists them. Trailing "in (the) headlines"
is stripped. Anything else in the DM is ignored — it's not a chatbot.

Topics can also be edited by hand in `topics.json` (`id` doubles as the
`state/<id>.json` seen-history filename). `topics.json` is **gitignored** (personal
+ runtime-mutated); only `topics.example.json` is committed.

## Run
```sh
uv run --all-packages topic-watcher            # one news check now
uv run --all-packages topic-watcher --listen   # resident command listener
```

## Deploy
```sh
deploy/install.sh topic-watcher   # installs units; enables the timer + listener
```
Runtime state (per-topic seen-history, the DM cursor, `watch.log`) lives under
`state/` (gitignored). When migrating hosts, copy `state/_dm_cursor.json` across
so the listener doesn't re-process old DMs, and the per-topic `*.json` so already-
seen items don't re-notify.

## Relationship to mediabot
Independent process and code — it doesn't import or call mediabot, only reads two
env-var values and one DM channel at runtime. If the bot token is rotated, this
picks up the new one on the next run.
