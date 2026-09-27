"""Separate Alpaca paper accounts for manual orders and the v2 strategy mirror.

Only the fixed paper API URL can receive orders. The strategy's research ledger is
kept intact and Alpaca activity is journaled independently on the state volume.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import quote

import httpx

from .manual_paper import ASSETS, OrderError
from .storage import atomic_write

PAPER_URL = "https://paper-api.alpaca.markets"
TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
CRYPTO_MIN = 10.0


class BrokerError(RuntimeError):
    pass


def broker_symbol(asset: str) -> str:
    if asset not in ASSETS:
        raise OrderError("Activo no disponible")
    return asset.replace("/USDT", "/USD")


def display(symbol: str) -> str:
    if symbol in ("BTCUSD", "ETHUSD", "SOLUSD"):
        return symbol[:-3] + "/USDT"
    return symbol.replace("/USD", "/USDT") if symbol.endswith("/USD") else symbol


def amount_string(value: float, places: int) -> str:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise OrderError("Cantidad inválida")
    result = format(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_DOWN), "f")
    if float(result) <= 0:
        raise OrderError("Cantidad bajo la precisión permitida")
    return result


class PaperAPI:
    def __init__(self, key: str, secret: str, transport=None):
        if not key or not secret:
            raise BrokerError("Faltan claves de la cuenta Alpaca paper")
        self.http = httpx.Client(base_url=PAPER_URL, timeout=12, transport=transport,
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError("Alpaca paper no responde; orden sin confirmar") from exc
        if response.status_code >= 400:
            try:
                reason = str(response.json().get("message", "error"))[:120]
            except ValueError:
                reason = "error"
            raise BrokerError(f"Alpaca paper HTTP {response.status_code}: {reason}")
        return response.json() if response.content else None

    def account(self) -> dict:
        a = self.request("GET", "/v2/account")
        if a.get("status") != "ACTIVE" or a.get("trading_blocked") or a.get("account_blocked"):
            raise BrokerError("La cuenta paper no permite operar")
        return a

    def positions(self) -> list[dict]:
        return self.request("GET", "/v2/positions")

    def orders(self, status="open") -> list[dict]:
        return self.request("GET", "/v2/orders", params={"status": status, "limit": 100, "direction": "desc"})

    def by_client_id(self, client_id: str) -> dict | None:
        try:
            return self.request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_id})
        except BrokerError as exc:
            if "HTTP 404:" in str(exc):
                return None
            raise

    def asset(self, asset: str) -> dict:
        return self.request("GET", f"/v2/assets/{quote(broker_symbol(asset), safe='')}")

    def submit(self, payload: dict) -> dict:
        return self.request("POST", "/v2/orders", json=payload)

    def cancel(self, order_id: str) -> None:
        self.request("DELETE", f"/v2/orders/{order_id}")


def enabled() -> bool:
    return os.environ.get("HERMES_ALPACA_PAPER", "off").lower() == "on"


def configured() -> bool:
    return enabled() and all(os.environ.get(f"HERMES_ALPACA_{role}_{part}")
                             for role in ("MANUAL", "AUTO") for part in ("KEY", "SECRET"))


def from_env(role: str) -> PaperAPI:
    if not configured():
        raise BrokerError("Pendiente de configurar dos cuentas Alpaca paper")
    key = os.environ.get(f"HERMES_ALPACA_{role.upper()}_KEY", "")
    secret = os.environ.get(f"HERMES_ALPACA_{role.upper()}_SECRET", "")
    return PaperAPI(key, secret)


def distinct(manual: PaperAPI, auto: PaperAPI) -> tuple[dict, dict]:
    a, b = manual.account(), auto.account()
    if not a.get("id") or not b.get("id") or a["id"] == b["id"]:
        raise BrokerError("Las cuentas paper manual y automática deben ser distintas")
    return a, b


def owned(positions: list[dict], asset: str) -> dict | None:
    name = broker_symbol(asset).replace("/", "")
    return next((p for p in positions if str(p.get("symbol", "")).replace("/", "") == name), None)


def significant(positions: list[dict]) -> list[dict]:
    return [p for p in positions if abs(float(p["qty"]) * float(p["current_price"])) > .01]


def order_view(o: dict) -> dict:
    ts = o.get("filled_at") or o.get("submitted_at") or ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        when = 0
    return {"id": o.get("client_order_id"), "asset": display(o.get("symbol", "")),
            "side": o.get("side"), "qty": float(o.get("filled_qty") or 0),
            "price": float(o.get("filled_avg_price") or 0), "pnl": None,
            "status": o.get("status"), "ts": when}


def ensure_asset(api: PaperAPI, asset: str, side: str) -> None:
    details = api.asset(asset)
    if details.get("status") != "active" or not details.get("tradable"):
        raise BrokerError("Activo no negociable en Alpaca paper")
    if ASSETS[asset] == "ETF":
        if not details.get("fractionable") or not api.request("GET", "/v2/clock").get("is_open"):
            raise BrokerError("ETF fraccionado no disponible o mercado cerrado")


class PaperManual:
    def __init__(self, path: Path, api: PaperAPI, other: PaperAPI):
        self.path, self.api, self.other = Path(path), api, other
        self.lock = threading.RLock()

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {
            "account_id": None, "pending": None, "halted": None}

    def save(self, s: dict) -> None:
        atomic_write(self.path, json.dumps(s, indent=2))

    def account(self, s: dict) -> dict:
        account, _ = distinct(self.api, self.other)
        if s["account_id"] and s["account_id"] != account["id"]:
            raise BrokerError("Cambió la cuenta paper manual; revise el historial")
        if s["account_id"] is None:
            if self.api.positions() or self.api.orders():
                raise BrokerError("La cuenta manual debe estar vacía antes de conectarla")
            if not 45 <= float(account["equity"]) <= 55:
                raise BrokerError("Configura el saldo inicial de la cuenta manual paper a 50 USD")
            s["account_id"] = account["id"]
            self.save(s)
        return account

    def reconcile(self, s: dict) -> dict | None:
        pending = s.get("pending")
        if not pending:
            return None
        order = self.api.by_client_id(pending["client_id"])
        if order and order.get("status") in TERMINAL:
            if order["status"] != "filled" and float(order.get("filled_qty") or 0) > 0:
                s["halted"] = "Orden parcial: revisar en Alpaca antes de continuar"
            s["pending"] = None
            self.save(s)
        return order

    def state(self) -> dict:
        with self.lock:
            s = self.load()
            account = self.account(s)
            pending = self.reconcile(s)
            positions, marks = {}, {}
            for p in self.api.positions():
                asset = display(p["symbol"])
                if asset not in ASSETS:
                    s["halted"] = "Activo ajeno al panel en la cuenta dedicada"
                    self.save(s)
                    continue
                positions[asset] = {"qty": float(p["qty"]), "cost": float(p["cost_basis"])}
                marks[asset] = {"price": float(p["current_price"]), "asof": time.time(), "source": "Alpaca paper"}
            orders = [order_view(o) for o in self.api.orders("all")
                      if str(o.get("client_order_id", "")).startswith("hv2m-")]
            orders.sort(key=lambda o: o["ts"])
            return {"ready": True, "cash": float(account["cash"]), "equity": float(account["equity"]),
                    "positions": positions, "marks": marks, "orders": orders,
                    "pending": order_view(pending) if s.get("pending") and pending else None,
                    "halted": s.get("halted"), "account_suffix": str(account.get("account_number", ""))[-4:]}

    def order(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) != {"id", "asset", "side", "amount"}:
            raise OrderError("Campos de orden inválidos")
        id_, asset, side = data["id"], data["asset"], data["side"]
        if not isinstance(id_, str) or not id_.isascii() or not id_.isalnum() or not 8 <= len(id_) <= 50:
            raise OrderError("Identificador inválido")
        if not isinstance(asset, str) or asset not in ASSETS or side not in ("buy", "sell") or isinstance(data["amount"], bool):
            raise OrderError("Activo u operación inválida")
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError) as exc:
            raise OrderError("Cantidad inválida") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise OrderError("Cantidad inválida")
        client_id = "hv2m-" + id_
        with self.lock:
            s = self.load()
            account = self.account(s)
            existing = self.api.by_client_id(client_id)
            if existing:
                if existing.get("symbol", "").replace("/", "") != broker_symbol(asset).replace("/", "") or existing.get("side") != side:
                    raise OrderError("Identificador ya usado para otra operación")
                self.reconcile(s)
                return order_view(existing)
            if s["pending"] and s["pending"]["client_id"] != client_id:
                raise BrokerError("Existe una orden pendiente de reconciliar")
            if s["halted"]:
                raise BrokerError(s["halted"])
            ensure_asset(self.api, asset, side)
            if self.api.orders():
                raise BrokerError("Resuelve las órdenes abiertas en Alpaca antes de continuar")
            if side == "buy":
                minimum = CRYPTO_MIN if ASSETS[asset] == "crypto" else 5.0
                if amount < minimum or amount > float(account["cash"]) * .98:
                    raise OrderError("Monto fuera del saldo en efectivo (reserva 2 %) o bajo el mínimo")
                size = {"notional": amount_string(amount, 2)}
            else:
                position = owned(self.api.positions(), asset)
                if not position or amount > float(position["qty"]) + 1e-10:
                    raise OrderError("No hay suficientes unidades; no se permiten shorts")
                size = {"qty": amount_string(min(amount, float(position["qty"])), 9)}
            payload = {"symbol": broker_symbol(asset), "side": side, "type": "market",
                       "time_in_force": "gtc" if ASSETS[asset] == "crypto" else "day",
                       "client_order_id": client_id, **size}
            if s["pending"] and (s["pending"]["payload"] != payload):
                raise OrderError("Identificador pendiente corresponde a otra orden")
            s["pending"] = {"client_id": client_id, "payload": payload}
            self.save(s)  # persist intent before any broker POST
            order = self.api.submit(payload)
            if order.get("client_order_id") != client_id:
                raise BrokerError("Respuesta inesperada; reconcilia en Alpaca")
            self.reconcile(s)
            return order_view(order)


class PaperAuto:
    def __init__(self, path: Path, api: PaperAPI, other: PaperAPI):
        self.path, self.api, self.other = Path(path), api, other

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {
            "account_id": None, "cursor": None, "pending": None, "halted": None, "last_order": None}

    def save(self, s: dict) -> None:
        atomic_write(self.path, json.dumps(s, indent=2))

    def state(self) -> dict:
        s = self.load()
        _, account = distinct(self.other, self.api)
        if s["account_id"] and s["account_id"] != account["id"]:
            raise BrokerError("Cambió la cuenta paper del bot")
        return {**s, "ready": True, "cash": float(account["cash"]),
                "equity": float(account["equity"]), "account_suffix": str(account.get("account_number", ""))[-4:],
                "positions": [{"asset": display(p["symbol"]), "qty": float(p["qty"]),
                               "value": float(p["market_value"])} for p in self.api.positions()]}

    def sync(self, book) -> None:
        s = self.load()
        _, account = distinct(self.other, self.api)
        if s["account_id"] and s["account_id"] != account["id"]:
            s["halted"] = "account_changed"
        if s["halted"]:
            self.save(s)
            return
        if s["cursor"] is None:
            if book.state["position"] or book.state["pending"]:
                return  # first mirror only while the strategy is flat
            if self.api.positions() or self.api.orders():
                s["halted"] = "account_not_empty"
            elif not 45 <= float(account["equity"]) <= 55:
                s["halted"] = "set_paper_balance_to_50_usd"
            else:
                s["account_id"], s["cursor"] = account["id"], len(book.state["events"])
            self.save(s)
            return
        pending = s["pending"]
        if pending:
            order = self.api.by_client_id(pending["client_id"])
            if order is None:
                if time.time() - pending["created_at"] > 60:
                    s["halted"] = "paper_order_unconfirmed"
                    self.save(s)
                    return
                order = self.api.submit(pending["payload"])
            if order["status"] in TERMINAL:
                if order["status"] != "filled" or float(order.get("filled_qty") or 0) <= 0:
                    s["halted"] = "paper_order_not_filled"
                else:
                    held = owned(self.api.positions(), pending["asset"])
                    exists = bool(held and float(held["qty"]) * float(held["current_price"]) > .01)
                    if exists != (pending["side"] == "buy"):
                        s["halted"] = "paper_position_mismatch"
                s["last_order"] = order_view(order)
                s["cursor"] = pending["index"] + 1
                s["pending"] = None
                self.save(s)
            elif time.time() - pending["created_at"] > 60:
                # GTC crypto orders must not remain open after the strategy moves on.
                if order.get("id"):
                    self.api.cancel(order["id"])
                s["halted"] = "paper_order_timeout_check_alpaca"
                self.save(s)
            return
        if self.api.orders():
            s["halted"] = "unexpected_open_order"
            self.save(s)
            return
        for index in range(s["cursor"], len(book.state["events"])):
            event = book.state["events"][index]
            s["cursor"] = index + 1
            if event.get("event") not in ("entry", "exit"):
                continue
            if abs(time.time() * 1000 - event["ts"]) > 120_000:
                s["halted"] = "stale_strategy_event"
                break
            asset = event["asset"]
            side = "buy" if event["event"] == "entry" else "sell"
            positions = self.api.positions()
            if side == "buy":
                if significant(positions) or not book.state["position"] or book.state["position"]["asset"] != asset:
                    s["halted"] = "strategy_position_mismatch"
                    break
                ensure_asset(self.api, asset, side)
                notional = min(float(event["notional"]), float(account["cash"]) * .98,
                               float(account["equity"]) * book.cfg.max_exposure)
                if notional < CRYPTO_MIN:
                    s["halted"] = "alpaca_min_order_10_usd"
                    break
                size = {"notional": amount_string(notional, 2)}
            else:
                held = owned(positions, asset)
                if not held:
                    s["halted"] = "paper_position_missing"
                    break
                size = {"qty": amount_string(float(held["qty"]), 9)}
            client_id = "hv2a-" + f"{index:012x}"
            payload = {"symbol": broker_symbol(asset), "side": side, "type": "market",
                       "time_in_force": "gtc", "client_order_id": client_id, **size}
            s["pending"] = {"client_id": client_id, "payload": payload, "asset": asset,
                            "side": side, "index": index, "created_at": time.time()}
            self.save(s)
            self.sync(book)
            return
        self.save(s)
