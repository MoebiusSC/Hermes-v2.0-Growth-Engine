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

LIVE_PAPER_DAYS = 90
LIVE_MIN_TRADES = 200
LIVE_MIN_TRADES_PER_ASSET = 20
LIVE_MIN_PROFIT_FACTOR = 1.25
LIVE_MAX_DRAWDOWN = 0.08
LIVE_MIN_BROKER_FILLS = 30


def _readiness_gate(key: str, label: str, status: str, value, target: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "value": value, "target": target, "detail": detail}


def _active_validation(meta: dict) -> dict:
    active = meta.get("active_change") or {}
    return active.get("validation") or meta.get("active_validation") or {}


def live_readiness(state: dict, metrics: dict, assets: tuple[str, ...],
                   broker_state: dict | None = None) -> dict:
    """Deterministic, display-only gates for considering a future micro-live pilot.

    This never enables live trading. Missing operational telemetry blocks readiness rather
    than being treated as success.
    """
    evidence = _active_validation(state.get("optimizer", {}))
    evidence_gates = []
    labels = {
        "leakage": "Sin leakage temporal",
        "walk_forward": "Walk-forward",
        "dsr": "DSR ≥ 95%",
        "pbo": "PBO ≤ 20%",
        "costs": "Costos estresados",
        "bootstrap": "Bootstrap por bloques",
    }
    for key in ("leakage", "walk_forward", "dsr", "pbo", "costs", "bootstrap"):
        raw = evidence.get(key, {})
        raw_status = raw.get("status")
        status = "PASS" if raw_status == "PASS" else "FAIL" if raw_status == "FAIL" else "PENDING"
        value = raw_status or "Sin evidencia activa"
        if key in ("dsr", "pbo") and raw.get("probability") is not None:
            value = round(float(raw["probability"]), 6)
        evidence_gates.append(_readiness_gate(key, labels[key], status, value,
            "PASS" if key not in ("dsr", "pbo") else ("≥ 0.95" if key == "dsr" else "≤ 0.20")))

    curve = state.get("curve") or []
    paper_days = 0.0
    if len(curve) >= 2:
        try:
            start = dt.datetime.fromisoformat(curve[0]["ts"])
            end = dt.datetime.fromisoformat(curve[-1]["ts"])
            paper_days = max(0.0, (end - start).total_seconds() / 86400)
        except (KeyError, TypeError, ValueError):
            paper_days = 0.0
    trades = state.get("trades") or []
    counts = {asset: sum(t.get("asset") == asset for t in trades) for asset in assets}
    min_asset_trades = min(counts.values()) if counts else 0
    wins = sum(float(t.get("pnl", 0)) for t in trades if float(t.get("pnl", 0)) > 0)
    losses = -sum(float(t.get("pnl", 0)) for t in trades if float(t.get("pnl", 0)) < 0)
    pf = (wins / losses if losses else (999.0 if wins else 0.0))
    drawdown = float(metrics.get("max_drawdown") or 0.0)
    paper_gates = [
        _readiness_gate("paper_days", "Paper continuo", "PASS" if paper_days >= LIVE_PAPER_DAYS else "PENDING",
                        round(paper_days, 1), f"≥ {LIVE_PAPER_DAYS} días"),
        _readiness_gate("paper_trades", "Operaciones cerradas", "PASS" if len(trades) >= LIVE_MIN_TRADES else "PENDING",
                        len(trades), f"≥ {LIVE_MIN_TRADES}"),
        _readiness_gate("asset_sample", "Muestra mínima por moneda", "PASS" if min_asset_trades >= LIVE_MIN_TRADES_PER_ASSET else "PENDING",
                        min_asset_trades, f"≥ {LIVE_MIN_TRADES_PER_ASSET} cierres/moneda"),
        _readiness_gate("profit_factor", "Profit factor neto", "PASS" if pf >= LIVE_MIN_PROFIT_FACTOR else "PENDING",
                        round(pf, 3), f"≥ {LIVE_MIN_PROFIT_FACTOR}"),
        _readiness_gate("drawdown", "Drawdown máximo", "PASS" if drawdown <= LIVE_MAX_DRAWDOWN else "FAIL",
                        round(drawdown, 6), f"≤ {LIVE_MAX_DRAWDOWN:.0%}"),
    ]

    market = state.get("market_data") or {}
    market_status = market.get("status")
    operational = [
        _readiness_gate("engine_halt", "Motor sin bloqueo", "PASS" if not state.get("halted") else "FAIL",
                        state.get("halted") or "Activo", "Sin halt"),
        _readiness_gate("market_data", "Datos de mercado sincronizados",
                        "PASS" if market_status == "ready" else "FAIL" if market_status == "blocked" else "PENDING",
                        market_status or "Sin diagnóstico", "ready"),
    ]
    if broker_state is None:
        operational.append(_readiness_gate("broker_ledger", "Ledger Alpaca paper", "PENDING",
                                           "Sin estado", "Conciliado y sin órdenes pendientes"))
        operational.append(_readiness_gate("broker_telemetry", "Telemetría de ejecución", "UNMEASURED",
                                           "Sin telemetría", f"≥ {LIVE_MIN_BROKER_FILLS} fills; 0 duplicados/fallos"))
    else:
        cursor = broker_state.get("cursor")
        caught_up = cursor is not None and cursor == len(state.get("events") or [])
        broker_problem = broker_state.get("halted_auto")
        broker_pending = broker_state.get("pending")
        ledger_status = "FAIL" if broker_problem else "PENDING" if broker_pending or not caught_up else "PASS"
        ledger_value = broker_problem or ("Orden pendiente" if broker_pending else
                       "Conciliado" if caught_up else "Cursor pendiente")
        operational.append(_readiness_gate("broker_ledger", "Ledger Alpaca paper", ledger_status,
                                           ledger_value, "Conciliado y sin órdenes pendientes"))
        telemetry = broker_state.get("auto_telemetry")
        if not telemetry or not telemetry.get("since"):
            operational.append(_readiness_gate("broker_telemetry", "Telemetría de ejecución", "UNMEASURED",
                                               "Comienza con este despliegue",
                                               f"≥ {LIVE_MIN_BROKER_FILLS} fills; 0 duplicados/fallos"))
        else:
            fills = int(telemetry.get("filled_orders", 0))
            failures = int(telemetry.get("reconciliation_failures", 0))
            duplicates = int(telemetry.get("duplicate_client_ids", 0))
            status = "FAIL" if failures or duplicates else "PASS" if fills >= LIVE_MIN_BROKER_FILLS else "PENDING"
            operational.append(_readiness_gate(
                "broker_telemetry", "Telemetría de ejecución", status,
                f"{fills} fills · {duplicates} duplicados · {failures} fallos",
                f"≥ {LIVE_MIN_BROKER_FILLS} fills; 0 duplicados/fallos"))

    sections = [
        {"key": "evidence", "label": "Evidencia cuantitativa", "gates": evidence_gates},
        {"key": "paper", "label": "Desempeño paper", "gates": paper_gates},
        {"key": "operations", "label": "Operación e infraestructura", "gates": operational},
    ]
    blockers = [g for section in sections for g in section["gates"] if g["status"] != "PASS"]
    return {
        "status": "MICRO_LIVE_READY" if not blockers else "PAPER_ONLY",
        "ready": not blockers,
        "blocking_count": len(blockers),
        "sections": sections,
        "thresholds": {
            "paper_days": LIVE_PAPER_DAYS, "trades": LIVE_MIN_TRADES,
            "trades_per_asset": LIVE_MIN_TRADES_PER_ASSET,
            "profit_factor": LIVE_MIN_PROFIT_FACTOR, "max_drawdown": LIVE_MAX_DRAWDOWN,
            "broker_fills": LIVE_MIN_BROKER_FILLS,
        },
        "note": "Solo diagnóstico. No habilita órdenes reales ni modifica el riesgo.",
    }


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
