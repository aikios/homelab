#!/usr/bin/env bash
# Install or upgrade a homelab Python service on the current host.
#
#   deploy/install.sh <service-name> [<cmd>...]
#
# With <cmd>s, only the units named <service>-<cmd>.* are installed (for services
# whose units are split across hosts, e.g. `install.sh media-transcoder worker`).
#
# Assumes: this repo is checked out on the host, uv is installed, and
# passwordless sudo is available (for copying/enabling systemd units).
# Timers, path units and Restart=always services are enabled+started; oneshots are left
# 'static' (their .timer drives them).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
svc="${1:?usage: install.sh <service-name> [<cmd>...]}"
shift
cmds=("$@")
dir="$REPO/services/$svc"
[ -d "$dir" ] || { echo "no such service: services/$svc" >&2; exit 1; }

UV="${UV:-$HOME/.local/bin/uv}"   # uv is not on PATH over non-interactive ssh
command -v "$UV" >/dev/null 2>&1 || { echo "uv not found at $UV" >&2; exit 1; }

# The committed unit files are host-agnostic templates: %%DEPLOY_USER%% / %%REPO%%
# / %%UV%% are filled in here with this host's real values, so nobody's username
# or home path lives in the tracked tree. Override DEPLOY_USER to run as another user.
DEPLOY_USER="${DEPLOY_USER:-$(id -un)}"

# One shared venv for the whole workspace. --all-packages installs every service
# member (and its console script) into it, so deployed services coexist instead
# of pruning each other — the root project doesn't depend on them. Units then run
# with `uv run --no-sync`, so a fire never re-syncs (and never evicts a sibling).
# Renamed service? Retire the old units and carry over untracked per-host files
# (.env, state/) from the old dir first. No-op when already migrated. Runs before
# the sync, with the existing venv's python rather than uv: a leftover old dir
# matches the services/* member glob without a pyproject.toml, which makes every
# uv command fail. A fresh host has no venv yet, and nothing to migrate either.
if [ -x "$REPO/.venv/bin/python" ]; then
  "$REPO/.venv/bin/python" "$REPO/deploy/migrate.py" "$svc"
fi

echo "==> syncing workspace env (one lockfile, all members)"
"$UV" sync --project "$REPO" --all-packages


# Per-host setup beyond the venv (e.g. stream-resolver fetching Chromium).
if [ -x "$dir/scripts/setup.sh" ]; then
  echo "==> running $svc setup"
  UV="$UV" "$dir/scripts/setup.sh"
fi

wanted() {  # is this unit file selected by the <cmd> filter?
  [ ${#cmds[@]} -eq 0 ] && return 0
  local stem c; stem="$(basename "${1%.*}")"
  for c in "${cmds[@]}"; do [ "$stem" = "$svc-$c" ] && return 0; done
  return 1
}

if compgen -G "$dir/systemd/*" >/dev/null; then
  echo "==> installing systemd units for $svc"
  for unit in "$dir"/systemd/*.service "$dir"/systemd/*.timer "$dir"/systemd/*.path; do
    [ -e "$unit" ] && wanted "$unit" || continue
    sed -e "s|%%DEPLOY_USER%%|$DEPLOY_USER|g" \
        -e "s|%%REPO%%|$REPO|g" \
        -e "s|%%UV%%|$UV|g" "$unit" \
      | sudo -n tee /etc/systemd/system/"$(basename "$unit")" >/dev/null
    echo "   installed $(basename "$unit")"
  done
  sudo -n systemctl daemon-reload
  for unit in "$dir"/systemd/*.timer "$dir"/systemd/*.path "$dir"/systemd/*.service; do
    [ -e "$unit" ] && wanted "$unit" || continue
    base="$(basename "$unit")"
    if [[ "$base" == *.timer || "$base" == *.path ]] || grep -q 'Restart=always' "$unit"; then
      sudo -n systemctl enable --now "$base"
      echo "   enabled $base"
    fi
  done
else
  echo "==> no systemd/ units for $svc (nothing to enable)"
fi

echo "done: $svc"
