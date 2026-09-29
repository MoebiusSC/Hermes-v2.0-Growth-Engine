import dataclasses
import asyncio
import json
import tempfile
import unittest

import numpy as np
from pathlib import Path
from unittest.mock import patch

from hermes_trading.growth import (BAR_MS, HOUR_MS, GrowthConfig, Portfolio, candidate,
                                   sui_replica_candidate)
from hermes_trading.growth_lab import assess
from hermes_trading.growth_run import (_recent_market_data_complete, _recover_market_gap, _restore, _save,
                                       _expand_universe, report)
from hermes_trading.score import score


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.cfg = GrowthConfig(assets=("BTC/USDT", "ETH/USDT", "SOL/USDT"))

    def _signal(self, asset="BTC/USDT", ts=BAR_MS):
        return {"asset": asset, "regime": "RANGE", "strength": 0.2,
                "stop_fraction": 0.02, "decision_ms": ts}

    def _bar(self, price=100, low=99, high=101):
        return {"open": price, "high": high, "low": low, "close": price}

    def test_shared_account_multiple_positions_respect_global_risk(self):
        p = Portfolio(self.cfg)
        signals = [self._signal('ETH/USDT'), {**self._signal(), 'strength': 0.5},
                   self._signal('SOL/USDT')]
        p.decide(signals, BAR_MS)
        self.assertFalse(p.state['positions'])
        self.assertEqual(set(p.state['pending']), {'BTC/USDT', 'ETH/USDT', 'SOL/USDT'})
        for asset in p.pending_assets():
            p.on_bar(asset, self._bar(), BAR_MS)
        self.assertEqual(len(p.state['positions']), 3)
        self.assertLessEqual(p.open_risk(), p.equity() * self.cfg.max_portfolio_risk + 1e-8)
        self.assertLessEqual(p.gross_exposure(), p.equity() * self.cfg.max_total_exposure + 1e-8)
        for pos in p.state['positions'].values():
            self.assertLessEqual(pos['risk_amount'], pos['equity_at_entry'] * self.cfg.risk_per_trade + 1e-8)
        p.decide([self._signal('BTC/USDT', 2 * BAR_MS)], 2 * BAR_MS)
        self.assertFalse(p.state['pending'])

    def test_signal_specific_target_and_strategy_are_persisted(self):
        p = Portfolio(self.cfg)
        signal = {**self._signal(), "target_r": 3.0, "strategy": "sui_ema_26_55"}
        p.decide([signal], BAR_MS)
        p.on_bar("BTC/USDT", self._bar(), BAR_MS)
        pos = p.state["positions"]["BTC/USDT"]
        self.assertAlmostEqual((pos["target"] - pos["entry"]) / (pos["entry"] - pos["stop"]), 3.0, places=6)
        self.assertEqual(pos["strategy"], "sui_ema_26_55")
        p.on_bar("BTC/USDT", self._bar(100, pos["stop"] + .1, pos["target"] + 1), 2 * BAR_MS)
        self.assertEqual(p.state["trades"][0]["strategy"], "sui_ema_26_55")

    def test_sui_replica_only_fires_on_closed_hour_bullish_cross(self):
        bars = {"t": [59 * HOUR_MS + 45 * 60_000], "open": [100.], "high": [101.],
                "low": [99.], "close": [100.]}
        hourly = {"t": [i * HOUR_MS for i in range(60)], "close": [100.] * 60}
        fast = np.full(60, np.nan)
        slow = np.full(60, np.nan)
        fast[-2:], slow[-2:] = [99.9, 100.2], [100.0, 100.1]
        with patch("hermes_trading.growth.ema_series", side_effect=[fast, slow]):
            signal = sui_replica_candidate("SUI/USDT", bars, hourly, GrowthConfig())
        self.assertEqual(signal["strategy"], "sui_ema_26_55")
        self.assertEqual(signal["target_r"], 3.0)
        self.assertAlmostEqual(signal["stop_fraction"], 0.018)

        bars["t"][-1] += BAR_MS
        with patch("hermes_trading.growth.ema_series") as mocked:
            self.assertIsNone(sui_replica_candidate("SUI/USDT", bars, hourly, GrowthConfig()))
            mocked.assert_not_called()

    def test_stop_wins_when_bar_touches_stop_and_target(self):
        p = Portfolio(self.cfg)
        p.decide([self._signal()], BAR_MS)
        p.on_bar("BTC/USDT", self._bar(), BAR_MS)
        pos = p.state["positions"]["BTC/USDT"]
        p.on_bar("BTC/USDT", self._bar(100, pos["stop"] - 1, pos["target"] + 1), 2 * BAR_MS)
        self.assertEqual(p.state["trades"][0]["reason"], "stop")
        self.assertLess(p.equity(), self.cfg.capital)

    def test_paper_fill_uses_current_quote_and_rejects_late_fill(self):
        p = Portfolio(self.cfg)
        p.decide([self._signal()], BAR_MS)
        p.paper_fill_pending("BTC/USDT", 105, BAR_MS + 20_000)
        self.assertGreater(p.state["positions"]["BTC/USDT"]["entry"], 105)
        q = Portfolio(self.cfg)
        q.decide([self._signal()], BAR_MS)
        q.paper_fill_pending("BTC/USDT", 105, BAR_MS + 61_000)
        self.assertFalse(q.state["positions"])

    def test_gap_latches_new_entries(self):
        p = Portfolio(self.cfg)
        p.on_bar("BTC/USDT", self._bar(), BAR_MS)
        p.on_bar("BTC/USDT", self._bar(), 3 * BAR_MS)
        self.assertEqual(p.state["halted"], "market_data_gap")
        p.decide([self._signal(ts=4 * BAR_MS)], 4 * BAR_MS)
        self.assertFalse(p.state["pending"])

    def test_gap_recovers_only_after_continuous_fresh_bars_when_flat(self):
        p = Portfolio(self.cfg)
        p.state["halted"] = "market_data_gap"
        now = 20 * BAR_MS
        times = list(range(8 * BAR_MS, 20 * BAR_MS, BAR_MS))
        feeds = {asset: ({"t": times}, {"t": [18 * BAR_MS]}, {}) for asset in self.cfg.assets}
        p.state["last_bar"] = {asset: times[-1] for asset in self.cfg.assets}
        self.assertTrue(_recent_market_data_complete(p, feeds, now))
        broken = {**feeds, "ETH/USDT": ({"t": times[:-2] + [times[-1]]}, {"t": [18 * BAR_MS]}, {})}
        self.assertFalse(_recent_market_data_complete(p, broken, now))
        self.assertFalse(asyncio.run(_recover_market_gap(p, broken, now, None)))
        self.assertTrue(asyncio.run(_recover_market_gap(p, feeds, now, None)))
        self.assertIsNone(p.state["halted"])
        self.assertEqual(p.state["events"][-1]["event"], "market_data_gap_recovered")

    def test_expand_persisted_universe_only_after_fresh_feed_and_broker_check(self):
        desired = GrowthConfig()
        now = 1000 * HOUR_MS
        last = now - BAR_MS
        old = Portfolio(self.cfg)
        old.state["cash"] = 50.2693
        old.state["last_bar"] = {a: last for a in self.cfg.assets}
        old.state["events"].append({"event": "exit", "ts": last, "asset": "BTC/USDT"})
        bars = {"t": [last - i * BAR_MS for i in range(249, -1, -1)]}
        hours = {"t": [now - HOUR_MS * i for i in range(250, 0, -1)]}
        for data in (bars, hours):
            data.update({k: [100.0] * 250 for k in ("open", "high", "low", "close")})
        async def fetch(asset, tf, limit, fresh=False):
            return bars if tf == "15m" else hours

        class Broker:
            available = False
            def can_expand_assets(self, book, added):
                self.added = added
                return self.available

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.json"
            _save(old, path, self.cfg)
            book = _restore(desired, path)
            broker = Broker()
            with patch("hermes_trading.adapters.price.ohlcv", new=fetch):
                self.assertFalse(asyncio.run(_expand_universe(book, desired, path, broker, now)))
                self.assertEqual(book.cfg.assets, self.cfg.assets)
                broker.available = True
                self.assertTrue(asyncio.run(_expand_universe(book, desired, path, broker, now)))
            self.assertEqual(broker.added, ("XRP/USDT", "LINK/USDT", "SUI/USDT"))
            self.assertEqual(book.state["cash"], 50.2693)
            self.assertEqual(book.state["events"][0]["event"], "exit")
            self.assertEqual(json.loads(path.read_text())["baseline_config"]["assets"], list(desired.assets))
            self.assertEqual(_restore(desired, path).cfg.assets, desired.assets)

    def test_report_attributes_per_strategy_metrics(self):
        p = Portfolio(self.cfg)
        p.state["trades"] = [
            {"asset": "BTC/USDT", "pnl": 1.0, "pnl_pct": 0.02, "closed_ms": 1, "strategy": "hermes_core"},
            {"asset": "BTC/USDT", "pnl": -0.5, "pnl_pct": -0.01, "closed_ms": 2, "strategy": "hermes_core"},
            {"asset": "SUI/USDT", "pnl": 2.0, "pnl_pct": 0.04, "closed_ms": 3, "strategy": "sui_ema_26_55"},
        ]
        p.state["curve"] = [
            {"ts": "2026-01-01T00:00:00+00:00", "equity": 50.0},
            {"ts": "2026-01-02T00:00:00+00:00", "equity": 52.5},
        ]
        result = report(p)["strategy_metrics"]
        self.assertEqual(result["hermes_core"]["trades"], 2)
        self.assertAlmostEqual(result["hermes_core"]["return"], 0.01)
        self.assertEqual(result["hermes_core"]["profit_factor"], 2.0)
        self.assertEqual(result["sui_ema_26_55"]["trades"], 1)
        self.assertAlmostEqual(result["sui_ema_26_55"]["return"], 0.04)
        self.assertEqual(result["sui_ema_26_55"]["win_rate"], 1.0)

    def test_legacy_single_position_state_migrates_without_reset(self):
        legacy_cfg = dataclasses.asdict(self.cfg)
        for field in ('max_positions', 'max_portfolio_risk', 'max_total_exposure'):
            legacy_cfg.pop(field)
        legacy_state = Portfolio(self.cfg).state
        legacy_state['position'] = {'asset': 'BTC/USDT', 'entry': 100.0, 'qty': 0.1, 'cost': 10.01,
                                    'stop': 98.0, 'target': 105.0, 'opened_ms': 1,
                                    'equity_at_entry': 50.0, 'regime': 'RANGE'}
        legacy_state.pop('positions')
        legacy_state['pending'] = None
        legacy_state['cash'] = 39.99
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'account.json'
            path.write_text(json.dumps({'config': legacy_cfg, 'state': legacy_state}))
            restored = _restore(self.cfg, path)
        self.assertIn('BTC/USDT', restored.state['positions'])
        self.assertFalse(restored.state['pending'])
        self.assertAlmostEqual(restored.equity({'BTC/USDT': 100.0}), 49.99)
    def test_drawdown_latches_across_restart(self):
        p = Portfolio(self.cfg)
        p.state["cash"] = 46.5
        p.decide([self._signal()], BAR_MS)
        self.assertEqual(p.state["halted"], "drawdown_limit")
        recovered = Portfolio(self.cfg, p.state)
        recovered.decide([self._signal(ts=2 * BAR_MS)], 2 * BAR_MS)
        self.assertFalse(recovered.state["pending"])

    def test_score_uses_mtm_for_composite(self):
        goal = {"target_return_30d": 0.05, "max_drawdown": 0.08, "min_sharpe": 1.2}
        trade = [{"pnl_pct": 0.01, "opened_at": "2026-01-01T00:00:00+00:00",
                  "closed_at": "2026-01-01T00:30:00+00:00"}]
        curve = [{"ts": "2026-01-01T00:00:00+00:00", "equity": 50},
                 {"ts": "2026-01-01T00:15:00+00:00", "equity": 45},
                 {"ts": "2026-01-01T00:30:00+00:00", "equity": 50.5}]
        self.assertLess(score(trade, goal, curve), score(trade, goal))

    def test_lab_cannot_modify_risk(self):
        altered = dataclasses.replace(self.cfg, risk_per_trade=0.01)
        with self.assertRaisesRegex(ValueError, "alpha"):
            assess(self.cfg, altered, {}, {})

    def test_no_signal_on_insufficient_history(self):
        self.assertIsNone(candidate("BTC/USDT", {"t": [], "open": [], "high": [],
                                                   "low": [], "close": []}, {}, self.cfg))

    def test_future_hourly_candle_does_not_change_signal(self):
        prices = [100.0] * 490 + [98, 96, 94, 92, 90, 90.5]
        bars = {"t": [i * BAR_MS for i in range(len(prices))], "open": prices,
                "high": [p + 0.5 for p in prices], "low": [p - 0.5 for p in prices], "close": prices}
        hourly = {"t": [i * HOUR_MS for i in range(130)], "close": [100.0] * 130}
        cfg = dataclasses.replace(self.cfg, high_vol_percentile=100)
        first = candidate("BTC/USDT", bars, hourly, cfg)
        self.assertEqual(first["regime"], "RANGE")
        hourly["close"][-1] = 10_000  # this hour has not closed at the decision time
        self.assertEqual(first, candidate("BTC/USDT", bars, hourly, cfg))


if __name__ == "__main__":
    unittest.main()
