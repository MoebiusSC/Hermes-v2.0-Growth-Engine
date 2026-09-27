"""Independent, persistent paper wallet for user-initiated crypto and ETF orders.

No exchange or broker order API is imported here. A quote is always obtained on the
server before recording an order; browser-supplied prices are never accepted.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .storage import atomic_write

ASSETS = {"BTC/USDT": "crypto", "ETH/USDT": "crypto", "SOL/USDT": "crypto",
          "SPY": "ETF", "VOO": "ETF", "QQQ": "ETF"}
KRAKEN = {"BTC/USDT": "XBTUSDT", "ETH/USDT": "ETHUSDT", "SOL/USDT": "SOLUSDT"}
INITIAL_CASH = 50.0
MIN_BUY = 5.0
MAX_BODY = 2048


class OrderError(ValueError):
    pass


class QuoteError(RuntimeError):
    pass


def quote(asset: str) -> dict:
    """Public market data, possibly delayed. The latest timestamp controls eligibility."""
    if asset not in ASSETS:
        raise OrderError("Activo no disponible")
    now = time.time()
    try:
        if ASSETS[asset] == "crypto":
            with httpx.Client(timeout=8) as client:
                response = client.get("https://api.kraken.com/0/public/OHLC",
                                      params={"pair": KRAKEN[asset], "interval": 1})
                response.raise_for_status()
                data = response.json()
            if data.get("error"):
                raise QuoteError("Proveedor de cripto rechazó la cotización")
            rows = next(v for k, v in data["result"].items() if k != "last")
            latest = rows[-1]
            price, asof, source = float(latest[4]), float(latest[0]), "Kraken · vela 1 min"
            tradable = 0 <= now - asof <= 120
        else:
            import yfinance as yf

            bars = yf.Ticker(asset).history(period="1d", interval="1m", auto_adjust=False)
            if bars.empty:
                raise QuoteError("Sin cotización intradía del ETF")
            price = float(bars["Close"].iloc[-1])
            asof = float(bars.index[-1].timestamp())
            source = "Yahoo Finance · vela 1 min (puede tener retraso)"
            local = dt.datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
            session = local.weekday() < 5 and dt.time(9, 30) <= local.time() < dt.time(16)
            tradable = session and 0 <= now - asof <= 20 * 60
        if not math.isfinite(price) or price <= 0 or not math.isfinite(asof) or asof > now + 5:
            raise QuoteError("Cotización inválida")
        return {"asset": asset, "price": price, "asof": asof, "source": source,
                "tradable": tradable}
    except Exception as exc:
        if isinstance(exc, (OrderError, QuoteError)):
            raise
        raise QuoteError("No se pudo obtener una cotización fiable") from exc


class ManualWallet:
    def __init__(self, path: Path, quote_provider=quote):
        self.path = Path(path)
        self.quote_provider = quote_provider
        self.lock = threading.RLock()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"cash": INITIAL_CASH, "positions": {}, "orders": [], "marks": {}}

    def _save(self, state: dict) -> None:
        atomic_write(self.path, json.dumps(state, allow_nan=False, indent=2))

    def state(self) -> dict:
        with self.lock:
            s = self._load()
            equity = s["cash"] + sum(p["qty"] * s["marks"].get(a, {"price": p["cost"] / p["qty"]})["price"]
                                     for a, p in s["positions"].items())
            return {**s, "equity": equity, "initial_cash": INITIAL_CASH,
                    "assets": ASSETS, "min_buy": MIN_BUY}

    def market_quote(self, asset: str) -> dict:
        q = self.quote_provider(asset)
        with self.lock:
            s = self._load()
            if asset in s["positions"]:
                s["marks"][asset] = {"price": q["price"], "asof": q["asof"], "source": q["source"]}
                self._save(s)
        return q

    def order(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) != {"id", "asset", "side", "amount"}:
            raise OrderError("Campos de orden inválidos")
        order_id, asset, side = data["id"], data["asset"], data["side"]
        if not isinstance(order_id, str) or not 8 <= len(order_id) <= 80 or not order_id.isascii() or not order_id.isalnum():
            raise OrderError("Identificador de orden inválido")
        if not isinstance(asset, str) or asset not in ASSETS or side not in ("buy", "sell"):
            raise OrderError("Activo o tipo de orden inválido")
        if isinstance(data["amount"], bool):
            raise OrderError("Cantidad inválida")
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError) as exc:
            raise OrderError("Cantidad inválida") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise OrderError("Cantidad inválida")

        with self.lock:
            s = self._load()
            for previous in s["orders"]:
                if previous["id"] == order_id:
                    return previous  # response lost after a successful, persisted order
            if side == "buy" and (amount < MIN_BUY or amount > s["cash"] + 1e-8):
                raise OrderError("Compra mínima 5 USD; comprueba el saldo disponible")
            position = s["positions"].get(asset)
            if side == "sell" and (not position or amount > position["qty"] + 1e-10):
                raise OrderError("No tienes suficientes unidades para vender")
            q = self.quote_provider(asset)
            if not q["tradable"]:
                raise OrderError("Mercado cerrado o cotización demasiado antigua; orden no registrada")
            fee = 0.001 if ASSETS[asset] == "crypto" else 0.0
            impact = 0.0004 if ASSETS[asset] == "crypto" else 0.0005
            fill = q["price"] * (1 + impact if side == "buy" else 1 - impact)
            if side == "buy":
                qty = amount / (fill * (1 + fee))
                s["cash"] = max(0.0, s["cash"] - amount)
                position = s["positions"].setdefault(asset, {"qty": 0.0, "cost": 0.0})
                position["qty"] += qty
                position["cost"] += amount
                fee_paid, pnl = qty * fill * fee, None
            else:
                qty = min(amount, position["qty"])
                proceeds = qty * fill * (1 - fee)
                basis = position["cost"] * qty / position["qty"]
                position["qty"] -= qty
                position["cost"] -= basis
                if position["qty"] < 1e-10:
                    del s["positions"][asset]
                s["cash"] += proceeds
                fee_paid, pnl = qty * fill * fee, proceeds - basis
            record = {"id": order_id, "ts": time.time(), "asset": asset, "side": side,
                      "qty": qty, "price": fill, "fee": fee_paid, "cash_change": -amount if side == "buy" else proceeds,
                      "pnl": pnl, "quote_asof": q["asof"], "source": q["source"]}
            s["orders"].append(record)
            s["marks"][asset] = {"price": q["price"], "asof": q["asof"], "source": q["source"]}
            self._save(s)
            return record
