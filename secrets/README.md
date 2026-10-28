# secrets/

Host-level secrets shared by more than one service. **Only `*.example` files are
committed**; the real `shared.env` is gitignored (`*.env`).

## shared.env
Keys that multiple services need, read via `homelab_core.config.shared_env()`
(services point `HOMELAB_SHARED_ENV` at this file in their systemd units):

- `DISCORD_BOT_TOKEN`, `OWNER_DISCORD_ID` — the one Discord bot every notifier
  (rain-notifier, media-transcoder) reuses, and media-concierge's own identity.
- `ANTHROPIC_API_KEY` — used by media-concierge.

Per-service keys (arr/qbit/jellyfin URLs, media paths, cost thresholds, …) do
**not** go here — they live in that service's own gitignored `.env`.

## Setup on a fresh host
```sh
cp secrets/shared.env.example secrets/shared.env
chmod 600 secrets/shared.env
# fill in real values
```
