import tempfile
import time
import unittest
import json
import base64
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import httpx
from pathlib import Path

from hermes_trading.alpaca_paper_bridge import (BrokerError, PaperAPI, PaperAuto, PaperManual,
                                                 amount_string, broker_symbol, display, quantity_string)
from hermes_trading.growth import GrowthConfig, Portfolio
from hermes_trading.growth_web import handler_factory
from hermes_trading.growth_run import _save


class FakeAPI:
    def __init__(self, account_id):
        self.account_id, self.cash, self.holdings, self.history = account_id, 50., {}, {}
        self.posts = 0
        self.lose_response = False
        self.unavailable = set()

    def account(self):
        return {"id": self.account_id, "account_number": self.account_id,
                "cash": str(self.cash), "equity": str(self.cash + sum(p["qty"] * 100 for p in self.holdings.values()))}

    def positions(self):
        return [{"symbol": a, "qty": str(p["qty"]), "cost_basis": str(p["qty"] * 100),
                 "current_price": "100", "market_value": str(p["qty"] * 100)} for a,p in self.holdings.items()]

    def orders(self, status="open"):
        return list(self.history.values()) if status == "all" else []

    def by_client_id(self, id_):
        return self.history.get(id_)

    def asset(self, asset):
        return {"status": "active" if asset not in self.unavailable else "inactive",
                "tradable": asset not in self.unavailable, "fractionable": True,
                "class": "crypto" if "/" in asset else "us_equity"}

    def request(self, method, path):
        return {"is_open": True}

    def submit(self, payload):
        self.posts += 1
        sym,side=payload["symbol"],payload["side"]
        qty=float(payload.get("qty") or float(payload["notional"])/100)
        if side == "buy":
            self.holdings[sym] = {"qty": self.holdings.get(sym,{"qty":0})["qty"]+qty}
            self.cash -= qty*100
        else:
            self.holdings[sym]["qty"] -= qty
            if self.holdings[sym]["qty"] < 1e-8: del self.holdings[sym]
            self.cash += qty*100
        record={**payload,"status":"filled","filled_qty":str(qty),"filled_avg_price":"100",
                "submitted_at":"2026-09-27T00:00:00Z"}
        self.history[payload["client_order_id"]] = record
        if self.lose_response:
            self.lose_response=False
            raise BrokerError("Respuesta perdida")
        return record


