"""Delta public market data and optional read-only credential verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from urllib.parse import quote, urlsplit, unquote

import requests


ENDPOINTS = {
    "india": ("https://api.india.delta.exchange", "wss://public-socket.india.delta.exchange"),
    # Delta's Global docs route public channels to this shared public endpoint.
    "global": ("https://api.delta.exchange", "wss://public-socket.india.delta.exchange"),
}


def epoch_seconds(value) -> float:
    result = float(value)
    if result > 1e14:
        result /= 1_000_000
    elif result > 1e11:
        result /= 1000
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Invalid Delta timestamp")
    return result


def positive(value) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Delta returned a missing/non-positive price or contract value")
    return result


class DeltaClient:
    def __init__(self, asset='gold', credential_scope='read'):
        if asset not in {'gold', 'silver'}:
            raise ValueError('Unknown Delta asset')
        if credential_scope not in {'read', 'live'}:
            raise ValueError('Delta credential scope must be read or live')
        self.asset = asset
        self.credential_scope = credential_scope
        self.region = os.getenv("DELTA_EXCHANGE", "india").strip().lower()
        default_symbol = "PAXGUSDT" if self.region == "global" else "PAXGUSD"
        default_symbol = default_symbol if asset == 'gold' else 'SLVONUSD'
        self.symbol = os.getenv(f"DELTA_{asset.upper()}_SYMBOL", default_symbol).strip().upper()
        self.enabled = os.getenv("DELTA_PAPER_ENABLED", "false").lower() == "true"
        self.live_enabled = os.getenv("DELTA_LIVE_ENABLED", "false").lower() == "true"
        self.base_url, self.ws_url = ENDPOINTS.get(self.region, ("", ""))
        self.proxy = os.getenv("DELTA_PROXY_URL", "").strip()
        if credential_scope == "live":
            self.key = os.getenv("DELTA_LIVE_API_KEY", "").strip()
            self.secret = os.getenv("DELTA_LIVE_API_SECRET", "").strip()
        else:
            self.key = os.getenv("DELTA_API_KEY", "").strip()
            self.secret = os.getenv("DELTA_API_SECRET", "").strip()
        self.session = requests.Session()
        self.session.trust_env = False
        if self.proxy:
            parsed = urlsplit(self.proxy)
            if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
                raise ValueError("DELTA_PROXY_URL must be an HTTP CONNECT proxy URL with a port")
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        self.session.headers.update({"Accept": "application/json", "User-Agent": "algo-delta-paper/1.0"})

    def configuration_error(self):
        if not self.enabled:
            return "Set DELTA_PAPER_ENABLED=true in Railway to start Delta paper data."
        if self.region not in ENDPOINTS:
            return "Set DELTA_EXCHANGE to india or global."
        if not self.symbol or not self.symbol.replace("-", "").isalnum():
            return f"Set DELTA_{self.asset.upper()}_SYMBOL to the exact Delta perpetual symbol."
        return None

    def _signed_headers(self, method: str, path: str, params=None, body: str = ""):
        if not self.key or not self.secret:
            raise ValueError("Both DELTA_API_KEY and DELTA_API_SECRET are required")
        stamp = str(int(time.time()))
        request_path = requests.Request(method, self.base_url + path, params=params).prepare().path_url
        signature = hmac.new(
            self.secret.encode(),
            f"{method}{stamp}{request_path}{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return {"api-key": self.key, "timestamp": stamp, "signature": signature}

    def request(self, method: str, path: str, params=None, payload=None, *, private=False, envelope=False):
        if not self.base_url:
            raise ValueError("Delta exchange is not configured")
        headers = {}
        body = ""
        if private:
            if method.upper() in {"POST", "PUT", "DELETE"} and self.credential_scope != "live":
                raise ValueError("Delta trading endpoints require DELTA_LIVE_API_KEY / DELTA_LIVE_API_SECRET")
            allowed = {
                ("GET", "/v2/wallet/balances"),
                ("GET", "/v2/positions/margined"),
                ("GET", "/v2/orders"),
                ("GET", "/v2/orders/history"),
                ("POST", "/v2/orders"),
                ("PUT", "/v2/orders"),
                ("DELETE", "/v2/orders"),
            }
            if (method.upper(), path) not in allowed:
                raise ValueError("Delta private endpoint is not allowlisted")
            if payload is not None:
                body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            headers = self._signed_headers(method.upper(), path, params=params, body=body)
            headers["Content-Type"] = "application/json"
        try:
            response = self.session.request(
                method.upper(),
                self.base_url + path,
                params=params,
                data=body if body else None,
                headers=headers,
                timeout=(5, 10),
            )
        except requests.RequestException:
            # Requests exceptions can contain proxy credentials. Do not expose them.
            raise RuntimeError("Delta connection failed via configured proxy" if self.proxy else "Delta direct connection failed") from None
        if response.status_code != 200:
            raise RuntimeError(f"Delta HTTP {response.status_code}; check exchange, proxy/IP or API permissions")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise RuntimeError(f"Delta API error: {code}")
        return payload if envelope else payload["result"]

    def get(self, path: str, params=None, *, private=False, envelope=False):
        return self.request("GET", path, params=params, private=private, envelope=envelope)

    def post(self, path: str, payload=None, *, private=True, envelope=False):
        return self.request("POST", path, payload=payload or {}, private=private, envelope=envelope)

    def put(self, path: str, payload=None, *, private=True, envelope=False):
        return self.request("PUT", path, payload=payload or {}, private=private, envelope=envelope)

    def delete(self, path: str, payload=None, *, private=True, envelope=False):
        return self.request("DELETE", path, payload=payload or {}, private=private, envelope=envelope)

    def product(self):
        data = self.get(f"/v2/products/{quote(self.symbol, safe='')}")
        if data.get("symbol") != self.symbol or data.get("contract_type") != "perpetual_futures":
            raise ValueError(f"DELTA_{self.asset.upper()}_SYMBOL must identify a perpetual futures product")
        if data.get("state") != "live" or data.get("trading_status") != "operational":
            raise ValueError("Selected Delta product is not operational")
        if data.get("notional_type") != "vanilla" or data.get("is_quanto"):
            raise ValueError("This paper model supports linear non-quanto contracts only")
        positive(data["contract_value"])
        positive(data["tick_size"])
        gold_text = f"{data.get('description', '')} {data.get('underlying_asset', {}).get('symbol', '')} {self.symbol}".lower()
        expected = ('gold', 'xau', 'paxg') if self.asset == 'gold' else ('silver', 'xag', 'slvon')
        if not any(word in gold_text for word in expected):
            raise ValueError(f"Configured Delta product does not identify a {self.asset} underlying")
        return data

    def candles(self, resolution: str, start: int, end: int):
        return self.get("/v2/history/candles", {"symbol": self.symbol, "resolution": resolution, "start": start, "end": end})

    def recent_trade(self):
        data = self.get(f"/v2/trades/{quote(self.symbol, safe='')}")
        rows = data if isinstance(data, list) else data.get("trades", [])
        if not rows:
            raise ValueError("Delta returned no recent trades")
        row = max(rows, key=lambda r: epoch_seconds(r["timestamp"]))
        return positive(row["price"]), epoch_seconds(row["timestamp"])

    def websocket_options(self):
        if not self.proxy:
            return {"http_no_proxy": ["*"]}
        parsed = urlsplit(self.proxy)
        options = {"http_proxy_host": parsed.hostname, "http_proxy_port": parsed.port, "proxy_type": "http"}
        if parsed.username:
            options["http_proxy_auth"] = (unquote(parsed.username), unquote(parsed.password or ""))
        return options
