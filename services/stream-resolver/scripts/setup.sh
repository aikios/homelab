#!/usr/bin/env bash
# Per-host setup for stream-resolver, run by deploy/install.sh after the workspace
# sync: fetch the headless Chromium matching the pinned playwright (a no-op when
# already present), and install its system libraries only if it can't launch.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
run() { "$UV" run --no-sync --project "$REPO" "$@"; }

run playwright install chromium
if ! run python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()
" >/dev/null 2>&1; then
  echo "   chromium can't launch; installing its system deps"
  sudo -n "$REPO/.venv/bin/playwright" install-deps chromium
fi
