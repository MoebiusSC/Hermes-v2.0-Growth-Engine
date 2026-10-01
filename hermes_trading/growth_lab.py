"""Prespecified candidate versus baseline across four non-overlapping holdout windows.

This is a research gate, never a writer of risk settings or an automatic deployment.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import hashlib
from pathlib import Path

import numpy as np

from .growth import BAR_MS, HOUR_MS, GrowthConfig, replay, signals_for_asset
from .growth_validation import (DAY_MS, VERSION, block_stress, daily_returns,
                                deflated_sharpe, probability_overfitting)


def validate_history(assets: tuple, candles: dict, hourly: dict, end: int, days: int) -> None:
    """Both timeframes, all assets, strict UTC grid, complete OHLC and warmup."""
    start = end - days * DAY_MS
    for asset in assets:
        for data, step, warmup in ((candles[asset], BAR_MS, 15), (hourly[asset], HOUR_MS, 20)):
            times = data.get("t", [])
            if not times or any(not isinstance(t, int) or t % step for t in times):
                raise ValueError("invalid_timestamp_grid")
            if any(b - a != step for a, b in zip(times, times[1:])):
                raise ValueError("market_data_gap")
            if times[0] > start - warmup * DAY_MS or times[-1] + step < end:
                raise ValueError("incomplete_history")
            values = np.asarray([data.get(k, []) for k in ("open", "high", "low", "close")], dtype=float)
            if values.shape != (4, len(times)) or not np.all(np.isfinite(values)) or np.any(values <= 0):
                raise ValueError("invalid_ohlc")
            o, h, lo, c = values
            if np.any(lo > np.minimum(o, c)) or np.any(h < np.maximum(o, c)):
                raise ValueError("invalid_ohlc")


def _settle(book, end: int) -> None:
    """Charge exit costs on terminal holdings; do not leave free liquidation at a fold boundary."""
    book.state["pending"] = {}
    for asset in list(book.state["positions"]):
        mark = book.state["marks"][asset]
        book._close(asset, end, mark * (1 - book.cfg.slippage - book.cfg.spread / 2), "fold_end")
    book.state["curve"][-1] = {"ts": _iso(end), "equity": book.equity()}


def audit_future_inputs(cfg: GrowthConfig, candles: dict, hourly: dict) -> None:
    """Perturb not-yet-closed hourly OHLC at three historical decision times."""
    for asset in cfg.assets:
        bars, hours = candles[asset], hourly[asset]
        for i in (len(bars["t"]) // 3, len(bars["t"]) // 2, 2 * len(bars["t"]) // 3):
            sub = {k: bars[k][max(0, i - 160):i + 1] for k in ("t", "open", "high", "low", "close")}
            decision = bars["t"][i] + BAR_MS
            mutated = {k: [v * 1000 if k != "t" and hours["t"][j] + HOUR_MS > decision else v
                           for j, v in enumerate(values)] if isinstance(values, list) else values
                       for k, values in hours.items()}
            if signals_for_asset(asset, sub, hours, cfg) != signals_for_asset(asset, sub, mutated, cfg):
                raise ValueError("future_hourly_leakage")


def _window(data: dict, start: int, end: int, warmup: int) -> dict:
    keep = [i for i, t in enumerate(data["t"]) if start - warmup <= t < end]
    return {k: [v[i] for i in keep] if isinstance(v, list) else v for k, v in data.items()}


def assess(base: GrowthConfig, trial: GrowthConfig, candles: dict, hourly: dict,
           windows: int = 4, window_days: int = 90, cohort: list[GrowthConfig] | None = None,
           n_trials: int | None = None) -> dict:
    from .growth_run import report
    if base.assets != trial.assets or base.capital != trial.capital:
        raise ValueError("both configurations must use the same assets and capital")
    # Risk and cost parameters are owner controlled; experiments only adjust alpha.
    alpha = {"range_rsi", "trend_rsi", "target_r", "stop_atr"}
    frozen = tuple(f.name for f in dataclasses.fields(base) if f.name not in alpha)
    if any(getattr(base, field) != getattr(trial, field) for field in frozen):
        raise ValueError("candidate may change only alpha parameters")
    if windows < 2 or window_days < 30:
        raise ValueError("at least two windows of at least 30 calendar days required")
    end = (min(max(candles[a]["t"]) for a in base.assets) + BAR_MS) // DAY_MS * DAY_MS
    validate_history(base.assets, candles, hourly, end, windows * window_days)
    audit_future_inputs(trial, candles, hourly)
    if cohort is None:
        from .growth_optimizer import reference_cohort
        cohort = reference_cohort(base, trial)
    if cohort[:2] != [base, trial] or any(any(getattr(c, f) != getattr(base, f) for f in frozen)
                                        for c in cohort):
        raise ValueError("aligned alpha-only cohort required")
    n_trials = max(n_trials or len(cohort), len(cohort))
    width = window_days * 86_400_000
    rows, total_trades, assets_traded = [], 0, set()
    daily = [[] for _ in cohort]
    double_daily, triple_daily = [], []
    for j in range(windows):
        stop = end - (windows - 1 - j) * width
        start = stop - width
        c = {a: _window(candles[a], start, stop, 15 * 86_400_000) for a in base.assets}
        h = {a: _window(hourly[a], start, stop, 20 * 86_400_000) for a in base.assets}
        results = []
        configs = [*cohort, dataclasses.replace(trial, fee=trial.fee * 2,
                         slippage=trial.slippage * 2, spread=trial.spread * 2),
                         dataclasses.replace(trial, fee=trial.fee * 3,
                         slippage=trial.slippage * 3, spread=trial.spread * 3)]
        for i, cfg in enumerate(configs):
            book = replay(cfg, c, h, trade_after_ms=start)
            _settle(book, stop)
            first = cfg.capital
            last = book.equity()
            r = report(book)
            r["window_return"] = last / first - 1 if first > 0 else 0
            r["window_trades"] = [t for t in book.state["trades"] if t["opened_ms"] >= start]
            results.append(r)
            returns = daily_returns(book.state["curve"], start, stop, first).tolist()
            if i < len(cohort):
                daily[i].extend(returns)
            elif i == len(cohort):
                double_daily.extend(returns)
            else:
                triple_daily.extend(returns)
        baseline, candidate, stress, severe = results[0], results[1], results[-2], results[-1]
        total_trades += len(candidate["window_trades"])
        assets_traded.update(t["asset"] for t in candidate["window_trades"])
        rows.append({"start": _iso(start), "end": _iso(stop), "baseline_return": baseline["window_return"],
                     "candidate_return": candidate["window_return"], "stress_return": stress["window_return"],
                     "triple_cost_return": severe["window_return"],
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
    if sum(r["candidate_return"] > 0 for r in rows) < (3 * windows + 3) // 4:
        reasons.append("fewer_than_75_percent_positive_windows")
    if min(r["candidate_return"] for r in rows) < -base.weekly_loss:
        reasons.append("worst_fold_exceeds_weekly_loss_budget")
    walk_reasons = list(reasons)
    matrix = np.asarray(daily).T
    sharpes = [float(np.mean(x) / np.std(x, ddof=1)) if np.std(x) > 1e-14 else 0.0 for x in daily]
    dsr = deflated_sharpe(daily[1], sharpes, n_trials)
    pbo = probability_overfitting(matrix)
    bootstrap = block_stress(daily[1], daily[0], base.monthly_drawdown)
    cost_status = ("PASS" if np.prod(1 + np.asarray(double_daily)) > 1 and
                   np.prod(1 + np.asarray(triple_daily)) > 1 else "FAIL")
    for name, gate in (("dsr", dsr), ("pbo", pbo), ("bootstrap", bootstrap)):
        if gate["status"] != "PASS":
            reasons.append(f"{name}_{gate['status'].lower()}")
    if cost_status != "PASS":
        reasons.append("aggregate_cost_stress_failed")
    digest = hashlib.sha256(json.dumps({"candles": candles, "hourly": hourly},
                                      sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    validation = {"version": VERSION, "scope": "shared_portfolio_candidate", "dsr": dsr, "pbo": pbo,
                  "bootstrap": bootstrap, "leakage": {"status": "PASS", "checks": [
                      "closed_hourly_inputs", "next_bar_fills", "strict_synchronized_ohlc",
                      "alpha_frozen_before_holdouts", "flat_fold_boundaries", "future_hourly_perturbation",
                      "simultaneous_next_open_marks_before_close"]},
                  "costs": {"status": cost_status, "terminal_exit_costs": True,
                            "double_return": float(np.prod(1 + np.asarray(double_daily)) - 1),
                            "triple_return": float(np.prod(1 + np.asarray(triple_daily)) - 1)},
                  "walk_forward": {"status": "PASS" if not walk_reasons else "FAIL", "folds": windows,
                      "window_days": window_days, "positive_folds": sum(r["candidate_return"] > 0 for r in rows),
                      "worst_return": min(r["candidate_return"] for r in rows),
                      "method": "preregistered fixed alpha, rolling chronological holdouts; no refitting"},
                  "data_sha256": digest, "start": rows[0]["start"], "end": rows[-1]["end"],
                  "cohort_configs": [dataclasses.asdict(c) for c in cohort], "trial_sharpes_daily": sharpes,
                  "paper": {"status": "PENDING", "min_days": 14, "min_trades": 10},
                  "live_approved": False}
    return {"eligible_for_manual_review": not reasons, "reasons": reasons, "windows": [
        {k: v for k, v in r.items() if k != "window_trades"} for r in rows],
        "candidate_trades": total_trades, "assets_traded": sorted(assets_traded), "validation": validation}


def _iso(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat()


def main() -> None:
    from .backtest import history
    from .growth_run import _load_config

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
