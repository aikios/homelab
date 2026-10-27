# media-concierge — SPEC

(Formerly `mediabot`; renamed 2026-10-26.)

A Discord-driven agent that manages the Jellyfin media library on the host
with minimal user input.

## Goal

The owner tells it in plain language what to watch and how they want it
(quality / audio language / subtitle language / burned or soft), and it does the
whole pipeline: find → download → identify → rename into Jellyfin layout → attach
subtitles → rescan → report. Plus: report what's in the library, and reclaim disk.

## Non-goals

- No web UI. Discord only.
- Not reimplementing torrent management — the existing *arr stack is the engine.
- No local LLM. Uses the Anthropic API (`claude-sonnet-5`).

## Host facts (as of build)

- Ubuntu 22.04, 4 vCPU, 7 GB RAM, AMD Richland APU — **no usable HW video encoder**.
- Single disk: `/` 109 G, ~41 G free. `/media/{JellyFin-Movies,JellyFin-Shows,downloads}`.
- ffmpeg/ffprobe: `/usr/lib/jellyfin-ffmpeg/{ffmpeg,ffprobe}` (full build, subtitle filters OK).
- Docker services: prowlarr :9696, radarr :7878, sonarr :8989, qbittorrent :8080,
  jellyseerr :5055, jellyfin-mcp :8098 (read-only). Jellyfin (systemd) :8096.
- Prowlarr → Radarr+Sonarr fullSync already configured. qBittorrent category `radarr`
  (add `sonarr`, `manual`).
- the deploy user has passwordless sudo and docker group.

## Architecture

Framework: **`anthropic` Python SDK + Tool Runner** (`client.beta.messages.tool_runner`,
`@beta_tool` decorators). Not the Claude Agent SDK — that needs Node + the Claude Code
CLI, absent on this host. Tool Runner is pure Python, drives the agent loop, and we
own the tool set. `pause_turn` is not expected (no server tools) but the loop mirrors
history and can restart if it ever appears.

```
Discord  ──►  bot.py (discord.py)  ──►  agent.py (anthropic SDK, tool_runner)
                owner-gate                system prompt + @beta_tool registry
                per-channel history        model = $MEDIA_CONCIERGE_MODEL (default claude-opus-5)
                                              │
   @beta_tool functions (src/media_concierge/tools.py) ──► thin API clients (src/media_concierge/clients/*)
     find / get / queue_status / library_report
     disk_report / reclaim_space
     import_file (guessit + Radarr/Sonarr lookup) + junk cleanup
     attach_subs: fetch (subliminal) / mux / burn (ffmpeg, bg job)
     jellyfin_scan / remove
     run_shell (arbitrary, logged) — covers the long tail / "full perms"
```

- **Sessions**: `bot.py` keeps a `list[MessageParam]` per Discord channel id (the
  runner keeps its own copy internally; we mirror it for continuity across messages).
  Trimmed to last ~40 turns; dropped after 30 min idle.
