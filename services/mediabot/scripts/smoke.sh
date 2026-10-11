#!/usr/bin/env bash
# Read-only smoke test: exercises every client + a couple of tools. No downloads.
set -uo pipefail
cd "$(dirname "$0")/.."

run() { echo; echo "### $*"; uv run --no-dev python -m "$@" 2>&1 | sed 's/^/  /'; }

run mediabot.clients.arr
run mediabot.clients.qbit
run mediabot.clients.jellyfin

echo; echo "### tools: library_report / disk_report / queue_status / find"
uv run --no-dev python - <<'PY' 2>&1 | sed 's/^/  /'
from mediabot import tools
print("--- library_report ---");  print(tools.library_report("all", "summary"))
print("--- disk_report ---");     print(tools.disk_report())
print("--- queue_status ---");    print(tools.queue_status())
print("--- find 'the bear' ---"); print(tools.find("the bear", "show"))
PY

echo; echo "smoke done."
