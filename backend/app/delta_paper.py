"""Durable, isolated Delta paper positions. No exchange order methods exist."""
from __future__ import annotations

import copy
import datetime
import time
import uuid

from .storage_namespace import namespaced_value
from .supabase_client import run_with_supabase
from .delta_reporting import paper_margin


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DeltaStore:
    def __init__(self, key):
        self.key = namespaced_value(key)

    def load(self):
        rows = run_with_supabase(lambda db: db.table("delta_paper_state").select("state").eq("storage_key", self.key).execute()).data
        return rows[0]["state"] if rows else None

    def save(self, state, trade=None):
        run_with_supabase(lambda db: db.rpc("save_delta_paper", {
            "p_key": self.key, "p_state": state, "p_trade": trade,
        }).execute())

    def trades(self, offset=0, limit=100, before=None):
        def query(db):
            request = db.table('delta_paper_trades').select('trade').eq('storage_key', self.key)
            if before:
                request = request.lte('closed_at', before)
            return request.order('closed_at', desc=True).order('id', desc=True).range(offset, offset + limit - 1).execute()
        rows = run_with_supabase(query).data
        return [row["trade"] for row in rows or []]


class DeltaPaperBroker:
    def __init__(self, store, defaults, product):
        self.store = store
        self.product = product
        self.state = store.load() or {
            "settings": defaults, "position": None, "gross_pnl": 0.0,
            "fees": 0.0, "closed_count": 0, "buy_count": 0, "sell_count": 0,
            "cooldown_until": 0.0,
        }
        self.starting_capital = 0
        self.on_position_closed = None

    def commit(self, state, trade=None):
        # Persist first: a failed write must not pretend an entry/exit succeeded.
        self.store.save(state, trade)
        self.state = state

    def open_positions(self):
        position = self.state.get("position")
        return [copy.deepcopy(position)] if position else []

    def open_trade(self, symbol, side, qty, entry_price, sl_price, target_price, trigger, snapshot, entry_time=None):
        if self.state.get("position"):
            raise ValueError("A Delta paper position is already open")
        if min(entry_price, sl_price, target_price) <= 0:
            raise ValueError("Entry, target and stop must remain positive")
        state = copy.deepcopy(self.state)
        state["manual_guard"] = None
        state["position"] = {
            "id": uuid.uuid4().hex, "symbol": symbol, "side": side, "qty": qty,
            "entry_price": entry_price, "entry_time": entry_time or utc_now(),
            "sl_price": sl_price, "initial_sl": sl_price, "target_price": target_price,
            "entry_trigger": trigger, "signal_snapshot": copy.deepcopy(snapshot),
            "contract_value": float(self.product["contract_value"]),
            "quote_currency": self.product.get("quoting_asset", {}).get("symbol"),
            "initial_margin_percent": self.product.get("initial_margin"),
            "estimated_entry_margin": paper_margin(entry_price, qty, self.product["contract_value"], self.product.get("initial_margin")),
            "fee_rate": float(self.product.get("taker_commission_rate") or 0),
            "trailing_sl_active": False,
        }
        state[f"{side.lower()}_count"] += 1
        self.commit(state)

    @staticmethod
    def pnl(position, price):
        direction = 1 if position["side"] == "BUY" else -1
        return direction * (price - position["entry_price"]) * position["qty"] * position["contract_value"]

    def close_trade(self, position, exit_price, exit_reason):
        current = self.state.get("position")
        if not current or current["id"] != position["id"]:
            return
        gross = self.pnl(current, exit_price)
        fees = (current["entry_price"] + exit_price) * current["qty"] * current["contract_value"] * current["fee_rate"]
        trade = {**copy.deepcopy(current), "exit_price": exit_price, "exit_time": utc_now(),
                 "exit_reason": exit_reason, "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees}
        state = copy.deepcopy(self.state)
        state.update(position=None, gross_pnl=state["gross_pnl"] + gross,
                     fees=state["fees"] + fees, closed_count=state["closed_count"] + 1)
        if exit_reason in {"SL", "TRAILING_SL"}:
            state["cooldown_until"] = time.time() + 30
        if exit_reason == "MANUAL_EXIT" and not state["settings"].get("manual_exit_reentry_enabled"):
            state["manual_guard"] = {"side": current["side"], "setup_time": current["signal_snapshot"].get("setup_time")}
        self.commit(state, trade)
        print(f"[delta-paper] closed {current['symbol']} {current['side']} reason={exit_reason} gross={gross:.6f}")
        if self.on_position_closed:
            self.on_position_closed(position=current, exit_price=exit_price, exit_reason=exit_reason, exit_time=trade["exit_time"])

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
        # Never undo a manually tightened stop when breakeven activates later.
        updated["sl_price"] = (max(updated['sl_price'], updated['entry_price']) if updated['side'] == 'BUY'
                               else min(updated['sl_price'], updated['entry_price']))
        updated["trailing_sl_active"] = True
        updated["signal_snapshot"]["silver_breakeven"].update(armed=True, armed_at=utc_now())
        self.commit(state)
        return copy.deepcopy(updated)

    def should_exit_at_target(self, settings, position=None):
        return True
