# stacks

Third-party apps that run as containers, captured as **config** (not code): each
app gets a directory with a `docker-compose.yml` and, where it has secrets, a
scrubbed `.env.example`. Real `.env` files and all bind-mounted `config/`/`data/`/
state are gitignored (see `.gitignore` here — only compose, `*.env.example`, and
docs are tracked), so a fresh host can recreate the stack without exposing secrets
or bulky state.

## Stacks
- **arr-stack** — radarr, sonarr, prowlarr, qbittorrent, jellyseerr
  (linuxserver.io + jellyseerr images). Each app keeps its config in a sibling
  bind mount; `/media` is mounted for library + downloads. `.env.example` records
  the credential surface (the apps generate their own API keys on first run).
- **flaresolverr** — Cloudflare-challenge solver for prowlarr indexers; joins the
  arr-stack network (`arr-stack_default`, external).
- **jellyfin-mcp** — read-only MCP server over Jellyfin; `JELLYFIN_MCP_TOKEN`
  gates HTTP access.
- **matter-server** — Matter/Thread controller for Home Assistant (`host`
  network, dbus mount).
- **homeassistant** — Home Assistant (`host` network, `privileged`); all state in
  `./config`. See its README for the portability rationale + migration steps.
- **openclaw** — openclaw gateway + cli (prebuilt `openclaw:local` image).

## Recreate a stack on a host
```sh
cd stacks/<app>
cp .env.example .env     # where present; fill in real values
docker compose up -d
```
Bring the arr-stack up before flaresolverr (which joins its network).
