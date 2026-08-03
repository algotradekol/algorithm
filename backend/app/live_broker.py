"""
LiveBroker wraps the same persistence/accounting model as PaperBroker,
but sends actual Fyers orders before recording the trade in the app.

The live path writes to separate live_* tables so the paper flow stays
isolated and backwards-compatible.
"""
from __future__ import annotations

from .fyers_client import get_fyers_model, get_wallet_balance
from .paper_broker import PaperBroker


class LiveBroker(PaperBroker):
    def state_table_name(self) -> str:
        return "live_algo_state"

    def positions_table_name(self) -> str:
        return "live_positions"

    def trades_table_name(self) -> str:
        return "live_trades"

    def close_stale_open_positions(self) -> int:
        # Avoid auto-closing real broker positions on startup. Reconcile them
        # manually or through a dedicated live clean-up flow instead.
        return 0

    def open_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        entry_trigger: str | None = None,
        signal_snapshot: dict | None = None,
    ):
        qty = self._cap_qty_to_live_funds(qty, entry_price)
        if qty < 1:
            raise RuntimeError("Fyers live order failed: available live funds are below the current share price.")
        order_response = self._place_live_order(symbol, side, qty)
        if not self._looks_successful(order_response):
            raise RuntimeError(f"Fyers live order failed: {order_response}")
        super().open_trade(symbol, side, qty, entry_price, sl_price, target_price, entry_trigger, signal_snapshot)

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        side = position["side"]
        qty = int(position["qty"])
        exit_side = "SELL" if side == "BUY" else "BUY"
        order_response = self._place_live_order(position["symbol"], exit_side, qty)
        if not self._looks_successful(order_response):
            raise RuntimeError(f"Fyers live exit order failed: {order_response}")
        super().close_trade(position, exit_price, exit_reason)

    def _place_live_order(self, symbol: str, side: str, qty: int) -> dict:
        fyers = get_fyers_model()
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 2,  # market order
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "isSliceOrder": False,
        }
        response = fyers.place_order(payload)
        print(f"[live_broker] place_order {symbol} {side} x{qty}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def _looks_successful(self, response: dict) -> bool:
        if not isinstance(response, dict):
            return False
        status = str(response.get("s") or response.get("status") or "").lower()
        if status in {"ok", "success", "accepted"}:
            return True
        return any(response.get(key) for key in ("id", "order_id", "orderId"))

    def _cap_qty_to_live_funds(self, requested_qty: int, entry_price: float) -> int:
        if requested_qty < 1 or entry_price <= 0:
            return 0
        try:
            funds = get_wallet_balance("live")
        except Exception:
            return int(requested_qty)

        summary = funds.get("summary") if isinstance(funds, dict) else {}
        if not isinstance(summary, dict):
            return int(requested_qty)

        balance = summary.get("available_margin")
        if balance is None:
            balance = summary.get("wallet_balance")
        try:
            affordable_qty = int(float(balance) // float(entry_price))
        except (TypeError, ValueError, ZeroDivisionError):
            return int(requested_qty)
        if affordable_qty < 1:
            return 0
        return min(int(requested_qty), affordable_qty)
