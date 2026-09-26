"""Prespecified candidate versus baseline across four non-overlapping holdout windows.

This is a research gate, never a writer of risk settings or an automatic deployment.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
from pathlib import Path

from .growth import GrowthConfig, replay
from .growth_run import _load_config, report


def _window(data: dict, start: int, end: int, warmup: int) -> dict:
    keep = [i for i, t in enumerate(data["t"]) if start - warmup <= t < end]
    return {k: [v[i] for i in keep] if isinstance(v, list) else v for k, v in data.items()}


def assess(base: GrowthConfig, trial: GrowthConfig, candles: dict, hourly: dict,
           windows: int = 4, window_days: int = 90) -> dict:
    if base.assets != trial.assets or base.capital != trial.capital:
        raise ValueError("both configurations must use the same assets and capital")
    # Risk and cost parameters are owner controlled; experiments only adjust alpha.
    frozen = ("risk_per_trade", "max_exposure", "daily_loss", "weekly_loss", "monthly_drawdown",
              "min_order_usd", "fee", "slippage", "spread")
    if any(getattr(base, field) != getattr(trial, field) for field in frozen):
        raise ValueError("candidate may change only alpha parameters")
    end = min(max(candles[a]["t"]) for a in base.assets) + 900_000
    width = window_days * 86_400_000
    rows, total_trades, assets_traded = [], 0, set()
    for j in range(windows):
        stop = end - (windows - 1 - j) * width
        start = stop - width
        c = {a: _window(candles[a], start, stop, 15 * 86_400_000) for a in base.assets}
        h = {a: _window(hourly[a], start, stop, 20 * 86_400_000) for a in base.assets}
        results = []
        for cfg in (base, trial, dataclasses.replace(trial, fee=trial.fee * 2,
                                                     slippage=trial.slippage * 2, spread=trial.spread * 2)):
            book = replay(cfg, c, h, trade_after_ms=start)
            first = cfg.capital
            last = book.equity()
            r = report(book)
            r["window_return"] = last / first - 1 if first > 0 else 0
            r["window_trades"] = [t for t in book.state["trades"] if t["opened_ms"] >= start]
            results.append(r)
        baseline, candidate, stress = results
        total_trades += len(candidate["window_trades"])
        assets_traded.update(t["asset"] for t in candidate["window_trades"])
        rows.append({"start": _iso(start), "end": _iso(stop), "baseline_return": baseline["window_return"],
                     "candidate_return": candidate["window_return"], "stress_return": stress["window_return"],
                     "baseline_dd": baseline["max_drawdown"], "candidate_dd": candidate["max_drawdown"],
                     "candidate_trades": len(candidate["window_trades"])})
    reasons = []
    if total_trades < 30:
        reasons.append("fewer_than_30_candidate_trades")
    if len(assets_traded) < 3:
        reasons.append("fewer_than_3_traded_assets")
    if sum(r["candidate_return"] > 0 for r in rows) < max(2, (windows + 1) // 2):
        reasons.append("insufficient_positive_windows")
    if statistics.median(r["candidate_return"] - r["baseline_return"] for r in rows) <= 0:
        reasons.append("median_return_not_better")
    if max(r["candidate_dd"] - r["baseline_dd"] for r in rows) > 0.01:
        reasons.append("drawdown_worse_by_over_one_point")
    if statistics.median(r["stress_return"] for r in rows) <= 0:
        reasons.append("double_cost_stress_not_positive")
    return {"eligible_for_manual_review": not reasons, "reasons": reasons, "windows": [
        {k: v for k, v in r.items() if k != "window_trades"} for r in rows],
        "candidate_trades": total_trades, "assets_traded": sorted(assets_traded)}


def _iso(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


def main() -> None:
    from .backtest import history

    parser = argparse.ArgumentParser(description="Compare a prespecified alpha change; never auto-apply")
    parser.add_argument("--baseline", type=Path, default=Path("growth.json"))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=4)
    parser.add_argument("--window-days", type=int, default=90)
    args = parser.parse_args()
    if args.windows < 2 or args.window_days < 30:
        parser.error("at least two windows of at least 30 days")
    base, trial = _load_config(args.baseline), _load_config(args.candidate)
    days = args.windows * args.window_days + 22
    candles = {a: history(a, "15m", days) for a in base.assets}
    hourly = {a: history(a, "1h", days) for a in base.assets}
    print(json.dumps(assess(base, trial, candles, hourly, args.windows, args.window_days), indent=2))


if __name__ == "__main__":
    main()
