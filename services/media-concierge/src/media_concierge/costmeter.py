"""Local API-spend meter.

Anthropic's Messages API key can't read the account balance, so we estimate: sum
`usage` from every request the tool runner makes, price it, and subtract from a
running "credit remaining" figure the owner sets ("I topped up $40" -> add_credit).
Warns on Discord when it drops below MEDIA_CONCIERGE_COST_WARN_USD.
"""
from __future__ import annotations

import json
import time

from .config import CFG

# $ per 1M tokens: (input, output, cache_write_5m, cache_read)
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 2.50, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
    "claude-fable-5-1": (10.0, 50.0, 12.50, 1.0),
    "claude-fable-5": (10.0, 50.0, 12.50, 1.0),
}
_DEFAULT_PRICE = (5.0, 25.0, 6.25, 0.50)

_PATH = CFG.state_dir / "cost.json"


def _load() -> dict:
    if _PATH.exists():
        try:
            return json.loads(_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"remaining_usd": float(CFG.cost_credit_start), "spent_usd": 0.0,
            "turns": 0, "last_warn": 0.0, "configured": CFG.cost_credit_start > 0}


def _save(d: dict) -> None:
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(_PATH)


def price_turn(model: str, usages: list) -> float:
    pin, pout, pcw, pcr = PRICES.get(model, _DEFAULT_PRICE)
    total = 0.0
    for u in usages:
        it = getattr(u, "input_tokens", 0) or 0
        ot = getattr(u, "output_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        total += (it * pin + ot * pout + cw * pcw + cr * pcr) / 1_000_000
    return total


def record(model: str, usages: list) -> tuple[float, dict]:
    """Price this turn, subtract from remaining, persist. Returns (turn_cost, state)."""
    cost = price_turn(model, usages)
    d = _load()
    d["spent_usd"] = round(d.get("spent_usd", 0.0) + cost, 4)
    d["remaining_usd"] = round(d.get("remaining_usd", 0.0) - cost, 4)
    d["turns"] = d.get("turns", 0) + 1
    _save(d)
    return cost, d


def add_credit(usd: float) -> dict:
    d = _load()
    d["remaining_usd"] = round(d.get("remaining_usd", 0.0) + usd, 4)
    d["configured"] = True
    d["last_warn"] = 0.0
    _save(d)
    return d


def set_remaining(usd: float) -> dict:
    d = _load()
    d["remaining_usd"] = round(usd, 4)
    d["configured"] = True
    d["last_warn"] = 0.0
    _save(d)
    return d


def status() -> dict:
    return _load()


def check_warn() -> str | None:
    """Return a warning string if credit is low and we haven't warned recently."""
    d = _load()
    if not d.get("configured"):
        return None
    rem = d.get("remaining_usd", 0.0)
    if rem > CFG.cost_warn_usd:
        return None
    if time.time() - d.get("last_warn", 0.0) < CFG.cost_warn_cooldown_h * 3600:
        return None
    d["last_warn"] = time.time()
    _save(d)
    spent = d.get("spent_usd", 0.0)
    if rem <= 0:
        return (f"🛑 API credit estimate is **${rem:.2f}** (spent ~${spent:.2f}). "
                f"media-concierge calls will start failing. Top up: "
                f"<https://console.anthropic.com/settings/billing>")
    return (f"⚠️ ~**${rem:.2f}** API credit left (spent ~${spent:.2f} over "
            f"{d.get('turns',0)} turns). Re-up soon: "
            f"<https://console.anthropic.com/settings/billing>")
