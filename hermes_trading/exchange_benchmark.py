"""Passive public-orderbook benchmark for future live venue selection.

No authenticated endpoints, no order submission, and no influence on the paper strategy.
It samples public spot books and estimates conservative taker->taker round-trip cost
for representative notionals.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import time
from pathlib import Path

import httpx

from .storage import atomic_write

SAMPLE_INTERVAL_MS = 15 * 60_000
NOTIONALS = (25.0, 100.0, 500.0)
MAX_CLIENT_TIMEOUT = 5.0

DEFAULT_FEES = {
    "Binance": {
        "maker_bps": 10.0, "taker_bps": 10.0,
        "note": "Usuario regular; descuento BNB no aplicado en el ranking conservador.",
        "alternative": {"label": "BNB -25%", "maker_bps": 7.5, "taker_bps": 7.5},
    },
    "OKX": {
        "maker_bps": 8.0, "taker_bps": 10.0,
        "note": "Supuesto usuario estándar spot global; verificar tasa de la cuenta antes de live.",
    },
    "Bybit": {
        "maker_bps": 10.0, "taker_bps": 10.0,
        "note": "Usuario no VIP spot.",
    },
    "Kraken": {
        "maker_bps": 40.0, "taker_bps": 80.0,
        "note": "Nivel inicial vigente antes del 5-oct-2026; cambia automáticamente en esa fecha.",
        "from_2026_10_05": {"maker_bps": 0.0, "taker_bps": 8.0},
    },
}


def _fees(now_ms: int | None = None) -> dict:
    fees = json.loads(json.dumps(DEFAULT_FEES))
    when = dt.datetime.fromtimestamp((now_ms or int(time.time() * 1000))/1000, dt.timezone.utc)
    if when.date() >= dt.date(2026, 10, 5):
        fees["Kraken"]["maker_bps"] = fees["Kraken"]["from_2026_10_05"]["maker_bps"]
        fees["Kraken"]["taker_bps"] = fees["Kraken"]["from_2026_10_05"]["taker_bps"]
        fees["Kraken"]["note"] = "Programa spot efectivo desde 5-oct-2026."
    override = os.environ.get("HERMES_EXCHANGE_FEES_JSON")
    if override:
        try:
            custom = json.loads(override)
            for venue, values in custom.items():
                if venue in fees and isinstance(values, dict):
                    for key in ("maker_bps", "taker_bps", "note"):
                        if key in values:
                            fees[venue][key] = values[key]
        except (ValueError, TypeError):
            pass
    return fees


def _symbol(venue: str, asset: str) -> str:
    base = asset.split("/")[0].upper()
    if venue == "OKX":
        return f"{base}-USDT"
    if venue == "Kraken":
        kraken = {"BTC": "XBT", "DOGE": "XDG"}.get(base, base)
        return f"{kraken}USDT"
    return f"{base}USDT"


def _levels(rows) -> list[tuple[float, float]]:
    out = []
    for row in rows or []:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(qty) and price > 0 and qty > 0:
            out.append((price, qty))
    return out


async def _fetch_book(client: httpx.AsyncClient, venue: str, asset: str) -> tuple[list, list, int | None]:
    symbol = _symbol(venue, asset)
    if venue == "Binance":
        r = await client.get("https://api.binance.com/api/v3/depth",
                             params={"symbol": symbol, "limit": 100})
        r.raise_for_status()
        data = r.json()
        return _levels(data.get("bids")), _levels(data.get("asks")), None
    if venue == "OKX":
        r = await client.get("https://www.okx.com/api/v5/market/books",
                             params={"instId": symbol, "sz": 100})
        r.raise_for_status()
        data = r.json()
        if data.get("code") not in (None, "0") or not data.get("data"):
            raise ValueError(data.get("msg") or "OKX order book unavailable")
        book = data["data"][0]
        return _levels(book.get("bids")), _levels(book.get("asks")), int(book.get("ts") or 0) or None
    if venue == "Bybit":
        r = await client.get("https://api.bybit.com/v5/market/orderbook",
                             params={"category": "spot", "symbol": symbol, "limit": 200})
        r.raise_for_status()
        data = r.json()
        if int(data.get("retCode", -1)) != 0:
            raise ValueError(data.get("retMsg") or "Bybit order book unavailable")
        book = data["result"]
        return _levels(book.get("b")), _levels(book.get("a")), int(book.get("ts") or 0) or None
    if venue == "Kraken":
        r = await client.get("https://api.kraken.com/0/public/Depth",
                             params={"pair": symbol, "count": 100})
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise ValueError("; ".join(data["error"]))
        result = data.get("result") or {}
        if not result:
            raise ValueError("Kraken pair unavailable")
        book = next(iter(result.values()))
        return _levels(book.get("bids")), _levels(book.get("asks")), None
    raise ValueError("unknown venue")


def _walk_buy(asks: list[tuple[float, float]], quote_amount: float) -> float | None:
    remaining, base = quote_amount, 0.0
    for price, qty in asks:
        value = price * qty
        take = min(remaining, value)
        base += take / price
        remaining -= take
        if remaining <= 1e-9:
            return quote_amount / base if base > 0 else None
    return None


def _walk_sell(bids: list[tuple[float, float]], base_amount: float) -> float | None:
    remaining, proceeds = base_amount, 0.0
    for price, qty in bids:
        take = min(remaining, qty)
        proceeds += take * price
        remaining -= take
        if remaining <= 1e-12:
            return proceeds / base_amount if base_amount > 0 else None
    return None


def estimate_cost(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                  notional: float, taker_bps: float) -> dict | None:
    if not bids or not asks or notional <= 0:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid <= 0 or best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    buy = _walk_buy(asks, notional)
    base_qty = notional / mid
    sell = _walk_sell(bids, base_qty)
    if buy is None or sell is None:
        return None
    buy_bps = max(0.0, (buy / mid - 1) * 10_000)
    sell_bps = max(0.0, (1 - sell / mid) * 10_000)
    spread_bps = (best_ask - best_bid) / mid * 10_000
    total_bps = buy_bps + sell_bps + 2 * float(taker_bps)
    return {
        "mid": mid,
        "spread_bps": spread_bps,
        "buy_impact_bps": buy_bps,
        "sell_impact_bps": sell_bps,
        "roundtrip_bps": total_bps,
        "roundtrip_usd": notional * total_bps / 10_000,
    }


def _empty_state(now_ms: int) -> dict:
    fees = _fees(now_ms)
    return {
        "version": 1,
        "started_ms": now_ms,
        "last_sample_ms": 0,
        "sample_cycles": 0,
        "notionals": list(NOTIONALS),
        "fee_assumptions": fees,
        "stats": {},
        "winners": {str(int(n)): {venue: 0 for venue in fees} for n in NOTIONALS},
        "winner_totals": {str(int(n)): 0 for n in NOTIONALS},
    }


def _stat_node(state: dict, venue: str, asset: str) -> dict:
    venue_node = state["stats"].setdefault(venue, {})
    return venue_node.setdefault(asset, {
        "ok": 0, "errors": 0, "spread_sum": 0.0,
        "sizes": {str(int(n)): {"count": 0, "sum": 0.0, "max": 0.0} for n in NOTIONALS},
        "latest": None,
    })


def _update_observation(state: dict, venue: str, asset: str, now_ms: int,
                        result: dict | None = None, error: str | None = None) -> None:
    node = _stat_node(state, venue, asset)
    if error or not result:
        node["errors"] += 1
        node["latest"] = {"ts": now_ms, "error": (error or "unavailable")[:160]}
        return
    node["ok"] += 1
    node["spread_sum"] += float(result["spread_bps"])
    node["latest"] = {"ts": now_ms, **result}
    for key, cost in result["costs"].items():
        if not cost:
            continue
        bucket = node["sizes"][key]
        bucket["count"] += 1
        bucket["sum"] += float(cost["roundtrip_bps"])
        bucket["max"] = max(bucket["max"], float(cost["roundtrip_bps"]))


async def sample_cycle(path: Path, assets: tuple[str, ...], now_ms: int | None = None) -> dict:
    now_ms = int(now_ms or time.time() * 1000)
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _empty_state(now_ms)
    except (OSError, ValueError, TypeError):
        state = _empty_state(now_ms)
    if now_ms - int(state.get("last_sample_ms", 0)) < SAMPLE_INTERVAL_MS:
        return state

    fees = _fees(now_ms)
    state["fee_assumptions"] = fees
    state["notionals"] = list(NOTIONALS)
    for n in NOTIONALS:
        key = str(int(n))
        state.setdefault("winners", {}).setdefault(key, {venue: 0 for venue in fees})
        state.setdefault("winner_totals", {}).setdefault(key, 0)

    timeout = httpx.Timeout(MAX_CLIENT_TIMEOUT)
    headers = {"User-Agent": "HermesV2-ExchangeBenchmark/1.0"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        async def one(venue: str, asset: str):
            try:
                bids, asks, source_ts = await _fetch_book(client, venue, asset)
                if not bids or not asks:
                    raise ValueError("empty order book")
                best_bid, best_ask = bids[0][0], asks[0][0]
                mid = (best_bid + best_ask) / 2
                costs = {}
                for n in NOTIONALS:
                    costs[str(int(n))] = estimate_cost(bids, asks, n, fees[venue]["taker_bps"])
                return venue, asset, {
                    "spread_bps": (best_ask - best_bid) / mid * 10_000,
                    "best_bid": best_bid, "best_ask": best_ask,
                    "source_ts": source_ts, "costs": costs,
                }, None
            except Exception as exc:
                return venue, asset, None, f"{type(exc).__name__}: {exc}"

        tasks = [one(venue, asset) for venue in fees for asset in assets]
        results = await asyncio.gather(*tasks)

    latest_by_asset = {asset: {} for asset in assets}
    for venue, asset, result, error in results:
        _update_observation(state, venue, asset, now_ms, result, error)
        if result:
            latest_by_asset[asset][venue] = result

    for n in NOTIONALS:
        key = str(int(n))
        for asset, venue_rows in latest_by_asset.items():
            candidates = []
            for venue, row in venue_rows.items():
                cost = row["costs"].get(key)
                if cost:
                    candidates.append((float(cost["roundtrip_bps"]), venue))
            if candidates:
                _, winner = min(candidates)
                state["winners"][key][winner] = state["winners"][key].get(winner, 0) + 1
                state["winner_totals"][key] += 1

    state["last_sample_ms"] = now_ms
    state["sample_cycles"] = int(state.get("sample_cycles", 0)) + 1
    atomic_write(path, json.dumps(state, indent=2, allow_nan=False))
    return state


def snapshot(path: Path) -> dict:
    if not path.exists():
        now = int(time.time() * 1000)
        return {"ready": False, "message": "Esperando la primera muestra pública",
                "notionals": list(NOTIONALS), "fee_assumptions": _fees(now)}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ready": False, "message": "Estado de benchmark no disponible",
                "notionals": list(NOTIONALS), "fee_assumptions": _fees()}

    venues = {}
    all_assets = sorted({asset for rows in state.get("stats", {}).values() for asset in rows})
    for venue, assets in state.get("stats", {}).items():
        ok = errors = 0
        spread_sum = 0.0
        size_totals = {str(int(n)): {"count": 0, "sum": 0.0, "max": 0.0} for n in NOTIONALS}
        latest_ts = 0
        for node in assets.values():
            ok += int(node.get("ok", 0)); errors += int(node.get("errors", 0))
            spread_sum += float(node.get("spread_sum", 0.0))
            latest_ts = max(latest_ts, int((node.get("latest") or {}).get("ts", 0)))
            for key, bucket in node.get("sizes", {}).items():
                total = size_totals.setdefault(key, {"count": 0, "sum": 0.0, "max": 0.0})
                total["count"] += int(bucket.get("count", 0))
                total["sum"] += float(bucket.get("sum", 0.0))
                total["max"] = max(total["max"], float(bucket.get("max", 0.0)))
        total_attempts = ok + errors
        venue_sizes = {}
        for key, bucket in size_totals.items():
            count = bucket["count"]
            avg = bucket["sum"] / count if count else None
            venue_sizes[key] = {
                "samples": count,
                "avg_roundtrip_bps": avg,
                "avg_roundtrip_usd": (float(key) * avg / 10_000) if avg is not None else None,
                "max_roundtrip_bps": bucket["max"] if count else None,
                "win_share": (state.get("winners", {}).get(key, {}).get(venue, 0) /
                              state.get("winner_totals", {}).get(key, 1)
                              if state.get("winner_totals", {}).get(key, 0) else None),
            }
        venues[venue] = {
            "availability": ok / total_attempts if total_attempts else 0.0,
            "samples_ok": ok, "errors": errors,
            "avg_spread_bps": spread_sum / ok if ok else None,
            "sizes": venue_sizes, "latest_ts": latest_ts,
            "fees": state.get("fee_assumptions", {}).get(venue, {}),
        }

    assets_summary = {}
    for asset in all_assets:
        latest = {}
        for venue, rows in state.get("stats", {}).items():
            item = (rows.get(asset) or {}).get("latest")
            if item:
                latest[venue] = item
        best = {}
        for n in state.get("notionals", NOTIONALS):
            key = str(int(float(n)))
            choices = []
            for venue, item in latest.items():
                cost = (item.get("costs") or {}).get(key)
                if cost:
                    choices.append((float(cost["roundtrip_bps"]), venue))
            if choices:
                bps, venue = min(choices)
                best[key] = {"venue": venue, "roundtrip_bps": bps,
                             "roundtrip_usd": float(key) * bps / 10_000}
        assets_summary[asset] = {"best": best, "latest": latest}

    return {
        "ready": bool(state.get("sample_cycles")),
        "started_ms": state.get("started_ms"),
        "last_sample_ms": state.get("last_sample_ms"),
        "sample_cycles": state.get("sample_cycles", 0),
        "notionals": state.get("notionals", list(NOTIONALS)),
        "fee_assumptions": state.get("fee_assumptions", {}),
        "venues": venues,
        "assets": assets_summary,
        "method": "Public order books; conservative taker→taker fee + bid/ask + visible depth impact.",
        "note": "No envía órdenes. Las tarifas son supuestos configurables; verificar la tasa real de la cuenta antes de live.",
    }
