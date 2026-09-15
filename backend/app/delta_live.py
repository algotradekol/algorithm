"""Durable Delta live execution with local strategy tracking."""
from __future__ import annotations

import copy
import datetime
import time
import uuid

from .delta_client import positive
from .delta_paper import DeltaPaperBroker, utc_now
from .delta_reporting import paper_margin


def _fmt_price(value) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def _order_id(row):
    return row.get("id") if isinstance(row, dict) else None


class DeltaLiveBroker(DeltaPaperBroker):
    """Live Delta orders, while retaining the same state shape as paper.

    Entries are market IOC. Protective exits are exchange-side reduce-only
    trigger orders, so a late app-side exit attempt cannot open a reverse leg.
    """

    def __init__(self, store, defaults, product, client):
        super().__init__(store, defaults, product)
        self.client = client
        self.mode = "live"

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
        return self.client.post("/v2/orders", payload, private=True)

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
            print(f"[delta-live] cancel ignored for order {order_id}: {exc}")
            return None

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

    def open_trade(self, symbol, side, qty, entry_price, sl_price, target_price, trigger, snapshot, entry_time=None):
        if self.state.get("position"):
            raise ValueError("A Delta live position is already tracked")
        if min(entry_price, sl_price, target_price) <= 0:
            raise ValueError("Entry, target and stop must remain positive")
        qty = int(qty)
        if qty < 1:
            raise ValueError("Delta live size must be at least 1 lot")
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
        fill_price = positive(entry.get("average_fill_price") or entry.get("limit_price") or entry_price)
        stop_order = self._place_order(self._protection_payload(side, qty, sl_price, "stop_loss_order"))
        target_order = self._place_order(self._protection_payload(side, qty, target_price, "take_profit_order"))
        state = copy.deepcopy(self.state)
        state["manual_guard"] = None
        configured_initial_sl = snapshot.get("configured_initial_sl_price", sl_price)
        three_candle = snapshot.get("delta_three_candle_tsl")
        live_orders = {
            "entry_order_id": _order_id(entry),
            "stop_order_id": _order_id(stop_order),
            "target_order_id": _order_id(target_order),
            "entry_order": entry,
            "stop_order": stop_order,
            "target_order": target_order,
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
            "estimated_entry_margin": paper_margin(fill_price, qty, self.product["contract_value"], self.product.get("initial_margin")),
            "fee_rate": float(self.product.get("taker_commission_rate") or 0),
            "trailing_sl_active": bool(three_candle and three_candle.get("events")),
            "execution": "live",
            "live_orders": live_orders,
        }
        state[f"{side.lower()}_count"] += 1
        self.commit(state)
        print(f"[delta-live] opened {symbol} {side} qty={qty} entry={fill_price:g} sl={sl_price:g} target={target_price:g}")

    def close_trade(self, position, exit_price, exit_reason):
        current = self.state.get("position")
        if not current or current["id"] != position["id"]:
            return
        orders = current.get("live_orders") or {}
        self._cancel_order(orders.get("stop_order_id"))
        self._cancel_order(orders.get("target_order_id"))
        close_order = self._place_order({
            "product_id": self._product_id(),
            "size": int(current["qty"]),
            "side": self._opposite_side(current["side"]),
            "order_type": "market_order",
            "time_in_force": "ioc",
            "reduce_only": True,
            "client_order_id": self._client_order_id("dlc"),
        })
        confirmed_exit = positive(close_order.get("average_fill_price") or close_order.get("limit_price") or exit_price)
        gross = self.pnl(current, confirmed_exit)
        fees = (current["entry_price"] + confirmed_exit) * current["qty"] * current["contract_value"] * current["fee_rate"]
        trade = {**copy.deepcopy(current), "exit_price": confirmed_exit, "exit_time": utc_now(),
                 "exit_reason": exit_reason, "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees,
                 "close_order": close_order}
        state = copy.deepcopy(self.state)
        state.update(position=None, gross_pnl=state["gross_pnl"] + gross,
                     fees=state["fees"] + fees, closed_count=state["closed_count"] + 1)
        if exit_reason in {"MANUAL_EXIT", "SL", "TRAILING_SL", "TARGET"}:
            raw_minutes = state["settings"].get("post_exit_cooldown_minutes")
            cooldown_minutes = 5.0 if raw_minutes is None else float(raw_minutes)
            state["cooldown_until"] = time.time() + cooldown_minutes * 60 if cooldown_minutes > 0 else 0.0
            state["cooldown_reason"] = exit_reason if cooldown_minutes > 0 else None
        if exit_reason == "MANUAL_EXIT" and not state["settings"].get("manual_exit_reentry_enabled"):
            state["manual_guard"] = {"side": current["side"], "setup_time": current["signal_snapshot"].get("setup_time")}
        self.commit(state, trade)
        print(f"[delta-live] closed {current['symbol']} {current['side']} reason={exit_reason} gross={gross:.6f}")
        if self.on_position_closed:
            self.on_position_closed(position=current, exit_price=confirmed_exit, exit_reason=exit_reason, exit_time=trade["exit_time"])

    def apply_trailing_stop(self, position, ltp, settings):
        snapshot = position["signal_snapshot"]
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
        amended = self._edit_order(order_id, {
            "product_id": self._product_id(),
            "size": int(position["qty"]),
            "stop_price": _fmt_price(stop_price),
        })
        target_state = state if state is not None else copy.deepcopy(self.state)
        target_state["position"].setdefault("live_stop_amends", []).append({
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "order_id": order_id,
            "stop_price": stop_price,
            "response": amended,
        })
        if state is None:
            self.commit(target_state)

    def update_protection(self, current, sl, target, ltp):
        orders = current.get("live_orders") or {}
        stop_order_id = orders.get("stop_order_id")
        target_order_id = orders.get("target_order_id")
        if not stop_order_id or not target_order_id:
            raise ValueError("Tracked Delta protection orders are missing; reload account/orders before editing")
        state = copy.deepcopy(self.state)
        position = state["position"]
        stop_response = self._edit_order(stop_order_id, {
            "product_id": self._product_id(),
            "size": int(current["qty"]),
            "stop_price": _fmt_price(sl),
        })
        target_response = self._edit_order(target_order_id, {
            "product_id": self._product_id(),
            "size": int(current["qty"]),
            "stop_price": _fmt_price(target),
        })
        position.setdefault("protection_edits", []).append({
            "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source": "manual_live",
            "previous_sl": current["sl_price"],
            "previous_target": current["target_price"],
            "new_sl": sl,
            "new_target": target,
            "ltp": ltp,
            "stop_response": stop_response,
            "target_response": target_response,
        })
        position.update(sl_price=sl, target_price=target)
        protection = position["signal_snapshot"].get("silver_breakeven")
        if protection:
            position["signal_snapshot"]["silver_breakeven"]["target_price"] = target
        self.commit(state)
        return copy.deepcopy(position)
