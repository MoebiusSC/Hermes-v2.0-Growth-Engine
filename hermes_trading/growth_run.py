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


def _save(book: Portfolio, path: Path, baseline: GrowthConfig | None = None) -> None:
    # Atomic state, separate JSONL audit copies can be derived from events/trades/curve.
    atomic_write(path, json.dumps({"baseline_config": dataclasses.asdict(baseline or book.cfg),
                                   "config": dataclasses.asdict(book.cfg), "state": book.state}, indent=2))


def _restore(cfg: GrowthConfig, state_path: Path) -> Portfolio:
    if not state_path.exists():
        return Portfolio(cfg)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    if saved.get("baseline_config", saved["config"]) != json.loads(json.dumps(dataclasses.asdict(cfg))):
        raise RuntimeError("baseline config changed while account has state; review/migrate explicitly")
    active = GrowthConfig(**{**saved["config"], "assets": tuple(saved["config"]["assets"])})
    if any(getattr(active, field) != getattr(cfg, field) for field in
           ("capital", "assets", "risk_per_trade", "max_exposure", "daily_loss", "weekly_loss",
            "monthly_drawdown", "min_order_usd", "fee", "slippage", "spread")):
        raise RuntimeError("saved risk or cost config differs from baseline")
    return Portfolio(active, saved["state"])


async def _paper(cfg: GrowthConfig, state_path: Path, once: bool) -> None:
    from .adapters import price
    from . import growth_optimizer as optimizer
    from .alpaca_paper_bridge import PaperAuto, configured, from_env

    enabled = os.environ.get("HERMES_AUTOTUNE", "off").lower() == "on"
    book = _restore(cfg, state_path)
    mirror = (PaperAuto(state_path.with_name("alpaca_shared.json"), from_env())
              if configured() and not once else None)
    meta = optimizer.initialise(book.state, int(time.time() * 1000))
    meta["enabled"] = enabled
    task: asyncio.Task | None = None

    async def autotune(now_ms: int) -> None:
        nonlocal task
        if not enabled or once:
            return
        verdict = optimizer.forward_verdict(meta, book.state, book.equity(), now_ms)
        if verdict:
            active_change = meta.pop("active_change")
            if verdict == "revert":
                previous = active_change["previous_config"]
                book.cfg = GrowthConfig(**{**previous, "assets": tuple(previous["assets"])})
            optimizer.record(meta, {"event": verdict, "change": active_change["change"]}, now_ms)
            book.state["events"].append({"ts": now_ms, "event": f"optimizer_{verdict}",
                                         "change": active_change["change"]})
            meta["next_due_ms"] = now_ms + optimizer.INTERVAL_MS
        if task and task.done():
            try:
                result = task.result()
            except Exception as exc:
                result = {"accepted": False, "reason": f"research_error:{type(exc).__name__}"}
            task = None
            meta["candidate_index"] += 1
            meta["next_due_ms"] = now_ms + (optimizer.INTERVAL_MS if result["accepted"] else optimizer.RETRY_MS)
            if result["accepted"] and not (book.state["position"] or book.state["pending"] or book.state["halted"]):
                trial, _ = optimizer.candidate_config(book.cfg, meta["candidate_index"] - 1)
                meta["active_change"] = {"previous_config": dataclasses.asdict(book.cfg),
                                         "change": result["change"], "applied_ms": now_ms,
                                         "trade_count": len(book.state["trades"]),
                                         "equity_at_apply": book.equity()}
                book.cfg = trial
                result["event"] = "applied"
            elif result["accepted"]:
                result = {**result, "accepted": False, "event": "deferred", "reason": "not_flat_or_halted"}
            else:
                result["event"] = "rejected"
            optimizer.record(meta, result, now_ms)
            book.state["events"].append({"ts": now_ms, "event": f"optimizer_{result['event']}",
                                         "reason": result["reason"], "change": result.get("change")})
            print(json.dumps({"optimizer": result["event"], "reason": result["reason"],
                              "change": result.get("change")}), flush=True)
        if (task is None and meta.get("active_change") is None and
                now_ms >= meta["next_due_ms"] and
                not (book.state["position"] or book.state["pending"] or book.state["halted"])):
            task = asyncio.create_task(asyncio.to_thread(optimizer.evaluate, book.cfg,
                                                         meta["candidate_index"]))
            meta["running"] = True
        else:
            meta["running"] = task is not None
    try:
        failures = 0
        while True:
            now = time.time() * 1000
            try:
                feeds = {}
                for asset in book.cfg.assets:
                    raw = await price.ohlcv(asset, "15m", 250, fresh=True)
                    bars = closed(raw, "15m", now)
                    hours = closed(await price.ohlcv(asset, "1h", 250, fresh=True), "1h", now)
                    if len(bars["t"]) < 120 or len(hours["t"]) < 108:
                        raise RuntimeError(f"insufficient closed candles for {asset}")
                    feeds[asset] = (bars, hours, raw)
                newest = {asset: feed[0]["t"][-1] for asset, feed in feeds.items()}
                if len(set(newest.values())) != 1 or now - min(newest.values()) > 2 * BAR_MS:
                    book.state["halted"] = "stale_or_unsynchronized_market_data"
                failures = 0
                if not book.state["last_bar"]:
                    # First boot starts observing NOW, never invents fills in past candles.
                    for asset, (bars, _, _) in feeds.items():
                        book.state["last_bar"][asset] = bars["t"][-1]
                        book.state["marks"][asset] = bars["close"][-1]
                    book.state["curve"].append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                 "equity": book.equity()})
                else:
                    latest = max(bars["t"][-1] for bars, _, _ in feeds.values())
                    for ts in range(min(book.state["last_bar"].values()) + BAR_MS, latest + 1, BAR_MS):
                        signals = []
                        for asset, (bars, hours, _) in feeds.items():
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
                            signal = candidate(asset, sub, hours, book.cfg)
                            if signal:
                                signals.append(signal)
                        book.decide(signals, ts + BAR_MS)
                        if ts == latest and book.state["pending"]:
                            chosen = book.state["pending"]["asset"]
                            raw = feeds[chosen][2]
                            # A forming next bar supplies the current observed quote. If missing or
                            # late, skip the signal rather than assume a historical open fill.
                            if raw["t"][-1] == ts + BAR_MS:
                                book.paper_fill_pending(float(raw["close"][-1]), int(time.time() * 1000))
                            else:
                                book.state["pending"] = None
                                book.state["halted"] = "missing_current_quote"
                await autotune(int(now))
                _save(book, state_path, cfg)
                if mirror:
                    try:
                        await asyncio.to_thread(mirror.sync, book)
                    except Exception as exc:
                        # A broker outage cannot rewrite or silently reset the research account.
                        print(f"Alpaca paper mirror pending: {type(exc).__name__}: {exc}", flush=True)
                print(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), **report(book)}), flush=True)
            except Exception as exc:
                failures += 1
                if failures >= 3:
                    book.state["halted"] = "consecutive_data_errors"
                book.state["events"].append({"ts": int(now), "event": "data_error", "error": type(exc).__name__})
                _save(book, state_path, cfg)
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
