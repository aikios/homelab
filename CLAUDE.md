# homelab — working rules

This repo is **homelab as code**. Read SPEC.md for architecture, decisions,
and the migration plan. Develop here on the server (Remote-SSH); the running
systemd services run from this checkout, so edit + `sudo systemctl restart <svc>`
(or the next timer fire for oneshots) = deploy. Migrate one service at a time and
verify it live before starting the next.

## Hard rules
- Service names follow `<subject>-<doer>` (SPEC.md "Restructure"): one name for the
  dir, package, units (`<name>[-<cmd>]`) and env prefix (`<NAME>_`). Renames go
  through `deploy/renames.yml`; never `git mv` a whole live service dir (it drags the
  gitignored .env/state along) — move tracked files and let migrate.py do the rest.
- NEVER add a Claude co-author trailer to commits (no `Co-Authored-By: Claude ...`).
- Secrets never on argv / in env visible to `ps` — pass tokens to curl via `-K -`
  (stdin config). See libs/core/src/homelab_core/notify.py.
- This will become a PUBLIC repo. Never commit secrets, tokens, real hostnames/IPs,
  or owner Discord IDs — only `*.example` files. Real values go in gitignored
  `.env` / `deploy/hosts/*.local.yml`. gitleaks pre-commit is set up
  (`uvx pre-commit install` to enable the hook in a fresh clone).
- The stream-resolver **source site is a secret**: never name it (domain, brand, or
  title strings) in code, docs, tests, or commit messages. It lives only in the
  gitignored `services/stream-resolver/.env` as `STREAM_RESOLVER_SOURCE_URL`.

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

## Status / handoff
**All 6 migration-plan steps are done and the repo is live, public, and hardened
at `github.com/aikios/homelab`.** Everything below is the state to resume from.

What exists and runs live on the server (all verified):
- **services/** — rain-notifier (08:00 timer), news-notifier (removed 2026-10-28; was a 2s `--listen` resident
  + 09:00 news timer), media-concierge (resident Discord Tool-Runner bot). All run from
  this checkout via systemd; media-concierge's agent turn was smoke-tested on anthropic
  1.13.0.
- **libs/core** — config / ha / notify (Discord REST primitives) / ui (the shared
  `Messenger` protocol + `Discord` REST client the notifiers program against).
- **stacks/** — 6 third-party apps as compose + scrubbed `.env.example` (config
  captures only; the containers still run from `~/services/<stack>`, NOT the repo —
  running host-up's stack portion here would clash by `container_name`).
- **TV stack** — stream-resolver (blacklodge + vega), tv-launcher (blacklodge),
  media-transcoder (`:submit,notify` on blacklodge, `:worker` on vega).
- **deploy/** — `install.sh <svc> [<cmd>...]` (migrate.py → `uv sync --all-packages`
  → `scripts/setup.sh` if present → install units, optionally only `<svc>-<cmd>.*`;
  units run `uv run --no-sync` so all members coexist in one venv) and
  `host-up.sh [host]` (reads `deploy/hosts/hosts.local.yml`; `svc:cmd,cmd` entries).

Key mechanics to remember:
- **Units are host-agnostic templates** (`%%DEPLOY_USER%%` / `%%REPO%%` / `%%UV%%`);
  `install.sh` substitutes real values at install time. Edit the template, re-run
  `install.sh`, not the installed `/etc/systemd/system/*` copies.
- **Secrets:** cross-service creds in gitignored `secrets/shared.env` (via
  `HOMELAB_SHARED_ENV`); per-service keys in `services/<svc>/.env`; stack creds in
  `stacks/<app>/.env`; host inventory in `deploy/hosts/hosts.local.yml`. Repo is
  fully de-personalized (no names/IPs/coords/usernames); history was squashed clean.
- **Git remote** is HTTPS via gh's credential helper (no SSH key on this host). The
  gh token has `repo` scope.

Open follow-ups (none blocking; also in SPEC.md "Open / to decide later"):
- media-concierge carries **pre-existing ruff (71) / ty (~20)** debt — not from the
  migration; clean up as its own pass (touches live code).
- Two secret-scanning sub-toggles (non-provider patterns, validity checks) didn't
  flip via the old gh's API — enable in GitHub Settings → Code security if wanted.
- The pre-repo `~/services/{rain-alert,topic-watcher,mediabot}` and `~/vizio_watch`
  were retired 2026-10-26 (archived in `~/backups/pre-repo-services-2026-10-26.tgz`);
  the `~/services/<stack>` compose dirs remain the running copies for the containers.
- Optional: a `Messenger` adapter so media-concierge's discord.py bot presents the same
  outbound interface (deferred — its Gateway event loop isn't a `Messenger`).
- Optional: LICENSE + repo topics for the public repo.
