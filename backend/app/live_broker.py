"""
LiveBroker wraps the same persistence/accounting model as PaperBroker,
but sends actual Fyers orders before recording the trade in the app.

The live path writes to separate live_* tables so the paper flow stays
isolated and backwards-compatible.
"""
from __future__ import annotations

import datetime
import time

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
        actual_entry_price, actual_entry_time = self._resolve_fill_details(
            symbol=symbol,
            side=side,
            qty=qty,
            order_response=order_response,
            fallback_price=entry_price,
        )
        if side == "BUY":
            sl_price = actual_entry_price * (sl_price / entry_price) if entry_price else sl_price
            target_price = actual_entry_price * (target_price / entry_price) if entry_price else target_price
        else:
            sl_price = actual_entry_price * (sl_price / entry_price) if entry_price else sl_price
            target_price = actual_entry_price * (target_price / entry_price) if entry_price else target_price
        super().open_trade(
            symbol,
            side,
            qty,
            actual_entry_price,
            sl_price,
            target_price,
            entry_trigger,
            signal_snapshot,
            entry_time=actual_entry_time,
        )

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        side = position["side"]
        qty = int(position["qty"])
        exit_side = "SELL" if side == "BUY" else "BUY"
        order_response = self._place_live_order(position["symbol"], exit_side, qty)
        if not self._looks_successful(order_response):
            raise RuntimeError(f"Fyers live exit order failed: {order_response}")
        actual_exit_price, actual_exit_time = self._resolve_fill_details(
            symbol=position["symbol"],
            side=exit_side,
            qty=qty,
            order_response=order_response,
            fallback_price=exit_price,
        )
        super().close_trade(position, actual_exit_price, exit_reason, exit_time=actual_exit_time)

    def _place_live_order(self, symbol: str, side: str, qty: int) -> dict:
        # Railway egress IP is registered as the Fyers app Secondary IP, so
        # orders can egress directly from Railway and land at Fyers with a
        # whitelisted source IP. Bypassing the GCP proxy avoids the silent
        # Google-Cloud-edge drop of Railway's IP range that made every proxy
        # attempt time out for ~48s per order.
        fyers = get_fyers_model(use_proxy=False)
        # Fyers V3 place_order rejects with code -99 "Bad request" if any of
        # limitPrice / stopPrice / stopLoss / takeProfit are missing, even for
        # market orders where the value must be 0. isSliceOrder is not a real
        # V3 field and can also trigger the same rejection — drop it.
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 2,               # 1=Limit 2=Market 3=SL 4=SLM
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
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

    def _resolve_fill_details(
        self,
        symbol: str,
        side: str,
        qty: int,
        order_response: dict,
        fallback_price: float,
    ) -> tuple[float, str]:
        fyers = get_fyers_model("live")
        order_id = self._extract_order_id(order_response)
        deadline = time.time() + 8
        latest_match = None
        while time.time() < deadline:
            latest_match = self._find_latest_fill(fyers, symbol, side, qty, order_id)
            if latest_match:
                break
            time.sleep(0.5)
        price = self._extract_fill_price(latest_match, fallback_price)
        executed_at = self._extract_fill_time(latest_match)
        return price, executed_at

    def _find_latest_fill(
        self,
        fyers,
        symbol: str,
        side: str,
        qty: int,
        order_id: str | None,
    ) -> dict | None:
        candidates: list[dict] = []
        for loader in (lambda: fyers.tradebook(), lambda: fyers.tradehistory({})):
            try:
                response = loader()
            except Exception:
                continue
            for row in self._iter_rows(response):
                if not isinstance(row, dict):
                    continue
                row_symbol = str(row.get("symbol") or row.get("fySymbol") or "").upper()
                if row_symbol != symbol.upper():
                    continue
                row_side = self._normalize_side(row.get("side") or row.get("transactionType") or row.get("buySell"))
                if row_side and row_side != side.upper():
                    continue
                row_order_id = self._extract_order_id(row)
                if order_id and row_order_id and row_order_id != order_id:
                    continue
                row_qty = self._safe_int(
                    row.get("qty"),
                    row.get("filledQty"),
                    row.get("tradedQty"),
                    row.get("fillQty"),
                )
                if row_qty is not None and row_qty != qty:
                    continue
                candidates.append(row)
        if not candidates:
            return None
        candidates.sort(
            key=lambda row: self._parse_fill_time(row) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        )
        return candidates[-1]

    def _iter_rows(self, response) -> list[dict]:
        if isinstance(response, list):
            return [row for row in response if isinstance(row, dict)]
        if not isinstance(response, dict):
            return []
        for key in (
            "tradeBook",
            "tradebook",
            "tradeHistory",
            "tradehistory",
            "orderBook",
            "orderbook",
            "data",
        ):
            rows = response.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
            if isinstance(rows, dict):
                for nested_key in ("rows", "items", "orders", "trades"):
                    nested_rows = rows.get(nested_key)
                    if isinstance(nested_rows, list):
                        return [row for row in nested_rows if isinstance(row, dict)]
        return []

    def _extract_order_id(self, row: dict | None) -> str | None:
        if not isinstance(row, dict):
            return None
        for key in ("id", "order_id", "orderId", "fyOrderId", "exchangeOrderId"):
            value = row.get(key)
            if value:
                return str(value)
        return None

    def _normalize_side(self, raw) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip().upper()
        if text in {"1", "BUY", "B"}:
            return "BUY"
        if text in {"-1", "SELL", "S"}:
            return "SELL"
        return None

    def _safe_float(self, *values) -> float | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _safe_int(self, *values) -> int | None:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return None

    def _extract_fill_price(self, row: dict | None, fallback_price: float) -> float:
        if not isinstance(row, dict):
            return float(fallback_price)
        return float(
            self._safe_float(
                row.get("tradedPrice"),
                row.get("tradePrice"),
                row.get("tradeAvgPrice"),
                row.get("avgPrice"),
                row.get("price"),
                row.get("ltp"),
                fallback_price,
            )
            or fallback_price
        )

    def _extract_fill_time(self, row: dict | None) -> str:
        parsed = self._parse_fill_time(row)
        if parsed:
            return parsed.isoformat()
        return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

    def _parse_fill_time(self, row: dict | None) -> datetime.datetime | None:
        if not isinstance(row, dict):
            return None
        for key in (
            "tradeTime",
            "tradeDateTime",
            "tradedAt",
            "tradedOn",
            "updatedAt",
            "updated_at",
            "orderDateTime",
            "createdAt",
            "timestamp",
            "time",
        ):
            value = row.get(key)
            parsed = self._coerce_datetime(value)
            if parsed:
                return parsed
        return None

    def _coerce_datetime(self, value) -> datetime.datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            if number > 1_000_000_000_000:
                number /= 1000
            try:
                return datetime.datetime.fromtimestamp(number, tz=datetime.timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        iso_candidate = text.replace("Z", "+00:00")
        try:
            parsed = datetime.datetime.fromisoformat(iso_candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
        except ValueError:
            pass
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
        return None
