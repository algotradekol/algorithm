"""Delta public market data and optional read-only credential verification."""
from __future__ import annotations

import hashlib
import hmac
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
    def __init__(self):
        self.region = os.getenv("DELTA_EXCHANGE", "india").strip().lower()
        default_symbol = "PAXGUSDT" if self.region == "global" else "PAXGUSD"
        self.symbol = os.getenv("DELTA_GOLD_SYMBOL", default_symbol).strip().upper()
        self.enabled = os.getenv("DELTA_PAPER_ENABLED", "false").lower() == "true"
        self.base_url, self.ws_url = ENDPOINTS.get(self.region, ("", ""))
        self.proxy = os.getenv("DELTA_PROXY_URL", "").strip()
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
            return "Set DELTA_GOLD_SYMBOL to the exact Delta gold perpetual symbol."
        return None

    def get(self, path: str, params=None, *, private=False, envelope=False):
        if not self.base_url:
            raise ValueError("Delta exchange is not configured")
        headers = {}
        if private:
            if path not in {"/v2/wallet/balances", "/v2/positions/margined", "/v2/orders", "/v2/orders/history"}:
                raise ValueError("Only allowlisted read-only Delta account endpoints are supported")
            if not self.key or not self.secret:
                raise ValueError("Both DELTA_API_KEY and DELTA_API_SECRET are required")
            stamp = str(int(time.time()))
            # Sign the exact encoded query that requests will send, including '?'.
            request_path = requests.Request("GET", self.base_url + path, params=params).prepare().path_url
            signature = hmac.new(self.secret.encode(), f"GET{stamp}{request_path}".encode(), hashlib.sha256).hexdigest()
            headers = {"api-key": self.key, "timestamp": stamp, "signature": signature}
        try:
            response = self.session.get(self.base_url + path, params=params, headers=headers, timeout=(5, 10))
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

    def product(self):
        data = self.get(f"/v2/products/{quote(self.symbol, safe='')}")
        if data.get("symbol") != self.symbol or data.get("contract_type") != "perpetual_futures":
            raise ValueError("DELTA_GOLD_SYMBOL must identify a perpetual futures product")
        if data.get("state") != "live" or data.get("trading_status") != "operational":
            raise ValueError("Selected Delta product is not operational")
        if data.get("notional_type") != "vanilla" or data.get("is_quanto"):
            raise ValueError("This paper model supports linear non-quanto gold contracts only")
        positive(data["contract_value"])
        positive(data["tick_size"])
        gold_text = f"{data.get('description', '')} {data.get('underlying_asset', {}).get('symbol', '')} {self.symbol}".lower()
        if not any(word in gold_text for word in ("gold", "xau", "paxg")):
            raise ValueError("Configured Delta product does not identify a gold underlying")
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
