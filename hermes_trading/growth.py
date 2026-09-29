"""Hermes v2 research and paper portfolio. Long spot only, one shared USD account.

Bar-close signals become orders at the next bar open. Multiple assets may be open
simultaneously under one global risk and exposure budget. Stops are checked before targets.
The same Portfolio.on_bar method is used by historical replay and the paper worker.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
from typing import Mapping

import numpy as np

from .strategy import atr_series, ema_series, rsi_series

BAR_MS = 15 * 60_000
HOUR_MS = 60 * 60_000

SUI_REPLICA_ASSET = "SUI/USDT"
SUI_REPLICA_FAST_EMA = 26
SUI_REPLICA_SLOW_EMA = 55
SUI_REPLICA_STOP_FRACTION = 0.018
SUI_REPLICA_TARGET_R = 3.0


@dataclasses.dataclass(frozen=True)
class GrowthConfig:
    capital: float = 50.0
    assets: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "LINK/USDT", "SUI/USDT")
    risk_per_trade: float = 0.005
    max_exposure: float = 0.50
    max_positions: int = 3
    max_portfolio_risk: float = 0.015
    max_total_exposure: float = 0.90
    daily_loss: float = 0.015
    weekly_loss: float = 0.03
    monthly_drawdown: float = 0.06
    fee: float = 0.001
    slippage: float = 0.0002
    spread: float = 0.0002
    min_order_usd: float = 5.0
    min_edge_multiple: float = 2.0
    stop_atr: float = 2.0
    target_r: float = 2.5
    high_vol_percentile: float = 80.0
    range_rsi: float = 30.0
    trend_rsi: float = 40.0

    def __post_init__(self):
        if self.capital <= 0 or not self.assets or len(set(self.assets)) != len(self.assets):
            raise ValueError("positive capital and unique assets required")
        if not all("/" in asset for asset in self.assets):
            raise ValueError("v2 accepts spot crypto pairs only")
        for name in ("risk_per_trade", "max_exposure", "max_portfolio_risk", "max_total_exposure",
                     "daily_loss", "weekly_loss", "monthly_drawdown"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between zero and one")
        if not isinstance(self.max_positions, int) or self.max_positions < 1:
            raise ValueError("max_positions must be a positive integer")
        if self.max_portfolio_risk < self.risk_per_trade:
            raise ValueError("max_portfolio_risk cannot be below risk_per_trade")
        if any(getattr(self, x) < 0 for x in ("fee", "slippage", "spread")):
            raise ValueError("negative trading costs")
        if self.min_order_usd <= 0 or self.stop_atr <= 0 or self.target_r <= 0:
            raise ValueError("invalid order or exit parameters")


def _day(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc)


def _closed_hourly(hourly: dict, decision_ms: int) -> np.ndarray:
    t = np.asarray(hourly["t"])
    return np.asarray(hourly["close"], dtype=float)[t + HOUR_MS <= decision_ms]


def candidate(asset: str, bars: dict, hourly: dict, cfg: GrowthConfig) -> dict | None:
    """Only arrays ending on a closed 15m bar may be passed in. No future 1h candle leaks in."""
    close = np.asarray(bars["close"], dtype=float)
    if len(close) < 120 or any(len(bars[k]) != len(close) for k in ("t", "open", "high", "low")):
        return None
    hour = _closed_hourly(hourly, int(bars["t"][-1]) + BAR_MS)
    if len(hour) < 108:
        return None
    rsi = rsi_series(close)
    atr = atr_series(bars["high"], bars["low"], close)
    ema50, ema100 = ema_series(hour, 50), ema_series(hour, 100)
    if not np.all(np.isfinite([rsi[-1], rsi[-2], atr[-1], ema50[-1], ema50[-4], ema100[-1]])):
        return None
    recent_vol = atr[-101:] / close[-101:]
    recent_vol = recent_vol[np.isfinite(recent_vol)]
    if len(recent_vol) < 80 or atr[-1] / close[-1] > np.percentile(recent_vol, cfg.high_vol_percentile):
        return None
    separation = abs(ema50[-1] - ema100[-1]) / hour[-1]
    slope = (ema50[-1] - ema50[-4]) / hour[-1]
    if ema50[-1] > ema100[-1] and slope > 0.001 and separation > 0.002:
        regime = "TREND_UP"
    elif ema50[-1] < ema100[-1] and slope < -0.001 and separation > 0.002:
        return None  # spot long only
    elif separation < 0.008 and abs(slope) < 0.005:
        regime = "RANGE"
    else:
        return None
    if regime == "RANGE":
        valid = rsi[-2] < cfg.range_rsi and rsi[-1] > rsi[-2] and rsi[-1] < 40
        strength = (cfg.range_rsi - rsi[-2]) / 100 + (rsi[-1] - rsi[-2]) / 100
    else:
        valid = rsi[-2] <= cfg.trend_rsi < rsi[-1] and close[-1] > ema50[-1] * 0.98
        strength = (rsi[-1] - rsi[-2]) / 100 + min(slope, 0.02)
    if not valid:
        return None
    stop_distance = cfg.stop_atr * float(atr[-1])
    potential = cfg.target_r * stop_distance / float(close[-1])
    round_trip_cost = 2 * (cfg.fee + cfg.slippage) + cfg.spread
    if potential <= cfg.min_edge_multiple * round_trip_cost:
        return None
    return {"asset": asset, "regime": regime, "strength": float(strength),
            "stop_fraction": stop_distance / float(close[-1]), "decision_ms": int(bars["t"][-1]) + BAR_MS,
            "strategy": "hermes_core", "target_r": cfg.target_r}


def sui_replica_candidate(asset: str, bars: dict, hourly: dict, cfg: GrowthConfig) -> dict | None:
    """Long-only adaptation of the validated SUI EMA(26/55) replica on closed 1h candles."""
    if asset != SUI_REPLICA_ASSET or not bars.get("t"):
        return None
    decision_ms = int(bars["t"][-1]) + BAR_MS
    if decision_ms % HOUR_MS:
        return None
    hour = _closed_hourly(hourly, decision_ms)
    if len(hour) < SUI_REPLICA_SLOW_EMA + 2 or hour[-1] <= 0:
        return None
    fast = ema_series(hour, SUI_REPLICA_FAST_EMA)
    slow = ema_series(hour, SUI_REPLICA_SLOW_EMA)
    if not np.all(np.isfinite([fast[-1], fast[-2], slow[-1], slow[-2]])):
        return None
    if not (fast[-2] <= slow[-2] and fast[-1] > slow[-1]):
        return None
    potential = SUI_REPLICA_TARGET_R * SUI_REPLICA_STOP_FRACTION
    round_trip_cost = 2 * (cfg.fee + cfg.slippage) + cfg.spread
    if potential <= cfg.min_edge_multiple * round_trip_cost:
        return None
    relative_gap = max(0.0, float(fast[-1] - slow[-1]) / float(hour[-1]))
    strength = 0.12 + min(0.08, relative_gap * 10)
    return {"asset": asset, "regime": "SUI_EMA_REPLICA", "strength": strength,
            "stop_fraction": SUI_REPLICA_STOP_FRACTION, "decision_ms": decision_ms,
            "strategy": "sui_ema_26_55", "target_r": SUI_REPLICA_TARGET_R}


def signals_for_asset(asset: str, bars: dict, hourly: dict, cfg: GrowthConfig) -> list[dict]:
    """Core Hermes signal plus opt-in experimental signals for the same synchronized close."""
    out = []
    core = candidate(asset, bars, hourly, cfg)
    if core:
        out.append(core)
    replica = sui_replica_candidate(asset, bars, hourly, cfg)
    if replica:
        out.append(replica)
    return out


class Portfolio:
    """Shared-cash, multi-position spot portfolio with a global open-risk budget."""

    def __init__(self, cfg: GrowthConfig, state: dict | None = None):
        self.cfg = cfg
        self.state = state if state is not None else {
            "cash": cfg.capital, "positions": {}, "pending": {}, "marks": {},
            "anchors": {}, "high_water": cfg.capital, "halted": None,
            "last_bar": {}, "trades": [], "curve": [], "events": [],
        }
        self._normalise_state()

    def _normalise_state(self) -> None:
        """Migrate the legacy single-position state without changing balances or trades."""
        s = self.state
        if "positions" not in s:
            legacy = s.pop("position", None)
            s["positions"] = {legacy["asset"]: legacy} if legacy else {}
        elif not isinstance(s["positions"], dict):
            s["positions"] = {p["asset"]: p for p in s["positions"]}
        pending = s.get("pending")
        if pending is None:
            s["pending"] = {}
        elif isinstance(pending, dict) and "asset" in pending:
            s["pending"] = {pending["asset"]: pending}
        elif not isinstance(pending, dict):
            s["pending"] = {}
        s.setdefault("marks", {})
        s.setdefault("anchors", {})
        s.setdefault("high_water", self.cfg.capital)
        s.setdefault("halted", None)
        s.setdefault("last_bar", {})
        s.setdefault("trades", [])
        s.setdefault("curve", [])
        s.setdefault("events", [])

    def equity(self, prices: Mapping[str, float] | None = None) -> float:
        s = self.state
        marks = prices or s["marks"]
        value = float(s["cash"])
        for asset, pos in s["positions"].items():
            mark = float(marks.get(asset, pos["entry"]))
            value += float(pos["qty"]) * mark
        return value

    def gross_exposure(self, prices: Mapping[str, float] | None = None) -> float:
        s = self.state
        marks = prices or s["marks"]
        return float(sum(float(pos["qty"]) * float(marks.get(asset, pos["entry"]))
                         for asset, pos in s["positions"].items()))

    def open_risk(self) -> float:
        cfg = self.cfg
        total = 0.0
        for pos in self.state["positions"].values():
            if "risk_amount" in pos:
                total += float(pos["risk_amount"])
            else:
                per_unit = max(0.0, float(pos["entry"]) - float(pos["stop"]))
                per_unit += float(pos["entry"]) * (2 * cfg.fee + 2 * cfg.slippage + cfg.spread)
                total += float(pos["qty"]) * per_unit
        return float(total)

    def risk_snapshot(self) -> dict:
        eq = self.equity()
        budget = eq * self.cfg.max_portfolio_risk
        exposure_cap = eq * self.cfg.max_total_exposure
        return {
            "open_risk": self.open_risk(),
            "risk_budget": budget,
            "risk_utilization": self.open_risk() / budget if budget > 0 else 0.0,
            "gross_exposure": self.gross_exposure(),
            "exposure_cap": exposure_cap,
            "exposure_utilization": self.gross_exposure() / exposure_cap if exposure_cap > 0 else 0.0,
            "open_positions": len(self.state["positions"]),
            "max_positions": self.cfg.max_positions,
        }

    def _limits(self, ts: int) -> str | None:
        s, cfg = self.state, self.cfg
        eq = self.equity()
        d = _day(ts)
        keys = {"day": d.strftime("%Y-%m-%d"), "week": f"{d.isocalendar().year}-W{d.isocalendar().week:02d}",
                "month": d.strftime("%Y-%m")}
        for interval, key in keys.items():
            if s["anchors"].get(interval, {}).get("key") != key:
                s["anchors"][interval] = {"key": key, "equity": eq}
        s["high_water"] = max(s["high_water"], eq)
        for interval, limit in (("day", cfg.daily_loss), ("week", cfg.weekly_loss)):
            anchor = s["anchors"][interval]["equity"]
            if eq <= anchor * (1 - limit):
                s["halted"] = f"{interval}_loss_limit"
        if eq <= s["high_water"] * (1 - cfg.monthly_drawdown):
            s["halted"] = "drawdown_limit"
        return s["halted"]

    def _close(self, asset: str, ts: int, price: float, reason: str) -> None:
        s, cfg = self.state, self.cfg
        pos = s["positions"].get(asset)
        if not pos:
            return
        qty = float(pos["qty"])
        proceeds = qty * price * (1 - cfg.fee)
        s["cash"] += proceeds
        pnl = proceeds - float(pos["cost"])
        s["trades"].append({"asset": pos["asset"], "opened_ms": pos["opened_ms"], "closed_ms": ts,
                            "entry": pos["entry"], "exit": price, "qty": qty, "pnl": pnl,
                            "pnl_pct": pnl / pos["equity_at_entry"], "reason": reason, "regime": pos["regime"],
                            "strategy": pos.get("strategy", "hermes_core"),
                            "target_r": pos.get("target_r", cfg.target_r),
                            "risk_amount": pos.get("risk_amount")})
        del s["positions"][asset]
        s["events"].append({"ts": ts, "event": "exit", "asset": pos["asset"], "reason": reason,
                            "strategy": pos.get("strategy", "hermes_core")})

    def _open(self, ts: int, bar: dict, signal: dict) -> None:
        s, cfg = self.state, self.cfg
        asset = signal["asset"]
        if (self._limits(ts) or asset in s["positions"] or
                len(s["positions"]) >= cfg.max_positions):
            return
        entry = float(bar["open"]) * (1 + cfg.slippage + cfg.spread / 2)
        if entry <= 0 or not math.isfinite(entry):
            return
        dist = float(signal["stop_fraction"]) * entry
        risk_per_unit = dist + entry * (2 * cfg.fee + 2 * cfg.slippage + cfg.spread)
        equity = self.equity()
        remaining_risk = max(0.0, equity * cfg.max_portfolio_risk - self.open_risk())
        remaining_exposure = max(0.0, equity * cfg.max_total_exposure - self.gross_exposure())
        target_risk = min(equity * cfg.risk_per_trade, remaining_risk)
        notional = min(target_risk / risk_per_unit * entry if risk_per_unit > 0 else 0.0,
                       equity * cfg.max_exposure, remaining_exposure,
                       s["cash"] / (1 + cfg.fee))
        if notional < cfg.min_order_usd:
            s["events"].append({"ts": ts, "event": "skip", "asset": asset,
                                "reason": "minimum_order_or_global_risk_budget"})
            return
        qty = notional / entry
        risk_amount = qty * risk_per_unit
        cost = notional * (1 + cfg.fee)
        s["cash"] -= cost
        target_r = float(signal.get("target_r", cfg.target_r))
        strategy = str(signal.get("strategy", "hermes_core"))
        s["positions"][asset] = {"asset": asset, "entry": entry, "qty": qty, "cost": cost,
                                 "stop": entry - dist, "target": entry + target_r * dist,
                                 "opened_ms": ts, "equity_at_entry": equity, "regime": signal["regime"],
                                 "strategy": strategy, "target_r": target_r,
                                 "risk_amount": risk_amount}
        s["events"].append({"ts": ts, "event": "entry", "asset": asset, "notional": notional,
                            "risk_amount": risk_amount, "strategy": strategy})

    def on_bar(self, asset: str, bar: dict, ts: int) -> None:
        """Process exactly one completed 15m bar; each asset may fill one queued signal."""
        s = self.state
        if asset not in self.cfg.assets or s["last_bar"].get(asset, -1) >= ts:
            return
        if not all(math.isfinite(float(bar[k])) and float(bar[k]) > 0 for k in ("open", "high", "low", "close")):
            s["halted"] = "invalid_market_data"
            return
        if not (bar["low"] <= min(bar["open"], bar["close"]) <= bar["high"]
                and bar["low"] <= max(bar["open"], bar["close"]) <= bar["high"]):
            s["halted"] = "invalid_ohlc"
            return
        last = s["last_bar"].get(asset)
        if last is not None and ts - last > BAR_MS:
            s["pending"] = {}
            s["halted"] = "market_data_gap"
        s["last_bar"][asset] = ts
        pending = s["pending"].pop(asset, None)
        if pending and pending["decision_ms"] <= ts and not s["halted"]:
            self._open(ts, bar, pending)
        pos = s["positions"].get(asset)
        if pos:
            if bar["low"] <= pos["stop"]:
                self._close(asset, ts + BAR_MS, min(float(bar["open"]), pos["stop"]) *
                            (1 - self.cfg.slippage - self.cfg.spread / 2), "stop")
            elif bar["high"] >= pos["target"]:
                self._close(asset, ts + BAR_MS, max(float(bar["open"]), pos["target"]) *
                            (1 - self.cfg.slippage - self.cfg.spread / 2), "target")
        s["marks"][asset] = float(bar["close"])
        self._limits(ts + BAR_MS)

    def decide(self, signals: list[dict], ts: int) -> None:
        """Rank signals globally and queue as many as the position/risk budget permits."""
        s, cfg = self.state, self.cfg
        if not self._limits(ts):
            eligible = [x for x in signals if x and x["decision_ms"] == ts
                        and x["asset"] not in s["positions"] and x["asset"] not in s["pending"]]
            strongest_by_asset = {}
            for signal in eligible:
                current = strongest_by_asset.get(signal["asset"])
                if current is None or (signal["strength"], signal.get("strategy", "")) > (
                        current["strength"], current.get("strategy", "")):
                    strongest_by_asset[signal["asset"]] = signal
            ranked = sorted(strongest_by_asset.values(),
                            key=lambda x: (x["strength"], x["asset"]), reverse=True)
            position_slots = max(0, cfg.max_positions - len(s["positions"]) - len(s["pending"]))
            eq = self.equity()
            remaining_risk = max(0.0, eq * cfg.max_portfolio_risk - self.open_risk())
            nominal_trade_risk = max(eq * cfg.risk_per_trade, 1e-12)
            risk_slots = int((remaining_risk + 1e-12) // nominal_trade_risk)
            slots = min(position_slots, risk_slots)
            for rank, signal in enumerate(ranked[:slots]):
                s["pending"][signal["asset"]] = {**signal, "rank": rank}
        s["curve"].append({"ts": _day(ts).isoformat(), "equity": self.equity()})

    def pending_assets(self) -> list[str]:
        return [asset for asset, _ in sorted(self.state["pending"].items(),
                                              key=lambda item: (item[1].get("rank", 999), item[0]))]

    def paper_fill_pending(self, asset: str, price: float, ts: int) -> None:
        """Paper only: fill one queued asset from a fresh quote; stale signals are skipped."""
        signal = self.state["pending"].pop(asset, None)
        if signal is None:
            return
        if ts - signal["decision_ms"] > 60_000:
            self.state["events"].append({"ts": ts, "event": "skip", "asset": asset,
                                         "reason": "late_paper_fill"})
            return
        if price <= 0 or not math.isfinite(price):
            self.state["halted"] = "invalid_market_data"
            return
        self._open(ts, {"open": price}, signal)


def replay(cfg: GrowthConfig, candles: Mapping[str, dict], hourly: Mapping[str, dict],
           trade_after_ms: int = 0) -> Portfolio:
    """Multi-asset portfolio replay with next-open fills, a shared balance and synchronized ranking."""
    book = Portfolio(cfg)
    by_time = {int(t) for asset in cfg.assets for t in candles[asset]["t"]}
    indices = {asset: {int(t): i for i, t in enumerate(candles[asset]["t"])} for asset in cfg.assets}
    for ts in sorted(by_time):
        signals = []
        pending_order = book.pending_assets()
        ordered_assets = pending_order + [a for a in cfg.assets if a not in pending_order]
        for asset in ordered_assets:
            i = indices[asset].get(ts)
            if i is None:
                continue
            data = candles[asset]
            bar = {k: data[k][i] for k in ("open", "high", "low", "close")}
            book.on_bar(asset, bar, ts)
            # Freeze all inputs at this close. Last 15m bar is t=ts, 1h bars are filtered by close.
            sub = {k: data[k][max(0, i - 160):i + 1] for k in ("t", "open", "high", "low", "close")}
            signals.extend(signals_for_asset(asset, sub, hourly[asset], cfg))
        book.decide(signals if ts + BAR_MS >= trade_after_ms else [], ts + BAR_MS)
    return book