- **Long jobs** (downloads, burns): fire-and-track. Agent returns immediately with a
  job id; a background poller posts progress/*done* to the originating channel.
  Jobs table in `state/jobs.json` (under the service dir, gitignored).
- **Permissions**: `can_use_tool` hook. Read tools auto-allow. Mutating tools
  (add/download/delete/shell/burn) auto-allow too (owner-only bot, server is
  disposable) but every call is appended to `logs/actions.jsonl`. `rm` is never
  used — deletes go through Radarr/Sonarr (which trash) or `trash-cli`.

## External APIs / auth

| Service      | Auth                                   | Source |
|--------------|----------------------------------------|--------|
| Anthropic    | `ANTHROPIC_API_KEY`                    | reused from openclaw `.env` |
| Discord      | `DISCORD_BOT_TOKEN` (app 1529…)        | reused from openclaw `.env` |
| Radarr/Sonarr/Prowlarr | `X-Api-Key`                  | extracted from each `config.xml` → `.arr-keys.env` |
| qBittorrent  | `admin` + generated password           | media-concierge sets it, updates arr clients |
| Jellyfin     | `X-Emby-Token` API key                 | **owner provides** (Dashboard → API Keys) |
| TMDB/TVDB    | none — via Radarr/Sonarr `/…/lookup`   | proxied |
| Subtitles    | subliminal; OpenSubtitles key optional | `.env` `OPENSUBTITLES_*` if provided |

Shared creds live in the host-level `~/homelab/secrets/shared.env` (git-ignored);
media-concierge's own service keys in its git-ignored `.env`:
```
ANTHROPIC_API_KEY=…
DISCORD_BOT_TOKEN=…
OWNER_DISCORD_ID=…            # numeric user id — bot ignores everyone else
JELLYFIN_URL=http://localhost:8096
JELLYFIN_API_KEY=…
RADARR_URL=http://localhost:7878
SONARR_URL=http://localhost:8989
PROWLARR_URL=http://localhost:9696
QBIT_URL=http://localhost:8080
QBIT_USER=admin
QBIT_PASS=…
OPENSUBTITLES_API_KEY=        # optional
MEDIA_CONCIERGE_FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg
MEDIA_CONCIERGE_FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe
MEDIA_CONCIERGE_MOVIES_DIR=/media/JellyFin-Movies
MEDIA_CONCIERGE_SHOWS_DIR=/media/JellyFin-Shows
MEDIA_CONCIERGE_DOWNLOADS_DIR=/media/downloads
MEDIA_CONCIERGE_MIN_FREE_GB=8                 # reclaim target floor
MEDIA_CONCIERGE_MODEL=claude-sonnet-5
```

## Defaults (the "minimal input" contract)

If the owner doesn't specify, assume:
- quality **1080p** (Radarr/Sonarr profile `HD-1080p`)
- audio **English**
- subtitles **English**, **soft** (muxed track, not burned)
- after a download that pushes free space < `MEDIA_CONCIERGE_MIN_FREE_GB`, the agent first proposes
  a reclaim (watched → oldest-added) and asks once; on repeat low-space it just does it.

Burn-in only when the owner says "burn"/"hardcode"/"baked in". Warn it's slow
(no HW encoder): ~0.3–1× realtime.

## Download confirmation gate (hard rule)

`grab` (the tool that actually sends a torrent to qBittorrent) is **never** called
without the owner confirming the specific release first. The flow is always:

1. `find` → resolve the title (TMDB/TVDB id, year, overview).
2. `search_releases` → Prowlarr search; return ranked candidates each with:
   release **name**, **size**, **seeders/leechers**, parsed **resolution**, parsed
   **video codec**, parsed **audio** language(s)/codec, parsed **subtitle** hints,
   **indexer**, release group.
3. Bot posts the top candidate (+ 2–3 alternatives) and **waits**. It states exactly
   what will be grabbed: "`<release name>` · 1080p x265 · ENG audio · 12.3 GB · 340
   seeders · from 1337x — grab this?"
4. Only on an explicit yes / "grab #2" / etc. does the agent call `grab(release_guid)`.
5. `grab` registers a job; the poller reports import → subtitle step → scan → done.

The system prompt makes this non-negotiable and forbids `grab` before a confirm in
the same conversation. `get` is a convenience that runs steps 1–3 only.

`add_magnet` is **exempt** from this gate: the owner handing over a magnet link (or
`.torrent` URL) is itself the confirmation of exactly what to download. See
"Manual magnet import" below.

## Manual magnet import

The indexer search (step 2 above) only surfaces releases Radarr/Sonarr can parse and
match to an exact title+year(+season/episode) under the requested quality profile.
Well-seeded releases with nonstandard names, year mismatches, season-pack labels, or
off-profile quality are dropped by the arr before the owner sees them — even though
Prowlarr's indexers (incl. 1337x) return them. When the owner finds such a release
himself, `add_magnet` bypasses the arrs entirely:

1. The magnet (or `.torrent` URL) is added to qBittorrent under category `manual`
   (save path `MEDIA_CONCIERGE_DOWNLOADS_DIR`), tagged `mb-<rand>` so the poller can track it by
   tag (robust against name/metadata resolution lag); infohash is a fallback match.
2. A `magnet` job is registered (`media_kind`, `title_hint`, subs prefs in detail).
3. The poller reports progress, then on completion runs
   `organize.import_download()` — which, unlike `import_file`, files **every**
   episode of a season pack as `Title - SxxExx.ext` (movies: biggest video as
   `Title (Year)`), identifying via guessit + arr metadata lookup from the torrent
   name. It then muxes styled subs per imported file, removes the now-emptied
   torrent from qBittorrent (files were moved, not copied — avoids the dangling-seed
   `file_open` errors), and triggers a Jellyfin scan.

Note: magnet imports are **not** tracked by Radarr/Sonarr (they never grabbed them),
so they appear in Jellyfin but not in the arr library views.

## qBittorrent status monitor

`qbmonitor.tick()` runs inside the bot poller every cycle (before the per-job
loop, so it runs even with no bot jobs open) and watches EVERY torrent in
qBittorrent, however it was added — bot grab/magnet, Radarr/Sonarr, or the qBit
UI. It DMs the owner on four edges, keyed by infohash in `state/qbmon.json`
(so each fires once and survives restarts; the first run after a fresh state file
records existing torrents silently — no `added` burst):

- **added** — a new infohash appears.
- **progress** — a summary of active downloads every `MEDIA_CONCIERGE_QBMON_SUMMARY_MIN` min
  (default 5). A download gaining < `MEDIA_CONCIERGE_QBMON_FAST_PCT` (1%) in a window is a
  trickle: it's held out of the summary until it accumulates `MEDIA_CONCIERGE_QBMON_SLOW_STEP_PCT`
  (5%) since last shown, so a near-dead torrent alerts per 5% rather than every
  5 min at ~0%.
- **stalled** — no progress for `MEDIA_CONCIERGE_QBMON_STALL_MIN` min (default 10); warns once
  per stall episode, re-arms when it resumes.
- **completed** — reaches 100%; suppressed when an open bot job already tracks it
  (that job posts the richer `imported into library` message instead).

This replaces the old per-job incremental progress pings (removed) with one
qBit-wide view. Per-job handlers still post import + subtitle results.

## Tool catalog (v1)

Read:
- `library_report(kind=all|movies|shows, detail=summary|full)` — titles, years,
  sizes, resolutions, missing-subs flags. Via jellyfin API + filesystem.
- `queue_status()` — active downloads from qBittorrent + arr queue with % / ETA.
- `disk_report()` — df + top-N largest titles + reclaim candidates.
- `find(query, kind=auto|movie|show)` — Radarr/Sonarr lookup → tmdb/tvdb id, year,
  overview, whether already in library.
- `search_releases(title|tmdb_id, kind, want_resolution=1080p, want_audio=en,
   season=None, episode=None, limit=8)` — Prowlarr search; returns ranked candidates
   with the parsed fields listed in "Download confirmation gate". **Read-only.**

Mutate (all logged to `logs/actions.jsonl`):
- `grab(release_guid or indexer_id+guid, kind, target_title)` — send that exact
  release to qBittorrent (via Prowlarr `/api/v1/search` grab or arr release push).
  **Only after the owner confirms.** Registers a job.
- `add_magnet(magnet, title_hint=None, kind=auto, subs_lang=en, subs_mode=soft)` —
  add a magnet/`.torrent` URL the owner found himself and full-import it into the
  library (handles season packs). Gate-exempt. Registers a `magnet` job. See
  "Manual magnet import".
- `get(title, kind=auto, ...)` — convenience: runs `find` + `search_releases` and
  returns the candidates for confirmation. Does **not** grab.
- `import_file(path, kind=auto, title_hint=None)` — file already on disk: guessit
  parse → arr lookup → (confirm) → move/rename into `Title (Year)/…` → strip junk
  (`.DS_Store`, `._*`, `*sample*`) → jellyfin scan.
- `attach_subs(item, lang=en, mode=soft|burn)` — fetch if absent (subliminal), then
  mux (instant) or burn (ffmpeg bg job).
- `reclaim_space(target_gb=MEDIA_CONCIERGE_MIN_FREE_GB, dry_run=True)` — rank watched → oldest-added;
  delete via Radarr/Sonarr (trashes files) + trash-cli for orphans.
- `jellyfin_scan(path=None)` — targeted or full library refresh.
- `remove(title, kind, delete_files=True)` — unmonitor + delete via arr.
- `job_status(job_id=None)` — list tracked long jobs (downloads, burns).
- `run_shell(cmd, why)` — arbitrary shell on the host as the deploy user (passwordless
  sudo available). Logged with the stated reason. Covers anything the typed tools
  don't. `rm` discouraged — prefer `trash`.

## Milestones

1. **Infra prep** (no code): qbit known password; add 1337x/YTS/EZTV to Prowlarr;
   turn on Radarr/Sonarr rename + folders; add `sonarr`/`manual` qbit categories;
   `trash-cli` + confirm jellyfin-ffmpeg path.
2. **Clients** (`clients/`): radarr, sonarr, prowlarr, qbit, jellyfin — typed thin
   wrappers, each with a `__main__` smoke test.
3. **Tools** (`tools/`) wrapping clients; `library_report`, `disk_report`,
   `queue_status`, `find` first (read-only, safe to iterate).
4. **Agent** (`agent.py`): system prompt, tool registry, permission hook, logging.
5. **Discord** (`bot.py`): owner-gate, per-channel session, long-job poller.
6. **`get` / `import_file` / `subtitles` / `reclaim`** — the mutating core, each
   validated on one real title.
7. **systemd** `media_concierge.service`, enable, live test end-to-end from Discord.

## Testing

Each `clients/*.py` and `tools/*.py` runs standalone (`uv run python -m media_concierge.clients.radarr`)
and prints a real call result. `scripts/smoke.sh` runs them all. No mutation in smoke
tests except against a throwaway title the script also cleans up.

## Open questions / risks

- 7 GB RAM: agent + discord.py is light (~150 MB); risk is ffmpeg burn + Jellyfin
  transcode concurrently. Burn jobs run `nice -19` and one-at-a-time.
- Subtitle provider reliability without OpenSubtitles key — subliminal falls back to
  Podnapisi/TVsubtitles/Gestdown; quality varies. Ask owner for a free OS key.
- 1337x via Prowlarr can rate-limit / go down; keep 2–3 indexers so search degrades
  gracefully.

## Run & usage (merged from the former README)

Discord agent that runs the Jellyfin media stack on the server. See `SPEC.md`.

```
Discord ──▶ bot.py ──▶ agent.py (anthropic Tool Runner, model=$MEDIA_CONCIERGE_MODEL)
                          └─ tools.py ─▶ Radarr / Sonarr / Prowlarr / qBittorrent / Jellyfin
```

### Layout
- `src/media_concierge/clients/` — thin REST wrappers, each runnable: `uv run python -m media_concierge.clients.arr`
- `src/media_concierge/tools.py` — the `@beta_tool` functions the model calls
- `src/media_concierge/agent.py` — system prompt + `run_turn()`
- `src/media_concierge/bot.py` — Discord front-end + background job poller
- `scripts/setup_infra.py` — one-time: indexers, arr renaming, qbit categories
- `scripts/setup_flare_indexers.py` — adds FlareSolverr proxy + 1337x/EZTV
- `scripts/smoke.sh` — read-only end-to-end check

### Run
```
cd ~/homelab                                 # uv workspace root (one lockfile)
uv sync --all-packages
# shared creds (ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN, OWNER_DISCORD_ID) go in
# ~/homelab/secrets/shared.env; media-concierge's own keys here:
cp services/media-concierge/.env.example services/media-concierge/.env   # fill JELLYFIN_API_KEY, QBIT_PASS, …
uv run python scripts/setup_infra.py
bash scripts/smoke.sh
sudo cp systemd/media_concierge.service /etc/systemd/system/
sudo systemctl enable --now media-concierge
journalctl -u media-concierge -f
```

### Usage (Discord)
DM the bot, or @mention it in a channel. Only `OWNER_DISCORD_ID` is obeyed.
- "what's in the library" / "what's downloading" / "how much space is left"
- "get me Dune Part Two in 1080p" → it shows release candidates and waits for you
  to pick one before it downloads
- "grab #2" / "yes" → downloads, imports, adds subs, rescans, reports back
- "burn spanish subs into <file>" → slow background re-encode
- "clean up /media/downloads/<thing>" → identifies + files into the library
- "free up 20 GB" → shows a plan (watched → oldest); "do it" to execute

### Services touched
qBittorrent auth: subnet-whitelisted (`172.16.0.0/12` + localhost) so host + arr
bypass login. FlareSolverr at :8191 for Cloudflare-gated indexers. Radarr/Sonarr
renaming is ON so imports land as `Title (Year)/…`.
