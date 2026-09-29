"""One Alpaca paper account with separate manual and strategy ownership ledgers.

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

from .manual_paper import CRYPTO, OrderError, asset_kind
from .storage import atomic_write

PAPER_URL = "https://paper-api.alpaca.markets"
TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
CRYPTO_MIN = 10.0


class BrokerError(RuntimeError):
    pass


def broker_symbol(asset: str) -> str:
    asset_kind(asset)
    return asset.replace("/USDT", "/USD")


def display(symbol: str) -> str:
    for base in CRYPTO:
        if symbol in (f"{base}USD", f"{base}/USD"):
            return f"{base}/USDT"
    return symbol


def amount_string(value: float, places: int) -> str:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise OrderError("Cantidad inválida")
    result = format(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_DOWN), "f")
    if float(result) <= 0:
        raise OrderError("Cantidad bajo la precisión permitida")
    return result


def quantity_string(value: float, details: dict) -> str:
    step = details.get("min_trade_increment")
    if step is None:
        return amount_string(value, 9)
    try:
        increment = Decimal(str(step))
        amount = Decimal(str(value))
        if not increment.is_finite() or increment <= 0 or not amount.is_finite() or amount <= 0:
            raise ValueError("Precisión inválida")
        result = (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    except (ValueError, ArithmeticError) as exc:
        raise BrokerError("Precisión de activo inválida en Alpaca") from exc
    if result <= 0:
        raise OrderError("Unidades inferiores al incremento mínimo de Alpaca")
    return format(result, "f")


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
    return enabled() and all(os.environ.get(f"HERMES_ALPACA_MANUAL_{part}")
                             for part in ("KEY", "SECRET"))


def from_env() -> PaperAPI:
    if not configured():
        raise BrokerError("Pendiente de configurar la cuenta Alpaca paper")
    key = os.environ.get("HERMES_ALPACA_MANUAL_KEY", "")
    secret = os.environ.get("HERMES_ALPACA_MANUAL_SECRET", "")
    return PaperAPI(key, secret)


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


def ensure_asset(api: PaperAPI, asset: str, side: str) -> dict:
    kind = asset_kind(asset)
    details = api.asset(asset)
    if details.get("status") != "active" or not details.get("tradable"):
        raise BrokerError("Activo no negociable en Alpaca paper")
    if details.get("class") not in (None, "crypto" if kind == "crypto" else "us_equity"):
        raise BrokerError("El símbolo no corresponde al tipo de activo solicitado")
    if kind != "crypto":
        if not details.get("fractionable") or not api.request("GET", "/v2/clock").get("is_open"):
            raise BrokerError("Acción/ETF fraccionado no disponible o mercado cerrado")
    return details


_LOCK = threading.RLock()  # The Railway service runs its HTTP server and worker in one process.


class SharedPaper:
    def __init__(self, path: Path, api: PaperAPI):
        self.path, self.api = Path(path), api

    def load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "account_id": None, "manual": {}, "auto_asset": None, "auto_qty": 0,
            "cursor": None, "skipped_asset": None, "pending": None, "manual_sent": {},
            "halted_manual": None, "halted_auto": None, "last_order": None}

    def save(self, state: dict) -> None:
        atomic_write(self.path, json.dumps(state, indent=2, allow_nan=False))

    def account(self, state: dict) -> dict:
        account = self.api.account()
        if not account.get("id"):
            raise BrokerError("La cuenta paper no tiene identificador")
        if state["account_id"] and state["account_id"] != account["id"]:
            raise BrokerError("Cambió la cuenta paper; revise el historial antes de operar")
        if state["account_id"] is None:
            if self.api.positions() or self.api.orders():
                raise BrokerError("La cuenta paper debe estar vacía antes de conectarla")
            if not 45 <= float(account["equity"]) <= 55:
                raise BrokerError("Configura el saldo inicial de la cuenta paper a 50 USD")
            state["account_id"] = account["id"]
            self.save(state)
        return account

    def reconcile(self, state: dict) -> dict | None:
        pending = state["pending"]
        if not pending:
            return None
        order = self.api.by_client_id(pending["client_id"])
        if order is None:
            if time.time() - pending["created_at"] > 60:
                state["halted_" + pending["owner"]] = "Orden sin confirmar: revisar en Alpaca"
                self.save(state)
            return None
        if order.get("status") in TERMINAL:
            qty = float(order.get("filled_qty") or 0)
            owner, asset = pending["owner"], pending["asset"]
            if order["status"] != "filled" or qty <= 0:
                state["halted_" + owner] = "Orden no ejecutada o parcial: revisar en Alpaca"
                if qty > 0:
                    state["halted_manual"] = state["halted_auto"] = "Orden parcial: revisar en Alpaca"
            else:
                held = owned(self.api.positions(), asset)
                held_qty = float(held["qty"]) if held else 0.
                if owner == "manual":
                    if pending["side"] == "buy" and state["auto_asset"] == asset:
                        state["halted_manual"] = state["halted_auto"] = "Posición superpuesta: revisar en Alpaca"
                    state["manual"][asset] = held_qty
                    if held_qty <= 1e-9:
                        state["manual"].pop(asset, None)
                else:
                    state["auto_asset"] = asset if held_qty > 1e-9 else None
                    state["auto_qty"] = held_qty
                    state["cursor"] = pending["index"] + 1
                    state["last_order"] = order_view(order)
            state["pending"] = None
            self.save(state)
        return order

    def validate(self, state: dict, positions: list[dict]) -> None:
        if state["pending"]:
            return  # A broker fill can be ahead of the pending order's reconciliation.
        if self.api.orders():
            raise BrokerError("Hay una orden abierta en Alpaca; revisar antes de operar")
        expected = dict(state["manual"])
        if state["auto_asset"]:
            if state["auto_asset"] in expected:
                raise BrokerError("El bot y la cartera manual comparten un activo; revisar en Alpaca")
            expected[state["auto_asset"]] = state["auto_qty"]
        actual = {display(p["symbol"]): float(p["qty"]) for p in positions if abs(float(p["qty"])) > 1e-9}
        if actual.keys() != expected.keys() or any(abs(actual[a] - q) > max(1e-8, q * 1e-7)
                                                     for a, q in expected.items()):
            raise BrokerError("Posiciones de Alpaca difieren del registro Hermes; revisar en Alpaca")


class PaperManual(SharedPaper):
    def availability(self, asset: str) -> dict:
        kind = asset_kind(asset)
        try:
            details = self.api.asset(asset)
        except BrokerError as exc:
            if "HTTP 404:" in str(exc):
                return {"asset": asset, "available": False, "reason": "No listado en Alpaca paper"}
            raise
        available = (details.get("status") == "active" and bool(details.get("tradable"))
                     and details.get("class") in (None, "crypto" if kind == "crypto" else "us_equity")
                     and (kind == "crypto" or bool(details.get("fractionable"))))
        return {"asset": asset, "available": available,
                "reason": None if available else "No negociable en Alpaca paper con órdenes fraccionarias"}

    def state(self) -> dict:
        with _LOCK:
            s = self.load()
            account = self.account(s)
            pending = self.reconcile(s)
            broker_positions = self.api.positions()
            self.validate(s, broker_positions)
            positions, marks = {}, {}
            for p in broker_positions:
                asset = display(p["symbol"])
                if asset in s["manual"]:
                    positions[asset] = {"qty": float(p["qty"]), "cost": float(p["cost_basis"])}
                    marks[asset] = {"price": float(p["current_price"]), "asof": time.time(), "source": "Alpaca paper"}
            orders = [order_view(o) for o in self.api.orders("all")
                      if str(o.get("client_order_id", "")).startswith("hv2m-")]
            orders.sort(key=lambda o: o["ts"])
            return {"ready": True, "cash": float(account["cash"]), "equity": float(account["equity"]),
                    "positions": positions, "marks": marks, "orders": orders,
                    "pending": order_view(pending) if s["pending"] and pending else None,
                    "halted": s["halted_manual"], "account_suffix": str(account.get("account_number", ""))[-4:]}

    def order(self, data: dict) -> dict:
        if not isinstance(data, dict) or set(data) != {"id", "asset", "side", "amount"}:
            raise OrderError("Campos de orden inválidos")
        id_, asset, side = data["id"], data["asset"], data["side"]
        if not isinstance(id_, str) or not id_.isascii() or not id_.isalnum() or not 8 <= len(id_) <= 50:
            raise OrderError("Identificador inválido")
        if side not in ("buy", "sell") or isinstance(data["amount"], bool):
            raise OrderError("Activo u operación inválida")
        kind = asset_kind(asset)
        try:
            amount = float(data["amount"])
        except (TypeError, ValueError) as exc:
            raise OrderError("Cantidad inválida") from exc
        if not math.isfinite(amount) or amount <= 0:
            raise OrderError("Cantidad inválida")
        client_id = "hv2m-" + id_
        with _LOCK:
            s = self.load()
            account = self.account(s)
            self.reconcile(s)
            sent = s["manual_sent"].get(client_id)
            if sent:
                if sent != {"asset": asset, "side": side, "amount": amount}:
                    raise OrderError("Identificador ya usado para otra operación")
                existing = self.api.by_client_id(client_id)
                if existing:
                    return order_view(existing)
                raise BrokerError("Orden pendiente sin respuesta; revisar en Alpaca")
            if self.api.by_client_id(client_id):
                raise OrderError("Identificador ya usado en Alpaca")
            if s["pending"]:
                raise BrokerError("Existe una orden pendiente de reconciliar")
            if s["halted_manual"]:
                raise BrokerError(s["halted_manual"])
            if s["auto_asset"] == asset:
                raise BrokerError("Este activo pertenece al bot; elige otro o espera su salida")
            self.validate(s, self.api.positions())
            details = ensure_asset(self.api, asset, side)
            if side == "buy":
                minimum = CRYPTO_MIN if kind == "crypto" else 5.0
                if amount < minimum or amount > float(account["cash"]) * .98:
                    raise OrderError("Monto fuera del saldo en efectivo (reserva 2 %) o bajo el mínimo")
                size = {"notional": amount_string(amount, 2)}
            else:
                available = float(s["manual"].get(asset, 0))
                if amount > available + 1e-10 or available <= 0:
                    raise OrderError("No hay suficientes unidades manuales; no se permiten shorts")
                size = {"qty": quantity_string(min(amount, available), details)}
            payload = {"symbol": broker_symbol(asset), "side": side, "type": "market",
                       "time_in_force": "gtc" if kind == "crypto" else "day",
                       "client_order_id": client_id, **size}
            s["pending"] = {"owner": "manual", "client_id": client_id, "payload": payload,
                            "asset": asset, "side": side, "created_at": time.time()}
            s["manual_sent"][client_id] = {"asset": asset, "side": side, "amount": amount}
            self.save(s)  # Persist intent before any broker POST.
            order = self.api.submit(payload)
            if order.get("client_order_id") != client_id:
                raise BrokerError("Respuesta inesperada; reconcilia en Alpaca")
            self.reconcile(s)
            return order_view(order)


class PaperAuto(SharedPaper):
    def can_expand_assets(self, book, new_assets: tuple[str, ...]) -> bool:
        """Read-only broker and ownership gate for a larger automatic universe."""
        with _LOCK:
            s = self.load()
            if (book.state["position"] or book.state["pending"] or s["pending"] or
                    s["auto_asset"] or abs(float(s["auto_qty"])) > 1e-9 or
                    s["halted_auto"] or s["cursor"] != len(book.state["events"])):
                return False
            self.account(s)
            self.validate(s, self.api.positions())
            for asset in new_assets:
                ensure_asset(self.api, asset, "buy")
            return True

    def recover_transport_halt_if_flat(self, book) -> bool:
        """Clear only a stale transport warning after checking the paper broker."""
        if book.state["position"] or book.state["pending"]:
            return False
        with _LOCK:
            s = self.load()
            if s["pending"] or s["auto_asset"] or abs(float(s["auto_qty"])) > 1e-9:
                return False
            if s["cursor"] is not None and s["cursor"] != len(book.state["events"]):
                return False
            warning = s["halted_auto"]
            if warning not in (None, "Alpaca paper no responde; orden sin confirmar"):
                return False
            self.account(s)  # Confirms identity and account availability.
            positions = self.api.positions()
            self.validate(s, positions)  # Includes open orders and manual ownership.
            if warning:
                previous = s.get("last_order")
                if not previous or previous.get("status") != "filled" or not previous.get("id"):
                    return False
                order = self.api.by_client_id(previous["id"])
                if not order or order.get("status") != "filled":
                    return False
                if abs(float(order.get("filled_qty") or 0) - float(previous["qty"])) > 1e-8:
                    return False
                s["halted_auto"] = None
                s["transport_recovered_at"] = time.time()
                self.save(s)
            return True

    def state(self) -> dict:
        with _LOCK:
            s = self.load()
            account = self.account(s)
            self.reconcile(s)
            positions = self.api.positions()
            self.validate(s, positions)
            held = owned(positions, s["auto_asset"]) if s["auto_asset"] else None
            return {"ready": True, "cash": float(account["cash"]), "equity": float(account["equity"]),
                    "account_suffix": str(account.get("account_number", ""))[-4:],
                    "positions": [{"asset": s["auto_asset"], "qty": float(held["qty"]),
                                   "value": float(held["market_value"])}] if held else [],
                    "cursor": s["cursor"], "pending": s["pending"], "halted": s["halted_auto"],
                    "skipped_asset": s["skipped_asset"], "last_order": s["last_order"]}

    def sync(self, book) -> None:
        with _LOCK:
            s = self.load()
            account = self.account(s)
            self.reconcile(s)
            if s["halted_auto"] or s["pending"]:
                return
            positions = self.api.positions()
            try:
                self.validate(s, positions)
            except BrokerError as exc:
                s["halted_auto"] = str(exc)
                self.save(s)
                return
            if s["cursor"] is None:
                if book.state["position"] or book.state["pending"]:
                    return  # Start mirroring only when the research strategy is flat.
                s["cursor"] = len(book.state["events"])
                self.save(s)
                return
            for index in range(s["cursor"], len(book.state["events"])):
                event = book.state["events"][index]
                s["cursor"] = index + 1
                if event.get("event") not in ("entry", "exit"):
                    continue
                if abs(time.time() * 1000 - event["ts"]) > 120_000:
                    s["halted_auto"] = "stale_strategy_event"
                    break
                asset = event["asset"]
                side = "buy" if event["event"] == "entry" else "sell"
                if side == "buy":
                    if s["auto_asset"] or not book.state["position"] or book.state["position"]["asset"] != asset:
                        s["halted_auto"] = "strategy_position_mismatch"
                        break
                    if asset in s["manual"]:
                        s["skipped_asset"] = asset
                        continue  # Never net the bot and manual units in the same broker symbol.
                    ensure_asset(self.api, asset, side)
                    notional = min(float(event["notional"]), float(account["cash"]) * .98,
                                   float(account["equity"]) * book.cfg.max_exposure)
                    if notional < CRYPTO_MIN:
                        s["halted_auto"] = "alpaca_min_order_10_usd"
                        break
                    size = {"notional": amount_string(notional, 2)}
                elif s["skipped_asset"] == asset:
                    s["skipped_asset"] = None
                    continue
                else:
                    if s["auto_asset"] != asset:
                        s["halted_auto"] = "paper_position_missing"
                        break
                    held = owned(positions, asset)
                    if not held or abs(float(held["qty"]) - s["auto_qty"]) > 1e-8:
                        s["halted_auto"] = "paper_position_mismatch"
                        break
                    size = {"qty": quantity_string(float(held["qty"]), self.api.asset(asset))}
                client_id = "hv2a-" + f"{index:012x}"
                payload = {"symbol": broker_symbol(asset), "side": side, "type": "market",
                           "time_in_force": "gtc", "client_order_id": client_id, **size}
                s["pending"] = {"owner": "auto", "client_id": client_id, "payload": payload,
                                "asset": asset, "side": side, "index": index, "created_at": time.time()}
                self.save(s)
                order = self.api.submit(payload)
                if order.get("client_order_id") != client_id:
                    raise BrokerError("Respuesta inesperada; reconcilia en Alpaca")
                self.reconcile(s)
                return
            self.save(s)
