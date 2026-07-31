"""
fyers_client.py — thin wrapper around Fyers' live WebSocket and
historical candle REST API, used by engine.py.
"""
import datetime
import threading
import time

try:
    from fyers_apiv3 import fyersModel
    from fyers_apiv3.FyersWebsocket import data_ws
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - keeps /health alive if SDK is missing
    fyersModel = None
    data_ws = None
    HTTPAdapter = None
    Retry = None

try:  # pragma: no cover - compatibility shim for older FYERS SDK retries
    from urllib3.util.retry import Retry as _Urllib3Retry
except Exception:  # pragma: no cover
    _Urllib3Retry = None


def _patch_retry_compatibility() -> None:
    """Allow older FYERS SDK code to keep using method_whitelist.

    urllib3 2.x renamed Retry(method_whitelist=...) to
    Retry(allowed_methods=...). The FYERS SDK still passes the old keyword in
    some environments, which breaks funds/login/profile calls before we even
    get a response. We translate the legacy kwarg so both versions work.
    """
    if _Urllib3Retry is None:
        return
    original_init = _Urllib3Retry.__init__
    if getattr(original_init, "_codex_retry_compat", False):
        return

    def patched_init(self, *args, **kwargs):
        method_whitelist = kwargs.pop("method_whitelist", None)
        if method_whitelist is not None and "allowed_methods" not in kwargs:
            kwargs["allowed_methods"] = method_whitelist
        return original_init(self, *args, **kwargs)

    patched_init._codex_retry_compat = True  # type: ignore[attr-defined]
    _Urllib3Retry.__init__ = patched_init


_patch_retry_compatibility()

from .audit_log import audit_log
from .runtime_mode import get_active_broker_key, get_fyers_config, get_runtime_trading_mode
from .timezone import IST
from .fyers_auth import get_stored_access_token, get_stored_token_row

RECENT_LOGIN_GRACE_SECONDS = 180