class PaperBridgeTests(unittest.TestCase):
    def test_expansion_requires_both_paper_assets_and_flat_ledgers(self):
        api = FakeAPI("paper")
        book = Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            mirror = PaperAuto(Path(folder) / "shared.json", api)
            mirror.sync(book)
            api.unavailable.add("XRP/USDT")
            with self.assertRaisesRegex(BrokerError, "no negociable"):
                mirror.can_expand_assets(book, ("XRP/USDT", "LINK/USDT"))
            api.unavailable.clear()
            self.assertTrue(mirror.can_expand_assets(book, ("XRP/USDT", "LINK/USDT")))
            book.state["events"].append({"event": "optimizer_rejected"})
            self.assertFalse(mirror.can_expand_assets(book, ("XRP/USDT", "LINK/USDT")))
            self.assertEqual(api.posts, 0)

    def test_client_is_pinned_to_paper_api_and_encodes_crypto_symbol(self):
        seen=[]
        def reply(request):
            seen.append((str(request.url),request.headers.get("APCA-API-KEY-ID")))
            return httpx.Response(200,json={"status":"active","tradable":True})
        api=PaperAPI("paper-key","paper-secret",transport=httpx.MockTransport(reply))
        self.assertTrue(api.asset("BTC/USDT")["tradable"])
        self.assertTrue(seen[0][0].startswith("https://paper-api.alpaca.markets/v2/assets/"))
        self.assertIn("BTC%2FUSD",seen[0][0])
        self.assertEqual(seen[0][1],"paper-key")

    def test_manual_orders_share_paper_account_and_reconcile_unknown_response(self):
        api = FakeAPI("paper1234")
        with tempfile.TemporaryDirectory() as folder:
            wallet=PaperManual(Path(folder)/"shared.json",api)
            buy={"id":"manual0001","asset":"BTC/USDT","side":"buy","amount":10}
            api.lose_response=True
            with self.assertRaises(BrokerError): wallet.order(buy)
            self.assertEqual(api.posts,1)
            self.assertIsNotNone(wallet.load()["pending"])
            self.assertEqual(wallet.order(buy)["status"],"filled")
            self.assertEqual(api.posts,1)
            self.assertIsNone(wallet.load()["pending"])
            self.assertAlmostEqual(wallet.state()["positions"]["BTC/USDT"]["qty"],.1)
            with self.assertRaisesRegex(ValueError,"unidades"):
                wallet.order({"id":"manual0002","asset":"BTC/USDT","side":"sell","amount":.2})
            wallet.order({"id":"manual0003","asset":"BTC/USDT","side":"sell","amount":.1})
            self.assertFalse(wallet.state()["positions"])

    def test_precision(self):
        self.assertEqual(broker_symbol("SOL/USDT"),"SOL/USD")
        self.assertEqual(broker_symbol("PAXG/USDT"),"PAXG/USD")
        self.assertEqual(display("DOGEUSD"),"DOGE/USDT")
        self.assertEqual(broker_symbol("BRK.B"),"BRK.B")
        self.assertEqual(amount_string(.1234567899,9),"0.123456789")
        self.assertEqual(quantity_string(.12349,{"min_trade_increment":"0.0001"}),"0.1234")

    def test_stocks_and_new_crypto_only_trade_when_broker_asset_is_available(self):
        api=FakeAPI("paper")
        api.unavailable.add("BNB/USDT")
        with tempfile.TemporaryDirectory() as folder:
            wallet=PaperManual(Path(folder)/"shared.json",api)
            self.assertFalse(wallet.availability("BNB/USDT")["available"])
            with self.assertRaisesRegex(BrokerError,"no negociable"):
                wallet.order({"id":"manual0001","asset":"BNB/USDT","side":"buy","amount":10})
            self.assertEqual(api.posts,0)
            self.assertTrue(wallet.availability("AAPL")["available"])
            wallet.order({"id":"manual0002","asset":"AAPL","side":"buy","amount":10})
            self.assertEqual(list(wallet.state()["positions"]),["AAPL"])
            self.assertAlmostEqual(wallet.state()["cash"],40)

    def test_auto_mirror_only_fresh_events_after_flat_initialization(self):
        api=FakeAPI("paper")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"shared.json",api)
            mirror.sync(book)
            self.assertEqual(mirror.load()["cursor"],0)
            now=int(time.time()*1000)
            book.state["position"]={"asset":"BTC/USDT"}
            book.state["events"].append({"ts":now,"event":"entry","asset":"BTC/USDT","notional":12})
            mirror.sync(book)
            self.assertEqual(api.posts,1)
            self.assertEqual(mirror.load()["cursor"],1)
            book.state["position"]=None
            book.state["events"].append({"ts":now,"event":"exit","asset":"BTC/USDT"})
            mirror.sync(book)
            self.assertEqual(api.posts,2)
            self.assertEqual(mirror.load()["cursor"],2)
            self.assertFalse(api.holdings)
            self.assertIsNone(mirror.load()["halted_auto"])

    def test_transport_warning_clears_only_after_broker_reconciliation(self):
        api=FakeAPI("paper")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"shared.json",api)
            mirror.sync(book)
            order=api.submit({"symbol":"BTC/USD","side":"buy","notional":"10",
                              "client_order_id":"buy1"})
            api.submit({"symbol":"BTC/USD","side":"sell","qty":order["filled_qty"],
                        "client_order_id":"sell1"})
            s=mirror.load()
            s["last_order"]={"id":"sell1","qty":float(order["filled_qty"]),"status":"filled"}
            s["halted_auto"]="Alpaca paper no responde; orden sin confirmar"
            mirror.save(s)
            self.assertTrue(mirror.recover_transport_halt_if_flat(book))
            self.assertIsNone(mirror.load()["halted_auto"])
            self.assertEqual(api.posts,2)
            s=mirror.load();s["halted_auto"]="Alpaca paper no responde; orden sin confirmar"
            mirror.save(s);api.holdings["BTC/USD"]={"qty":.1}
            with self.assertRaises(BrokerError):
                mirror.recover_transport_halt_if_flat(book)
            self.assertIsNotNone(mirror.load()["halted_auto"])

    def test_auto_does_not_import_existing_strategy_position(self):
        api=FakeAPI("paper")
        book=Portfolio(GrowthConfig())
        book.state["position"]={"asset":"BTC/USDT"}
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"shared.json",api)
            mirror.sync(book)
            self.assertIsNone(mirror.load()["cursor"])
            self.assertEqual(api.posts,0)

    def test_auto_halts_on_existing_broker_position_or_stale_signal(self):
        api=FakeAPI("paper")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"shared.json"
            api.holdings["SPY"]={"qty":.1}
            mirror=PaperAuto(path,api)
            with self.assertRaisesRegex(BrokerError,"vacía"):
                mirror.sync(book)
            self.assertEqual(api.posts,0)
        api.holdings.clear()
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"shared.json",api)
            mirror.sync(book)
            book.state["position"]={"asset":"BTC/USDT"}
            book.state["events"].append({"ts":int(time.time()*1000)-180_000,
                                          "event":"entry","asset":"BTC/USDT","notional":12})
            mirror.sync(book)
            self.assertEqual(mirror.load()["halted_auto"],"stale_strategy_event")
            self.assertEqual(api.posts,0)

    def test_authenticated_dashboard_routes_paper_orders_to_shared_account(self):
        api=FakeAPI("paper")
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/"account.json"
            _save(Portfolio(GrowthConfig()),state)
            server=ThreadingHTTPServer(("127.0.0.1",0),handler_factory(
                state,"a-long-unique-password",paper_accounts=(api,)))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                base=f"http://127.0.0.1:{server.server_port}"
                key=base64.b64encode(b"admin:a-long-unique-password").decode()
                headers={"Authorization":f"Basic {key}","Content-Type":"application/json",
                         "Origin":base,"X-Hermes-Action":"manual-paper"}
                with urllib.request.urlopen(urllib.request.Request(base+"/api/alpaca/manual/state",
                                               headers=headers),timeout=3) as response:
                    self.assertEqual(json.load(response)["cash"],50)
                api.unavailable.add("PAXG/USDT")
                with urllib.request.urlopen(urllib.request.Request(base+"/api/alpaca/manual/asset?asset=PAXG%2FUSDT",
                                               headers=headers),timeout=3) as response:
                    self.assertFalse(json.load(response)["available"])
                data=json.dumps({"id":"manual0001","asset":"SPY","side":"buy","amount":10}).encode()
                request=urllib.request.Request(base+"/api/alpaca/manual/order",data=data,headers=headers,method="POST")
                with urllib.request.urlopen(request,timeout=3) as response:
                    self.assertEqual(json.load(response)["order"]["status"],"filled")
                self.assertEqual(api.posts,1)
                self.assertEqual(json.loads(state.read_text())["state"]["cash"],50)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)

    def test_manual_cannot_sell_bot_position_and_bot_preserves_manual_etf(self):
        api=FakeAPI("paper")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"shared.json"
            manual, mirror=PaperManual(path,api),PaperAuto(path,api)
            manual.order({"id":"manual0001","asset":"SPY","side":"buy","amount":10})
            mirror.sync(book)
            now=int(time.time()*1000)
            book.state["position"]={"asset":"BTC/USDT"}
            book.state["events"].append({"ts":now,"event":"entry","asset":"BTC/USDT","notional":12})
            mirror.sync(book)
            self.assertEqual(api.posts,2)
            self.assertEqual(set(api.holdings),{"SPY","BTC/USD"})
            self.assertEqual(list(manual.state()["positions"]),["SPY"])
            self.assertEqual(mirror.state()["positions"][0]["asset"],"BTC/USDT")
            with self.assertRaisesRegex(BrokerError,"pertenece al bot"):
                manual.order({"id":"manual0002","asset":"BTC/USDT","side":"sell","amount":.12})
            book.state["position"]=None
            book.state["events"].append({"ts":now,"event":"exit","asset":"BTC/USDT"})
            mirror.sync(book)
            self.assertIn("SPY",api.holdings)
            self.assertNotIn("BTC/USD",api.holdings)

    def test_bot_skips_manual_symbol_until_strategy_exits(self):
        api=FakeAPI("paper")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"shared.json"
            manual, mirror=PaperManual(path,api),PaperAuto(path,api)
            manual.order({"id":"manual0001","asset":"BTC/USDT","side":"buy","amount":10})
            mirror.sync(book)
            now=int(time.time()*1000)
            book.state["position"]={"asset":"BTC/USDT"}
            book.state["events"].append({"ts":now,"event":"entry","asset":"BTC/USDT","notional":12})
            mirror.sync(book)
            self.assertEqual(api.posts,1)
            self.assertEqual(mirror.state()["skipped_asset"],"BTC/USDT")
            book.state["position"]=None
            book.state["events"].append({"ts":now,"event":"exit","asset":"BTC/USDT"})
            mirror.sync(book)
            self.assertEqual(api.posts,1)
            self.assertIsNone(mirror.state()["skipped_asset"])

    def test_external_position_change_blocks_new_orders(self):
        api=FakeAPI("paper")
        with tempfile.TemporaryDirectory() as folder:
            manual=PaperManual(Path(folder)/"shared.json",api)
            manual.state()
            api.holdings["BTC/USD"]={"qty":.1}
            with self.assertRaisesRegex(BrokerError,"difieren"):
                manual.order({"id":"manual0001","asset":"ETH/USDT","side":"buy","amount":10})
            self.assertEqual(api.posts,0)


if __name__ == "__main__": unittest.main()
