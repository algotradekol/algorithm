"""Durable, isolated Delta paper positions. No exchange order methods exist."""
from __future__ import annotations

import copy
import datetime
import time
import uuid

from .delta_log import delta_log
from .storage_namespace import namespaced_value
from .supabase_client import run_with_supabase
from .delta_reporting import paper_margin
from .trailing_stop import calculate_point_trailing


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

    def now_iso(self):
        return utc_now()

    def open_positions(self):
        position = self.state.get("position")
        return [copy.deepcopy(position)] if position else []

    def open_trade(self, symbol, side, qty, entry_price, sl_price, target_price, trigger, snapshot, entry_time=None):
        if self.state.get("position"):
            raise ValueError("A Delta paper position is already open")
        if min(entry_price, sl_price, target_price) <= 0:
            raise ValueError("Entry, target and stop must remain positive")
        delta_log(
            "paper_open_start",
            symbol=symbol,
            side=side,
            qty=qty,
            entry=entry_price,
            sl=sl_price,
            target=target_price,
            trigger=trigger,
            setup_time=(snapshot or {}).get("setup_time"),
            exit_mode=(snapshot or {}).get("silver_exit_policy"),
        )
        state = copy.deepcopy(self.state)
        state["manual_guard"] = None
        configured_initial_sl = snapshot.get("configured_initial_sl_price", sl_price)
        three_candle = snapshot.get("delta_three_candle_tsl")
        ladder = snapshot.get("delta_ladder_tsl")
        state["position"] = {
            "id": uuid.uuid4().hex, "symbol": symbol, "side": side, "qty": qty,
            "entry_price": entry_price, "entry_time": entry_time or utc_now(),
            "sl_price": sl_price, "initial_sl": configured_initial_sl, "target_price": target_price,
            "entry_trigger": trigger, "signal_snapshot": copy.deepcopy(snapshot),
            "contract_value": float(self.product["contract_value"]),
            "quote_currency": self.product.get("quoting_asset", {}).get("symbol"),
            "initial_margin_percent": self.product.get("initial_margin"),
            "leverage": snapshot.get("leverage"),
            "size_mode": snapshot.get("size_mode", "lots"),
            "configured_pax_size": snapshot.get("configured_pax_size"),
            "estimated_entry_margin": paper_margin(entry_price, qty, self.product["contract_value"], self.product.get("initial_margin"), snapshot.get("leverage")),
            "fee_rate": float(self.product.get("taker_commission_rate") or 0),
            "trailing_sl_active": bool((three_candle and three_candle.get("events")) or (ladder and ladder.get("events"))),
        }
        state[f"{side.lower()}_count"] += 1
        self.commit(state)
        delta_log("paper_open_committed", symbol=symbol, side=side, qty=qty, entry=entry_price, sl=sl_price, target=target_price)

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
        if exit_reason in {"MANUAL_EXIT", "SL", "TRAILING_SL", "TARGET"}:
            raw_minutes = state["settings"].get("post_exit_cooldown_minutes")
            cooldown_minutes = 5.0 if raw_minutes is None else float(raw_minutes)
            # Do not shorten an existing cooldown — a user-set MANUAL_PAUSE
            # (e.g. 1 hr rest) must survive a fill that closes right after.
            auto_until = time.time() + cooldown_minutes * 60 if cooldown_minutes > 0 else 0.0
            existing_until = float(state.get("cooldown_until") or 0)
            if auto_until > existing_until:
                state["cooldown_until"] = auto_until
                state["cooldown_reason"] = exit_reason if cooldown_minutes > 0 else None
        if exit_reason == "MANUAL_EXIT" and not state["settings"].get("manual_exit_reentry_enabled"):
            state["manual_guard"] = {"side": current["side"], "setup_time": current["signal_snapshot"].get("setup_time")}
        self.commit(state, trade)
        delta_log(
            "paper_close_committed",
            symbol=current.get("symbol"),
            side=current.get("side"),
            reason=exit_reason,
            exit=exit_price,
            gross=gross,
            fees=fees,
            net=gross - fees,
            cooldown_until=state.get("cooldown_until"),
            cooldown_reason=state.get("cooldown_reason"),
        )
        cooldown_note = (
            f" cooldown={state['settings'].get('post_exit_cooldown_minutes')}m"
            if exit_reason in {"MANUAL_EXIT", "SL", "TRAILING_SL", "TARGET"}
            else " cooldown=bypassed"
        )
        print(f"[delta-paper] closed {current['symbol']} {current['side']} reason={exit_reason} gross={gross:.6f}{cooldown_note}")
        if self.on_position_closed:
            self.on_position_closed(position=current, exit_price=exit_price, exit_reason=exit_reason, exit_time=trade["exit_time"])

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
        # Never undo a manually tightened stop when breakeven activates later.
        updated["sl_price"] = (max(updated['sl_price'], updated['entry_price']) if updated['side'] == 'BUY'
                               else min(updated['sl_price'], updated['entry_price']))
        updated["trailing_sl_active"] = True
        updated["signal_snapshot"]["silver_breakeven"].update(armed=True, armed_at=self.now_iso())
        self.commit(state)
        delta_log(
            "paper_breakeven_armed",
            symbol=updated.get("symbol"),
            side=updated.get("side"),
            ltp=ltp,
            new_sl=updated.get("sl_price"),
            activation=activation,
        )
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
                "time": self.now_iso(),
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
                "paper_ladder_tsl_evaluated",
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
        if tighter and not breached:
            updated["sl_price"] = candidate
            updated["trailing_sl_active"] = True
        self.commit(state)
        delta_log(
            "paper_three_candle_tsl_evaluated",
            symbol=updated.get("symbol"),
            side=updated.get("side"),
            status=status,
            candidate_sl=candidate,
            previous_sl=current_sl,
            completed_close=close,
            candles=details.get("candles"),
        )
        if tighter and breached:
            self.close_trade(updated, close, "TRAILING_SL")
            return None
        return copy.deepcopy(updated)

    def should_exit_at_target(self, settings, position=None):
        return True
