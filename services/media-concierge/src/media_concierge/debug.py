"""Local debug channel — talk to the LIVE media-concierge without Discord or a browser.

The running bot (bot.py) listens on a unix socket at services/media-concierge/state/debug.sock
when MEDIABOT_DEBUG_SOCK != 0. This client sends one prompt and prints the agent's
reply. It runs the SAME agent turn and tools as a real owner DM (grab/remove/etc.
actually execute, the cost meter records, the background poller tracks downloads) —
only the Discord transport is skipped. The socket is local-only (0600); anyone who
can reach it already has host access, i.e. the same power as the run_shell tool.

    media-concierge-dm "grab veep s7"
    media-concierge-dm "disk report"
    echo "remove veep s5" | media-concierge-dm
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

# services/media-concierge/state/debug.sock — computed without importing CFG so the client
# needs no env. Mirrors config.ROOT (parents[2]) / "state".
SOCK = Path(__file__).resolve().parents[2] / "state" / "debug.sock"
TIMEOUT = 360  # headroom over the bot's 330s per-turn ceiling


def send(prompt: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(str(SOCK))
    except OSError as e:
        return (f"cannot reach the media-concierge debug socket at {SOCK}: {e}\n"
                f"(is media-concierge running with MEDIABOT_DEBUG_SOCK=1?)")
    try:
        s.sendall(prompt.encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            try:
                b = s.recv(65536)
            except TimeoutError:
                chunks.append(b"\n[timed out waiting for reply]")
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode(errors="replace")
    finally:
        s.close()


def main() -> None:
    prompt = " ".join(sys.argv[1:]).strip() or sys.stdin.read().strip()
    if not prompt:
        sys.stderr.write('usage: media-concierge-dm "<message to the bot>"\n')
        raise SystemExit(2)
    print(send(prompt))


if __name__ == "__main__":
    main()
