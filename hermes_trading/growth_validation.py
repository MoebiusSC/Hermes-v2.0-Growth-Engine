"""Deterministic research gates. Daily simple returns; no LLM or live-order path.

DSR: Bailey & Lopez de Prado (2014), doi:10.3905/jpm.2014.40.5.094.
PBO/CSCV: Bailey et al., https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf.
Effective sample size and block bootstrap are conservative diagnostics, not
proof of independence, protection against regime changes, or live approval.
"""
from __future__ import annotations

import datetime as dt
import itertools
import math
from statistics import NormalDist

import numpy as np

VERSION = "quant-validation-v1"
DAY_MS = 86_400_000
MIN_DAYS = 90
DSR_THRESHOLD = 0.95
PBO_THRESHOLD = 0.20


def daily_returns(curve: list[dict], start: int, end: int, capital: float) -> np.ndarray:
    """Use full UTC days only, with initial equity and end-of-window exit costs.

Windows must have midnight boundaries and every 15m close. Never silently
compress missing observations or annualise irregular ticks as daily returns.
"""
    if start % DAY_MS or end % DAY_MS or end <= start or capital <= 0:
        raise ValueError("full UTC calendar days and positive capital required")
    points = {int(round(dt.datetime.fromisoformat(p["ts"]).timestamp() * 1000)): float(p["equity"])
              for p in curve}
    values = [capital]
    for boundary in range(start + DAY_MS, end + 1, DAY_MS):
        if boundary not in points:
            raise ValueError("missing_daily_equity")
        values.append(points[boundary])
    v = np.asarray(values)
    if not np.all(np.isfinite(v)) or np.any(v <= 0):
        raise ValueError("invalid_equity")
    return v[1:] / v[:-1] - 1


def _sr(r: np.ndarray) -> float:
    std = float(np.std(r, ddof=1))
    return float(np.mean(r) / std) if std > 1e-14 else 0.0


