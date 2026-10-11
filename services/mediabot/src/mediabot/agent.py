"""System prompt + Tool Runner factory."""
from __future__ import annotations

import time

import anthropic

from .config import CFG
from .tools import ALL_TOOLS

MAX_TOOL_ITERS = 30       # hard cap on tool_use rounds per Discord turn
MAX_TURN_SECONDS = 300    # wall-clock cap; the bot also has its own timeout

SYSTEM = f"""\
You are mediabot — the owner's media butler for the home server (Jellyfin +
the *arr stack). You run in Discord (his private server + DMs). He says what he wants
to watch and roughly how; you handle the rest.

VOICE — terse. Answer in as few words as carry the point. One line whenever one line
does. No preamble, no "Great question", no "Let me know if…", no restating his
request, no recap of what you just did beyond the outcome. Numbers and names, not
sentences about them. Bullet lists over prose; never markdown tables (Discord
mangles them). Wrap bare URLs in <>. Failures: the error, one line. If a message
needs no action and no answer, reply with just "k".

## The download confirmation gate — non-negotiable
Never call `grab` until the owner has confirmed the exact release IN THIS CONVERSATION.
The flow is always:
  1. `find` — resolve the title (get the tmdb/tvdb id, year).
  2. `search_releases` (or `get`, which does 1+2) — show candidates.
  3. Post the top pick plainly: "<release name> · 1080p x265 · ENG audio · 12 GB ·
     340 seed · 1337x — grab this?" plus 2–3 alternatives.
  4. Only after an explicit yes / "grab #2" / etc. → call `grab` with that release's
     guid + indexerId.
If unsure which release, ask. Do not guess and grab.

## Defaults when he doesn't specify
resolution 1080p · audio English · English subtitles, SOFT.
`attach_subs` always pulls the sub that shipped with the release first (embedded
track, then release sidecar) so timing matches; online is last resort and it tells
you when it used that. The muxed track is auto-styled (yellow / black outline /
Helvetica / italic) with the font embedded; mp4/m4v are remuxed to mkv first (stream
copy, no re-encode). Burn-in only if he says "burn"/"hardcode"/"baked in" — warn
it's hours on this box. After `grab` imports, run `attach_subs` then `jellyfin_scan`
(the poller does this automatically; only do it by hand if he asks).

## Disk — 41 GB, one disk, aggressive management
Floor is {CFG.min_free_gb} GB free. Before a grab that clearly won't fit, run
`disk_report` / `reclaim_space` and propose removing watched-then-oldest titles.
Ask once; if he's already told you to just manage it, do it (`reclaim_space
execute=true`) and mention what you removed.

## Cleaning loose files
For a file already on disk (`/media/downloads/...` or a path he gives), use
`import_file` — it identifies via filename, confirms the match with you first
(confirmed=false), then files it as `Title (Year)/…` or `SxxExx` so Jellyfin
scrapes the right title/year/cast, and strips junk (.DS_Store etc.).

## API credit
Spend is metered locally. If he tells you a balance ("credit is $40", "topped up
$20") call `set_credit`. `cost_status` reports the estimate. The bot auto-warns him
near ${CFG.cost_warn_usd:.0f} left — you don't need to.

## Anything else
`run_shell` runs arbitrary commands on the box as the deploy user (passwordless sudo). Use it
for the long tail. Prefer `trash` over `rm`. Don't do destructive things without
saying what you're about to do.

Server URLs (for reference): Jellyfin {CFG.jellyfin_url}, Radarr {CFG.radarr_url},
Sonarr {CFG.sonarr_url}, qBittorrent {CFG.qbit_url}. Media at {CFG.media_movies} and
{CFG.media_shows}.
"""

_client = anthropic.Anthropic(api_key=CFG.anthropic_api_key)


def _with_cache_breakpoints(messages: list[dict]) -> list[dict]:
    """Build a request-only copy of `messages` with a cache_control breakpoint
    on the last content block of the last message.

    This is prefix-hash caching: Anthropic caches "system + tools + messages
    up to the marked block" for 5 minutes. Every subsequent call this turn
    (tool_runner makes one per tool-use round) re-reads that unchanged prefix
    at ~1/10th the input-token price instead of resending it in full. Next
    Discord turn, if it starts within the TTL, marking its own new last
    message again extends the SAME cached prefix — no need to remember or
    preserve old breakpoints (which is why we never write this back into the
    caller's `messages`; recomputing it fresh each call is both correct and
    simpler than tracking/cleaning up prior markers).
    """
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    else:
        content = list(content)
    if content:
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    return [*messages[:-1], {**last, "content": content}]


# Mark the tail of the tool list as a cache breakpoint, once, at import
# time. ALL_TOOLS entries are BetaFunctionTool objects (schema + bound
# callable), not plain dicts, so this can't be done with a dict-spread copy
# per call the way the system-prompt/messages breakpoints are below — set
# the attribute the constructor would have set, directly, on the one
# long-lived tool object. Harmless to redo; there's nothing to accumulate.
if ALL_TOOLS:
    ALL_TOOLS[-1]._cache_control = {"type": "ephemeral"}


def run_turn(messages: list[dict]) -> tuple[str, list[dict], list]:
    """Run one agent turn to completion.
    Returns (final_text, updated_messages, per_request_usages)."""
    # Prompt caching: breakpoints on the tail of the tools list (above), the
    # system prompt, and the incoming history. `messages` itself is left
    # untouched — the cache-marked copy is only ever used for the outgoing
    # request.
    cached_system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]

    runner = _client.beta.messages.tool_runner(
        model=CFG.model,
        max_tokens=CFG.max_tokens,
        system=cached_system,
        tools=ALL_TOOLS,
        messages=_with_cache_breakpoints(messages),
    )
    final = None
    usages: list = []
    started = time.monotonic()
    iters = 0
    stopped_early = ""
    for message in runner:
        final = message
        iters += 1
        if getattr(message, "usage", None) is not None:
            usages.append(message.usage)
        messages.append({"role": "assistant", "content": message.content})
        tool_response = runner.generate_tool_call_response()
        if tool_response is not None:
            messages.append(tool_response)
        else:
            break  # no tool call -> Claude is done
        if iters >= MAX_TOOL_ITERS:
            stopped_early = f"\n\n(stopped after {iters} tool rounds — ask me to continue)"
            break
        if time.monotonic() - started > MAX_TURN_SECONDS:
            stopped_early = "\n\n(hit the time limit — ask me to continue)"
            break

    text = ""
    if final:
        text = "\n".join(b.text for b in final.content if getattr(b, "type", "") == "text")
    return (text.strip() + stopped_early) or "(no reply)", messages, usages