def get_fyers_model(mode: str | None = None, *, use_proxy: bool = True):
    if fyersModel is None:
        raise RuntimeError(
            "Fyers SDK is not installed in this environment. "
            "Install the backend requirements before using live Fyers features."
        )
    effective_mode = mode or get_runtime_trading_mode()
    token = get_stored_access_token(effective_mode)
    if not token:
        raise RuntimeError(
            f"No Fyers access token for {effective_mode} mode in Supabase yet. "
            "Use the Login to Fyers button first."
        )
    config = get_fyers_config(effective_mode)
    client_id = config["client_id"]
    fyers = fyersModel.FyersModel(token=token, is_async=False, client_id=client_id, log_path="")
    session = getattr(getattr(fyers, "service", None), "session", None)
    proxy_url = config.get("proxy_url") if use_proxy else None
    if session is not None and not use_proxy:
        # Read-only calls must not inherit HTTP(S)_PROXY from the container.
        session.proxies.clear()
        session.trust_env = False
    if proxy_url and session is not None:
        proxies = {"http": proxy_url, "https": proxy_url}
        session.proxies.update(proxies)
        session.trust_env = False
        
        # Add retry strategy with exponential backoff for proxy timeouts
        if HTTPAdapter and Retry:
            retry_strategy = Retry(
                total=3,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
                backoff_factor=1,  # 1s, 2s, 4s delays
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        
        # Increase connection timeout for proxy traversal
        session.timeout = (10, 60)  # (connect_timeout, read_timeout) in seconds
    return fyers


def get_connection_status() -> dict:
    effective_mode = get_runtime_trading_mode()
    broker = get_active_broker_key(effective_mode)
    token_row = get_stored_token_row(effective_mode)
    token = token_row.get("access_token") if token_row else None
    refresh_token_present = bool(token_row and token_row.get("refresh_token"))
    if not token:
        return {
            "connected": False,
            "status": "disconnected",
            "message": "No Fyers access token found. Login to Fyers before trading.",
            "refresh_token_present": refresh_token_present,
            "broker": broker,
            "trading_mode": effective_mode,
        }

    try:
        # Profile validation is read-only. Keeping it off the trading proxy
        # prevents a dead proxy from making a valid OAuth session look expired.
        response = get_fyers_model(effective_mode, use_proxy=False).get_profile()
    except Exception as exc:
        if _is_recent_token_row(token_row, RECENT_LOGIN_GRACE_SECONDS):
            return {
                "connected": True,
                "status": "rechecking",
                "message": f"Fyers login is still settling after a fresh login; retrying verification ({exc}).",
                "refresh_token_present": refresh_token_present,
                "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
                "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
                "broker": broker,
                "trading_mode": effective_mode,
            }
        return {
            "connected": False,
            "status": "error",
            "message": f"Fyers token check failed: {exc}",
            "refresh_token_present": refresh_token_present,
            "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
            "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
            "broker": broker,
            "trading_mode": effective_mode,
        }

    if response.get("s") == "ok":
        return {
            "connected": True,
            "status": "connected",
            "message": "Fyers token is valid.",
            "refresh_token_present": refresh_token_present,
            "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
            "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
            "broker": broker,
            "trading_mode": effective_mode,
        }

    if _is_recent_token_row(token_row, RECENT_LOGIN_GRACE_SECONDS):
        return {
            "connected": True,
            "status": "rechecking",
            "message": response.get("message") or "Fyers login is still settling after a fresh login; verification will retry.",
            "refresh_token_present": refresh_token_present,
            "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
            "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
            "broker": broker,
            "trading_mode": effective_mode,
        }

    return {
        "connected": False,
        "status": "expired",
        "message": response.get("message") or "Fyers token is missing, expired, or rejected.",
        "refresh_token_present": refresh_token_present,
        "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
        "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
        "broker": broker,
        "trading_mode": effective_mode,
    }


def _is_recent_token_row(token_row: dict | None, max_age_seconds: int) -> bool:
    if not token_row:
        return False
    candidates = [
        token_row.get("access_token_updated_at"),
        token_row.get("refresh_token_updated_at"),
        token_row.get("updated_at"),
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    for value in candidates:
        if not value:
            continue
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        if (now - parsed).total_seconds() <= max_age_seconds:
            return True
    return False


def get_previous_close(symbol: str) -> float | None:
    """Previous trading day's closing price, needed by Algo 1's gap check."""
    fyers = get_fyers_model()
    today = datetime.date.today()
    lookback = today - datetime.timedelta(days=10)  # covers weekends/holidays
    data = {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": lookback.isoformat(), "range_to": (today - datetime.timedelta(days=1)).isoformat(),
        "cont_flag": "1",
    }
    response = fyers.history(data)
    candles = response.get("candles", [])
    if not candles:
        return None
    return candles[-1][4]  # [timestamp, open, high, low, close, volume] -> close


def get_price_history(symbol: str, resolution: str = "15", days: int = 5) -> dict:
    """Recent historical candles normalized for the frontend history tab."""
    fyers = get_fyers_model()
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=max(days, 1))
    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start_date.isoformat(),
        "range_to": today.isoformat(),
        "cont_flag": "1",
    }
    response = fyers.history(data)
    candles = response.get("candles", [])
    warning = None
    if not candles:
        warning = response.get("message") or response.get("errmsg") or f"Fyers returned no candles for {symbol} ({resolution}, {days} days)."
    return {
        "candles": [
        {
            "time": datetime.datetime.fromtimestamp(candle[0]).isoformat(),
            "open": candle[1],
            "high": candle[2],
            "low": candle[3],
            "close": candle[4],
            "volume": candle[5],
        }
        for candle in candles
        ],
        "warning": warning,
        "raw_status": response.get("s"),
    }


def get_recent_intraday_candles(symbol: str, resolution: str = "1", days: int = 5, limit: int = 120) -> list[dict]:
    """Recent completed intraday candles for indicator warmup before market open."""
    fyers = get_fyers_model()
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=max(days, 1))
    end_date = today - datetime.timedelta(days=1)
    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start_date.isoformat(),
        "range_to": end_date.isoformat(),
        "cont_flag": "1",
    }
    response = fyers.history(data)
    candles = response.get("candles", [])
    normalized = [
        {
            "time": datetime.datetime.fromtimestamp(candle[0], tz=IST).replace(tzinfo=None),
            "open": candle[1],
            "high": candle[2],
            "low": candle[3],
            "close": candle[4],
            "volume": candle[5],
        }
        for candle in candles
    ]
    return normalized[-limit:]


