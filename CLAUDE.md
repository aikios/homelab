# homelab — working rules

This repo is **homelab as code**. Read SPEC.md for architecture, decisions,
and the migration plan. Develop here on the server (Remote-SSH); the running
systemd services run from this checkout, so edit + `sudo systemctl restart <svc>`
(or the next timer fire for oneshots) = deploy. Migrate one service at a time and
verify it live before starting the next.

## Hard rules
- NEVER add a Claude co-author trailer to commits (no `Co-Authored-By: Claude ...`).
- Secrets never on argv / in env visible to `ps` — pass tokens to curl via `-K -`
  (stdin config). See libs/core/src/homelab_core/notify.py.
- This will become a PUBLIC repo. Never commit secrets, tokens, real hostnames/IPs,
  or owner Discord IDs — only `*.example` files. Real values go in gitignored
  `.env` / `deploy/hosts/*.local.yml`. gitleaks pre-commit is set up
  (`uvx pre-commit install` to enable the hook in a fresh clone).

## Tooling
- uv for everything: `uv sync`, `uv run`, `uv add`. (Interactively on this host uv
  is on PATH; over non-interactive ssh use `~/.local/bin/uv`.)
- Before committing: `uvx ruff check` and `uvx ty check`.

## Host notes
- Central time (systemd OnCalendar is local). Passwordless `sudo -n`.
- Shared secrets (Discord bot token + owner id, Anthropic key) live in the
  host-level `secrets/shared.env` (gitignored); services point HOMELAB_SHARED_ENV
  there and read it via core.config.shared_env. Per-service keys stay in that
  service's own `.env`. HA home coordinates come from a sudo-read of
  ~/services/homeassistant/config/.storage/core.config.

## Status / next
Scaffold done. **rain-alert** and **topic-watcher** migrated into services/ and
running from this checkout on the server (verified live). core.notify has the
Discord REST primitives and core.ui adds the shared Discord UI layer (Messenger
protocol + Discord class) the notifiers program against; core.ha/config reconciled.
Deploy model:
`deploy/install.sh <svc>` runs `uv sync --all-packages`; units run via
`uv run --no-sync` so all workspace members coexist in the one venv.
**mediabot** is also migrated (faithful lift-and-shift into services/mediabot,
resident `mediabot.service` running from the repo; shared creds in secrets/
shared.env, service keys in its own .env). Its pre-existing ruff/ty findings were
left as-is (debt, not from the migration).
All six third-party **stacks** are exported to stacks/ (compose + scrubbed
.env.example; a stacks/.gitignore whitelists only compose/examples/docs). The
**hosts inventory** (deploy/hosts/hosts.local.yml, gitignored) + `deploy/host-up.sh
[host]` give one-command host reproduction (install.sh per service, compose up per
stack). All 6 migration-plan steps are done.
NEXT: add the GitHub remote (public, hardened) when ready. Note: the repo stacks/
are config captures — the containers still run from ~/services/<stack> on
the server (host-up's stack portion is for a fresh host; running it here would
clash with the live containers by container_name). The old ~/services owner dirs
remain as fallback — retire once confident.
