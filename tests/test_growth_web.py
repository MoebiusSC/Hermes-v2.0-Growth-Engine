import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from hermes_trading.growth import GrowthConfig, Portfolio
from hermes_trading.growth_run import _save
from hermes_trading.growth_web import handler_factory


class DashboardTests(unittest.TestCase):
    def test_dashboard_auth_and_read_only_state(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "account.json"
            _save(Portfolio(GrowthConfig()), state)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(state, "a-long-unique-password"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/health", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(base + "/api/state", timeout=3)
                self.assertEqual(denied.exception.code, 401)
                key = base64.b64encode(b"admin:a-long-unique-password").decode()
                request = urllib.request.Request(base + "/api/state", headers={"Authorization": f"Basic {key}"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.load(response)
                self.assertEqual(payload["metrics"]["equity"], 50)
                self.assertNotIn("config", payload)
                self.assertIsNone(payload["position"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_manual_order_requires_auth_origin_and_records_paper_only(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "account.json"
            _save(Portfolio(GrowthConfig()), state)
            def fake_quote(asset):
                return {"asset": asset, "price": 100.0, "asof": time.time(),
                        "source": "test", "tradable": True}
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(state, "a-long-unique-password", fake_quote))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                key = base64.b64encode(b"admin:a-long-unique-password").decode()
                body = json.dumps({"id": "manual0001", "asset": "SPY", "side": "buy", "amount": 10}).encode()
                def post(headers):
                    request = urllib.request.Request(base + "/api/manual/order", data=body, method="POST", headers=headers)
                    return urllib.request.urlopen(request, timeout=3)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post({})
                self.assertEqual(denied.exception.code, 401)
                headers = {"Authorization": f"Basic {key}", "Content-Type": "application/json",
                           "X-Hermes-Action": "manual-paper", "Origin": "https://attacker.example"}
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post(headers)
                self.assertEqual(denied.exception.code, 403)
                headers["Origin"] = base
                with post(headers) as response:
                    result = json.load(response)
                self.assertEqual(result["order"]["asset"], "SPY")
                with post(headers) as response:
                    self.assertEqual(json.load(response)["order"], result["order"])
                request = urllib.request.Request(base + "/api/manual/state", headers={"Authorization": f"Basic {key}"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    wallet = json.load(response)
                self.assertEqual(wallet["cash"], 40)
                self.assertEqual(len(wallet["orders"]), 1)
                self.assertEqual(json.loads(state.read_text())["state"]["cash"], 50)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
