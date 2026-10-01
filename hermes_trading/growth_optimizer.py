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
    meta = state.setdefault("optimizer", {
        "next_due_ms": now_ms + 60 * 60_000,
        "candidate_index": 0,
        "active_change": None,
        "last_decision": None,
        "history": [],
    })
    # Lifetime count is separate from the bounded display history; never reset on restart.
    meta.setdefault("trial_count", max(0, meta.get("candidate_index", 0)))
    meta.setdefault("legacy_trial_count_is_lower_bound", bool(meta.get("candidate_index", 0)))
    meta.setdefault("last_assessment", None)
    return meta


def candidate_config(cfg: GrowthConfig, index: int) -> tuple[GrowthConfig | None, dict]:
    field, step = STEPS[index % len(STEPS)]
    old = getattr(cfg, field)
    new = round(old + step, 4)
    lo, hi = ALPHA_BOUNDS[field]
    change = {"field": field, "from": old, "to": new}
    return (dataclasses.replace(cfg, **{field: new}) if lo <= new <= hi else None), change


def reference_cohort(cfg: GrowthConfig, trial: GrowthConfig) -> list[GrowthConfig]:
    """Base, preregistered candidate, one nearby control; never choose the best of them.

PBO diagnoses only this local family. Lifetime trial count also covers previous
cycles, including rejected candidates and both stress replays.
"""
    changed = next((field for field in ALPHA_BOUNDS if getattr(cfg, field) != getattr(trial, field)), None)
    controls = []
    if changed:
        opposite = 2 * getattr(cfg, changed) - getattr(trial, changed)
        lo, hi = ALPHA_BOUNDS[changed]
        if lo <= opposite <= hi:
            controls.append(dataclasses.replace(cfg, **{changed: opposite}))
    controls.extend(c for i in range(len(STEPS)) if (c := candidate_config(cfg, i)[0]) is not None)
    control = next((c for c in controls if c not in (cfg, trial)), None)
    return [cfg, trial] + ([control] if control else [])


def reserve_trials(meta: dict, cfg: GrowthConfig, index: int, now_ms: int) -> int:
    """Persist BEFORE research starts so crashes/timeouts do not erase attempts.

Count cohort and two cost scenarios conservatively, even if data acquisition
fails before completing the scheduled replays.
"""
    trial, change = candidate_config(cfg, index)
    count = len(reference_cohort(cfg, trial)) + 2 if trial else 1
    meta["trial_count"] = meta.get("trial_count", 0) + count
    meta["research_intent"] = {"ts": now_ms, "index": index, "change": change,
                                "reserved_trials": count, "total_trials": meta["trial_count"]}
    return meta["trial_count"]


def evaluate(cfg: GrowthConfig, index: int, n_trials: int | None = None) -> dict:
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
    latest = [candles[a]["t"][-1] for a in cfg.assets]
    if max(latest) - min(latest) > BAR_MS or time.time() * 1000 - end > 2 * BAR_MS:
        return {"accepted": False, "reason": "stale_or_unsynchronized_history", "change": change}
    earliest = end - (WINDOWS * WINDOW_DAYS + 15) * DAY_MS
    if any(candles[a]["t"][0] > earliest or hourly[a]["t"][0] > end -
           (WINDOWS * WINDOW_DAYS + 20) * DAY_MS or
           end - candles[a]["t"][-1] > 2 * BAR_MS for a in cfg.assets):
        return {"accepted": False, "reason": "incomplete_history", "change": change}
    # Missing crypto candles invalidate the replay rather than silently improving its score.
    if any(any(b - a != BAR_MS for a, b in zip(candles[asset]["t"], candles[asset]["t"][1:]))
           for asset in cfg.assets):
        return {"accepted": False, "reason": "market_data_gap", "change": change}
    cohort = reference_cohort(cfg, trial)
    try:
        result = assess(cfg, trial, candles, hourly, windows=WINDOWS, window_days=WINDOW_DAYS,
                        cohort=cohort, n_trials=max(n_trials or 0, len(cohort) + 2))
    except ValueError as exc:
        return {"accepted": False, "reason": str(exc), "change": change}
    last = result["windows"][-1]
    # Freshest window is a confirmation gate. A tiny advantage is indistinguishable from noise.
    validation = result.get("validation", {})
    passed = all(validation.get(gate, {}).get("status") == "PASS" for gate in
                 ("leakage", "costs", "walk_forward", "dsr", "pbo", "bootstrap"))
    accepted = (passed and result["eligible_for_manual_review"] and last["candidate_trades"] >= 5 and
                last["candidate_return"] - last["baseline_return"] >= 0.0025 and
                last["candidate_return"] > 0 and last["stress_return"] > 0)
    return {"accepted": accepted, "reason": "passed" if accepted else "validation_failed",
            "change": change, "assessment": result,
            "evaluated_config": dataclasses.asdict(cfg),
            "sources": {a: {"15m": candles[a].get("source"), "1h": hourly[a].get("source")}
                        for a in cfg.assets}}


def record(meta: dict, decision: dict, now_ms: int) -> None:
    entry = {"ts": now_ms, **decision}
    meta["last_decision"] = entry
    meta.setdefault("history", []).append(entry)
    meta["history"] = meta["history"][-30:]
    if "assessment" in decision or "accepted" in decision:
        meta["last_assessment"] = {"ts": now_ms, "reason": decision.get("reason"),
             "state": "PAPER_OBSERVATION" if decision.get("event") == "applied" else
                      "VALIDATED" if decision.get("event") == "deferred" else "REJECTED",
             "validation": decision.get("assessment", {}).get("validation", {})}


def forward_verdict(meta: dict, state: dict, equity: float, now_ms: int) -> str | None:
    """Conservative live loss guard; a negative cohort is not proof of causation."""
    active = meta.get("active_change")
    if not active or state["positions"] or state["pending"]:
        return None
    if (now_ms - active["applied_ms"] < FORWARD_DAYS * DAY_MS or
            len(state["trades"]) - active["trade_count"] < FORWARD_TRADES):
        return None
    return "revert" if equity < active["equity_at_apply"] * 0.995 else "confirm"