def get_intraday_candles_for_range(symbol: str, start_date: datetime.date, end_date: datetime.date, resolution: str = "1") -> list[dict]:
    """Fetch normalized intraday candles for an explicit historical range."""
    fyers = get_fyers_model()
    response = fyers.history({
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start_date.isoformat(),
        "range_to": end_date.isoformat(),
        "cont_flag": "1",
    })
    candles = response.get("candles", [])
    return [
        {
            "time": datetime.datetime.fromtimestamp(candle[0], tz=IST).replace(tzinfo=None),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5] or 0),
        }
    for candle in candles
    ]


def get_wallet_balance(mode: str | None = None) -> dict:
    """Return FYERS funds information with a best-effort wallet summary."""
    effective_mode = mode or get_runtime_trading_mode()
    # Funds is read-only and does not need the whitelisted order egress path.
    # Live order placement still uses the proxy through LiveBroker.
    fyers = get_fyers_model(effective_mode, use_proxy=False)
    audit_log(
        "fyers",
        "funds request started",
        mode=effective_mode,
        broker=get_active_broker_key(effective_mode),
        read_only_direct=True,
    )
    response = fyers.funds()
    if not isinstance(response, dict):
        raise RuntimeError("Fyers funds returned an invalid response.")
    if response.get("s") == "error":
        message = response.get("message") or "Fyers rejected the funds request."
        code = response.get("code")
        suffix = f" (code {code})" if code is not None else ""
        raise RuntimeError(f"{message}{suffix}")
    return {
        "raw": response,
        "summary": _summarize_funds_response(response),
    }


def _summarize_funds_response(response: dict) -> dict:
    def parse_amount(value) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = (
                value.replace(",", "")
                .replace("₹", "")
                .replace("Rs.", "")
                .replace("Rs", "")
                .strip()
            )
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    def normalize_title(value) -> str:
        return " ".join(str(value or "").lower().replace("_", " ").split())

    fund_rows: list[dict] = []
    raw_fund_rows = response.get("fund_limit")
    if isinstance(raw_fund_rows, list):
        for index, row in enumerate(raw_fund_rows):
            if not isinstance(row, dict):
                continue
            normalized = {str(key).lower(): value for key, value in row.items()}
            equity = parse_amount(normalized.get("equityamount"))
            commodity = parse_amount(normalized.get("commodityamount"))
            explicit_amount = next(
                (
                    parsed
                    for key in ("amount", "totalamount", "value")
                    if (parsed := parse_amount(normalized.get(key))) is not None
                ),
                None,
            )
            total = (
                (equity or 0.0) + (commodity or 0.0)
                if equity is not None or commodity is not None
                else explicit_amount
            )
            if total is None:
                continue
            title = str(row.get("title") or row.get("name") or f"row {index + 1}").strip()
            fund_rows.append(
                {
                    "index": index,
                    "title": title,
                    "normalized_title": normalize_title(title),
                    "equity_amount": equity,
                    "commodity_amount": commodity,
                    "total": total,
                }
            )

    def pick_fund_row(titles: tuple[str, ...]) -> dict | None:
        normalized_titles = tuple(normalize_title(title) for title in titles)
        for wanted in normalized_titles:
            for row in fund_rows:
                if row["normalized_title"] == wanted:
                    return row
        for wanted in normalized_titles:
            for row in fund_rows:
                if wanted in row["normalized_title"]:
                    return row
        return None

    wallet_row = pick_fund_row(
        (
            "total balance",
            "available balance",
            "clear balance",
            "cash balance",
            "available funds",
            "limit at start of day",
        )
    )
    margin_row = pick_fund_row(
        (
            "available balance",
            "clear balance",
            "total balance",
            "available funds",
        )
    )
    if wallet_row is not None:
        source = (
            f"fund_limit[{wallet_row['index']}].{wallet_row['title']} "
            "(equityAmount + commodityAmount)"
        )
        margin_source = None
        if margin_row is not None:
            margin_source = (
                f"fund_limit[{margin_row['index']}].{margin_row['title']} "
                "(equityAmount + commodityAmount)"
            )
        return {
            "wallet_balance": wallet_row["total"],
            "wallet_balance_source": source,
            "equity_balance": wallet_row["equity_amount"],
            "commodity_balance": wallet_row["commodity_amount"],
            "available_margin": margin_row["total"] if margin_row is not None else None,
            "available_margin_source": margin_source,
            "fund_limit_rows": [
                {
                    "title": row["title"],
                    "equity_amount": row["equity_amount"],
                    "commodity_amount": row["commodity_amount"],
                    "total": row["total"],
                }
                for row in fund_rows
            ],
        }

    candidates: list[tuple[str, float]] = []

    def collect(value, path: str = ""):
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key).lower()
                child_path = f"{path}.{key}" if path else str(key)
                parsed = parse_amount(child)
                if parsed is not None:
                    if any(marker in key_text for marker in ("available", "balance", "cash", "margin", "fund")):
                        candidates.append((child_path, parsed))
                else:
                    collect(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, f"{path}[{index}]")

    collect(response)

    def pick(markers: tuple[str, ...]) -> tuple[str | None, float | None]:
        for path, amount in candidates:
            path_lower = path.lower()
            if any(marker in path_lower for marker in markers):
                return path, amount
        return None, None

    wallet_path, wallet_value = pick(("available", "balance", "cash"))
    margin_path, margin_value = pick(("margin",))
    return {
        "wallet_balance": wallet_value,
        "wallet_balance_source": wallet_path,
        "available_margin": margin_value,
        "available_margin_source": margin_path,
    }


