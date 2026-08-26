"""
fyers_order_updates.py — real-time Fyers → us sync via the Order Update
WebSocket (separate endpoint from market data).

Design B, Component 1 (2026-08-26). Motivation: today the client hit Exit
in the Fyers app; our reconciliation is polling-only (30s cadence, plus
2-poll confirmation and 300s grace) so our DB was stale for ~5 min and
the algo refused new entries the whole time. This module pushes Fyers
events (order fills, cancellations, position snapshots) to us in near-
real-time so LiveBroker.handle_order_event can act within a second.

Public API mirrors connect_live_feed() in fyers_client.py:
  socket = connect_order_update_feed(on_event_callback, on_status_callback)

The SDK is imported lazily so this module remains importable in test
environments that don't have fyers-apiv3 installed.
"""
from __future__ import annotations

import threading
import time

from .fyers_client import get_fyers_config, get_stored_access_token

try:
    from fyers_apiv3.FyersWebsocket import order_ws  # type: ignore
except Exception:  # pragma: no cover - only hit in bare test envs
    order_ws = None


_FEED_LABEL = "[fyers:order_updates]"


def _normalize_order_row(row: dict) -> dict:
    """Turn a Fyers 'orders' payload into the shape LiveBroker expects.

    Fyers V3 order rows use these keys (documented; we keep the raw payload
    around too for anything future code needs):
      id / orderNumber   → order_id
      symbol             → symbol
      status             → status (int; 2=FILLED, 1=CANCELLED, 5=REJECTED,
                                    4=TRANSIT, 6=PENDING)
      side               → +1 BUY, -1 SELL
      tradedPrice        → traded_price
      filledQty          → traded_qty
      orderDateTime      → traded_at
    """
    if not isinstance(row, dict):
        return {}
    order_id = row.get("id") or row.get("orderNumber") or row.get("order_id")
    side_raw = row.get("side")
    if isinstance(side_raw, (int, float)):
        side = "BUY" if int(side_raw) > 0 else "SELL"
    else:
        side = str(side_raw or "").upper()
    return {
        "kind": "order",
        "symbol": str(row.get("symbol") or "").strip(),
        "order_id": str(order_id) if order_id is not None else "",
        "status": row.get("status"),
        "side": side,
        "traded_price": row.get("tradedPrice") or row.get("traded_price"),
        "traded_qty": row.get("filledQty") or row.get("traded_qty"),
        "traded_at": row.get("orderDateTime") or row.get("traded_at"),
        "raw": row,
    }


def _normalize_position_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    return {
        "kind": "position",
        "symbol": str(row.get("symbol") or "").strip(),
        "net_qty": row.get("netQty") if row.get("netQty") is not None else row.get("net_qty"),
        "raw": row,
    }


def _normalize_trade_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    return {
        "kind": "trade",
        "symbol": str(row.get("symbol") or "").strip(),
        "order_id": str(row.get("orderNumber") or row.get("id") or ""),
        "traded_price": row.get("tradePrice") or row.get("traded_price"),
        "traded_qty": row.get("tradedQty") or row.get("traded_qty"),
        "traded_at": row.get("orderDateTime") or row.get("traded_at"),
        "raw": row,
    }


