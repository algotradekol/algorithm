"""
fyers_client.py — thin wrapper around Fyers' live WebSocket and
historical candle REST API, used by engine.py.
"""
import datetime
import copy
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
BROKER_POSITIONS_CACHE_TTL_SECONDS = 10
BROKER_POSITIONS_STALE_TTL_SECONDS = 300
BROKER_ORDERS_CACHE_TTL_SECONDS = 10
BROKER_ORDERS_STALE_TTL_SECONDS = 300
FYERS_FUNDS_CACHE_TTL_SECONDS = 20
FYERS_FUNDS_STALE_TTL_SECONDS = 300
# After Fyers rate-limits us (HTTP 429 / "code 429"), block every REST call
# for this many seconds so a polling frontend does not amplify the throttle
# into a self-sustaining storm.
FYERS_REST_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
_fyers_rest_ratelimit_until: dict[str, float] = {}


def _is_rate_limited(response_or_exc) -> bool:
    text = str(response_or_exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def _mark_rest_rate_limited(mode: str) -> None:
    _fyers_rest_ratelimit_until[mode] = time.monotonic() + FYERS_REST_RATE_LIMIT_COOLDOWN_SECONDS


def _rest_cooldown_remaining(mode: str) -> float:
    until = _fyers_rest_ratelimit_until.get(mode, 0.0)
    return max(0.0, until - time.monotonic())


_broker_positions_cache: dict[str, dict] = {}
_broker_positions_locks = {
    "paper": threading.Lock(),
    "live": threading.Lock(),
}
_broker_orders_cache: dict[str, dict] = {}
_broker_orders_locks = {
    "paper": threading.Lock(),
    "live": threading.Lock(),
}
_fyers_funds_cache: dict[str, dict] = {}
_fyers_funds_locks = {
    "paper": threading.Lock(),
    "live": threading.Lock(),
}


def get_fyers_model(
    mode: str | None = None,
    *,
    use_proxy: bool = True,
    retry_total: int = 3,
    request_timeout: tuple[int, int] = (10, 30),
):
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
                total=retry_total,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
                backoff_factor=1,  # 1s, 2s, 4s delays
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount("http://", adapter)
            session.mount("https://", adapter)

    if session is not None:
        # requests.Session has no effective `session.timeout` default. Injecting
        # it into request() prevents a slow proxy/FYERS account call from
        # occupying a FastAPI worker indefinitely.
        original_request = session.request

        def request_with_timeout(method, url, **kwargs):
            kwargs.setdefault("timeout", request_timeout)
            return original_request(method, url, **kwargs)

        session.request = request_with_timeout
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
        # A timeout, Railway rollout, or temporary FYERS failure does not prove
        # that the stored token was revoked. Keep the trading session available
        # and expose a degraded verification state until FYERS responds again.
        return {
            "connected": True,
            "status": "degraded",
            "message": f"Fyers token is stored, but verification is temporarily unavailable: {exc}",
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

    # F7 (2026-08-17): a 429 from Fyers/Cloudflare during profile verify is
    # a rate-limit, NOT an expired token. On 2026-08-17 morning the app was
    # force-logging users out because the profile check ran into 429 and the
    # response body carried a generic "invalid" message that fell through
    # to the "expired" branch below. Return a degraded state instead so
    # the token stays alive until Cloudflare cools down.
    if _is_rate_limited(response):
        return {
            "connected": True,
            "status": "degraded",
            "message": "Fyers verification throttled by rate limit; keeping session alive.",
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


def get_live_ltp_batch(symbols: list[str], mode: str | None = None) -> dict[str, float]:
    """Fetch current LTP for multiple symbols in one (or a few) Fyers quotes API calls.

    Fyers quotes endpoint accepts up to 50 symbols per call. Returns a dict of
    {symbol: ltp} for every symbol that returned a valid price. Missing symbols
    are simply absent from the result dict — callers must handle that.

    This is used as a fallback at entry time when the live WebSocket has not
    yet delivered a tick for a symbol (e.g. low-liquidity symbols at 9:16).
    """
    if not symbols:
        return {}
    try:
        fyers = get_fyers_model(mode, use_proxy=False)
    except Exception:
        return {}

    result: dict[str, float] = {}
    # Fyers quotes API allows up to 50 symbols per call
    BATCH_SIZE = 50
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i: i + BATCH_SIZE]
        try:
            response = fyers.quotes({"symbols": ",".join(batch)})
            rows = []
            if isinstance(response, dict):
                rows = response.get("d") or response.get("data") or []
            elif isinstance(response, list):
                rows = response
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # Fyers v3 wraps each symbol under a "v" key
                inner = row.get("v") or row
                sym = (
                    str(inner.get("symbol") or row.get("symbol") or "").upper()
                )
                ltp_raw = (
                    inner.get("lp")
                    or inner.get("ltp")
                    or inner.get("last_price")
                    or inner.get("close_price")
                )
                if sym and ltp_raw is not None:
                    try:
                        result[sym] = float(ltp_raw)
                    except (TypeError, ValueError):
                        pass
        except Exception as exc:
            print(f"[fyers_client] batch quotes failed for chunk {i//BATCH_SIZE}: {exc}")
    return result


def get_live_quotes_batch(symbols: list[str], mode: str | None = None) -> dict[str, dict]:
    """Fetch LTP + previous close in one Fyers quotes call. Same API as
    get_live_ltp_batch but returns {sym: {"ltp": x, "prev_close": y}} — used
    as a fast inline fallback when the background prev-close preload has not
    finished (typical in test-mode where the scan fires seconds after startup).
    """
    if not symbols:
        return {}
    try:
        fyers = get_fyers_model(mode, use_proxy=False)
    except Exception:
        return {}

    result: dict[str, dict] = {}
    BATCH_SIZE = 50
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i: i + BATCH_SIZE]
        try:
            response = fyers.quotes({"symbols": ",".join(batch)})
            rows = []
            if isinstance(response, dict):
                rows = response.get("d") or response.get("data") or []
            elif isinstance(response, list):
                rows = response
            for row in rows:
                if not isinstance(row, dict):
                    continue
                inner = row.get("v") or row
                sym = str(inner.get("symbol") or row.get("symbol") or "").upper()
                if not sym:
                    continue
                entry: dict = {}
                ltp_raw = (
                    inner.get("lp") or inner.get("ltp")
                    or inner.get("last_price") or inner.get("close_price")
                )
                if ltp_raw is not None:
                    try:
                        entry["ltp"] = float(ltp_raw)
                    except (TypeError, ValueError):
                        pass
                pc_raw = (
                    inner.get("prev_close_price")
                    or inner.get("prev_close")
                    or inner.get("previous_close")
                )
                if pc_raw is not None:
                    try:
                        entry["prev_close"] = float(pc_raw)
                    except (TypeError, ValueError):
                        pass
                if entry:
                    result[sym] = entry
        except Exception as exc:
            print(f"[fyers_client] batch quotes+pc failed for chunk {i//BATCH_SIZE}: {exc}")
    return result


def get_previous_close(symbol: str) -> float | None:
    """Previous trading day's closing price, needed by Algo 1's gap check."""
    fyers = get_fyers_model(use_proxy=False)
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
    fyers = get_fyers_model(use_proxy=False)
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
    fyers = get_fyers_model(use_proxy=False)
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
    fyers = get_fyers_model(use_proxy=False)
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


def _parse_candle_ohlcv(candle: list | dict, symbol: str = "") -> dict | None:
    """Safely extract OHLCV from a Fyers candle array or dict.

    Validates that the extracted prices form a valid candle:
    high >= open/close >= low AND prices are in realistic range (e.g., not 50x apart in 1 min).
    If validation fails, logs the issue and returns None.
    """
    try:
        # Try array format [ts, open, high, low, close, volume]
        if isinstance(candle, (list, tuple)) and len(candle) >= 6:
            ts = candle[0]
            open_price = float(candle[1])
            high_price = float(candle[2])
            low_price = float(candle[3])
            close_price = float(candle[4])
            volume = float(candle[5] or 0)

            # Sanity check 1: high >= max(open, close) and low <= min(open, close)
            max_price = max(open_price, close_price)
            min_price = min(open_price, close_price)

            if not (high_price >= max_price and low_price <= min_price and high_price >= low_price):
                print(f"[fyers_client] WARNING: {symbol} candle prices invalid: "
                      f"O={open_price} H={high_price} L={low_price} C={close_price}. "
                      f"Raw: {candle}")
                return None

            # Sanity check 2: Prices should be in same ballpark (within 50% for 1-min candle)
            # If open is 4550 but close is 223804, array indices are definitely wrong
            if open_price > 0 and close_price > 0:
                price_ratio = max(open_price, close_price) / min(open_price, close_price)
                if price_ratio > 1.5:  # More than 50% move in 1 minute is suspicious
                    # This might be volume, OI, or corrupted data
                    print(f"[fyers_client] WARNING: {symbol} candle has extreme move: "
                          f"O={open_price} C={close_price} (ratio={price_ratio:.1f}x). "
                          f"Raw: {candle}")
                    return None

            return {
                "ts": ts,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": volume,
            }

        # Try dict format (in case Fyers API changed)
        if isinstance(candle, dict):
            return {
                "ts": candle.get("time") or candle.get("timestamp"),
                "open": float(candle.get("open", 0)),
                "high": float(candle.get("high", 0)),
                "low": float(candle.get("low", 0)),
                "close": float(candle.get("close", 0)),
                "volume": float(candle.get("volume", 0)),
            }
    except (ValueError, TypeError, IndexError) as e:
        print(f"[fyers_client] ERROR parsing candle for {symbol}: {e}")
        return None

    return None


def get_single_minute_candle(symbol: str, candle_time_str: str) -> list[dict]:
    """Fetch only the single 1-minute candle at candle_time_str (HH:MM IST) for today.

    Uses Unix epoch timestamps so Fyers returns only a 2-minute window instead
    of the full trading day.  This makes the 500-symbol backfill ~375× faster
    and avoids Fyers rate-limit errors.
    """
    fyers = get_fyers_model(use_proxy=False)
    today = datetime.date.today()
    # Build exact start/end timestamps in IST. IST is a zoneinfo.ZoneInfo
    # (see app/timezone.py), NOT pytz — use tzinfo= kwarg, not .localize().
    # The old .localize() call raised AttributeError on every symbol and
    # collapsed the entire 9:15 backfill to 0 candles on 2026-08-10.
    candle_start = datetime.datetime.combine(
        today,
        datetime.time(*map(int, candle_time_str.split(":"))),
        tzinfo=IST,
    )
    # Fyers range_to is inclusive — request a 2-min window to be safe
    candle_end = candle_start + datetime.timedelta(minutes=2)
    response = fyers.history({
        "symbol": symbol,
        "resolution": "1",
        "date_format": "0",  # Unix epoch
        "range_from": str(int(candle_start.timestamp())),
        "range_to": str(int(candle_end.timestamp())),
        "cont_flag": "1",
    })
    candles = response.get("candles", [])
    result = []
    for candle in candles:
        parsed = _parse_candle_ohlcv(candle, symbol)
        if parsed is None:
            # Log first bad candle per symbol for debugging
            if not result:  # Only log first rejected candle
                print(f"[fyers_client] {symbol} @ {candle_time_str}: rejected invalid candle. "
                      f"Raw: {candle}")
            continue
        candle_ts = datetime.datetime.fromtimestamp(parsed["ts"], tz=IST).replace(tzinfo=None)
        if candle_ts.strftime("%H:%M") == candle_time_str:
            result.append({
                "time": candle_ts,
                "open": parsed["open"],
                "high": parsed["high"],
                "low": parsed["low"],
                "close": parsed["close"],
                "volume": parsed["volume"],
            })
    return result




def get_wallet_balance(mode: str | None = None) -> dict:
    """Return FYERS funds information with a best-effort wallet summary."""
    effective_mode = mode or get_runtime_trading_mode()
    now = time.monotonic()
    cached = _fyers_funds_cache.get(effective_mode)
    if cached and now - cached["cached_at"] <= FYERS_FUNDS_CACHE_TTL_SECONDS:
        result = copy.deepcopy(cached["result"])
        result.update({"cached": True, "stale": False, "syncing": False})
        return result

    # Fyers rate-limited us recently on any REST endpoint; keep serving stale
    # cache instead of another request that will also be throttled.
    cooldown_left = _rest_cooldown_remaining(effective_mode)
    if cooldown_left > 0 and cached:
        result = copy.deepcopy(cached["result"])
        result.update({
            "cached": True,
            "stale": True,
            "syncing": False,
            "warning": f"FYERS REST cooling down after 429; retrying in {int(cooldown_left)}s.",
        })
        return result

    lock = _fyers_funds_locks.setdefault(effective_mode, threading.Lock())
    if not lock.acquire(blocking=False):
        if cached and now - cached["cached_at"] <= FYERS_FUNDS_STALE_TTL_SECONDS:
            result = copy.deepcopy(cached["result"])
            result.update({
                "cached": True,
                "stale": True,
                "syncing": True,
                "warning": "A fresh FYERS funds request is already in progress.",
            })
            return result
        return {
            "raw": {},
            "summary": {},
            "available": False,
            "cached": False,
            "stale": False,
            "syncing": True,
            "warning": "A FYERS funds request is already in progress.",
        }

    # Funds is read-only and does not need the whitelisted order egress path.
    # Live order placement still uses the proxy through LiveBroker.
    try:
        fyers = get_fyers_model(
            effective_mode,
            use_proxy=False,
            retry_total=0,
            request_timeout=(5, 12),
        )
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
        result = {
            "raw": response,
            "summary": _summarize_funds_response(response),
            "available": True,
            "cached": False,
            "stale": False,
            "syncing": False,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _fyers_funds_cache[effective_mode] = {
            "cached_at": time.monotonic(),
            "result": copy.deepcopy(result),
        }
        return result
    except Exception as exc:
        if _is_rate_limited(exc):
            _mark_rest_rate_limited(effective_mode)
        cached = _fyers_funds_cache.get(effective_mode)
        if cached and time.monotonic() - cached["cached_at"] <= FYERS_FUNDS_STALE_TTL_SECONDS:
            result = copy.deepcopy(cached["result"])
            result.update({
                "cached": True,
                "stale": True,
                "syncing": False,
                "warning": f"FYERS funds refresh failed; showing the last known balance: {exc}",
            })
            audit_log(
                "fyers",
                "funds request using stale cache",
                mode=effective_mode,
                broker=get_active_broker_key(effective_mode),
                error=str(exc),
            )
            return result
        message = str(exc)
        if "No Fyers access token" in message:
            raise
        audit_log(
            "fyers",
            "funds request unavailable",
            mode=effective_mode,
            broker=get_active_broker_key(effective_mode),
            error=message,
        )
        return {
            "raw": {},
            "summary": {},
            "available": False,
            "cached": False,
            "stale": False,
            "syncing": False,
            "warning": f"FYERS funds are temporarily unavailable: {message}",
        }
    finally:
        lock.release()


def get_broker_positions(mode: str | None = None) -> dict:
    """Return currently open positions reported by the FYERS account."""
    effective_mode = mode or get_runtime_trading_mode()
    now = time.monotonic()
    cached = _broker_positions_cache.get(effective_mode)
    if cached and now - cached["cached_at"] <= BROKER_POSITIONS_CACHE_TTL_SECONDS:
        result = copy.deepcopy(cached["result"])
        result.update({"cached": True, "stale": False, "syncing": False})
        return result

    # Serve stale cache during a live 429 cooldown instead of piling on more
    # requests that will all be throttled and cause the cache never to update.
    cooldown_left = _rest_cooldown_remaining(effective_mode)
    if cooldown_left > 0 and cached:
        result = copy.deepcopy(cached["result"])
        result.update({
            "cached": True,
            "stale": True,
            "syncing": False,
            "warning": f"FYERS REST cooling down after 429; retrying in {int(cooldown_left)}s.",
        })
        return result

    lock = _broker_positions_locks.setdefault(effective_mode, threading.Lock())
    if not lock.acquire(blocking=False):
        if cached and now - cached["cached_at"] <= BROKER_POSITIONS_STALE_TTL_SECONDS:
            result = copy.deepcopy(cached["result"])
            result.update({
                "cached": True,
                "stale": True,
                "syncing": True,
                "warning": "A fresh FYERS positions request is already in progress.",
            })
            return result
        return {
            "mode": effective_mode,
            "broker": get_active_broker_key(effective_mode),
            "count": 0,
            "positions": [],
            "overall": {},
            "available": False,
            "cached": False,
            "stale": False,
            "syncing": True,
            "warning": "A FYERS positions request is already in progress.",
        }

    # Read-only account queries should stay off the trading proxy. The proxy is
    # required for order placement from the trading app's allowlisted IP, but
    # FYERS funds/profile already work more reliably on the direct path and
    # positions should behave the same way. This avoids proxy-side request
    # mangling that surfaces as FYERS "Bad request (code -99)" on positions.
    use_proxy = False
    try:
        # Account reads should fail quickly and be retried by the next poll,
        # rather than multiplying long proxy retries across dashboard tabs.
        fyers = get_fyers_model(
            effective_mode,
            use_proxy=use_proxy,
            retry_total=0,
            request_timeout=(5, 12),
        )
        audit_log(
            "fyers",
            "positions request started",
            mode=effective_mode,
            broker=get_active_broker_key(effective_mode),
            proxy_enabled=use_proxy,
        )
        response = fyers.positions()
        if not isinstance(response, dict):
            raise RuntimeError("Fyers positions returned an invalid response.")
        if response.get("s") == "error":
            message = response.get("message") or "Fyers rejected the positions request."
            code = response.get("code")
            suffix = f" (code {code})" if code is not None else ""
            raise RuntimeError(f"{message}{suffix}")

        positions = [
            normalized
            for row in response.get("netPositions", response.get("net_positions", [])) or []
            if isinstance(row, dict)
            and (normalized := _normalize_broker_position(row)) is not None
        ]
        # Second pass: hydrate entry_time from the intraday tradebook so
        # positions opened directly in the Fyers app show a real fill time
        # instead of "--". Silent no-op on failure.
        _enrich_positions_with_entry_times(fyers, positions)
        result = {
            "mode": effective_mode,
            "broker": get_active_broker_key(effective_mode),
            "count": len(positions),
            "positions": positions,
            "overall": response.get("overall") if isinstance(response.get("overall"), dict) else {},
            "available": True,
            "cached": False,
            "stale": False,
            "syncing": False,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _broker_positions_cache[effective_mode] = {
            "cached_at": time.monotonic(),
            "result": copy.deepcopy(result),
        }
        audit_log(
            "fyers",
            "positions request completed",
            mode=effective_mode,
            broker=get_active_broker_key(effective_mode),
            open_position_count=len(positions),
        )
        return result
    except Exception as exc:
        if _is_rate_limited(exc):
            _mark_rest_rate_limited(effective_mode)
        cached = _broker_positions_cache.get(effective_mode)
        if cached and time.monotonic() - cached["cached_at"] <= BROKER_POSITIONS_STALE_TTL_SECONDS:
            result = copy.deepcopy(cached["result"])
            result.update({
                "cached": True,
                "stale": True,
                "syncing": False,
                "warning": f"FYERS positions refresh failed; showing the last known snapshot: {exc}",
            })
            audit_log(
                "fyers",
                "positions request using stale cache",
                mode=effective_mode,
                broker=get_active_broker_key(effective_mode),
                error=str(exc),
            )
            return result
        message = str(exc)
        if "No Fyers access token" in message:
            raise
        audit_log(
            "fyers",
            "positions request unavailable",
            mode=effective_mode,
            broker=get_active_broker_key(effective_mode),
            error=message,
        )
        return {
            "mode": effective_mode,
            "broker": get_active_broker_key(effective_mode),
            "count": 0,
            "positions": [],
            "overall": {},
            "available": False,
            "cached": False,
            "stale": False,
            "syncing": False,
            "warning": f"FYERS positions are temporarily unavailable: {message}",
        }
    finally:
        lock.release()


def get_broker_orders(mode: str | None = None) -> dict:
    effective_mode = mode or get_runtime_trading_mode()
    lock = _broker_orders_locks.setdefault(effective_mode, threading.Lock())
    cache_entry = _broker_orders_cache.get(effective_mode)
    now = time.time()

    if cache_entry and now - cache_entry.get("cached_at", 0.0) < BROKER_ORDERS_CACHE_TTL_SECONDS:
        cached_result = copy.deepcopy(cache_entry["value"])
        cached_result.update({"cached": True, "stale": False, "syncing": False})
        return cached_result

    if not lock.acquire(blocking=False):
        if cache_entry:
            cached_result = copy.deepcopy(cache_entry["value"])
            is_stale = now - cache_entry.get("cached_at", 0.0) >= BROKER_ORDERS_CACHE_TTL_SECONDS
            cached_result.update({"cached": True, "stale": is_stale, "syncing": True})
            return cached_result
        return {
            "mode": effective_mode,
            "broker": get_active_broker_key(effective_mode),
            "count": 0,
            "orders": [],
            "available": False,
            "cached": False,
            "stale": False,
            "syncing": True,
            "warning": "FYERS orders are syncing. Try again in a moment.",
        }

    try:
        try:
            response = get_fyers_model(effective_mode, use_proxy=False).orderbook()
            raw_rows = response.get("orderBook", response.get("orderbook", []))
            orders = []
            if isinstance(raw_rows, list):
                for row in raw_rows:
                    if not isinstance(row, dict):
                        continue
                    normalized = _normalize_broker_order(row)
                    if normalized:
                        orders.append(normalized)

            result = {
                "mode": effective_mode,
                "broker": get_active_broker_key(effective_mode),
                "count": len(orders),
                "orders": orders,
                "available": True,
                "cached": False,
                "stale": False,
                "syncing": False,
                "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            _broker_orders_cache[effective_mode] = {
                "cached_at": now,
                "value": copy.deepcopy(result),
            }
            return result
        except Exception as exc:
            if cache_entry and now - cache_entry.get("cached_at", 0.0) < BROKER_ORDERS_STALE_TTL_SECONDS:
                result = copy.deepcopy(cache_entry["value"])
                result.update({
                    "cached": True,
                    "stale": True,
                    "syncing": False,
                    "warning": f"FYERS orders are temporarily unavailable: {exc}",
                })
                audit_log(
                    "fyers",
                    "orders request using stale cache",
                    mode=effective_mode,
                    broker=get_active_broker_key(effective_mode),
                    error=str(exc),
                )
                return result
            message = str(exc)
            if "No Fyers access token" in message:
                raise
            audit_log(
                "fyers",
                "orders request unavailable",
                mode=effective_mode,
                broker=get_active_broker_key(effective_mode),
                error=message,
            )
            return {
                "mode": effective_mode,
                "broker": get_active_broker_key(effective_mode),
                "count": 0,
                "orders": [],
                "available": False,
                "cached": False,
                "stale": False,
                "syncing": False,
                "warning": f"FYERS orders are temporarily unavailable: {message}",
            }
    finally:
        lock.release()


def _normalize_broker_position(row: dict) -> dict | None:
    """Normalize FYERS camelCase/snake_case position fields for the frontend."""
    lowered = {str(key).lower(): value for key, value in row.items()}

    def value(*keys):
        for key in keys:
            if key in row and row[key] is not None:
                return row[key]
            lowered_key = key.lower()
            if lowered_key in lowered and lowered[lowered_key] is not None:
                return lowered[lowered_key]
        return None

    def number(*keys, default: float = 0.0) -> float:
        raw = value(*keys)
        if isinstance(raw, bool) or raw is None:
            return default
        try:
            return float(str(raw).replace(",", "").strip())
        except (TypeError, ValueError):
            return default

    raw_net_qty = value("netQty", "net_qty")
    if raw_net_qty is None:
        quantity = abs(number("qty", "quantity"))
        side_value = number("side")
        net_qty = quantity * (-1 if side_value < 0 else 1)
    else:
        net_qty = number("netQty", "net_qty")
    if abs(net_qty) < 1e-12:
        return None

    side = "SELL" if net_qty < 0 else "BUY"
    entry_price = number("netAvg", "net_avg", "avgPrice", "avg_price")
    if entry_price <= 0:
        entry_price = number("sellAvg", "sell_avg") if side == "SELL" else number("buyAvg", "buy_avg")

    unrealized_pnl = number("unrealized_profit", "unrealizedProfit", "unrealized_pl")
    realized_pnl = number("realized_profit", "realizedProfit", "realized_pl")
    total_pnl_raw = value("pl", "pnl", "totalPnl", "total_pnl")
    total_pnl = (
        number("pl", "pnl", "totalPnl", "total_pnl")
        if total_pnl_raw is not None
        else realized_pnl + unrealized_pnl
    )
    symbol = str(value("symbol") or "").strip()
    if not symbol:
        return None

    return {
        "id": str(value("id", "positionId", "position_id") or symbol),
        "symbol": symbol,
        "side": side,
        "qty": abs(net_qty),
        "net_qty": net_qty,
        "entry_price": entry_price,
        "ltp": number("ltp", "lastPrice", "last_price"),
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "total_pnl": total_pnl,
        "product_type": str(value("productType", "product_type") or ""),
        "buy_qty": number("buyQty", "buy_qty"),
        "sell_qty": number("sellQty", "sell_qty"),
        # Populated in a second pass from Fyers's tradebook when available
        # (see _enrich_positions_with_entry_times). Left as None here so
        # positions still render even if the tradebook call fails.
        "entry_time": None,
    }


def _parse_broker_trade_time(row: dict) -> datetime.datetime | None:
    """Parse a Fyers tradebook row's timestamp into a UTC datetime.

    Fyers uses several key names and formats across product versions; try
    each in preference order and coerce to timezone-aware UTC. Returns
    None if nothing parses (caller falls back to leaving entry_time null).
    """
    for key in (
        "orderDateTime",
        "tradeDateTime",
        "tradeTime",
        "tradedAt",
        "tradedOn",
        "updatedAt",
        "updated_at",
        "createdAt",
        "timestamp",
        "time",
    ):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, (int, float)):
            number_value = float(raw)
            # Fyers occasionally emits milliseconds; heuristic split at 1e12.
            if number_value > 1_000_000_000_000:
                number_value /= 1000
            try:
                return datetime.datetime.fromtimestamp(number_value, tz=datetime.timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
        text = str(raw).strip()
        if not text:
            continue
        iso_candidate = text.replace("Z", "+00:00")
        try:
            parsed = datetime.datetime.fromisoformat(iso_candidate)
            if parsed.tzinfo is None:
                # Fyers wall-clock strings are IST; convert to UTC so the
                # frontend's formatDateTime renders correctly regardless of
                # server timezone.
                from .timezone import IST
                parsed = parsed.replace(tzinfo=IST)
            return parsed.astimezone(datetime.timezone.utc)
        except ValueError:
            pass
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                naive = datetime.datetime.strptime(text, fmt)
                from .timezone import IST
                return naive.replace(tzinfo=IST).astimezone(datetime.timezone.utc)
            except ValueError:
                continue
    return None


def _normalize_trade_side(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in {"1", "BUY", "B"}:
        return "BUY"
    if text in {"-1", "SELL", "S"}:
        return "SELL"
    return None


def _extract_tradebook_rows(response) -> list[dict]:
    """Fyers tradebook can return the list under any of several keys, or
    as a bare list, depending on SDK version. Normalize to a flat list of
    dicts."""
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    if not isinstance(response, dict):
        return []
    for key in ("tradeBook", "tradebook", "trades", "data"):
        rows = response.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            for nested_key in ("rows", "items", "trades"):
                nested = rows.get(nested_key)
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
    return []


def _enrich_positions_with_entry_times(fyers, positions: list[dict]) -> None:
    """For each open position, look up the earliest matching tradebook
    fill and stamp its timestamp as `entry_time`. Mutates `positions`
    in place. Silent no-op if the tradebook call fails — positions still
    render, just without accurate entry_time (frontend shows '--').

    Match is by (symbol, side). Fyers's intraday tradebook typically holds
    all of today's fills; if a manual position was opened yesterday the
    lookup returns None and entry_time stays null.
    """
    if not positions:
        return
    try:
        response = fyers.tradebook()
    except Exception as exc:
        print(f"[fyers_client] tradebook fetch for entry_time enrichment failed: {exc}")
        return
    rows = _extract_tradebook_rows(response)
    if not rows:
        return

    # Bucket earliest fill time by (symbol upper, side). One pass keeps
    # this O(trades) instead of O(trades * positions).
    earliest_by_key: dict[tuple[str, str], datetime.datetime] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("fySymbol") or "").upper()
        if not symbol:
            continue
        side = _normalize_trade_side(
            row.get("side")
            or row.get("transactionType")
            or row.get("buySell")
        )
        if side is None:
            continue
        parsed = _parse_broker_trade_time(row)
        if parsed is None:
            continue
        key = (symbol, side)
        prior = earliest_by_key.get(key)
        if prior is None or parsed < prior:
            earliest_by_key[key] = parsed

    for position in positions:
        key = (str(position.get("symbol", "")).upper(), position.get("side"))
        earliest = earliest_by_key.get(key)
        if earliest is not None:
            position["entry_time"] = earliest.isoformat()


def _normalize_broker_order(row: dict) -> dict | None:
    lowered = {str(key).lower(): value for key, value in row.items()}

    def value(*keys):
        for key in keys:
            if key in row and row[key] is not None:
                return row[key]
            lowered_key = key.lower()
            if lowered_key in lowered and lowered[lowered_key] is not None:
                return lowered[lowered_key]
        return None

    def number(*keys, default: float = 0.0) -> float:
        raw = value(*keys)
        if isinstance(raw, bool) or raw is None:
            return default
        try:
            return float(str(raw).replace(",", "").strip())
        except (TypeError, ValueError):
            return default

    symbol = str(value("symbol") or "").strip()
    if not symbol:
        return None

    status_text = str(value("statusDescription", "status_description", "status") or "").strip()
    status_upper = status_text.upper()
    if any(token in status_upper for token in ("CANCEL", "REJECT", "COMPLETE", "FILLED", "TRADED", "EXPIRE")):
        return None

    qty = abs(number("qty", "quantity", default=0.0))
    remaining_qty = abs(number("remainingQuantity", "remaining_quantity", "remainingQty", "remaining_qty", default=qty))
    filled_qty = abs(number("filledQty", "filled_qty", default=max(0.0, qty - remaining_qty)))
    if qty <= 0 and remaining_qty <= 0 and filled_qty <= 0:
        return None

    side_value = number("side")
    side = "SELL" if side_value < 0 else "BUY"
    limit_price = number("limitPrice", "limit_price")
    trigger_price = number("stopPrice", "stop_price", "triggerPrice", "trigger_price")
    entry_price = limit_price or trigger_price or number("price")
    product_type = str(value("productType", "product_type") or "")
    order_type = str(value("type", "orderType", "order_type") or "")

    return {
        "id": str(value("id", "orderId", "order_id") or f"{symbol}:{side}:{status_text or 'pending'}"),
        "symbol": symbol,
        "side": side,
        "qty": qty or remaining_qty or filled_qty,
        "remaining_qty": remaining_qty,
        "filled_qty": filled_qty,
        "entry_price": entry_price,
        "limit_price": limit_price,
        "trigger_price": trigger_price,
        "status": status_text or "Pending",
        "product_type": product_type,
        "order_type": order_type,
        "ltp": None,
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


def _critical_live_feed_symbols(symbols: list[str]) -> list[str]:
    """Non-NSE symbols whose silence should fail the feed health check."""
    critical: list[str] = []
    for symbol in symbols:
        exchange = symbol.split(":", 1)[0].upper() if ":" in symbol else ""
        if exchange != "NSE":
            critical.append(symbol)
    return critical


def _missing_critical_live_feed_symbols(subscribed_symbols: list[str], seen_symbols: set[str]) -> list[str]:
    """Critical subscribed symbols that have not produced a valid tick yet."""
    return [symbol for symbol in _critical_live_feed_symbols(subscribed_symbols) if symbol not in seen_symbols]


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
    seen_symbols_with_ticks: set[str] = set()

    # MCX diagnostic: log the raw shape of any message whose symbol
    # contains "SILVERMIC" (regardless of exchange prefix). If MCX ticks
    # ARE arriving but with a shape the on_tick filter drops (missing
    # ltp key, different symbol format, etc.), this will show it.
    # One log per unique raw shape so we don't flood.
    _mcx_shapes_seen: set = set()

    def on_message(message):
        nonlocal first_tick_received
        symbol = message.get("symbol")
        ltp = message.get("ltp")
        if symbol and ltp is not None:
            seen_symbols_with_ticks.add(symbol)
        try:
            sym_val = str(symbol or "")
            if "SILVERMIC" in sym_val.upper() or (isinstance(message, dict) and any(
                "SILVERMIC" in str(v).upper() for v in message.values() if isinstance(v, str)
            )):
                shape_key = f"{sym_val}|{sorted(message.keys()) if isinstance(message, dict) else type(message).__name__}"
                if shape_key not in _mcx_shapes_seen:
                    _mcx_shapes_seen.add(shape_key)
                    print(f"[fyers] MCX raw message shape: {message}")
        except Exception:
            pass
        if not first_tick_received and symbol and ltp is not None:
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
                        mcx_syms = [s for s in symbols if s.upper().startswith("MCX:")]
                        nse_count = sum(1 for s in symbols if s.upper().startswith("NSE:"))
                        bse_count = sum(1 for s in symbols if s.upper().startswith("BSE:"))
                        # Emit before subscribe so if the call raises we know
                        # what was in flight. MCX list is short enough to
                        # print fully — helps confirm SILVERMIC made it.
                        print(f"[fyers] WS subscribe breakdown: NSE={nse_count} BSE={bse_count} MCX={len(mcx_syms)} (MCX symbols: {mcx_syms})")
                        for i in range(0, len(symbols), 50):
                            socket.subscribe(symbols=symbols[i:i+50], data_type="SymbolUpdate")
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
        text = str(message)
        classified = "Fyers websocket error"
        if "429" in text or "Too Many Requests" in text:
            classified = "Fyers WS 429 rate-limited (handshake throttled)"
        report_status(connected=False, error=text, message=classified)

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
        # SDK auto-reconnect stacks with our watchdog and produces Fyers-side
        # 429 (Too Many Requests) on the WS handshake. We handle reconnects
        # ourselves with exponential backoff in engine._live_feed_watchdog_loop.
        reconnect=False,
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
        missing_critical = _missing_critical_live_feed_symbols(symbols, seen_symbols_with_ticks)
        if missing_critical:
            report_status(
                connected=False,
                error=(
                    "No Fyers market tick received for critical symbols within 30 seconds "
                    f"of subscription: {', '.join(missing_critical)}"
                ),
                message="Fyers websocket is receiving other symbols but not the live strategy feed",
            )
        elif not first_tick_received:
            report_status(
                connected=False,
                error="No Fyers market tick received within 30 seconds of subscription",
                message="Fyers websocket is open but not delivering market data",
            )

    threading.Thread(target=detect_missing_first_tick, daemon=True).start()
    return socket

