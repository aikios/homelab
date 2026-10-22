#!/usr/bin/env bash
# One-time per-host setup for sports-api's dedicated playwright venv.
#
# sports-api needs playwright + a headless Chromium, which are far too heavy for
# the shared uv workspace venv — so it lives in its own venv instead, and this
# service is excluded from the uv workspace. Re-run only on a fresh host (or to
# upgrade playwright); install.sh does NOT build this venv.
#
#   services/sports-api/bootstrap.sh
set -euo pipefail

VENV="${SPORTS_VENV:-$HOME/.venvs/sports}"
HERE="$(cd "$(dirname "$0")" && pwd)"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$HERE/requirements.txt"
"$VENV/bin/playwright" install chromium
# System libraries Chromium needs (needs root; falls back to non-sudo if allowed).
sudo "$VENV/bin/playwright" install-deps chromium \
  || "$VENV/bin/playwright" install-deps chromium

echo "sports-api venv ready at $VENV"
