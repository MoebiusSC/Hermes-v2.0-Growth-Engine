"""Read-only, password-protected web dashboard alongside the v2 paper worker."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .growth import GrowthConfig, Portfolio
from .growth_run import _load_config, _paper, report
from .manual_paper import MAX_BODY, ManualWallet, OrderError, QuoteError, quote as manual_quote

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("HERMES_GROWTH_STATE", "growth_state/account.json"))
MAX_PASSWORD_BYTES = 512


def _authorized(header: str | None, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:], validate=True)
        if len(raw) > MAX_PASSWORD_BYTES:
            return False
        username, provided = raw.split(b":", 1)
        return hmac.compare_digest(username, b"admin") and hmac.compare_digest(
            provided, password.encode("utf-8"))
    except (ValueError, UnicodeError):
        return False


def snapshot(path: Path) -> dict:
    if not path.exists():
        return {"ready": False, "message": "Esperando el primer ciclo paper"}
    saved = json.loads(path.read_text(encoding="utf-8"))
    raw = saved["config"]
    cfg = GrowthConfig(**{**raw, "assets": tuple(raw["assets"])})
    book = Portfolio(cfg, saved["state"])
    s = book.state
    position = s["position"]
    return {
        "ready": True,
        "updated_at": path.stat().st_mtime,
        "initial_capital": cfg.capital,
        "assets": cfg.assets,
        "metrics": report(book),
        "position": ({k: position[k] for k in ("asset", "entry", "qty", "stop", "target", "regime", "opened_ms")}
                     if position else None),
        "risk": {key: getattr(cfg, key) for key in ("risk_per_trade", "max_exposure", "daily_loss",
                                                   "weekly_loss", "monthly_drawdown")},
        "curve": s["curve"][-3000:],
        "trades": s["trades"][-100:][::-1],
        "events": s["events"][-100:][::-1],
        "last_bar": s["last_bar"],
        "optimizer": s.get("optimizer", {"enabled": False}),
        "alpha": {key: getattr(cfg, key) for key in ("range_rsi", "trend_rsi", "target_r", "stop_atr")},
    }


def handler_factory(state_path: Path, password: str, quote_provider=manual_quote):
    wallet = ManualWallet(state_path.with_name("manual_account.json"), quote_provider)

    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesGrowth/2"

        def _send(self, status: int, content: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="Hermes v2", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/health":
                self._send(200, b'{"status":"ok"}', "application/json; charset=utf-8")
                return
            if not _authorized(self.headers.get("Authorization"), password):
                self._send(401, b"Authentication required", "text/plain; charset=utf-8")
                return
            if path in ("/", "/index.html"):
                self._send(200, (HERE / "growth_dashboard.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/app.js":
                self._send(200, (HERE / "growth_dashboard.js").read_bytes(), "text/javascript; charset=utf-8")
            elif path == "/api/state":
                try:
                    result = snapshot(state_path)
                except (OSError, ValueError, KeyError, TypeError):
                    self._send(503, b'{"error":"Estado no disponible"}', "application/json; charset=utf-8")
                    return
                self._send(200, json.dumps(result, allow_nan=False).encode(), "application/json; charset=utf-8")
            elif path == "/api/manual/state":
                try:
                    result = wallet.state()
                except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
                    self._send(503, b'{"error":"Cuenta manual no disponible"}', "application/json; charset=utf-8")
                    return
                self._json(200, result)
            elif path == "/api/manual/quote":
                params = parse_qs(parsed.query)
                try:
                    if len(params.get("asset", [])) != 1:
                        raise OrderError("Selecciona un activo")
                    self._json(200, wallet.market_quote(params["asset"][0]))
                except OrderError as exc:
                    self._json(400, {"error": str(exc)})
                except (QuoteError, OSError, ValueError):
                    self._json(503, {"error": "Cotización no disponible; orden no registrada"})
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload, allow_nan=False).encode(), "application/json; charset=utf-8")

        def do_POST(self) -> None:
            if not _authorized(self.headers.get("Authorization"), password):
                self._send(401, b"Authentication required", "text/plain; charset=utf-8")
                return
            if urlsplit(self.path).path != "/api/manual/order":
                self._json(404, {"error": "Ruta no encontrada"})
                return
            # Cross-origin forms cannot set this header or send JSON without preflight.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if (self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                    or self.headers.get("X-Hermes-Action") != "manual-paper"
                    or not origin or urlsplit(origin).netloc != host
                    or urlsplit(origin).scheme not in ("https", "http")):
                self._json(403, {"error": "Solicitud no autorizada"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_BODY:
                    raise OrderError("Tamaño de solicitud inválido")
                data = json.loads(self.rfile.read(size))
                self._json(200, {"order": wallet.order(data)})
            except (OrderError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except QuoteError:
                self._json(503, {"error": "Cotización no disponible; orden no registrada"})
            except (OSError, ValueError, KeyError, TypeError):
                self._json(503, {"error": "Cuenta manual no disponible; revise el estado"})

        def log_message(self, fmt: str, *args) -> None:
            # Do not log URL parameters or credentials.
            pass

    return Handler


def main() -> None:
    if os.environ.get("HERMES_TRADING_MODE", "paper").lower() != "paper":
        raise SystemExit("Live execution is not implemented; HERMES_TRADING_MODE must be paper")
    password = os.environ.get("HERMES_DASHBOARD_PASSWORD", "")
    if not 16 <= len(password.encode("utf-8")) <= 256:
        raise SystemExit("Set HERMES_DASHBOARD_PASSWORD to 16–256 UTF-8 bytes")
    port = int(os.environ.get("PORT", "8080"))
    state_path = Path(os.environ.get("HERMES_GROWTH_STATE", str(STATE)))
    cfg = _load_config(Path(os.environ.get("HERMES_GROWTH_CONFIG", "growth.json")))
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_factory(state_path, password))
    threading.Thread(target=server.serve_forever, daemon=True, name="growth-dashboard").start()
    try:
        asyncio.run(_paper(cfg, state_path, once=False))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