def dispatch_message(message: dict, on_event_callback) -> int:
    """Parse a raw Fyers Order-WS payload into 1..N normalized events and
    push each through on_event_callback. Returns the count dispatched.

    Kept as a top-level function so smoke tests can exercise the parsing
    without spinning up a real websocket.

    Payload shapes observed on Fyers V3 (they wrap the interesting bit in
    either "orders" or "positions" or "trades" depending on the channel):
      {"s": "ok", "orders": {...single order row...}}
      {"s": "ok", "positions": [{...row...}, ...]}
      {"s": "ok", "trades": {...single trade row...}}
    Some SDK versions include a top-level "d" wrapper — we handle both.
    """
    if not isinstance(message, dict):
        return 0
    payload = message.get("d") if isinstance(message.get("d"), dict) else message

    dispatched = 0

    orders_field = payload.get("orders")
    if orders_field is not None:
        rows = orders_field if isinstance(orders_field, list) else [orders_field]
        for row in rows:
            event = _normalize_order_row(row)
            if event.get("symbol"):
                try:
                    on_event_callback(event)
                except Exception as exc:
                    print(f"{_FEED_LABEL} order-event handler raised: {exc}")
                dispatched += 1

    positions_field = payload.get("positions")
    if positions_field is not None:
        rows = positions_field if isinstance(positions_field, list) else [positions_field]
        for row in rows:
            event = _normalize_position_row(row)
            if event.get("symbol"):
                try:
                    on_event_callback(event)
                except Exception as exc:
                    print(f"{_FEED_LABEL} position-event handler raised: {exc}")
                dispatched += 1

    trades_field = payload.get("trades")
    if trades_field is not None:
        rows = trades_field if isinstance(trades_field, list) else [trades_field]
        for row in rows:
            event = _normalize_trade_row(row)
            if event.get("symbol"):
                try:
                    on_event_callback(event)
                except Exception as exc:
                    print(f"{_FEED_LABEL} trade-event handler raised: {exc}")
                dispatched += 1

    return dispatched


def connect_order_update_feed(on_event_callback, on_status_callback=None):
    """Open the Fyers Order Update WebSocket and stream events.

    Returns the underlying SDK socket handle so callers can close it on
    shutdown. Raises RuntimeError if the SDK is missing or no access
    token is stored yet.
    """
    if order_ws is None:
        raise RuntimeError(
            "Fyers order-update WebSocket SDK not installed; install fyers-apiv3."
        )
    token = get_stored_access_token()
    if not token:
        raise RuntimeError("No Fyers access token in Supabase yet")

    subscription_sent = False
    subscription_lock = threading.Lock()

    def report(**data):
        if on_status_callback:
            data.setdefault("feed_name", "order_updates")
            on_status_callback(data)

    def on_message(message):
        dispatch_message(message, on_event_callback)

    def subscribe_when_ready():
        nonlocal subscription_sent
        for _ in range(30):
            if getattr(socket, "is_connected", lambda: True)():
                with subscription_lock:
                    if subscription_sent:
                        return
                    try:
                        for data_type in ("OnOrders", "OnTrades", "OnPositions"):
                            socket.subscribe(data_type=data_type)
                        subscription_sent = True
                    except Exception as exc:
                        print(f"{_FEED_LABEL} subscribe error: {exc}")
                        report(connected=False, error=str(exc),
                               message="Order-update WS subscription failed")
                        return
                print(f"{_FEED_LABEL} subscribed to OnOrders + OnTrades + OnPositions")
                report(connected=True, message="Order-update WS subscribed")
                return
            time.sleep(0.5)
        report(connected=False, error="timeout",
               message="Order-update WS did not open within 15s")

    def on_open():
        threading.Thread(target=subscribe_when_ready, daemon=True).start()

    def on_error(msg):
        print(f"{_FEED_LABEL} WS error: {msg}")
        report(connected=False, error=str(msg), message="Order-update WS error")

    def on_close(msg):
        nonlocal subscription_sent
        with subscription_lock:
            subscription_sent = False
        print(f"{_FEED_LABEL} WS closed: {msg}")
        report(connected=False, error=str(msg), message="Order-update WS closed")

    access = f"{get_fyers_config()['client_id']}:{token}"
    socket = order_ws.FyersOrderSocket(
        access_token=access,
        log_path="",
        write_to_file=False,
        reconnect=False,
        on_connect=on_open,
        on_orders=on_message,
        on_trades=on_message,
        on_positions=on_message,
        on_general=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    try:
        socket.connect()
    except Exception as exc:
        report(connected=False, error=str(exc), message="Order-update WS connect failed")
        raise
    return socket
