import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_trading.exchange_benchmark import estimate_cost, sample_cycle, snapshot


class ExchangeBenchmarkTests(unittest.TestCase):
    def test_depth_cost_includes_spread_impact_and_two_taker_fees(self):
        bids=[(99.0, 10.0),(98.0, 10.0)]
        asks=[(101.0, .5),(102.0, 10.0)]
        out=estimate_cost(bids,asks,100.0,10.0)
        self.assertIsNotNone(out)
        self.assertGreater(out["roundtrip_bps"],20.0)
        self.assertAlmostEqual(out["spread_bps"],200.0,places=6)
        self.assertGreater(out["roundtrip_usd"],2.0)

    def test_sampler_persists_aggregate_and_never_needs_auth(self):
        async def fake_fetch(client,venue,asset):
            return ([(99.9,1000.0)],[(100.1,1000.0)],123)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"exchange_benchmark.json"
            with patch("hermes_trading.exchange_benchmark._fetch_book",new=fake_fetch):
                state=asyncio.run(sample_cycle(path,("BTC/USDT","ETH/USDT"),1_000_000))
            self.assertEqual(state["sample_cycles"],1)
            self.assertTrue(path.exists())
            out=snapshot(path)
            self.assertTrue(out["ready"])
            self.assertEqual(set(out["venues"]),{"Binance","OKX","Bybit","Kraken"})
            self.assertEqual(set(out["assets"]),{"BTC/USDT","ETH/USDT"})
            self.assertGreater(out["venues"]["Binance"]["sizes"]["25"]["avg_roundtrip_bps"],20)
            # A second call within 15 minutes is a no-op.
            with patch("hermes_trading.exchange_benchmark._fetch_book",side_effect=AssertionError("must not fetch")):
                again=asyncio.run(sample_cycle(path,("BTC/USDT",),1_000_001))
            self.assertEqual(again["sample_cycles"],1)

    def test_errors_reduce_availability_without_breaking_snapshot(self):
        async def fake_fetch(client,venue,asset):
            if venue=="Kraken":
                raise RuntimeError("pair unavailable")
            return ([(99.9,1000.0)],[(100.1,1000.0)],None)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"exchange_benchmark.json"
            with patch("hermes_trading.exchange_benchmark._fetch_book",new=fake_fetch):
                asyncio.run(sample_cycle(path,("BTC/USDT",),2_000_000))
            out=snapshot(path)
            self.assertEqual(out["venues"]["Kraken"]["availability"],0.0)
            self.assertEqual(out["venues"]["Kraken"]["errors"],1)
            self.assertEqual(out["venues"]["Binance"]["availability"],1.0)


if __name__=="__main__":
    unittest.main()
