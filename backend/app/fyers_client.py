"""
fyers_client.py — thin wrapper around Fyers' live WebSocket and
historical candle REST API, used by engine.py.
"""
import datetime
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from .config import FYERS_CLIENT_ID
from .fyers_auth import get_stored_access_token, get_stored_token_row


def get_fyers_model():
    token = get_stored_access_token()
    if not token:
        raise RuntimeError("No Fyers access token in Supabase yet. Use the Login to Fyers button first.")
    return fyersModel.FyersModel(token=token, is_async=False, client_id=FYERS_CLIENT_ID, log_path="")


def get_connection_status() -> dict:
    token_row = get_stored_token_row()
    token = token_row.get("access_token") if token_row else None
    refresh_token_present = bool(token_row and token_row.get("refresh_token"))
    if not token:
        return {
            "connected": False,
            "status": "disconnected",
            "message": "No Fyers access token found. Login to Fyers before trading.",
            "refresh_token_present": refresh_token_present,
        }

    try:
        response = get_fyers_model().get_profile()
    except Exception as exc:
        return {
            "connected": False,
            "status": "error",
            "message": f"Fyers token check failed: {exc}",
            "refresh_token_present": refresh_token_present,
            "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
            "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
        }

    if response.get("s") == "ok":
        return {
            "connected": True,
            "status": "connected",
            "message": "Fyers token is valid.",
            "refresh_token_present": refresh_token_present,
            "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
            "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
        }

    return {
        "connected": False,
        "status": "expired",
        "message": response.get("message") or "Fyers token is missing, expired, or rejected.",
        "refresh_token_present": refresh_token_present,
        "access_token_updated_at": token_row.get("access_token_updated_at") or token_row.get("updated_at"),
        "refresh_token_updated_at": token_row.get("refresh_token_updated_at"),
    }


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


def connect_live_feed(symbols: list[str], on_tick_callback):
    token = get_stored_access_token()
    if not token:
        raise RuntimeError("No Fyers access token in Supabase yet")

    def on_message(message):
        on_tick_callback(message)

    def on_open():
        socket.subscribe(symbols=symbols, data_type="SymbolUpdate")

    def on_error(message):
        print("Fyers WS error:", message)

    def on_close(message):
        print("Fyers WS closed:", message)

    socket = data_ws.FyersDataSocket(
        access_token=f"{FYERS_CLIENT_ID}:{token}",
        log_path="",
        on_connect=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    socket.connect()
    return socket
