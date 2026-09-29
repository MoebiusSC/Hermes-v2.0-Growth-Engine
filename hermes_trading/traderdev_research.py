"""Independent reconstruction experiment for public Trader.dev leaderboard strategies.

Research-only. It never reads/writes Hermes live state and never places orders.

Hypothesis under test:
  EMA fast/slow crossover + fixed percentage stop + R-multiple target,
  with next-bar execution, fees and slippage.

The public Trader.dev AVAX report exposes an optimisation sweep fast=12, slow=70,
so this family is the first falsifiable reconstruction candidate for BTC/SUI/LTC.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from dataclasses import dataclass

import ccxt
import numpy as np


TARGETS = {
    "BTC/USDT": {"timeframe": "2h", "return_pct": 19788.17, "max_dd_pct": 2.24, "win_rate": 0.666, "profit_factor": 14.22, "trades": 1790},
    "SUI/USDT": {"timeframe": "1h", "return_pct": 19744.32, "max_dd_pct": 7.22, "win_rate": 0.622, "profit_factor": 3.12, "trades": 1691},
    "LTC/USDT": {"timeframe": "1h", "return_pct": 19972.77, "max_dd_pct": 21.93, "win_rate": 0.601, "profit_factor": 2.77, "trades": 2145},
}

EXCHANGES = ("binance", "okx")
FEE = 0.001
SLIPPAGE = 0.0002


@dataclass(frozen=True)
class Params:
    fast: int
    slow: int
    stop_pct: float
    target_r: float


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    v = float(np.mean(x[:n]))
    out[n - 1] = v
    for i in range(n, len(x)):
        v = v + k * (x[i] - v)
        out[i] = v
    return out


def fetch(asset: str, tf: str, days: int) -> dict:
    since = int((time.time() - days * 86400) * 1000)
    errors = []
    for exid in EXCHANGES:
        try:
            ex = getattr(ccxt, exid)({"enableRateLimit": True})
            rows, cursor = [], since
            step = {"1h": 3600000, "2h": 7200000}[tf]
            while cursor < int(time.time() * 1000):
                page = ex.fetch_ohlcv(asset, timeframe=tf, since=cursor, limit=1000)
                if not page:
                    break
                page = [r for r in page if r[0] >= cursor]
                if not page:
                    break
                rows.extend(page)
                cursor = int(page[-1][0]) + step
                if len(page) < 100:
                    break
            if len(rows) >= 500:
                # Deduplicate timestamps.
                unique = {int(r[0]): r for r in rows}
                rows = [unique[k] for k in sorted(unique)]
                return {
                    "source": exid,
                    "t": np.asarray([r[0] for r in rows], dtype=np.int64),
                    "open": np.asarray([r[1] for r in rows], dtype=float),
                    "high": np.asarray([r[2] for r in rows], dtype=float),
                    "low": np.asarray([r[3] for r in rows], dtype=float),
                    "close": np.asarray([r[4] for r in rows], dtype=float),
                }
        except Exception as e:
            errors.append(f"{exid}:{type(e).__name__}:{e}")
    raise RuntimeError("; ".join(errors))


def simulate(data: dict, p: Params, start: int, end: int, initial: float = 10000.0) -> dict:
    o, h, lo, c = (data[k] for k in ("open", "high", "low", "close"))
    f, s = ema(c, p.fast), ema(c, p.slow)
    equity = initial
    peak = initial
    max_dd = 0.0
    pos = None
    pending = None
    trades = []
    curve = []

    def close_pos(price: float, reason: str):
        nonlocal equity, pos, peak, max_dd
        sign = pos["sign"]
        gross = sign * pos["qty"] * (price - pos["entry"])
        exit_fee = pos["qty"] * price * FEE
        pnl = gross - pos["entry_fee"] - exit_fee
        equity += pnl
        trades.append(pnl)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
        pos = None

    warm = max(p.slow + 2, start)
    for i in range(warm, min(end, len(c))):
        if pending is not None:
            action = pending
            pending = None
            fill = o[i]
            if action == "reverse_long":
                if pos:
                    side_slip = 1 + SLIPPAGE if pos["sign"] < 0 else 1 - SLIPPAGE
                    close_pos(fill * side_slip, "reverse")
                entry = fill * (1 + SLIPPAGE)
                qty = max(equity, 0.0) / entry
                dist = entry * p.stop_pct / 100
                pos = {"sign": 1, "entry": entry, "qty": qty, "entry_fee": qty * entry * FEE,
                       "stop": entry - dist, "target": entry + dist * p.target_r}
            elif action == "reverse_short":
                if pos:
                    side_slip = 1 - SLIPPAGE if pos["sign"] > 0 else 1 + SLIPPAGE
                    close_pos(fill * side_slip, "reverse")
                entry = fill * (1 - SLIPPAGE)
                qty = max(equity, 0.0) / entry
                dist = entry * p.stop_pct / 100
                pos = {"sign": -1, "entry": entry, "qty": qty, "entry_fee": qty * entry * FEE,
                       "stop": entry + dist, "target": entry - dist * p.target_r}

        if pos:
            if pos["sign"] > 0:
                if lo[i] <= pos["stop"]:
                    px = min(o[i], pos["stop"]) * (1 - SLIPPAGE)
                    close_pos(px, "stop")
                elif h[i] >= pos["target"]:
                    px = max(o[i], pos["target"]) * (1 - SLIPPAGE)
                    close_pos(px, "target")
            else:
                if h[i] >= pos["stop"]:
                    px = max(o[i], pos["stop"]) * (1 + SLIPPAGE)
                    close_pos(px, "stop")
                elif lo[i] <= pos["target"]:
                    px = min(o[i], pos["target"]) * (1 + SLIPPAGE)
                    close_pos(px, "target")

        mark = equity
        if pos:
            mark += pos["sign"] * pos["qty"] * (c[i] - pos["entry"]) - pos["entry_fee"] - pos["qty"] * c[i] * FEE
        peak = max(peak, mark)
        if peak:
            max_dd = max(max_dd, (peak - mark) / peak)
        curve.append(mark)

        if i + 1 >= end or np.isnan(f[i]) or np.isnan(s[i]) or np.isnan(f[i-1]) or np.isnan(s[i-1]):
            continue
        up = f[i] > s[i] and f[i - 1] <= s[i - 1]
        down = f[i] < s[i] and f[i - 1] >= s[i - 1]
        if up and (not pos or pos["sign"] < 0):
            pending = "reverse_long"
        elif down and (not pos or pos["sign"] > 0):
            pending = "reverse_short"

    if pos:
        px = c[min(end, len(c)) - 1] * (1 - SLIPPAGE if pos["sign"] > 0 else 1 + SLIPPAGE)
        close_pos(px, "end")

    wins = [x for x in trades if x > 0]
    losses = [-x for x in trades if x < 0]
    pf = (sum(wins) / sum(losses)) if losses else (99.0 if wins else 0.0)
    ret = equity / initial - 1
    return {
        "return_pct": ret * 100,
        "max_dd_pct": max_dd * 100,
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": pf,
        "trades": len(trades),
        "final_equity": equity,
    }


def train_score(m: dict) -> float:
    # Prefer expectancy and reasonable drawdown; do not reward explosive return alone.
    if m["trades"] < 25 or m["max_dd_pct"] > 45:
        return -1e9
    pf = min(m["profit_factor"], 5.0)
    return math.log1p(max(m["return_pct"], -99) / 100 + 1.0) + 0.6 * (pf - 1) - 0.025 * m["max_dd_pct"] + 0.15 * math.log1p(m["trades"])


def search(asset: str, target: dict, days: int) -> dict:
    data = fetch(asset, target["timeframe"], days)
    n = len(data["close"])
    split = int(n * 0.70)
    grid = [
        Params(fast, slow, stop, r)
        for fast in (8, 12, 16, 20, 26)
        for slow in (40, 55, 70, 90, 120)
        if fast < slow
        for stop in (0.8, 1.2, 1.8, 2.5, 3.5)
        for r in (1.5, 2.0, 3.0, 4.5)
    ]
    ranked = []
    for p in grid:
        train = simulate(data, p, 0, split)
        ranked.append((train_score(train), p, train))
    ranked.sort(key=lambda x: x[0], reverse=True)

    finalists = []
    for score, p, train in ranked[:12]:
        oos = simulate(data, p, max(0, split - p.slow - 2), n)
        robust = (
            oos["trades"] >= 8
            and oos["profit_factor"] > 1.0
            and oos["return_pct"] > 0
            and oos["max_dd_pct"] <= max(35.0, train["max_dd_pct"] * 1.5)
        )
        finalists.append({"params": p.__dict__, "train": train, "oos": oos, "robust": robust, "train_score": score})

    finalists.sort(key=lambda x: (x["robust"], x["oos"]["profit_factor"], x["oos"]["return_pct"]), reverse=True)
    best = finalists[0]
    whole = simulate(data, Params(**best["params"]), 0, n)
    target_gap = {
        "return_ratio_vs_public": whole["return_pct"] / target["return_pct"] if target["return_pct"] else None,
        "dd_delta_points": whole["max_dd_pct"] - target["max_dd_pct"],
        "win_rate_delta_points": (whole["win_rate"] - target["win_rate"]) * 100,
        "pf_ratio_vs_public": whole["profit_factor"] / target["profit_factor"] if target["profit_factor"] else None,
        "trade_ratio_vs_public": whole["trades"] / target["trades"] if target["trades"] else None,
    }
    return {
        "asset": asset,
        "timeframe": target["timeframe"],
        "source": data["source"],
        "bars": n,
        "from": dt.datetime.fromtimestamp(int(data["t"][0]) / 1000, dt.timezone.utc).isoformat(),
        "to": dt.datetime.fromtimestamp(int(data["t"][-1]) / 1000, dt.timezone.utc).isoformat(),
        "public_target": target,
        "best_replica": best,
        "whole_period": whole,
        "target_gap": target_gap,
        "top_finalists": finalists[:5],
        "verdict": "plausible_family" if best["robust"] else "family_not_reconstructed",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--asset", choices=list(TARGETS) + ["all"], default="all")
    args = ap.parse_args()
    assets = list(TARGETS) if args.asset == "all" else [args.asset]
    out = []
    for asset in assets:
        try:
            out.append(search(asset, TARGETS[asset], args.days))
        except Exception as e:
            out.append({"asset": asset, "error": f"{type(e).__name__}: {e}"})
    print(json.dumps({"method": "ema_crossover_fixed_stop_target", "fee_per_side": FEE, "slippage_per_side": SLIPPAGE, "results": out}, indent=2))


if __name__ == "__main__":
    main()