def connect_live_feed(symbols: list[str], on_tick_callback, on_status_callback=None):
    if data_ws is None:
        raise RuntimeError(
            "Fyers websocket SDK is not installed in this environment. "
            "Install the backend requirements before using live feed features."
        )
    token = get_stored_access_token()
    if not token:
        raise RuntimeError("No Fyers access token in Supabase yet")

    def report_status(**data):
        if on_status_callback:
            on_status_callback(data)

    first_tick_received = False
    subscription_sent = False
    subscription_lock = threading.Lock()

    def on_message(message):
        nonlocal first_tick_received
        if not first_tick_received and message.get("symbol") and message.get("ltp") is not None:
            first_tick_received = True
            report_status(
                connected=True,
                first_tick_received=True,
                message="Fyers websocket is receiving market ticks",
            )
        on_tick_callback(message)

    def subscribe_when_ready():
        """Wait for the SDK's real socket-open callback before subscribing.

        FyersDataSocket.connect() invokes the public on_connect callback after a
        fixed two-second delay, not when its underlying websocket is necessarily
        ready. Calling subscribe earlier can put the request in a queue which the
        SDK replaces when the real socket opens, producing a silent 0-tick feed.
        """
        nonlocal subscription_sent

        for _ in range(30):
            if socket.is_connected():
                with subscription_lock:
                    if subscription_sent:
                        return
                    try:
                        socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
                        subscription_sent = True
                    except Exception as exc:
                        print("Fyers WS subscription error:", exc)
                        report_status(
                            connected=False,
                            error=str(exc),
                            message="Fyers websocket subscription failed",
                        )
                        return
                print(f"[fyers] websocket subscribed to {len(symbols)} symbols")
                report_status(
                    connected=True,
                    subscribed_symbols=len(symbols),
                    message=f"Fyers websocket subscribed to {len(symbols)} symbols; waiting for first tick",
                )
                return
            time.sleep(0.5)

        report_status(
            connected=False,
            error="Timed out waiting for the Fyers websocket to open",
            message="Fyers websocket did not establish before subscription",
        )

    def on_open():
        threading.Thread(target=subscribe_when_ready, daemon=True).start()

    def on_error(message):
        print("Fyers WS error:", message)
        report_status(connected=False, error=str(message), message="Fyers websocket error")

    def on_close(message):
        nonlocal subscription_sent
        with subscription_lock:
            subscription_sent = False
        print("Fyers WS closed:", message)
        report_status(connected=False, error=str(message), message="Fyers websocket closed")

    socket = data_ws.FyersDataSocket(
        access_token=f"{get_fyers_config()['client_id']}:{token}",
        log_path="",
        litemode=False,
        reconnect=True,
        write_to_file=False,
        on_connect=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    try:
        socket.connect()
    except Exception as exc:
        report_status(connected=False, error=str(exc), message="Fyers websocket connect failed")
        raise

    def detect_missing_first_tick():
        # A successful TCP/WebSocket handshake is not enough for a trading
        # engine. Surface a missing market-data subscription quickly so the
        # engine watchdog can reconnect instead of displaying a false green
        # "connected" state until the scheduled scan has already been missed.
        time.sleep(30)
        if not first_tick_received:
            report_status(
                connected=False,
                error="No Fyers market tick received within 30 seconds of subscription",
                message="Fyers websocket is open but not delivering market data",
            )

    threading.Thread(target=detect_missing_first_tick, daemon=True).start()
    return socket

