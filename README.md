# homelab

A self-hosted home server as code: reusable Python services + the
third-party app stacks it runs. Clone it, point `deploy/install.sh` at a box, and
bring a service up on any Linux host with systemd + [uv](https://docs.astral.sh/uv/).

See [SPEC.md](SPEC.md) for the architecture and decisions.

## Layout
- `libs/core/` — `homelab-core`: shared config / Discord-notify / Home-Assistant
  helpers, so services share code instead of copy-pasting it.
- `services/` — owner-built Python daemons (uv workspace members). Each ships its
  own `systemd/` units and a `SPEC.md`.
- `stacks/` — third-party apps (Jellyfin, *arr, Home Assistant, …) as
  `docker-compose.yml` + `.env.example`.
- `deploy/` — `install.sh` and the per-host inventory.

## Develop
```sh
uv sync                 # build the workspace env from the single lockfile
uv run python -c "import homelab_core; print(homelab_core.__version__)"
```

## Deploy a service to a host
```sh
# on the host (repo checked out, uv installed, passwordless sudo):
deploy/install.sh rain-notifier
```

## Bring up a whole host from the inventory
```sh
# installs every service + starts every stack assigned to this host in
# deploy/hosts/hosts.local.yml (falls back to hosts.example.yml). Idempotent.
deploy/host-up.sh              # defaults to $(hostname)
deploy/host-up.sh myhost   # or name a host in the inventory
```
Copy `deploy/hosts/hosts.example.yml` to `hosts.local.yml` (gitignored) and fill
in your real hosts first. A stack that declares secrets (`.env.example`) is
skipped until you create its real `.env`.

## Secrets & safety (this repo is public)
- **Nothing host-specific is committed** — no secrets, tokens, real hostnames/IPs,
  home coordinates, usernames, or owner IDs. Only `*.example` files and templates.
- **Where real values live (all gitignored):** cross-service creds in
  `secrets/shared.env`; each service's own keys in its `services/<svc>/.env`;
  the host inventory in `deploy/hosts/hosts.local.yml`; stack creds in
  `stacks/<app>/.env`.
- **systemd units are host-agnostic templates** (`%%DEPLOY_USER%%` / `%%REPO%%` /
  `%%UV%%`); `deploy/install.sh` fills in this host's real user/paths at install
  time, so no username or home path lands in the tree.
- **In a fresh clone, run `uvx pre-commit install`** to enable the gitleaks hook.
  The repo also has GitHub secret-scanning + push-protection and Dependabot alerts
  enabled.
