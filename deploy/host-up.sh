#!/usr/bin/env bash
# Bring a whole host up from the inventory: install+enable the owner Python
# services assigned to it, and docker-compose up its third-party stacks.
#
#   deploy/host-up.sh [hostname]      # default: this machine's hostname
#
# Reads deploy/hosts/hosts.local.yml (real, gitignored) if present, else
# hosts.example.yml. Idempotent — safe to re-run. Assumes the repo is checked
# out on the host, uv is installed, passwordless sudo, and (for stacks) docker.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-$(hostname)}"
UV="${UV:-$HOME/.local/bin/uv}"
command -v "$UV" >/dev/null 2>&1 || { echo "uv not found at $UV" >&2; exit 1; }

inv() { "$UV" run --project "$REPO" python "$REPO/deploy/_inventory.py" "$HOST" "$1"; }

echo "==> bringing up host: $HOST"
echo "    inventory: $([ -f "$REPO/deploy/hosts/hosts.local.yml" ] && echo hosts.local.yml || echo hosts.example.yml)"

services="$(inv services)"
stacks="$(inv stacks)"

echo
echo "==> owner services: ${services:-（none）}"
# An entry is <service> or <service>:<cmd>[,<cmd>...] (only those units here).
for entry in $services; do
  svc="${entry%%:*}"; cmds=()
  [ "$entry" != "$svc" ] && IFS=, read -r -a cmds <<< "${entry#*:}"
  echo "---- install.sh $entry ----"
  UV="$UV" bash "$REPO/deploy/install.sh" "$svc" "${cmds[@]}"
done

echo
echo "==> third-party stacks: ${stacks:-（none）}"
if [ -n "$stacks" ]; then
  command -v docker >/dev/null 2>&1 || { echo "docker not found — cannot start stacks" >&2; exit 1; }
fi
for stack in $stacks; do
  dir="$REPO/stacks/$stack"
  [ -f "$dir/docker-compose.yml" ] || { echo "   !! no stacks/$stack/docker-compose.yml — skipping" >&2; continue; }
  # If the stack declares secrets (.env.example) but has no real .env, warn and
  # skip rather than start it half-configured.
  if [ -f "$dir/.env.example" ] && [ ! -f "$dir/.env" ]; then
    echo "   !! stacks/$stack needs an .env (see .env.example) — skipping" >&2
    continue
  fi
  echo "---- docker compose up -d: $stack ----"
  ( cd "$dir" && docker compose up -d )
done

echo
echo "done: $HOST"
