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
                                                 amount_string, broker_symbol, distinct)
from hermes_trading.growth import GrowthConfig, Portfolio
from hermes_trading.growth_web import handler_factory
from hermes_trading.growth_run import _save


class FakeAPI:
    def __init__(self, account_id):
        self.account_id, self.cash, self.holdings, self.history = account_id, 50., {}, {}
        self.posts = 0
        self.lose_response = False

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
        return {"status": "active", "tradable": True, "fractionable": True}

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

    def test_manual_orders_use_separate_paper_account_and_reconcile_unknown_response(self):
        manual, auto = FakeAPI("manual1234"), FakeAPI("auto4567")
        with tempfile.TemporaryDirectory() as folder:
            wallet=PaperManual(Path(folder)/"manual.json",manual,auto)
            buy={"id":"manual0001","asset":"BTC/USDT","side":"buy","amount":10}
            manual.lose_response=True
            with self.assertRaises(BrokerError): wallet.order(buy)
            self.assertEqual(manual.posts,1)
            self.assertIsNotNone(wallet.load()["pending"])
            self.assertEqual(wallet.order(buy)["status"],"filled")
            self.assertEqual(manual.posts,1)
            self.assertIsNone(wallet.load()["pending"])
            self.assertAlmostEqual(wallet.state()["positions"]["BTC/USDT"]["qty"],.1)
            with self.assertRaisesRegex(ValueError,"unidades"):
                wallet.order({"id":"manual0002","asset":"BTC/USDT","side":"sell","amount":.2})
            wallet.order({"id":"manual0003","asset":"BTC/USDT","side":"sell","amount":.1})
            self.assertFalse(wallet.state()["positions"])
            self.assertEqual(auto.posts,0)

    def test_distinct_accounts_and_precision(self):
        a=FakeAPI("same")
        with self.assertRaises(BrokerError): distinct(a,a)
        self.assertEqual(broker_symbol("SOL/USDT"),"SOL/USD")
        self.assertEqual(amount_string(.1234567899,9),"0.123456789")

    def test_auto_mirror_only_fresh_events_after_flat_initialization(self):
        manual,auto=FakeAPI("manual"),FakeAPI("auto")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"auto.json",auto,manual)
            mirror.sync(book)
            self.assertEqual(mirror.load()["cursor"],0)
            now=int(time.time()*1000)
            book.state["position"]={"asset":"BTC/USDT"}
            book.state["events"].append({"ts":now,"event":"entry","asset":"BTC/USDT","notional":12})
            mirror.sync(book)
            self.assertEqual(auto.posts,1)
            self.assertEqual(mirror.load()["cursor"],1)
            book.state["position"]=None
            book.state["events"].append({"ts":now,"event":"exit","asset":"BTC/USDT"})
            mirror.sync(book)
            self.assertEqual(auto.posts,2)
            self.assertEqual(mirror.load()["cursor"],2)
            self.assertFalse(auto.holdings)
            self.assertIsNone(mirror.load()["halted"])

    def test_auto_does_not_import_existing_strategy_position(self):
        manual,auto=FakeAPI("manual"),FakeAPI("auto")
        book=Portfolio(GrowthConfig())
        book.state["position"]={"asset":"BTC/USDT"}
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"auto.json",auto,manual)
            mirror.sync(book)
            self.assertIsNone(mirror.load()["cursor"])
            self.assertEqual(auto.posts,0)

    def test_auto_halts_on_existing_broker_position_or_stale_signal(self):
        manual,auto=FakeAPI("manual"),FakeAPI("auto")
        book=Portfolio(GrowthConfig())
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"auto.json"
            auto.holdings["SPY"]={"qty":.1}
            mirror=PaperAuto(path,auto,manual)
            mirror.sync(book)
            self.assertEqual(mirror.load()["halted"],"account_not_empty")
            self.assertEqual(auto.posts,0)
        auto.holdings.clear()
        with tempfile.TemporaryDirectory() as folder:
            mirror=PaperAuto(Path(folder)/"auto.json",auto,manual)
            mirror.sync(book)
            book.state["position"]={"asset":"BTC/USDT"}
            book.state["events"].append({"ts":int(time.time()*1000)-180_000,
                                          "event":"entry","asset":"BTC/USDT","notional":12})
            mirror.sync(book)
            self.assertEqual(mirror.load()["halted"],"stale_strategy_event")
            self.assertEqual(auto.posts,0)

    def test_authenticated_dashboard_routes_paper_orders_to_manual_account_only(self):
        manual,auto=FakeAPI("manual"),FakeAPI("auto")
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/"account.json"
            _save(Portfolio(GrowthConfig()),state)
            server=ThreadingHTTPServer(("127.0.0.1",0),handler_factory(
                state,"a-long-unique-password",paper_accounts=(manual,auto)))
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
                data=json.dumps({"id":"manual0001","asset":"SPY","side":"buy","amount":10}).encode()
                request=urllib.request.Request(base+"/api/alpaca/manual/order",data=data,headers=headers,method="POST")
                with urllib.request.urlopen(request,timeout=3) as response:
                    self.assertEqual(json.load(response)["order"]["status"],"filled")
                self.assertEqual(manual.posts,1)
                self.assertEqual(auto.posts,0)
                self.assertEqual(json.loads(state.read_text())["state"]["cash"],50)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)


if __name__ == "__main__": unittest.main()
