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

        # ── HARD SL + TARGET AT FYERS ──────────────────────────────────
        # Place protective orders IMMEDIATELY after entry fill so Fyers
        # holds the SL/Target server-side. If our app crashes at any
        # point, Fyers still auto-exits when either level is touched.
        # Both orders are on the reverse side and MIS/INTRADAY so they're
        # linked to the same intraday position.
        entry_order_id = self._extract_order_id(order_response)
        protective = self._place_protective_orders(
            symbol=symbol,
            entry_side=side,
            qty=qty,
            sl_price=sl_price,
            target_price=target_price,
        )
        # Persist Fyers order IDs in signal_snapshot so the reconciliation
        # thread can look them up and detect SL/Target fills without any
        # in-memory dependency (survives container restarts).
        merged_snapshot = dict(signal_snapshot or {})
        merged_snapshot["fyers_entry_order_id"] = entry_order_id
        merged_snapshot["fyers_sl_order_id"] = protective.get("sl_order_id")
        merged_snapshot["fyers_target_order_id"] = protective.get("target_order_id")
        merged_snapshot["fyers_sl_error"] = protective.get("sl_error")
        merged_snapshot["fyers_target_error"] = protective.get("target_error")

        super().open_trade(
            symbol,
            side,
            qty,
            actual_entry_price,
            sl_price,
            target_price,
            entry_trigger,
            merged_snapshot,
            entry_time=actual_entry_time,
        )

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        side = position["side"]
        qty = int(position["qty"])
        exit_side = "SELL" if side == "BUY" else "BUY"

        # Cancel any pending protective orders BEFORE placing the manual
        # market exit. Otherwise the SL/Target order stays live at Fyers
        # and can fire against our closed position — which Fyers would
        # execute as a fresh reverse trade (accidental short/long).
        snapshot = position.get("signal_snapshot") or {}
        for order_id_key in ("fyers_sl_order_id", "fyers_target_order_id"):
            protective_order_id = snapshot.get(order_id_key)
            if protective_order_id:
                self._cancel_fyers_order(protective_order_id, reason=f"close_trade:{exit_reason}")

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

    # ── Protective order helpers ──────────────────────────────────────
    def _place_protective_orders(
        self,
        symbol: str,
        entry_side: str,
        qty: int,
        sl_price: float,
        target_price: float,
    ) -> dict:
        """Place SL (SLM) + Target (LIMIT) orders on the reverse side of
        the entry. Returns Fyers order IDs (or None + error) for each."""
        exit_side = "SELL" if entry_side.upper() == "BUY" else "BUY"
        result = {
            "sl_order_id": None,
            "target_order_id": None,
            "sl_error": None,
            "target_error": None,
        }

        # Stop-Loss Market (type=4). Fires as market when stopPrice touched.
        try:
            sl_response = self._place_slm_order(symbol, exit_side, qty, sl_price)
            if self._looks_successful(sl_response):
                result["sl_order_id"] = self._extract_order_id(sl_response)
            else:
                result["sl_error"] = str(sl_response)
                print(f"[live_broker] SL order rejected {symbol}: {sl_response}")
        except Exception as exc:
            result["sl_error"] = str(exc)
            print(f"[live_broker] SL order exception {symbol}: {exc}")

        # Take-profit Limit (type=1). Fills at target price if market reaches it.
        try:
            tp_response = self._place_limit_order(symbol, exit_side, qty, target_price)
            if self._looks_successful(tp_response):
                result["target_order_id"] = self._extract_order_id(tp_response)
            else:
                result["target_error"] = str(tp_response)
                print(f"[live_broker] Target order rejected {symbol}: {tp_response}")
        except Exception as exc:
            result["target_error"] = str(exc)
            print(f"[live_broker] Target order exception {symbol}: {exc}")

        return result

    def _place_slm_order(self, symbol: str, side: str, qty: int, stop_price: float) -> dict:
        fyers = get_fyers_model(use_proxy=True)
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 4,                        # SLM
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": round(float(stop_price), 2),
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        response = fyers.place_order(payload)
        print(f"[live_broker] place_slm {symbol} {side} x{qty} @stop={stop_price}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def _place_limit_order(self, symbol: str, side: str, qty: int, limit_price: float) -> dict:
        fyers = get_fyers_model(use_proxy=True)
        payload = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 1,                        # LIMIT
            "side": 1 if side.upper() == "BUY" else -1,
            "productType": "INTRADAY",
            "limitPrice": round(float(limit_price), 2),
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        response = fyers.place_order(payload)
        print(f"[live_broker] place_limit {symbol} {side} x{qty} @limit={limit_price}: {response}")
        return response if isinstance(response, dict) else {"raw": response}

    def _cancel_fyers_order(self, order_id: str, reason: str = "") -> dict:
        try:
            fyers = get_fyers_model(use_proxy=True)
            response = fyers.cancel_order({"id": str(order_id)})
            print(f"[live_broker] cancel_order {order_id} ({reason}): {response}")
            return response if isinstance(response, dict) else {"raw": response}
        except Exception as exc:
            print(f"[live_broker] cancel_order failed {order_id} ({reason}): {exc}")
            return {"s": "error", "message": str(exc)}

    def reconcile_open_positions(self) -> dict:
        """Poll Fyers orderbook and detect SL/Target fills. For each fill:
        close our position record + cancel the sibling protective order so
        it doesn't accidentally reverse the position later. Meant to be
        called every ~30 seconds by the engine background loop.

        Returns a summary dict: {'reconciled': N, 'errors': M}.
        """
        summary = {"reconciled": 0, "errors": 0, "already_closed": 0}
        open_positions = self.open_positions()
        if not open_positions:
            return summary

        # Fetch the orderbook once and index by order_id for O(1) lookup.
        try:
            fyers = get_fyers_model(use_proxy=False)  # read-only, direct
            response = fyers.orderbook()
        except Exception as exc:
            print(f"[live_broker] reconcile orderbook fetch failed: {exc}")
            summary["errors"] += 1
            return summary

        orders_by_id: dict[str, dict] = {}
        for row in self._iter_rows(response):
            oid = self._extract_order_id(row)
            if oid:
                orders_by_id[oid] = row

        for position in open_positions:
            snapshot = position.get("signal_snapshot") or {}
            sl_id = snapshot.get("fyers_sl_order_id")
            tp_id = snapshot.get("fyers_target_order_id")
            symbol = position.get("symbol")

            filled_order = None
            filled_reason = None
            sibling_id = None

            for oid, reason, sibling in (
                (sl_id, "SL", tp_id),
                (tp_id, "TARGET", sl_id),
            ):
                if not oid:
                    continue
                order = orders_by_id.get(str(oid))
                if not order:
                    continue
                # Fyers orderbook status codes: 2 = FILLED / TRADED,
                # 1 = CANCELLED, 5 = REJECTED, 4 = TRANSIT, 6 = PENDING.
                status_code = self._safe_int(order.get("status"))
                if status_code == 2:
                    filled_order = order
                    filled_reason = reason
                    sibling_id = sibling
                    break

            if not filled_order:
                continue

            # Cancel the sibling protective order (if it's still open) so
            # it doesn't fire against a flat position.
            if sibling_id:
                sibling_order = orders_by_id.get(str(sibling_id))
                if sibling_order:
                    sibling_status = self._safe_int(sibling_order.get("status"))
                    if sibling_status in {4, 6}:  # transit / pending
                        self._cancel_fyers_order(
                            sibling_id,
                            reason=f"{filled_reason}_hit_cancel_sibling",
                        )

            # Record the exit in our positions/trades tables via
            # PaperBroker.close_trade, using the actual Fyers fill price
            # from the filled order.
            fill_price = self._extract_fill_price(filled_order, float(position.get("sl_price") if filled_reason == "SL" else position.get("target_price")))
            try:
                super().close_trade(
                    position,
                    fill_price,
                    exit_reason=f"{filled_reason}_FYERS",
                    exit_time=self._extract_fill_time(filled_order),
                )
                summary["reconciled"] += 1
                print(f"[live_broker] reconciled {symbol} {filled_reason} @ {fill_price}")
            except Exception as exc:
                summary["errors"] += 1
                print(f"[live_broker] reconcile close_trade failed for {symbol}: {exc}")

        return summary

    def _place_live_order(self, symbol: str, side: str, qty: int) -> dict:
        # Railway's egress IP pool has more entries than Fyers can whitelist
        # (Fyers app accepts only 2 IPs total), so we route orders through
        # the GCP Squid proxy so Fyers only ever sees one source IP
        # (34.100.255.224 = the GCP proxy's public IP, set as Fyers Primary IP).
        # Google Cloud silently drops Railway packets to the GCP VM directly,
        # so LIVE_FYERS_PROXY_URL must point at a Cloudflare Tunnel hostname
        # that terminates at the Squid box on the GCP VM.
        fyers = get_fyers_model(use_proxy=True)
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
        # Fyers V3 order responses always include an "id" field, even when
        # the order was REJECTED (e.g. code -99 "RED:'MIS' Orders are
        # disallowed after system square off" still ships with id). Earlier
        # revision treated presence of id as success and recorded phantom
        # positions in the paper broker table for orders Fyers never opened.
        # Trust only the explicit status string.
        if not isinstance(response, dict):
            return False
        status = str(response.get("s") or response.get("status") or "").lower()
        if status in {"error", "err", "failed", "reject", "rejected"}:
            return False
        if status in {"ok", "success", "accepted"}:
            return True
        # No status field at all — fall back to the id heuristic, but only
        # when there is no error/message field indicating a failure.
        has_error_indicator = any(
            response.get(k) for k in ("message", "error", "errmsg", "code")
        )
        if has_error_indicator:
            return False
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
