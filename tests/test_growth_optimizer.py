import asyncio
import dataclasses
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_trading import backtest
from hermes_trading.growth import BAR_MS, HOUR_MS, GrowthConfig, Portfolio
from hermes_trading.growth_optimizer import (DAY_MS, candidate_config, evaluate,
                                             forward_verdict, initialise)
from hermes_trading.growth_run import _paper, _restore, _save


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = GrowthConfig()

    def test_existing_account_survives_migration_and_alpha_changes_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "account.json"
            old = {"config": dataclasses.asdict(self.cfg), "state": Portfolio(self.cfg).state}
            path.write_text(json.dumps(old))
            book = _restore(self.cfg, path)
            self.assertEqual(book.equity(), 50)
            meta = initialise(book.state, 1000)
            self.assertFalse(meta["history"])
            book.cfg = dataclasses.replace(self.cfg, range_rsi=28)
            _save(book, path, self.cfg)
            self.assertEqual(_restore(self.cfg, path).cfg.range_rsi, 28)
            with self.assertRaisesRegex(RuntimeError, "baseline config changed"):
                _restore(dataclasses.replace(self.cfg, risk_per_trade=0.01), path)

    def test_one_bounded_alpha_change_and_forward_rollback_guard(self):
        trial, change = candidate_config(self.cfg, 0)
        self.assertEqual((change["field"], trial.range_rsi), ("range_rsi", 28))
        self.assertEqual(trial.risk_per_trade, self.cfg.risk_per_trade)
        self.assertIsNone(candidate_config(dataclasses.replace(self.cfg, range_rsi=26), 0)[0])
        state = Portfolio(self.cfg).state
        meta = initialise(state, 0)
        meta["active_change"] = {"applied_ms": 0, "trade_count": 0, "equity_at_apply": 50}
        state["trades"] = [{}] * 10
        self.assertIsNone(forward_verdict(meta, state, 49, 13 * DAY_MS))
        self.assertEqual(forward_verdict(meta, state, 49, 14 * DAY_MS), "revert")
        self.assertEqual(forward_verdict(meta, state, 50.2, 14 * DAY_MS), "confirm")
        state["position"] = {"asset": "BTC/USDT"}
        self.assertIsNone(forward_verdict(meta, state, 49, 14 * DAY_MS))

    def test_latest_holdout_and_history_coverage_gate_application(self):
        n = 143 * DAY_MS // BAR_MS
        times = [i * BAR_MS for i in range(n)]
        candles = {"t": times, "source": "test"}
        hours = {"t": [i * 4 * BAR_MS for i in range(n // 4)], "source": "test"}
        def history(asset, tf, days):
            return candles if tf == "15m" else hours
        rows = [{"candidate_trades": 8, "candidate_return": .03, "baseline_return": .01,
                 "stress_return": .01}] * 4
        assessment = {"eligible_for_manual_review": True, "windows": rows}
        with patch("hermes_trading.backtest.history", side_effect=history), patch(
                "hermes_trading.growth_optimizer.assess", return_value=assessment):
            self.assertTrue(evaluate(self.cfg, 0)["accepted"])
            assessment["windows"] = rows[:3] + [{**rows[0], "candidate_return": .011}]
            self.assertFalse(evaluate(self.cfg, 0)["accepted"])
            candles["t"] = times[9 * DAY_MS // BAR_MS:]
            self.assertEqual(evaluate(self.cfg, 0)["reason"], "incomplete_history")

    def test_worker_applies_research_without_resetting_paper_balance(self):
        class EndTest(Exception):
            pass

        now = int(time.time() * 1000)
        def bars(step):
            current = now // step * step
            timestamps = [current - j * step for j in range(249, -1, -1)]
            return {"t": timestamps, "open": [100.] * 250, "high": [101.] * 250,
                    "low": [99.] * 250, "close": [100.] * 250}
        async def ohlcv(asset, timeframe, count, fresh=True):
            return bars(BAR_MS if timeframe == "15m" else HOUR_MS)
        async def close():
            return None
        sleep_original = asyncio.sleep
        calls = 0
        async def sleep(seconds):
            nonlocal calls
            calls += 1
            if calls > 3:
                raise EndTest()
            await sleep_original(.01)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "account.json"
            p = Portfolio(self.cfg)
            meta = initialise(p.state, now)
            meta["next_due_ms"] = 0
            _save(p, path, self.cfg)
            result = {"accepted": True, "reason": "passed",
                      "change": {"field": "range_rsi", "from": 30., "to": 28.}}
            with patch.dict("os.environ", {"HERMES_AUTOTUNE": "on"}), patch(
                    "hermes_trading.adapters.price.ohlcv", side_effect=ohlcv), patch(
                    "hermes_trading.adapters.price.close", side_effect=close), patch(
                    "hermes_trading.growth_optimizer.evaluate", return_value=result), patch(
                    "hermes_trading.growth_run.asyncio.sleep", side_effect=sleep):
                with self.assertRaises(EndTest):
                    asyncio.run(_paper(self.cfg, path, once=False))
            saved = json.loads(path.read_text())
            self.assertEqual(saved["config"]["range_rsi"], 28)
            self.assertEqual(saved["state"]["cash"], 50)
            self.assertEqual(saved["state"]["optimizer"]["last_decision"]["event"], "applied")


if __name__ == "__main__":
    unittest.main()
