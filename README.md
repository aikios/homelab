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
deploy/install.sh rain-alert
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
- Never commit `.env`, tokens, real host IPs/hostnames, or owner IDs — only
  `*.example` files. Real values live in gitignored `.env` / `hosts.local.yml`.
- `uvx pre-commit install` enables the gitleaks hook. Turn on GitHub
  secret-scanning + push-protection for the repo too.
