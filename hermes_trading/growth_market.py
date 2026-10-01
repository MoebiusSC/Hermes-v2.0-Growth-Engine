"""Closed-candle synchronization and bounded recovery for the paper worker."""
from __future__ import annotations

import math

from .growth import BAR_MS
from .strategy import closed

DATA_HALTS = frozenset(("market_data_gap", "stale_or_unsynchronized_market_data",
                        "missing_current_quote", "consecutive_data_errors"))
PUBLICATION_GRACE_MS = 120_000


def pause(book, reason: str) -> None:
    # A feed failure must never replace a latched risk/broker/manual restriction.
    if book.state["halted"] is None or book.state["halted"] in DATA_HALTS:
        book.state["halted"] = reason
    book.state["pending"] = {}


def required_start(book, asset: str, latest: int) -> int:
    starts = [latest - 11 * BAR_MS]
    last = book.state["last_bar"].get(asset)
    if last is not None:
        starts.append(min(last + BAR_MS, latest))
    pos = book.state["positions"].get(asset)
    if pos:
        starts.append(int(pos["opened_ms"]) // BAR_MS * BAR_MS)
    return min(starts)


def missing_bars(bars: dict, start: int, latest: int) -> list[int]:
    present = set(bars["t"])
    return [ts for ts in range(start, latest + 1, BAR_MS) if ts not in present]


def validate_prices(bars: dict) -> bool:
    times = bars["t"]
    if any(b <= a for a, b in zip(times, times[1:])):
        return False
    if any(len(bars.get(k, [])) != len(times) for k in ("open", "high", "low", "close")):
        return False
    for i, ts in enumerate(times):
        o, h, low, c = (float(bars[k][i]) for k in ("open", "high", "low", "close"))
        if (ts % BAR_MS or not all(math.isfinite(x) and x > 0 for x in (o, h, low, c))
                or not low <= min(o, c) <= max(o, c) <= h):
            return False
    return True


def current_quote(raw: dict, now_ms: int) -> float | None:
    if not raw.get("t") or raw["t"][-1] != now_ms // BAR_MS * BAR_MS:
        return None
    value = float(raw["close"][-1])
    return value if math.isfinite(value) and value > 0 else None


async def prepare_feeds(book, feeds: dict, now_ms: int) -> bool:
    """Stage every asset before advancing any cursor or allowing a decision."""
    from .adapters import price

    expected = now_ms // BAR_MS * BAR_MS - BAR_MS
    problems = []
    for asset in book.cfg.assets:
        bars, hours, raw = feeds[asset]
        start = required_start(book, asset, expected)
        missing = missing_bars(bars, start, expected)
        # A just-closed final candle may simply not have been published yet.
        delayed = missing == [expected] and now_ms - (expected + BAR_MS) <= PUBLICATION_GRACE_MS
        if missing and not delayed:
            try:
                raw = await price.backfill(asset, raw, start, expected)
                bars = closed(raw, "15m", now_ms)
                feeds[asset] = (bars, hours, raw)
                missing = missing_bars(bars, start, expected)
            except Exception as exc:
                problems.append({"asset": asset, "source": raw.get("source"),
                                 "reason": "backfill_failed", "error": type(exc).__name__})
        if missing:
            problems.append({"asset": asset, "source": raw.get("source"),
                             "reason": "publication_delay" if delayed else "missing_candles",
                             "missing_count": len(missing), "first_missing_bar": missing[0]})
        if not validate_prices(bars) or not validate_prices(hours):
            problems.append({"asset": asset, "source": raw.get("source"), "reason": "invalid_ohlc"})
        if (not hours["t"] or now_ms - hours["t"][-1] > 2 * 60 * 60_000
                or any(b - a != 60 * 60_000 for a, b in zip(hours["t"][-12:], hours["t"][-11:]))):
            problems.append({"asset": asset, "source": raw.get("source"), "reason": "hourly_data_gap"})
        if current_quote(raw, now_ms) is None:
            problems.append({"asset": asset, "source": raw.get("source"), "reason": "missing_current_quote"})
    waiting = bool(problems) and all(p["reason"] in ("publication_delay", "missing_current_quote")
                                    for p in problems) and now_ms % BAR_MS <= PUBLICATION_GRACE_MS
    health = {"status": "waiting" if waiting else "blocked" if problems else "ready",
              "checked_ms": now_ms, "expected_closed_bar": expected, "problems": problems}
    previous = book.state.get("market_data", {})
    book.state["market_data"] = health
    if problems:
        book.state["pending"] = {}
        if not waiting:
            pause(book, "market_data_gap")
        if (previous.get("status"), previous.get("problems")) != (health["status"], problems):
            book.state["events"].append({"ts": now_ms, "event": "market_data_wait" if waiting
                                         else "market_data_gap", **health})
        return False
    return True


def audit_open_positions(book, feeds: dict, now_ms: int) -> bool:
    """Recheck missed exits; execute at today's quote, never a fictional past fill."""
    for asset, pos in list(book.state["positions"].items()):
        bars, _, raw = feeds[asset]
        start = int(pos["opened_ms"]) // BAR_MS * BAR_MS
        if missing_bars(bars, start, bars["t"][-1]):
            return False
        quote = current_quote(raw, now_ms)
        if quote is None:
            return False
        reason = None
        for i, ts in enumerate(bars["t"]):
            if ts < start:
                continue
            if bars["low"][i] <= pos["stop"]:
                reason = "market_data_recovery_stop"
            elif bars["high"][i] >= pos["target"]:
                reason = "market_data_recovery_target"
            if reason:
                break
        if reason:
            book._close(asset, now_ms, quote * (1 - book.cfg.slippage - book.cfg.spread / 2), reason)
        book.state["marks"][asset] = quote
    book._limits(now_ms)
    return True
