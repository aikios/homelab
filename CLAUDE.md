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
- **services/** — rain-alert (08:00 timer), news-notifier (2s `--listen` resident
  + 09:00 news timer), mediabot (resident Discord Tool-Runner bot). All run from
  this checkout via systemd; mediabot's agent turn was smoke-tested on anthropic
  1.13.0.
- **libs/core** — config / ha / notify (Discord REST primitives) / ui (the shared
  `Messenger` protocol + `Discord` REST client the notifiers program against).
- **stacks/** — 6 third-party apps as compose + scrubbed `.env.example` (config
  captures only; the containers still run from `~/services/<stack>`, NOT the repo —
  running host-up's stack portion here would clash by `container_name`).
- **deploy/** — `install.sh <svc>` (`uv sync --all-packages`, then installs units;
  units run `uv run --no-sync` so all members coexist in one venv) and
  `host-up.sh [host]` (reads `deploy/hosts/hosts.local.yml`).

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
- mediabot carries **pre-existing ruff (66) / ty (16)** debt — not from the
  migration; clean up as its own pass (touches live code).
- Two secret-scanning sub-toggles (non-provider patterns, validity checks) didn't
  flip via the old gh's API — enable in GitHub Settings → Code security if wanted.
- **Retire the old `~/services/{rain-alert,topic-watcher,mediabot}` dirs** (now
  orphaned fallbacks) once fully confident; the `~/services/<stack>` compose dirs
  remain the authoritative/running copies for the containers.
- Optional: a `Messenger` adapter so mediabot's discord.py bot presents the same
  outbound interface (deferred — its Gateway event loop isn't a `Messenger`).
- Optional: LICENSE + repo topics for the public repo.
