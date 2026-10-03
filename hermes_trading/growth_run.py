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

from .growth import BAR_MS, GrowthConfig, Portfolio, replay, signals_for_asset
from .growth_market import DATA_HALTS, audit_open_positions, current_quote, pause, prepare_feeds
from .exchange_benchmark import sample_cycle as sample_exchange_costs
from .score import metrics
from .storage import atomic_write
from .strategy import closed


def _cycle_delay(now_s: float | None = None) -> float:
    """Keep polling a few seconds after each minute instead of drifting into xx:59.xxx."""
    now_s = time.time() if now_s is None else now_s
    next_tick = (int(now_s) // 60 + 1) * 60 + 5
    return max(1.0, next_tick - now_s)


def _strategy_metrics(trades: list[dict], capital: float) -> dict:
    names = sorted({t.get("strategy", "hermes_core") for t in trades} | {"hermes_core", "sui_ema_26_55"})
    result = {}
    for name in names:
        rows = [t for t in trades if t.get("strategy", "hermes_core") == name]
        pnl = sum(float(t["pnl"]) for t in rows)
        wins = [float(t["pnl"]) for t in rows if float(t["pnl"]) > 0]
        losses = [-float(t["pnl"]) for t in rows if float(t["pnl"]) < 0]
        equity, peak, max_dd = capital, capital, 0.0
        for trade in sorted(rows, key=lambda x: x["closed_ms"]):
            equity += float(trade["pnl"])
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)
        result[name] = {
            "trades": len(rows),
            "pnl": round(pnl, 6),
            "return": pnl / capital if capital else 0.0,
            "win_rate": len(wins) / len(rows) if rows else 0.0,
            "profit_factor": round(sum(wins) / sum(losses), 3) if losses else (None if not wins else 999.0),
            "max_drawdown": max_dd,
        }
    return result


def _asset_metrics(trades: list[dict], assets: tuple[str, ...]) -> dict:
    """Historical realised P/L attribution for each configured asset."""
    result = {}
    for asset in assets:
        rows = sorted((t for t in trades if t.get("asset") == asset),
                      key=lambda t: int(t.get("closed_ms", 0)))
        total = sum(float(t.get("pnl", 0.0)) for t in rows)
        last = rows[-1] if rows else None
        result[asset] = {
            "trades": len(rows),
            "pnl": round(total, 6),
            "last_pnl": round(float(last.get("pnl", 0.0)), 6) if last else None,
            "last_closed_ms": int(last["closed_ms"]) if last and last.get("closed_ms") is not None else None,
        }
    return result


def report(book: Portfolio) -> dict:
    s = book.state
    trades, curve = s["trades"], s["curve"]
    m = metrics(trades, curve)
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    by_strategy = _strategy_metrics(trades, book.cfg.capital)
    by_asset = _asset_metrics(trades, book.cfg.assets)
    return {**m, "equity": round(book.equity(), 4), "cash": round(s["cash"], 4),
            "profit_factor": round(wins / losses, 3) if losses else None,
            "halted": s["halted"], "market_data": s.get("market_data"),
            "trades_by_asset": {a: row["trades"] for a, row in by_asset.items()},
            "asset_metrics": by_asset,
            "trades_by_strategy": {name: row["trades"] for name, row in by_strategy.items()},
            "strategy_metrics": by_strategy, **book.risk_snapshot()}


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
    baseline = saved.get("baseline_config", saved["config"])
    desired = json.loads(json.dumps(dataclasses.asdict(cfg)))
    for field in ("max_positions", "max_portfolio_risk", "max_total_exposure"):
        baseline.setdefault(field, desired[field])
        saved["config"].setdefault(field, desired[field])
    old_assets = list(baseline.get("assets", []))
    new_assets = list(desired.get("assets", []))
    expanding = (new_assets[:len(old_assets)] == old_assets and len(new_assets) > len(old_assets) and
                 {k: v for k, v in baseline.items() if k != "assets"} ==
                 {k: v for k, v in desired.items() if k != "assets"})
    if baseline != desired and not expanding:
        raise RuntimeError("baseline config changed while account has state; review/migrate explicitly")
    active = GrowthConfig(**{**saved["config"], "assets": tuple(saved["config"]["assets"])})
    if active.assets != tuple(baseline["assets"]) or any(getattr(active, field) != getattr(cfg, field) for field in
           ("capital", "risk_per_trade", "max_exposure", "max_positions", "max_portfolio_risk",
            "max_total_exposure", "daily_loss", "weekly_loss", "monthly_drawdown",
            "min_order_usd", "fee", "slippage", "spread")):
        raise RuntimeError("saved risk or cost config differs from baseline")
    return Portfolio(active, saved["state"])


