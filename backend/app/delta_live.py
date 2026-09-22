"""Durable Delta live execution with local strategy tracking."""
from __future__ import annotations

import copy
import datetime
import time
import uuid

from .delta_client import positive
from .delta_alerts import alert_trade_close, alert_trade_open
from .delta_log import delta_log
from .trailing_stop import calculate_point_trailing
from .delta_paper import DeltaPaperBroker, POST_EXIT_REST_REASONS, utc_now
from .delta_reporting import paper_margin


def _fmt_price(value) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def _order_id(row):
    return row.get("id") if isinstance(row, dict) else None


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class DeltaLiveBroker(DeltaPaperBroker):
    """Live Delta orders, while retaining the same state shape as paper.

    Entries are market IOC. Protective exits are exchange-side reduce-only
    trigger orders, so a late app-side exit attempt cannot open a reverse leg.
    """

    def __init__(self, store, defaults, product, client):
        super().__init__(store, defaults, product)
        self.client = client
        self.mode = "live"
        self.entry_guard = None
        self._cancel_error_logged_at = {}
        self._close_block_logged_at = {}

    def _side(self, side):
        return "buy" if side == "BUY" else "sell"

    def _opposite_side(self, side):
        return "sell" if side == "BUY" else "buy"

    def _product_id(self):
        product_id = self.product.get("id")
        if product_id is None:
            raise ValueError("Delta product_id unavailable; cannot place live order")
        return int(product_id)

    def _client_order_id(self, prefix):
        return f"{prefix}_{uuid.uuid4().hex}"[:32]

    def _place_order(self, payload):
        delta_log("live_order_submit", path="/v2/orders", payload={k: v for k, v in payload.items() if k != "client_order_id"})
        return self.client.post("/v2/orders", payload, private=True)

    def _place_bracket_order(self, side, stop_price, target_price):
        payload = {
            "product_id": self._product_id(),
            "stop_loss_order": {
                "order_type": "market_order",
                "stop_price": _fmt_price(stop_price),
            },
            "take_profit_order": {
                "order_type": "market_order",
                "stop_price": _fmt_price(target_price),
            },
            "bracket_stop_trigger_method": "last_traded_price",
        }
        delta_log("live_bracket_submit", side=side, payload=payload)
        return self.client.post("/v2/orders/bracket", payload, private=True, envelope=True)

    def _product_row(self, row):
        product = row.get("product") or {}
        product_id = row.get("product_id") or product.get("id")
        return str(product_id) == str(self._product_id()) or row.get("product_symbol") == self.client.symbol or product.get("symbol") == self.client.symbol

    def _active_product_orders(self):
        payload = self.client.get(
            "/v2/orders",
            {"page_size": 100, "states": "open,pending", "product_ids": str(self._product_id())},
            private=True,
            envelope=True,
        )
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise RuntimeError("Delta active orders response was invalid")
        filtered = [row for row in rows if isinstance(row, dict) and self._product_row(row)]
        delta_log(
            "live_active_orders",
            symbol=self.client.symbol,
            total_rows=len(rows),
            product_rows=len(filtered),
            orders=[
                {
                    "id": row.get("id"),
                    "state": row.get("state"),
                    "side": row.get("side"),
                    "size": row.get("size"),
                    "order_type": row.get("order_type"),
                    "stop_order_type": row.get("stop_order_type"),
                    "stop_price": row.get("stop_price"),
                    "limit_price": row.get("limit_price"),
                    "reduce_only": row.get("reduce_only"),
                }
                for row in filtered
            ],
        )
        return filtered

    def _live_product_size(self):
        payload = self.client.get("/v2/positions/margined", private=True, envelope=True)
        rows = payload.get("result")
        if not isinstance(rows, list):
            raise RuntimeError("Delta positions response was invalid")
        total = 0.0
        for row in rows:
            if not isinstance(row, dict) or not self._product_row(row):
                continue
            try:
                total += float(row.get("size") or 0)
            except (TypeError, ValueError):
                continue
        delta_log("live_product_size", symbol=self.client.symbol, size=total, rows=len(rows))
        return total

    def _is_protection_order(self, row):
        stop_type = row.get("stop_order_type")
        return stop_type in {"stop_loss_order", "take_profit_order"} or bool(row.get("reduce_only"))

    def _cancel_orphan_protection_orders(self):
        if abs(self._live_product_size()) > 0:
            return []
        cancelled = []
        for row in self._active_product_orders():
            if not self._is_protection_order(row):
                continue
            order_id = _order_id(row)
            if order_id:
                if self._cancel_order(order_id):
                    cancelled.append(order_id)
        if cancelled:
            print(f"[delta-live] cancelled orphan protection orders for {self.client.symbol}: {cancelled}")
        delta_log("live_orphan_cleanup", symbol=self.client.symbol, cancelled=cancelled)
        return cancelled

    def _bracket_children(self, rows=None, expected_side=None):
        children = {"stop_order": None, "target_order": None}
        for row in (self._active_product_orders() if rows is None else rows):
            if not self._product_row(row):
                continue
            if expected_side and row.get("side") != expected_side:
                continue
            if row.get("reduce_only") is False:
                continue
            stop_type = row.get("stop_order_type")
            if stop_type == "stop_loss_order" and not children["stop_order"]:
                children["stop_order"] = row
            elif stop_type == "take_profit_order" and not children["target_order"]:
                children["target_order"] = row
        if not children["stop_order"] or not children["target_order"]:
            delta_log("live_bracket_children_missing", symbol=self.client.symbol, children=children)
            raise RuntimeError("Delta bracket did not expose both stop-loss and target child orders")
        delta_log(
            "live_bracket_children_ok",
            symbol=self.client.symbol,
            stop_order_id=_order_id(children["stop_order"]),
            target_order_id=_order_id(children["target_order"]),
        )
        return children

    def _edit_bracket_children(self, children, qty, stop_price, target_price):
        responses = []
        for row, desired in (
            (children["stop_order"], stop_price),
            (children["target_order"], target_price),
        ):
            payload = {
                "product_id": self._product_id(),
                "size": int(qty),
                "stop_price": _fmt_price(desired),
            }
            delta_log("live_existing_bracket_leg_edit", order_id=_order_id(row), payload=payload)
            responses.append(self._edit_order(_order_id(row), payload))
        return responses

    def _adopt_existing_bracket(self, side, qty, stop_price, target_price, cause):
        rows = self._active_product_orders()
        children = self._bracket_children(rows=rows, expected_side=self._opposite_side(side))
        responses = self._edit_bracket_children(children, qty, stop_price, target_price)
        # Re-read after editing so the stored child payload matches exchange reality.
        refreshed = self._active_product_orders()
        children = self._bracket_children(rows=refreshed, expected_side=self._opposite_side(side))
        delta_log(
            "live_existing_bracket_adopted",
            side=side,
            qty=qty,
            stop_order_id=_order_id(children["stop_order"]),
            target_order_id=_order_id(children["target_order"]),
            cause=str(cause),
            edit_responses=responses,
        )
        return children, {"existing_bracket": True, "cause": str(cause), "edits": responses}

    def _place_or_adopt_bracket(self, side, qty, stop_price, target_price):
        try:
            bracket_response = self._place_bracket_order(side, stop_price, target_price)
            return self._bracket_children(expected_side=self._opposite_side(side)), bracket_response
        except Exception as exc:
            if "bracket_order_exists" not in str(exc):
                raise
            return self._adopt_existing_bracket(side, qty, stop_price, target_price, exc)

    def _emergency_close_unprotected_entry(self, side, qty, cause):
        try:
            delta_log("live_emergency_close_start", side=side, qty=qty, cause=str(cause))
            close_order = self._place_order({
                "product_id": self._product_id(),
                "size": int(qty),
                "side": self._opposite_side(side),
                "order_type": "market_order",
                "time_in_force": "ioc",
                "reduce_only": True,
                "client_order_id": self._client_order_id("dle_fail"),
            })
            self._require_filled_order(close_order, "emergency close")
        except Exception as close_exc:
            delta_log("live_emergency_close_failed", side=side, qty=qty, cause=str(cause), close_error=str(close_exc))
            raise RuntimeError(
                f"Delta live entry filled but bracket protection failed ({cause}) and emergency close also failed: {close_exc}"
            ) from close_exc
        delta_log("live_emergency_close_sent", side=side, qty=qty, cause=str(cause))
        raise RuntimeError(f"Delta live entry filled but bracket protection failed; emergency close sent: {cause}") from cause

    def _require_filled_order(self, order, purpose):
        if not isinstance(order, dict):
            raise RuntimeError(f"Delta live {purpose} did not return an order object")
        fill_price = _number(order.get("average_fill_price"))
        if fill_price is None or fill_price <= 0:
            raise RuntimeError(f"Delta live {purpose} was not filled; order_id={_order_id(order)} state={order.get('state')}")
        unfilled = _number(order.get("unfilled_size"), 0.0)
        if unfilled and unfilled > 0:
            raise RuntimeError(
                f"Delta live {purpose} only partially filled; "
                f"order_id={_order_id(order)} unfilled_size={unfilled:g}"
            )
        state = str(order.get("state") or "").lower()
        if state in {"open", "pending"}:
            raise RuntimeError(f"Delta live {purpose} is still {state}; order_id={_order_id(order)}")
        return positive(fill_price)

    def _edit_order(self, order_id, payload):
        if not order_id:
            raise ValueError("Delta order id missing")
        return self.client.put("/v2/orders", {"id": int(order_id), **payload}, private=True)

    def _cancel_order(self, order_id):
        if not order_id:
            return None
        try:
            return self.client.delete("/v2/orders", {"id": int(order_id), "product_id": self._product_id()}, private=True)
        except RuntimeError as exc:
            key = (str(order_id), str(exc))
            now = time.time()
            if now - self._cancel_error_logged_at.get(key, 0) >= 60:
                print(f"[delta-live] cancel ignored for order {order_id}: {exc}")
                self._cancel_error_logged_at[key] = now
            return None

    def _close_retry_blocked(self, current):
        retry_after = float(current.get("live_close_retry_after") or 0)
        if retry_after <= time.time():
            return None
        key = current.get("id")
        now = time.time()
        remaining = max(1, int(retry_after - now))
        error = current.get("live_close_error", {}).get("message", "previous Delta live close failed")
        if now - self._close_block_logged_at.get(key, 0) >= 60:
            print(f"[delta-live] close retry paused for {remaining}s: {error}")
            self._close_block_logged_at[key] = now
        return f"Delta live close retry paused for {remaining}s after previous failure: {error}"

    def _record_close_failure(self, current, exit_reason, exc):
        now = time.time()
        error = {
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "reason": exit_reason,
            "message": str(exc),
            "retry_after": now + 60,
        }
        state = copy.deepcopy(self.state)
        if state.get("position") and state["position"].get("id") == current.get("id"):
            attempts = int(state["position"].get("live_close_attempts") or 0) + 1
            state["position"]["live_close_attempts"] = attempts
            state["position"]["live_close_error"] = error
            state["position"]["live_close_retry_after"] = error["retry_after"]
            state["live_close_error"] = error
            try:
                self.commit(state)
            except Exception as save_exc:
                print(f"[delta-live] failed to persist close failure: {save_exc}")
        print(f"[delta-live] close failed reason={exit_reason}: {exc}")
        raise RuntimeError(f"Delta live close failed ({exit_reason}): {exc}") from exc

    def _protection_payload(self, side, qty, stop_price, kind):
        return {
            "product_id": self._product_id(),
            "size": int(qty),
            "side": self._opposite_side(side),
            "order_type": "market_order",
            "stop_order_type": kind,
            "stop_price": _fmt_price(stop_price),
            "stop_trigger_method": "last_traded_price",
            "time_in_force": "gtc",
            "reduce_only": True,
            "client_order_id": self._client_order_id("dlx"),
        }

    def _require_pending_order(self, order, purpose):
        if not isinstance(order, dict) or not _order_id(order):
            raise RuntimeError(f"Delta live {purpose} did not return a usable order object")
        state = str(order.get("state") or "").lower()
        if state in {"cancelled", "closed", "rejected"}:
            raise RuntimeError(f"Delta live {purpose} was not left pending; order_id={_order_id(order)} state={state}")
        return order

    def _place_protection_orders(self, side, qty, sl_price, target_price):
        placed = []
        try:
            stop_order = self._require_pending_order(
                self._place_order(self._protection_payload(side, qty, sl_price, "stop_loss_order")),
                "stop protection",
            )
            placed.append(_order_id(stop_order))
            target_order = self._require_pending_order(
                self._place_order(self._protection_payload(side, qty, target_price, "take_profit_order")),
                "target protection",
            )
            return stop_order, target_order
        except Exception:
            for order_id in placed:
                self._cancel_order(order_id)
            raise

    def open_trade(self, symbol, side, qty, entry_price, sl_price, target_price, trigger, snapshot, entry_time=None):
        if self.state.get("position"):
            raise ValueError("A Delta live position is already tracked")
        if self.entry_guard:
            self.entry_guard(side)
        if min(entry_price, sl_price, target_price) <= 0:
            raise ValueError("Entry, target and stop must remain positive")
        qty = int(qty)
        if qty < 1:
            raise ValueError("Delta live size must be at least 1 lot")
        delta_log(
            "live_open_start",
            symbol=symbol,
            side=side,
            qty=qty,
            requested_entry=entry_price,
            sl=sl_price,
            target=target_price,
            trigger=trigger,
            setup_time=(snapshot or {}).get("setup_time"),
        )
        self._cancel_orphan_protection_orders()
        product_id = self._product_id()
        entry = self._place_order({
            "product_id": product_id,
            "size": qty,
            "side": self._side(side),
            "order_type": "market_order",
            "time_in_force": "ioc",
            "reduce_only": False,
            "client_order_id": self._client_order_id("dle"),
        })
        fill_price = self._require_filled_order(entry, "entry")
        delta_log("live_entry_filled", symbol=symbol, side=side, qty=qty, fill_price=fill_price, order_id=_order_id(entry))
        try:
            stop_order, target_order = self._place_protection_orders(side, qty, sl_price, target_price)
        except Exception as exc:
            self._emergency_close_unprotected_entry(side, qty, exc)
        state = copy.deepcopy(self.state)
        state["manual_guard"] = None
        configured_initial_sl = snapshot.get("configured_initial_sl_price", sl_price)
        three_candle = snapshot.get("delta_three_candle_tsl")
        ladder = snapshot.get("delta_ladder_tsl")
        live_orders = {
            "entry_order_id": _order_id(entry),
            "stop_order_id": _order_id(stop_order),
            "target_order_id": _order_id(target_order),
            "entry_order": entry,
            "stop_order": stop_order,
            "target_order": target_order,
            "bracket_order": False,
            "protection_style": "per_timeframe_reduce_only",
        }
        snapshot = copy.deepcopy(snapshot)
        snapshot.update(execution="live", live_orders=copy.deepcopy(live_orders))
        state["position"] = {
            "id": uuid.uuid4().hex, "symbol": symbol, "side": side, "qty": qty,
            "entry_price": fill_price, "entry_time": entry_time or utc_now(),
            "sl_price": sl_price, "initial_sl": configured_initial_sl, "target_price": target_price,
            "entry_trigger": trigger, "signal_snapshot": snapshot,
            "contract_value": float(self.product["contract_value"]),
            "quote_currency": self.product.get("quoting_asset", {}).get("symbol"),
            "initial_margin_percent": self.product.get("initial_margin"),
            "leverage": snapshot.get("leverage"),
            "size_mode": snapshot.get("size_mode", "lots"),
            "configured_pax_size": snapshot.get("configured_pax_size"),
            "estimated_entry_margin": paper_margin(fill_price, qty, self.product["contract_value"], self.product.get("initial_margin"), snapshot.get("leverage")),
            "fee_rate": float(self.product.get("taker_commission_rate") or 0),
            "trailing_sl_active": bool((three_candle and three_candle.get("events")) or (ladder and ladder.get("events"))),
            "execution": "live",
            "live_orders": live_orders,
        }
        state[f"{side.lower()}_count"] += 1
        self.commit(state)
        delta_log(
            "live_open_committed",
            symbol=symbol,
            side=side,
            qty=qty,
            entry=fill_price,
            sl=sl_price,
            target=target_price,
            stop_order_id=_order_id(stop_order),
            target_order_id=_order_id(target_order),
        )
        print(f"[delta-live] opened {symbol} {side} qty={qty} entry={fill_price:g} sl={sl_price:g} target={target_price:g}")
        alert_trade_open(state["position"], self.alert_context)

    def close_trade(self, position, exit_price, exit_reason):
        current = self.state.get("position")
        if not current or current["id"] != position["id"]:
            return
        retry_block = self._close_retry_blocked(current)
        if retry_block:
            raise RuntimeError(retry_block)
        orders = current.get("live_orders") or {}
        delta_log(
            "live_close_start",
            symbol=current.get("symbol"),
            side=current.get("side"),
            qty=current.get("qty"),
            requested_exit=exit_price,
            reason=exit_reason,
            stop_order_id=orders.get("stop_order_id"),
            target_order_id=orders.get("target_order_id"),
        )
        self._cancel_order(orders.get("stop_order_id"))
        self._cancel_order(orders.get("target_order_id"))
        try:
            close_order = self._place_order({
                "product_id": self._product_id(),
                "size": int(current["qty"]),
                "side": self._opposite_side(current["side"]),
                "order_type": "market_order",
                "time_in_force": "ioc",
                "reduce_only": True,
                "client_order_id": self._client_order_id("dlc"),
            })
        except Exception as exc:
            self._record_close_failure(current, exit_reason, exc)
        try:
            confirmed_exit = self._require_filled_order(close_order, "close")
        except Exception as exc:
            self._record_close_failure(current, exit_reason, exc)
        gross = self.pnl(current, confirmed_exit)
        fees = (current["entry_price"] + confirmed_exit) * current["qty"] * current["contract_value"] * current["fee_rate"]
        trade = {**copy.deepcopy(current), "exit_price": confirmed_exit, "exit_time": utc_now(),
                 "exit_reason": exit_reason, "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees,
                 "close_order": close_order}
        state = copy.deepcopy(self.state)
        state.update(position=None, gross_pnl=state["gross_pnl"] + gross,
                     fees=state["fees"] + fees, closed_count=state["closed_count"] + 1)
        state["live_close_error"] = None
        if exit_reason in POST_EXIT_REST_REASONS | {"SL_EDITED", "TARGET_EDITED"}:
            raw_minutes = state["settings"].get("post_exit_cooldown_minutes")
            cooldown_minutes = 5.0 if raw_minutes is None else float(raw_minutes)
            # Do not shorten an existing cooldown — a user-set MANUAL_PAUSE
            # must not be replaced by the shorter post-exit rest.
            auto_until = time.time() + cooldown_minutes * 60 if cooldown_minutes > 0 else 0.0
            existing_until = float(state.get("cooldown_until") or 0)
            if auto_until > existing_until:
                state["cooldown_until"] = auto_until
                state["cooldown_reason"] = exit_reason if cooldown_minutes > 0 else None
        if exit_reason == "MANUAL_EXIT" and not state["settings"].get("manual_exit_reentry_enabled"):
            state["manual_guard"] = {"side": current["side"], "setup_time": current["signal_snapshot"].get("setup_time")}
        self.commit(state, trade)
        self._cancel_orphan_protection_orders()
        delta_log(
            "live_close_committed",
            symbol=current.get("symbol"),
            side=current.get("side"),
            reason=exit_reason,
            exit=confirmed_exit,
            gross=gross,
            fees=fees,
            net=gross - fees,
        )
        print(f"[delta-live] closed {current['symbol']} {current['side']} reason={exit_reason} gross={gross:.6f}")
        alert_trade_close(trade, self.alert_context)
        if self.on_position_closed:
            self.on_position_closed(position=current, exit_price=confirmed_exit, exit_reason=exit_reason, exit_time=trade["exit_time"])

    _INFERABLE_EXTERNAL_REASONS = {"SL", "TRAILING_SL", "TARGET"}

    def record_external_close(self, exit_price, exit_reason=None, exit_time=None):
        current = self.state.get("position")
        if not current:
            return False
        # Reconciler-triggered path passes exit_reason=None: infer the cause
        # from any recent live_close_error so audit rows show WHY Delta went
        # flat instead of the generic MANUAL_EXTERNAL_EXIT. Escalate to the
        # _EDITED variant when the fired level was manually moved.
        if exit_reason is None:
            recent = self.state.get("live_close_error") or {}
            attempted = recent.get("reason") if isinstance(recent, dict) else None
            if attempted in self._INFERABLE_EXTERNAL_REASONS:
                sl_edited = current.get("sl_source") == "manual"
                target_edited = current.get("target_source") == "manual"
                if attempted in {"SL", "TRAILING_SL"} and sl_edited:
                    exit_reason = "SL_EDITED"
                elif attempted == "TARGET" and target_edited:
                    exit_reason = "TARGET_EDITED"
                else:
                    exit_reason = attempted
            else:
                exit_reason = "MANUAL_EXTERNAL_EXIT"
        confirmed_exit = positive(exit_price or current.get("entry_price"))
        gross = self.pnl(current, confirmed_exit)
        fees = (current["entry_price"] + confirmed_exit) * current["qty"] * current["contract_value"] * current["fee_rate"]
        trade = {
            **copy.deepcopy(current),
            "exit_price": confirmed_exit,
            "exit_time": exit_time or utc_now(),
            "exit_reason": exit_reason,
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": gross - fees,
            "external_close": True,
        }
        state = copy.deepcopy(self.state)
        state.update(
            position=None,
            gross_pnl=state["gross_pnl"] + gross,
            fees=state["fees"] + fees,
            closed_count=state["closed_count"] + 1,
        )
        state["live_close_error"] = None
        raw_minutes = state["settings"].get("post_exit_cooldown_minutes")
        cooldown_minutes = 5.0 if raw_minutes is None else float(raw_minutes)
        # Do not shorten an existing cooldown — external Delta-side closes
        # must not clobber a longer user-set MANUAL_PAUSE.
        auto_until = time.time() + cooldown_minutes * 60 if cooldown_minutes > 0 else 0.0
        existing_until = float(state.get("cooldown_until") or 0)
        if auto_until > existing_until:
            state["cooldown_until"] = auto_until
            state["cooldown_reason"] = exit_reason if cooldown_minutes > 0 else None
        self.commit(state, trade)
        delta_log(
            "live_external_close_committed",
            symbol=current.get("symbol"),
            side=current.get("side"),
            reason=exit_reason,
            exit=confirmed_exit,
            gross=gross,
            fees=fees,
            net=gross - fees,
        )
        print(f"[delta-live] external close recorded {current['symbol']} {current['side']} reason={exit_reason} gross={gross:.6f}")
        alert_trade_close(trade, self.alert_context)
        if self.on_position_closed:
            self.on_position_closed(position=current, exit_price=confirmed_exit, exit_reason=exit_reason, exit_time=trade["exit_time"])
        return True

    def apply_trailing_stop(self, position, ltp, settings):
        snapshot = position["signal_snapshot"]
        ladder = snapshot.get("delta_ladder_tsl")
        if isinstance(ladder, dict):
            return self._apply_ladder_stop(position, ltp, settings)
        protection = snapshot.get("silver_breakeven")
        if not protection or protection.get("armed"):
            return position
        activation = protection["activation_price"]
        reached = ltp >= activation if position["side"] == "BUY" else ltp <= activation
        if not reached:
            return position
        state = copy.deepcopy(self.state)
        updated = state["position"]
        new_stop = (max(updated['sl_price'], updated['entry_price']) if updated['side'] == 'BUY'
                    else min(updated['sl_price'], updated['entry_price']))
        if float(new_stop) != float(updated["sl_price"]):
            self._amend_stop(updated, new_stop, state=state)
        updated["sl_price"] = new_stop
        updated["trailing_sl_active"] = True
        updated["signal_snapshot"]["silver_breakeven"].update(armed=True, armed_at=utc_now())
        self.commit(state)
        return copy.deepcopy(updated)

    def _apply_ladder_stop(self, position, ltp, settings):
        current = self.state.get("position")
        if not current or current["id"] != position["id"]:
            return position
        state = copy.deepcopy(self.state)
        updated = state["position"]
        snapshot = updated["signal_snapshot"]
        ladder = snapshot.get("delta_ladder_tsl")
        if not isinstance(ladder, dict):
            return copy.deepcopy(updated)
        highest = max(float(ladder.get("highest") or updated["entry_price"]), float(ltp), float(updated["entry_price"]))
        lowest = min(float(ladder.get("lowest") or updated["entry_price"]), float(ltp), float(updated["entry_price"]))
        result = calculate_point_trailing(
            entry=float(updated["entry_price"]),
            side=updated["side"],
            current_sl=float(updated["sl_price"]),
            highest=highest,
            lowest=lowest,
            activate_points=float(ladder.get("activation_points") or settings.get("tsl_activate_points") or 0),
            profit_step_points=float(ladder.get("profit_step_points") or settings.get("tsl_profit_step_points") or settings.get("tsl_activate_points") or 0),
            lock_step_points=float(ladder.get("lock_step_points") or settings.get("tsl_lock_step_points") or 0),
        )
        previous_step = int(ladder.get("step_index", -1))
        step_changed = result["trailing_active"] and int(result["step_index"]) > previous_step
        state_changed = False
        ladder.update(
            highest=result["highest"],
            lowest=result["lowest"],
            step_index=int(result["step_index"]) if result["trailing_active"] else previous_step,
            protected_points=result["protected_points"],
            armed=bool(ladder.get("armed") or result["trailing_active"]),
            status=("breakeven_locked" if result["trailing_active"] and int(result["step_index"]) == 0 else
                    "ladder_locked" if result["trailing_active"] else "waiting_for_initial_target"),
        )
        if result["trailing_active"] and (step_changed or result["sl_moved"]):
            event = {
                "time": utc_now(),
                "ltp": float(ltp),
                "previous_sl": result["previous_sl"],
                "new_sl": result["sl_price"],
                "gain_points": result["gain_points"],
                "protected_points": result["protected_points"],
                "step_index": int(result["step_index"]),
                "status": "breakeven_locked" if int(result["step_index"]) == 0 else "ladder_locked",
            }
            ladder.setdefault("evaluations", []).append(event)
            ladder["evaluations"] = ladder["evaluations"][-200:]
            if result["sl_moved"]:
                self._amend_stop(updated, result["sl_price"], state=state)
                ladder.setdefault("events", []).append(event)
                ladder["events"] = ladder["events"][-200:]
                updated["sl_price"] = result["sl_price"]
                state_changed = True
            else:
                state_changed = True
            updated["trailing_sl_active"] = True
        if state_changed:
            self.commit(state)
            delta_log(
                "live_ladder_tsl_evaluated",
                symbol=updated.get("symbol"),
                side=updated.get("side"),
                ltp=ltp,
                new_sl=updated.get("sl_price"),
                step_index=ladder.get("step_index"),
                protected_points=ladder.get("protected_points"),
            )
            return copy.deepcopy(updated)
        return position

    def apply_three_candle_stop(self, position, details, completed_close):
        current = self.state.get("position")
        if not current or current["id"] != position["id"]:
            return None
        state = copy.deepcopy(self.state)
        updated = state["position"]
        policy = updated["signal_snapshot"].get("delta_three_candle_tsl")
        if not isinstance(policy, dict):
            return copy.deepcopy(updated)
        candidate = float(details["candidate_sl"])
        close = float(completed_close)
        current_sl = float(updated["sl_price"])
        buy = updated["side"] == "BUY"
        tighter = candidate > current_sl if buy else candidate < current_sl
        breached = candidate >= close if buy else candidate <= close
        status = "breached" if tighter and breached else "accepted" if tighter else "not_tighter"
        evaluation = {**copy.deepcopy(details), "status": status, "previous_sl": current_sl, "completed_close": close}
        policy.setdefault("evaluations", []).append(evaluation)
        policy["evaluations"] = policy["evaluations"][-200:]
        policy["last_evaluation"] = evaluation
        if tighter:
            policy.setdefault("events", []).append(evaluation)
            policy["events"] = policy["events"][-200:]
            policy["status"] = status
        elif not policy.get("events"):
            policy["status"] = "waiting_for_tighter_post_entry_window"
        if tighter and breached:
            self.commit(state)
            self.close_trade(updated, close, "TRAILING_SL")
            return None
        if tighter:
            self._amend_stop(updated, candidate, state=state)
            updated["sl_price"] = candidate
            updated["trailing_sl_active"] = True
        self.commit(state)
        return copy.deepcopy(updated)

    def _amend_stop(self, position, stop_price, state=None):
        orders = position.get("live_orders") or {}
        order_id = orders.get("stop_order_id")
        if not order_id:
            raise ValueError("Live Delta stop order is missing; cannot amend tracked stop")
        if orders.get("bracket_order"):
            amended = self._edit_bracket(position, stop_price, position["target_price"])
        else:
            rows = self._active_product_orders()
            row = next((row for row in rows if str(row.get("id")) == str(order_id)
                        and self._product_row(row)
                        and row.get("side") == self._opposite_side(position["side"])
                        and row.get("stop_order_type") == "stop_loss_order"), None)
            if not row:
                raise ValueError("Tracked Delta stop order is no longer active; reload account/orders")
            amended = self._edit_order(order_id, {
                "product_id": self._product_id(),
                "size": int(row.get("size") or position["qty"]),
                "stop_price": _fmt_price(stop_price),
            })
        target_state = state if state is not None else copy.deepcopy(self.state)
        target_state["position"].setdefault("live_stop_amends", []).append({
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "order_id": order_id,
            "stop_price": stop_price,
            "response": amended,
        })
        delta_log(
            "live_stop_amended",
            symbol=position.get("symbol"),
            side=position.get("side"),
            order_id=order_id,
            stop_price=stop_price,
            bracket=bool(orders.get("bracket_order")),
        )
        if state is None:
            self.commit(target_state)

    def sync_protection(self, rows=None):
        """Import exchange prices only for this position's tracked order IDs."""
        if not self.state.get("position"):
            return
        rows = self._active_product_orders() if rows is None else rows
        state = copy.deepcopy(self.state)
        position = state["position"]
        orders = position.get("live_orders") or {}
        changes = {}
        for key, field, kind in (
            ("stop_order_id", "sl_price", "stop_loss_order"),
            ("target_order_id", "target_price", "take_profit_order"),
        ):
            row = next((row for row in rows if str(row.get("id")) == str(orders.get(key))
                        and self._product_row(row)
                        and row.get("side") == self._opposite_side(position["side"])
                        and row.get("stop_order_type") == kind), None)
            price = _number(row.get("stop_price")) if row else None
            if price is not None and price > 0 and price != position.get(field):
                changes[field] = {"previous": position.get(field), "new": price}
                position[field] = price
                position["sl_source" if field == "sl_price" else "target_source"] = "manual_external"
        if changes:
            event = {"time": utc_now(), "source": "delta_exchange", "changes": changes}
            position.setdefault("protection_edits", []).append(event)
            self.commit(state)
            delta_log("live_protection_synced", position_id=position.get("id"), **event)

    def _edit_bracket(self, position, sl, target):
        # PUT /orders/bracket edits an unfilled entry's attached parameters.
        # A filled position's bracket has real child orders: edit those legs.
        rows = self._active_product_orders()
        orders = position.get("live_orders") or {}
        edits = []
        for key, field, kind, desired in (
            ("stop_order_id", "sl_price", "stop_loss_order", sl),
            ("target_order_id", "target_price", "take_profit_order", target),
        ):
            row = next((row for row in rows if str(row.get("id")) == str(orders.get(key))), None)
            if not row or row.get("stop_order_type") != kind or row.get("side") != self._opposite_side(position["side"]):
                raise ValueError("Tracked Delta protection order is no longer active; reload account/orders")
            actual = _number(row.get("stop_price"))
            if actual != position.get(field):
                self.sync_protection(rows)
                raise ValueError("Protection changed on Delta while editing; reopen the editor")
            if desired != actual:
                edits.append((row, desired))
        responses = []
        try:
            for row, desired in edits:
                payload = {"product_id": self._product_id(), "size": int(row["size"]),
                           "stop_price": _fmt_price(desired)}
                delta_log("live_protection_leg_edit", order_id=row["id"], payload=payload)
                responses.append(self._edit_order(row["id"], payload))
        except Exception:
            # One leg may have succeeded. Refresh reality instead of claiming
            # an atomic failure or reverting a stop already accepted by Delta.
            try:
                self.sync_protection()
            except Exception as sync_error:
                delta_log("live_protection_sync_failed", error=str(sync_error))
            raise
        return responses

    def update_protection(self, current, sl, target, ltp, exit_mode_patch=None):
        orders = current.get("live_orders") or {}
        stop_order_id = orders.get("stop_order_id")
        target_order_id = orders.get("target_order_id")
        if not stop_order_id or not target_order_id:
            raise ValueError("Tracked Delta protection orders are missing; reload account/orders before editing")
        state = copy.deepcopy(self.state)
        position = state["position"]
        sl_changed = sl != current["sl_price"]
        target_changed = target != current["target_price"]
        if orders.get("bracket_order"):
            stop_response = self._edit_bracket(current, sl, target)
            target_response = stop_response
        else:
            rows = self._active_product_orders()
            tracked_rows = {}
            for key, field, kind in (
                ("stop_order_id", "sl_price", "stop_loss_order"),
                ("target_order_id", "target_price", "take_profit_order"),
            ):
                row = next((row for row in rows if str(row.get("id")) == str(orders.get(key))
                            and self._product_row(row)
                            and row.get("side") == self._opposite_side(current["side"])
                            and row.get("stop_order_type") == kind), None)
                if not row:
                    raise ValueError("Tracked Delta protection order is no longer active; reload account/orders")
                actual = _number(row.get("stop_price"))
                if actual != current.get(field):
                    self.sync_protection(rows)
                    raise ValueError("Protection changed on Delta while editing; reopen the editor")
                tracked_rows[key] = row
            stop_response = None
            target_response = None
            try:
                if sl_changed:
                    stop_response = self._edit_order(stop_order_id, {
                        "product_id": self._product_id(),
                        "size": int(tracked_rows["stop_order_id"].get("size") or current["qty"]),
                        "stop_price": _fmt_price(sl),
                    })
                if target_changed:
                    target_response = self._edit_order(target_order_id, {
                        "product_id": self._product_id(),
                        "size": int(tracked_rows["target_order_id"].get("size") or current["qty"]),
                        "stop_price": _fmt_price(target),
                    })
            except Exception:
                try:
                    self.sync_protection()
                except Exception as sync_error:
                    delta_log("live_protection_sync_failed", error=str(sync_error))
                raise
        edit_event = {
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "manual_live",
            "previous_sl": current["sl_price"],
            "previous_target": current["target_price"],
            "new_sl": sl,
            "new_target": target,
            "ltp": ltp,
            "stop_response": stop_response,
            "target_response": target_response,
        }
        if exit_mode_patch:
            edit_event["exit_mode_from"] = (current.get("signal_snapshot") or {}).get("silver_exit_policy")
            edit_event["exit_mode_to"] = exit_mode_patch.get("silver_exit_policy")
        position.setdefault("protection_edits", []).append(edit_event)
        position.update(sl_price=sl, target_price=target)
        if sl_changed:
            position["sl_source"] = "manual"
        if target_changed:
            position["target_source"] = "manual"
        if exit_mode_patch:
            from .strategies.delta_gold import apply_exit_mode_patch
            apply_exit_mode_patch(position, exit_mode_patch)
        else:
            protection = position["signal_snapshot"].get("silver_breakeven")
            if protection:
                position["signal_snapshot"]["silver_breakeven"]["target_price"] = target
            else:
                ladder = position["signal_snapshot"].get("delta_ladder_tsl")
                if isinstance(ladder, dict):
                    ladder["target_price"] = target
        self.commit(state)
        return copy.deepcopy(position)
