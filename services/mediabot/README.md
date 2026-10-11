# mediabot

Discord agent that runs the Jellyfin media stack on the server. See `SPEC.md`.

```
Discord ──▶ bot.py ──▶ agent.py (anthropic Tool Runner, model=$MODEL)
                          └─ tools.py ─▶ Radarr / Sonarr / Prowlarr / qBittorrent / Jellyfin
```

## Layout
- `src/mediabot/clients/` — thin REST wrappers, each runnable: `uv run python -m mediabot.clients.arr`
- `src/mediabot/tools.py` — the `@beta_tool` functions the model calls
- `src/mediabot/agent.py` — system prompt + `run_turn()`
- `src/mediabot/bot.py` — Discord front-end + background job poller
- `scripts/setup_infra.py` — one-time: indexers, arr renaming, qbit categories
- `scripts/setup_flare_indexers.py` — adds FlareSolverr proxy + 1337x/EZTV
- `scripts/smoke.sh` — read-only end-to-end check

## Run
```
cd ~/homelab                                 # uv workspace root (one lockfile)
uv sync --all-packages
# shared creds (ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN, OWNER_DISCORD_ID) go in
# ~/homelab/secrets/shared.env; mediabot's own keys here:
cp services/mediabot/.env.example services/mediabot/.env   # fill JELLYFIN_API_KEY, QBIT_PASS, …
uv run python scripts/setup_infra.py
bash scripts/smoke.sh
sudo cp systemd/mediabot.service /etc/systemd/system/
sudo systemctl enable --now mediabot
journalctl -u mediabot -f
```

## Usage (Discord)
DM the bot, or @mention it in a channel. Only `OWNER_DISCORD_ID` is obeyed.
- "what's in the library" / "what's downloading" / "how much space is left"
- "get me Dune Part Two in 1080p" → it shows release candidates and waits for you
  to pick one before it downloads
- "grab #2" / "yes" → downloads, imports, adds subs, rescans, reports back
- "burn spanish subs into <file>" → slow background re-encode
- "clean up /media/downloads/<thing>" → identifies + files into the library
- "free up 20 GB" → shows a plan (watched → oldest); "do it" to execute

## Services touched
qBittorrent auth: subnet-whitelisted (`172.16.0.0/12` + localhost) so host + arr
bypass login. FlareSolverr at :8191 for Cloudflare-gated indexers. Radarr/Sonarr
renaming is ON so imports land as `Title (Year)/…`.
