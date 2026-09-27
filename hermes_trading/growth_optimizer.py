"""Bounded, auditable paper-strategy tuning; never edits risk settings or source code."""
from __future__ import annotations

import dataclasses
import time

from .growth import BAR_MS, GrowthConfig
from .growth_lab import assess

DAY_MS = 86_400_000
INTERVAL_MS = 7 * DAY_MS
RETRY_MS = DAY_MS
FORWARD_DAYS = 14
FORWARD_TRADES = 10
WINDOWS = 4
WINDOW_DAYS = 30
ALPHA_BOUNDS = {
    "range_rsi": (26.0, 34.0),
    "trend_rsi": (36.0, 44.0),
    "target_r": (2.0, 3.0),
    "stop_atr": (1.6, 2.4),
}
# Exactly one preregistered candidate is assessed per cycle; no best-of-many search.
STEPS = (("range_rsi", -2), ("range_rsi", 2), ("trend_rsi", -2),
         ("trend_rsi", 2), ("target_r", -0.25), ("target_r", 0.25),
         ("stop_atr", -0.2), ("stop_atr", 0.2))


def initialise(state: dict, now_ms: int) -> dict:
    return state.setdefault("optimizer", {
        "next_due_ms": now_ms + 60 * 60_000,
        "candidate_index": 0,
        "active_change": None,
        "last_decision": None,
        "history": [],
    })


def candidate_config(cfg: GrowthConfig, index: int) -> tuple[GrowthConfig | None, dict]:
    field, step = STEPS[index % len(STEPS)]
    old = getattr(cfg, field)
    new = round(old + step, 4)
    lo, hi = ALPHA_BOUNDS[field]
    change = {"field": field, "from": old, "to": new}
    return (dataclasses.replace(cfg, **{field: new}) if lo <= new <= hi else None), change


def evaluate(cfg: GrowthConfig, index: int) -> dict:
    """Fetch closed public candles and compare one candidate on the same portfolio/costs."""
    trial, change = candidate_config(cfg, index)
    if trial is None:
        return {"accepted": False, "reason": "alpha_bound", "change": change}
    from .backtest import history

    days = WINDOWS * WINDOW_DAYS + 22
    candles = {asset: history(asset, "15m", days) for asset in cfg.assets}
    hourly = {asset: history(asset, "1h", days) for asset in cfg.assets}
    if any(not candles[a]["t"] or not hourly[a]["t"] for a in cfg.assets):
        return {"accepted": False, "reason": "incomplete_history", "change": change}
    end = min(candles[a]["t"][-1] for a in cfg.assets)
    earliest = end - (WINDOWS * WINDOW_DAYS + 15) * DAY_MS
    if any(candles[a]["t"][0] > earliest or hourly[a]["t"][0] > end -
           (WINDOWS * WINDOW_DAYS + 20) * DAY_MS or
           end - candles[a]["t"][-1] > 2 * BAR_MS for a in cfg.assets):
        return {"accepted": False, "reason": "incomplete_history", "change": change}
    # Missing crypto candles invalidate the replay rather than silently improving its score.
    if any(any(b - a != BAR_MS for a, b in zip(candles[asset]["t"], candles[asset]["t"][1:]))
           for asset in cfg.assets):
        return {"accepted": False, "reason": "market_data_gap", "change": change}
    result = assess(cfg, trial, candles, hourly, windows=WINDOWS, window_days=WINDOW_DAYS)
    last = result["windows"][-1]
    # Freshest window is a confirmation gate. A tiny advantage is indistinguishable from noise.
    accepted = (result["eligible_for_manual_review"] and last["candidate_trades"] >= 5 and
                last["candidate_return"] - last["baseline_return"] >= 0.0025 and
                last["candidate_return"] > 0 and last["stress_return"] > 0)
    return {"accepted": accepted, "reason": "passed" if accepted else "validation_failed",
            "change": change, "assessment": result,
            "sources": {a: {"15m": candles[a].get("source"), "1h": hourly[a].get("source")}
                        for a in cfg.assets}}


def record(meta: dict, decision: dict, now_ms: int) -> None:
    entry = {"ts": now_ms, **decision}
    meta["last_decision"] = entry
    meta.setdefault("history", []).append(entry)
    meta["history"] = meta["history"][-30:]


def forward_verdict(meta: dict, state: dict, equity: float, now_ms: int) -> str | None:
    """Conservative live loss guard; a negative cohort is not proof of causation."""
    active = meta.get("active_change")
    if not active or state["position"] or state["pending"]:
        return None
    if (now_ms - active["applied_ms"] < FORWARD_DAYS * DAY_MS or
            len(state["trades"]) - active["trade_count"] < FORWARD_TRADES):
        return None
    return "revert" if equity < active["equity_at_apply"] * 0.995 else "confirm"
