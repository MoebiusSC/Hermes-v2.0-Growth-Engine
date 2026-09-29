import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from hermes_trading.manual_paper import ASSETS, ManualWallet, OrderError, asset_kind, quote


class ManualPaperTests(unittest.TestCase):
    def test_buy_sell_and_restore_isolated_account(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manual_account.json"
            quotes = lambda asset: {"asset": asset, "price": 100.0, "asof": time.time(),
                                    "source": "test", "tradable": True}
            wallet = ManualWallet(path, quotes)
            buy = {"id": "manual0001", "asset": "SPY", "side": "buy", "amount": 20}
            first = wallet.order(buy)
            self.assertEqual(wallet.order(buy), first)
            self.assertEqual(len(wallet.state()["orders"]), 1)
            self.assertAlmostEqual(wallet.state()["cash"], 30)
            qty = first["qty"]
            self.assertAlmostEqual(qty * first["price"], 20)
            recovered = ManualWallet(path, quotes)
            sale = recovered.order({"id": "manual0002", "asset": "SPY", "side": "sell", "amount": qty})
            self.assertAlmostEqual(sale["pnl"], 20 * (.9995 / 1.0005 - 1))
            self.assertFalse(recovered.state()["positions"])
            self.assertEqual(len(recovered.state()["orders"]), 2)

    def test_limits_and_stale_quote_never_write_order(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manual_account.json"
            wallet = ManualWallet(path, lambda asset: {"asset": asset, "price": 100, "asof": 0,
                                                       "source": "test", "tradable": False})
            for fields in [
                {"id": "manual0001", "asset": "SPY", "side": "buy", "amount": 51},
                {"id": "manual0002", "asset": "BTC/USDT", "side": "buy", "amount": 4},
                {"id": "manual0003", "asset": "XYZ/USDT", "side": "buy", "amount": 10},
                {"id": "manual0004", "asset": "SPY", "side": "sell", "amount": 1},
                {"id": "manual0005", "asset": "SPY", "side": "buy", "amount": 10},
            ]:
                with self.assertRaises(OrderError):
                    wallet.order(fields)
            self.assertFalse(path.exists())

    def test_requested_coins_and_custom_us_stock_use_separate_manual_wallet(self):
        self.assertTrue({f"{x}/USDT" for x in ("DOGE", "BNB", "XRP", "LINK", "AVAX", "ADA", "SUI", "LTC", "PAXG")}
                        <= ASSETS.keys())
        self.assertEqual(asset_kind("BRK.B"), "stock")
        with tempfile.TemporaryDirectory() as folder:
            wallet=ManualWallet(Path(folder)/"manual.json", lambda a: {
                "asset": a, "price": 100., "asof": time.time(), "source": "test", "tradable": True})
            for name,asset in enumerate(("DOGE/USDT","PAXG/USDT","AAPL","BRK.B"),1):
                order=wallet.order({"id":f"manual{name:04}","asset":asset,"side":"buy","amount":10})
                self.assertEqual(order["asset"],asset)
                self.assertGreater(order["qty"],0)
            self.assertAlmostEqual(wallet.state()["cash"],10)
            self.assertEqual(len(wallet.state()["positions"]),4)

    def test_new_crypto_quote_uses_fresh_public_spot_bar(self):
        class Response:
            def __init__(self, stamp): self.stamp=stamp
            def raise_for_status(self): pass
            def json(self): return [[self.stamp, "1", "1", "1", "2.5"]]
        class Client:
            def __init__(self, stamp): self.stamp=stamp
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, url, params):
                self.assertion=(url,params)
                return Response(self.stamp)
        recent=int(time.time()//60*60*1000)
        with patch("hermes_trading.manual_paper.httpx.Client", return_value=Client(recent)):
            result=quote("PAXG/USDT")
            self.assertTrue(result["tradable"])
            self.assertEqual(result["price"],2.5)


if __name__ == "__main__":
    unittest.main()
