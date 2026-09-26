"""Growth Engine CLI: paper worker and offline portfolio research. No live order path."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
import time
from pathlib import Path

from .growth import BAR_MS, GrowthConfig, Portfolio, candidate, replay
from .score import metrics
from .storage import atomic_write
from .strategy import closed


def report(book: Portfolio) -> dict:
    s = book.state
    trades, curve = s["trades"], s["curve"]
    m = metrics(trades, curve)
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return {**m, "equity": round(book.equity(), 4), "cash": round(s["cash"], 4),
            "profit_factor": round(wins / losses, 3) if losses else None,
            "halted": s["halted"], "trades_by_asset": {a: sum(t["asset"] == a for t in trades)
                                                      for a in book.cfg.assets}}


def _load_config(path: Path) -> GrowthConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "assets" in data:
        data["assets"] = tuple(data["assets"])
    return GrowthConfig(**data)


def _save(book: Portfolio, path: Path) -> None:
    # Atomic state, separate JSONL audit copies can be derived from events/trades/curve.
    atomic_write(path, json.dumps({"config": dataclasses.asdict(book.cfg), "state": book.state}, indent=2))


async def _paper(cfg: GrowthConfig, state_path: Path, once: bool) -> None:
    from .adapters import price

    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if saved["config"] != json.loads(json.dumps(dataclasses.asdict(cfg))):
            raise RuntimeError("config changed while account has state; review/migrate state explicitly")
        book = Portfolio(cfg, saved["state"])
    else:
        book = Portfolio(cfg)
    try:
        failures = 0
        while True:
            now = time.time() * 1000
            try:
                feeds = {}
                for asset in cfg.assets:
                    bars = closed(await price.ohlcv(asset, "15m", 250), "15m", now)
                    hours = closed(await price.ohlcv(asset, "1h", 250), "1h", now)
                    if len(bars["t"]) < 120 or len(hours["t"]) < 108:
                        raise RuntimeError(f"insufficient closed candles for {asset}")
                    feeds[asset] = (bars, hours)
                newest = {asset: feed[0]["t"][-1] for asset, feed in feeds.items()}
                if len(set(newest.values())) != 1 or now - min(newest.values()) > 2 * BAR_MS:
                    book.state["halted"] = "stale_or_unsynchronized_market_data"
                failures = 0
                if not book.state["last_bar"]:
                    # First boot starts observing NOW, never invents fills in past candles.
                    for asset, (bars, _) in feeds.items():
                        book.state["last_bar"][asset] = bars["t"][-1]
                        book.state["marks"][asset] = bars["close"][-1]
                    book.state["curve"].append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                 "equity": book.equity()})
                else:
                    latest = max(bars["t"][-1] for bars, _ in feeds.values())
                    for ts in range(min(book.state["last_bar"].values()) + BAR_MS, latest + 1, BAR_MS):
                        signals = []
                        for asset, (bars, hours) in feeds.items():
                            if ts <= book.state["last_bar"].get(asset, -1):
                                continue
                            try:
                                i = bars["t"].index(ts)
                            except ValueError:
                                book.state["halted"] = "market_data_gap"
                                continue
                            if ts + BAR_MS > now:
                                continue
                            bar = {k: bars[k][i] for k in ("open", "high", "low", "close")}
                            book.on_bar(asset, bar, ts)
                            sub = {k: bars[k][max(0, i - 160):i + 1] for k in ("t", "open", "high", "low", "close")}
                            signal = candidate(asset, sub, hours, cfg)
                            if signal:
                                signals.append(signal)
                        book.decide(signals, ts + BAR_MS)
                _save(book, state_path)
                print(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **report(book)}), flush=True)
            except Exception as exc:
                failures += 1
                if failures >= 3:
                    book.state["halted"] = "consecutive_data_errors"
                book.state["events"].append({"ts": int(now), "event": "data_error", "error": type(exc).__name__})
                _save(book, state_path)
                print(f"paper data error ({failures}): {type(exc).__name__}: {exc}", flush=True)
            if once:
                return
            await asyncio.sleep(60)
    finally:
        await price.close()


def _research(cfg: GrowthConfig, days: int, stress: bool) -> dict:
    from .backtest import history

    feed = {asset: history(asset, "15m", days) for asset in cfg.assets}
    hourly = {asset: history(asset, "1h", days + 6) for asset in cfg.assets}
    book = replay(cfg, feed, hourly)
    result = {"period_days": days, "base": report(book)}
    if stress:
        stressed = dataclasses.replace(cfg, fee=cfg.fee * 2, slippage=cfg.slippage * 2, spread=cfg.spread * 2)
        result["double_costs"] = report(replay(stressed, feed, hourly))
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Hermes v2 shared-account paper/research engine")
    parser.add_argument("command", choices=("paper", "backtest"))
    parser.add_argument("--config", type=Path, default=Path("growth.json"))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("HERMES_GROWTH_STATE", "growth_state/account.json")))
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("HERMES_TRADING_MODE", "paper").lower() != "paper":
        parser.error("Growth Engine has no live execution adapter; HERMES_TRADING_MODE must be paper")
    cfg = _load_config(args.config)
    if args.command == "paper":
        asyncio.run(_paper(cfg, args.state, args.once))
    else:
        if args.days < 30:
            parser.error("at least 30 days of data required")
        print(json.dumps(_research(cfg, args.days, True), indent=2))


if __name__ == "__main__":
    main()