def effective_observations(r: np.ndarray) -> float:
    """Bartlett-weighted autocovariance inflation; never increase sample size."""
    x = r - r.mean()
    var = float(x @ x / len(x))
    if var <= 1e-28:
        return 0.0
    lag = min(10, len(x) // 5)
    inflation = 1 + 2 * sum((1 - k / (lag + 1)) * float(x[k:] @ x[:-k]) / len(x) / var
                            for k in range(1, lag + 1))
    return float(len(x) / max(1.0, inflation))


def deflated_sharpe(returns, trial_sharpes, n_trials: int) -> dict:
    """Sharpe and trial dispersion must both be UNannualised daily units.

Use all reserved trials, not just winners. Dispersion comes from the same
period/cohort; a null standard-error floor prevents near-identical trials from
making a large search look free. Positive serial correlation reduces n_eff.
"""
    r, trials = np.asarray(returns, dtype=float), np.asarray(trial_sharpes, dtype=float)
    unavailable = {"status": "INSUFFICIENT", "probability": None, "n_trials": n_trials}
    if (r.ndim != 1 or len(r) < MIN_DAYS or trials.ndim != 1 or len(trials) < 2
            or n_trials < len(trials) or not np.all(np.isfinite(r))
            or not np.all(np.isfinite(trials)) or np.std(r) <= 1e-14):
        return {**unavailable, "reason": "insufficient_nonconstant_daily_returns_or_trials"}
    n_eff = effective_observations(r)
    if n_eff < 45:
        return {**unavailable, "reason": "insufficient_effective_observations", "n_eff": n_eff}
    sr = _sr(r)
    z = (r - r.mean()) / r.std(ddof=0)
    skew, kurt = float(np.mean(z ** 3)), float(np.mean(z ** 4))
    dispersion = max(float(trials.std(ddof=1)), 1 / math.sqrt(n_eff - 1))
    normal, euler = NormalDist(), 0.5772156649015329
    expected = dispersion * ((1 - euler) * normal.inv_cdf(1 - 1 / n_trials)
                             + euler * normal.inv_cdf(1 - 1 / (n_trials * math.e)))
    variance = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if variance <= 0 or not math.isfinite(variance):
        return {**unavailable, "reason": "invalid_sharpe_variance"}
    probability = normal.cdf((sr - expected) * math.sqrt(n_eff - 1) / math.sqrt(variance))
    return {"status": "PASS" if probability >= DSR_THRESHOLD else "FAIL",
            "probability": probability, "threshold": DSR_THRESHOLD,
            "sharpe_daily": sr, "sharpe_annual": sr * math.sqrt(365.25),
            "expected_max_daily": expected, "trial_dispersion_daily": dispersion,
            "n_trials": n_trials, "cohort_size": len(trials), "n_obs": len(r),
            "n_eff": n_eff, "skew": skew, "kurtosis": kurt,
            "method": "DSR with null dispersion floor and HAC sample adjustment"}


def probability_overfitting(matrix, blocks: int = 8) -> dict:
    """CSCV over one aligned daily-return matrix; never pair unrelated runs.

This diagnoses selection within the preregistered family only. It is not a
replacement for chronological holdouts or prospective paper observations.
Tied in-sample winners share weight; at/below-median OOS ranks count as failures.
"""
    m = np.asarray(matrix, dtype=float)
    if (blocks != 8 or m.ndim != 2 or m.shape[0] < MIN_DAYS or m.shape[1] < 3
            or not np.all(np.isfinite(m))):
        return {"status": "INSUFFICIENT", "probability": None, "reason": "aligned_cohort_required"}
    chunks = np.array_split(np.arange(len(m)), blocks)
    failures = []
    for chosen in itertools.combinations(range(blocks), blocks // 2):
        inside = np.concatenate([chunks[i] for i in chosen])
        outside = np.concatenate([chunks[i] for i in range(blocks) if i not in chosen])
        train = np.array([_sr(m[inside, j]) for j in range(m.shape[1])])
        test = np.array([_sr(m[outside, j]) for j in range(m.shape[1])])
        winners = np.flatnonzero(np.isclose(train, train.max(), atol=1e-12, rtol=0))
        bad = []
        for winner in winners:
            rank = (np.sum(test < test[winner] - 1e-12)
                    + (np.sum(np.abs(test - test[winner]) <= 1e-12) + 1) / 2)
            bad.append(rank / (m.shape[1] + 1) <= 0.5)
        failures.append(float(np.mean(bad)))
    pbo = float(np.mean(failures))
    return {"status": "PASS" if pbo <= PBO_THRESHOLD else "FAIL", "probability": pbo,
            "threshold": PBO_THRESHOLD, "splits": len(failures), "blocks": blocks,
            "cohort_size": m.shape[1], "n_obs": len(m)}


def block_stress(candidate, baseline, max_drawdown: float, samples: int = 500) -> dict:
    """Paired circular 7-day blocks preserve local dependence; fixed seed, no fitting."""
    r, b = np.asarray(candidate), np.asarray(baseline)
    if len(r) < MIN_DAYS or len(r) != len(b) or not np.all(np.isfinite(r)) or np.any(r <= -1):
        return {"status": "INSUFFICIENT", "reason": "aligned_daily_returns_required"}
    rng = np.random.default_rng(20261001)
    starts = rng.integers(0, len(r), size=(samples, math.ceil(len(r) / 7)))
    indices = ((starts[..., None] + np.arange(7)) % len(r)).reshape(samples, -1)[:, :len(r)]
    paths = np.cumprod(1 + r[indices], axis=1)
    paths = np.column_stack([np.ones(samples), paths])
    dd = np.max(1 - paths / np.maximum.accumulate(paths, axis=1), axis=1)
    improvement = (r - b)[indices].mean(axis=1)
    lower, upper_dd = float(np.quantile(improvement, .05)), float(np.quantile(dd, .95))
    return {"status": "PASS" if lower > 0 and upper_dd <= max_drawdown else "FAIL",
            "mean_daily_advantage_p05": lower, "max_drawdown_p95": upper_dd,
            "drawdown_limit": max_drawdown, "samples": samples, "block_days": 7,
            "seed": 20261001}


def dashboard_rows(meta: dict) -> list[dict]:
    """Never relabel an existing paper strategy as statistically/live approved."""
    active = meta.get("active_change") or {}
    evidence = active.get("validation") or meta.get("active_validation") or {}
    rows = [{"strategy": "Hermes Core · configuración activa",
             "state": "PAPER_OBSERVATION" if evidence else "PAPER_LEGACY",
             "validation": evidence},
            {"strategy": "SUI EMA 26/55", "state": "PAPER_LEGACY", "validation": {},
             "reason": "Sin validación individual nueva; incluida en la cartera compartida"}]
    last = meta.get("last_assessment")
    if last:
        rows.append({"strategy": "Último candidato de cartera", "state": last.get("state", "CANDIDATE"),
                     "validation": last.get("validation", {}), "reason": last.get("reason"),
                     "evaluated_ms": last.get("ts")})
    return rows
