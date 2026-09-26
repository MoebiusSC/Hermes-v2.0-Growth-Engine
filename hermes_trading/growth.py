"""Hermes v2 research and paper portfolio. Long spot only, one shared USD account.

Bar-close signals become orders at the next bar open. Stops are checked before targets.
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


@dataclasses.dataclass(frozen=True)
class GrowthConfig:
    capital: float = 50.0
    assets: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    risk_per_trade: float = 0.005
    max_exposure: float = 0.50
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
        for name in ("risk_per_trade", "max_exposure", "daily_loss", "weekly_loss", "monthly_drawdown"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between zero and one")
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
            "stop_fraction": stop_distance / float(close[-1]), "decision_ms": int(bars["t"][-1]) + BAR_MS}


class Portfolio:
    """One shared cash balance. State can be serialized and recovered without resetting risk."""

    def __init__(self, cfg: GrowthConfig, state: dict | None = None):
        self.cfg = cfg
        self.state = state if state is not None else {
            "cash": cfg.capital, "position": None, "pending": None, "marks": {},
            "anchors": {}, "high_water": cfg.capital, "halted": None,
            "last_bar": {}, "trades": [], "curve": [], "events": [],
        }

    def equity(self, prices: Mapping[str, float] | None = None) -> float:
        s = self.state
        pos = s["position"]
        marks = prices or s["marks"]
        mark = marks.get(pos["asset"], pos["entry"]) if pos else 0.0
        return float(s["cash"] + (pos["qty"] * mark if pos else 0.0))

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

    def _close(self, ts: int, price: float, reason: str) -> None:
        s, cfg = self.state, self.cfg
        pos = s["position"]
        qty = pos["qty"]
        proceeds = qty * price * (1 - cfg.fee)
        s["cash"] += proceeds
        pnl = proceeds - pos["cost"]
        s["trades"].append({"asset": pos["asset"], "opened_ms": pos["opened_ms"], "closed_ms": ts,
                            "entry": pos["entry"], "exit": price, "qty": qty, "pnl": pnl,
                            "pnl_pct": pnl / pos["equity_at_entry"], "reason": reason, "regime": pos["regime"]})
        s["position"] = None
        s["events"].append({"ts": ts, "event": "exit", "asset": pos["asset"], "reason": reason})

    def _open(self, ts: int, bar: dict, signal: dict) -> None:
        s, cfg = self.state, self.cfg
        if self._limits(ts) or s["position"]:
            return
        entry = float(bar["open"]) * (1 + cfg.slippage + cfg.spread / 2)
        if entry <= 0 or not math.isfinite(entry):
            return
        dist = signal["stop_fraction"] * entry
        risk_per_unit = dist + entry * (2 * cfg.fee + 2 * cfg.slippage + cfg.spread)
        equity = self.equity()
        notional = min(equity * cfg.risk_per_trade / risk_per_unit * entry,
                       equity * cfg.max_exposure, s["cash"] / (1 + cfg.fee))
        if notional < cfg.min_order_usd:
            s["events"].append({"ts": ts, "event": "skip", "reason": "minimum_order_or_risk"})
            return
        qty = notional / entry
        cost = notional * (1 + cfg.fee)
        s["cash"] -= cost
        s["position"] = {"asset": signal["asset"], "entry": entry, "qty": qty, "cost": cost,
                         "stop": entry - dist, "target": entry + cfg.target_r * dist,
                         "opened_ms": ts, "equity_at_entry": equity, "regime": signal["regime"]}
        s["events"].append({"ts": ts, "event": "entry", "asset": signal["asset"], "notional": notional})

    def on_bar(self, asset: str, bar: dict, ts: int) -> None:
        """Process exactly one completed 15m bar; pending decision must be from an earlier close."""
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
            s["pending"] = None
            s["halted"] = "market_data_gap"
        s["last_bar"][asset] = ts
        pending = s["pending"]
        if pending and pending["asset"] == asset and pending["decision_ms"] <= ts:
            s["pending"] = None
            if not s["halted"]:
                self._open(ts, bar, pending)
        pos = s["position"]
        if pos and pos["asset"] == asset:
            if bar["low"] <= pos["stop"]:
                self._close(ts + BAR_MS, min(float(bar["open"]), pos["stop"]) *
                            (1 - self.cfg.slippage - self.cfg.spread / 2), "stop")
            elif bar["high"] >= pos["target"]:
                self._close(ts + BAR_MS, max(float(bar["open"]), pos["target"]) *
                            (1 - self.cfg.slippage - self.cfg.spread / 2), "target")
        s["marks"][asset] = float(bar["close"])
        self._limits(ts + BAR_MS)

    def decide(self, signals: list[dict], ts: int) -> None:
        """Rank all assets at one synchronized close; only the strongest may enter next bar."""
        s = self.state
        if not s["position"] and not s["pending"] and not self._limits(ts):
            eligible = [x for x in signals if x and x["decision_ms"] == ts]
            if eligible:
                s["pending"] = max(eligible, key=lambda x: (x["strength"], x["asset"]))
        s["curve"].append({"ts": _day(ts).isoformat(), "equity": self.equity()})


def replay(cfg: GrowthConfig, candles: Mapping[str, dict], hourly: Mapping[str, dict],
           trade_after_ms: int = 0) -> Portfolio:
    """Multi-asset portfolio replay with next-open fills, a shared balance and synchronized ranking."""
    book = Portfolio(cfg)
    by_time = {int(t) for asset in cfg.assets for t in candles[asset]["t"]}
    indices = {asset: {int(t): i for i, t in enumerate(candles[asset]["t"])} for asset in cfg.assets}
    for ts in sorted(by_time):
        signals = []
        for asset in cfg.assets:
            i = indices[asset].get(ts)
            if i is None:
                continue
            data = candles[asset]
            bar = {k: data[k][i] for k in ("open", "high", "low", "close")}
            book.on_bar(asset, bar, ts)
            # Freeze all inputs at this close. Last 15m bar is t=ts, 1h bars are filtered by close.
            sub = {k: data[k][max(0, i - 160):i + 1] for k in ("t", "open", "high", "low", "close")}
            signal = candidate(asset, sub, hourly[asset], cfg)
            if signal:
                signals.append(signal)
        book.decide(signals if ts + BAR_MS >= trade_after_ms else [], ts + BAR_MS)
    return book