async def _expand_universe(book: Portfolio, desired: GrowthConfig, state_path: Path,
                           mirror, now_ms: int) -> bool:
    """Start observing newly added assets at the current close; preserve every old ledger."""
    if book.cfg.assets == desired.assets:
        return False
    added = tuple(a for a in desired.assets if a not in book.cfg.assets)
    if (book.cfg.assets + added != desired.assets or book.state["positions"] or
            book.state["pending"] or book.state["halted"] or
            (book.state.get("optimizer", {}).get("active_change") or
             book.state.get("optimizer", {}).get("running"))):
        return False
    from .adapters import price
    from .strategy import closed
    feeds = {}
    try:
        for asset in added:
            bars = closed(await price.ohlcv(asset, "15m", 250, fresh=True), "15m", now_ms)
            hours = closed(await price.ohlcv(asset, "1h", 250, fresh=True), "1h", now_ms)
            times = bars["t"][-12:]
            if (len(bars["t"]) < 120 or len(hours["t"]) < 108 or
                    len(times) != 12 or any(b - a != BAR_MS for a, b in zip(times, times[1:])) or
                    not BAR_MS <= now_ms - times[-1] < 2 * BAR_MS or
                    now_ms - hours["t"][-1] > 2 * 60 * 60_000 or
                    book.state["last_bar"] and times[-1] !=
                    max(book.state["last_bar"].values())):
                return False
            feeds[asset] = bars
        if mirror and not await asyncio.to_thread(mirror.can_expand_assets, book, added):
            return False
    except Exception as exc:
        print(f"asset expansion deferred: {type(exc).__name__}: {exc}", flush=True)
        return False
    for asset, bars in feeds.items():
        book.state["last_bar"][asset] = bars["t"][-1]
        book.state["marks"][asset] = bars["close"][-1]
    book.cfg = dataclasses.replace(book.cfg, assets=desired.assets)
    meta = book.state.get("optimizer", {})
    meta["next_due_ms"] = now_ms + 60 * 60_000
    book.state["events"].append({"ts": now_ms, "event": "asset_universe_expanded",
                                 "added": list(added), "broker_checked": bool(mirror)})
    _save(book, state_path, desired)
    return True


def _recent_market_data_complete(book: Portfolio, feeds: dict, now_ms: int) -> bool:
    """Require three hours of synchronized, complete closed candles before recovery."""
    if book.state["halted"] not in DATA_HALTS or book.state["pending"]:
        return False
    newest = []
    for asset in book.cfg.assets:
        if asset not in feeds:
            return False
        bars, hours, _ = feeds[asset]
        times = bars["t"][-12:]
        if (len(times) != 12 or any(b - a != BAR_MS for a, b in zip(times, times[1:]))
                or book.state["last_bar"].get(asset) != times[-1]
                or not hours["t"] or now_ms - hours["t"][-1] > 2 * 60 * 60_000):
            return False
        newest.append(times[-1])
    return len(set(newest)) == 1 and BAR_MS <= now_ms - newest[0] < 2 * BAR_MS


async def _recover_market_gap(book: Portfolio, feeds: dict, now_ms: int, mirror) -> bool:
    if not _recent_market_data_complete(book, feeds, now_ms):
        return False
    if mirror:
        try:
            if not await asyncio.to_thread(mirror.reconcile_market_recovery, book):
                book.state.setdefault("market_data", {})["recovery"] = "broker_reconciliation_pending"
                return False
        except Exception as exc:
            book.state.setdefault("market_data", {})["recovery"] = str(exc)
            print(f"paper broker recovery deferred: {type(exc).__name__}: {exc}", flush=True)
            return False
    before = len(book.state["positions"])
    if not audit_open_positions(book, feeds, now_ms) or book.state["halted"] not in DATA_HALTS:
        return False
    if mirror and len(book.state["positions"]) != before:
        # Persist and mirror a recovery exit before clearing the entry restriction.
        return False
    book.state["halted"] = None
    book.state["events"].append({"ts": now_ms, "event": "market_data_gap_recovered",
                                 "latest_closed_bar": next(iter(book.state["last_bar"].values())),
                                 "verified_bars_per_asset": 12, "open_positions": len(book.state["positions"]),
                                 "broker_checked": bool(mirror)})
    return True


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
    if meta.get("research_intent"):
        # A redeploy interrupted research. Its budget stays counted and the same
        # candidate is retried; an unfinished run cannot become an approval.
        meta.pop("research_intent")
    meta["running"] = False
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
            meta["active_validation"] = (active_change.get("previous_validation") if verdict == "revert"
                                         else {**active_change.get("validation", {}),
                                               "paper": {"status": "OBSERVED", "days": optimizer.FORWARD_DAYS,
                                                         "trades": len(book.state["trades"]) - active_change["trade_count"]},
                                               "live_approved": False})
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
            meta.pop("research_intent", None)
            validation = result.get("assessment", {}).get("validation", {})
            if result.get("accepted") and (result.get("evaluated_config") != dataclasses.asdict(book.cfg)
                    or not all(validation.get(gate, {}).get("status") == "PASS" for gate in
                               ("leakage", "costs", "walk_forward", "dsr", "pbo", "bootstrap"))):
                result = {**result, "accepted": False, "reason": "stale_config_or_missing_gate_evidence"}
            meta["candidate_index"] += 1
            meta["next_due_ms"] = now_ms + (optimizer.INTERVAL_MS if result["accepted"] else optimizer.RETRY_MS)
            if result["accepted"] and not (book.state["positions"] or book.state["pending"] or book.state["halted"]):
                trial, _ = optimizer.candidate_config(book.cfg, meta["candidate_index"] - 1)
                meta["active_change"] = {"previous_config": dataclasses.asdict(book.cfg),
                                         "change": result["change"], "applied_ms": now_ms,
                                         "trade_count": len(book.state["trades"]),
                                         "equity_at_apply": book.equity(),
                                         "previous_validation": meta.get("active_validation"),
                                         "validation": result.get("assessment", {}).get("validation", {})}
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
                not (book.state["positions"] or book.state["pending"] or book.state["halted"])):
            total_trials = optimizer.reserve_trials(meta, book.cfg, meta["candidate_index"], now_ms)
            _save(book, state_path, cfg if book.cfg.assets == cfg.assets else
                  dataclasses.replace(cfg, assets=book.cfg.assets))
            task = asyncio.create_task(asyncio.to_thread(optimizer.evaluate, book.cfg,
                                                         meta["candidate_index"], total_trials))
            meta["running"] = True
        else:
            meta["running"] = task is not None
    benchmark_path = state_path.with_name("exchange_benchmark.json")
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
                ready = await prepare_feeds(book, feeds, int(now))
                failures = 0
                if ready and (book.state["halted"] or any(
                        now - (last + BAR_MS) > 60_000 for last in book.state["last_bar"].values())):
                    book.state["pending"] = {}
                if ready and not book.state["last_bar"]:
                    # First boot starts observing NOW, never invents fills in past candles.
                    for asset, (bars, _, _) in feeds.items():
                        book.state["last_bar"][asset] = bars["t"][-1]
                        book.state["marks"][asset] = bars["close"][-1]
                    book.state["curve"].append({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                 "equity": book.equity()})
                elif ready:
                    latest = min(bars["t"][-1] for bars, _, _ in feeds.values())
                    for ts in range(min(book.state["last_bar"].values()) + BAR_MS, latest + 1, BAR_MS):
                        signals = []
                        pending_order = book.pending_assets()
                        ordered_assets = pending_order + [a for a in book.cfg.assets if a not in pending_order]
                        for asset in ordered_assets:
                            bars, hours, _ = feeds[asset]
                            if ts <= book.state["last_bar"].get(asset, -1):
                                continue
                            i = bars["t"].index(ts)  # Entire common interval was verified before mutation.
                            bar = {k: bars[k][i] for k in ("open", "high", "low", "close")}
                            quote = current_quote(feeds[asset][2], int(now))
                            book.on_bar(asset, bar, ts, recovery_quote=(quote, int(now)))
                            sub = {k: bars[k][max(0, i - 160):i + 1] for k in ("t", "open", "high", "low", "close")}
                            signals.extend(signals_for_asset(asset, sub, hours, book.cfg))
                        if ts == latest and 0 <= now - (ts + BAR_MS) <= 60_000:
                            book.decide(signals, ts + BAR_MS)
                        if ts == latest and book.state["pending"]:
                            # A forming next bar supplies a fresh quote for every queued asset.
                            # Fill in global signal rank order so the risk budget is deterministic.
                            for chosen in list(book.pending_assets()):
                                raw = feeds[chosen][2]
                                if raw["t"][-1] == ts + BAR_MS:
                                    book.paper_fill_pending(chosen, float(raw["close"][-1]),
                                                            int(time.time() * 1000))
                                else:
                                    book.state["pending"] = {}
                                    pause(book, "missing_current_quote")
                                    break
                if ready and await _recover_market_gap(book, feeds, int(now), mirror):
                    print(json.dumps({"event": "market_data_gap_recovered",
                                      "latest_closed_bar": book.state["events"][-1]["latest_closed_bar"],
                                      "open_positions": len(book.state["positions"]),
                                      "broker_checked": bool(mirror)}), flush=True)
                if ready and await _expand_universe(book, cfg, state_path, mirror, int(now)):
                    print(json.dumps({"event": "asset_universe_expanded",
                                      "added": list(book.state["events"][-1]["added"])}), flush=True)
                await autotune(int(now))
                baseline = cfg if book.cfg.assets == cfg.assets else dataclasses.replace(cfg, assets=book.cfg.assets)
                _save(book, state_path, baseline)
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
                    pause(book, "consecutive_data_errors")
                book.state["events"].append({"ts": int(now), "event": "data_error", "error": type(exc).__name__})
                baseline = cfg if book.cfg.assets == cfg.assets else dataclasses.replace(cfg, assets=book.cfg.assets)
                _save(book, state_path, baseline)
                print(f"paper data error ({failures}): {type(exc).__name__}: {exc}", flush=True)
            try:
                await sample_exchange_costs(benchmark_path, book.cfg.assets, int(time.time() * 1000))
            except Exception as exc:
                # The passive venue benchmark must never pause or influence the strategy.
                print(f"exchange benchmark deferred: {type(exc).__name__}: {exc}", flush=True)
            if once:
                return
            await asyncio.sleep(_cycle_delay())
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
