"""
paper_broker.py — same idea as the earlier local version, but now
reads/writes Supabase so the frontend can see live state from any
device, and both algos get their own isolated capital pool + trade
log (keyed by algo_id).
"""
from __future__ import annotations

import datetime

from .charges import calculate_charges, get_charges_config
from .storage_namespace import current_storage_values, namespaced_value
from .supabase_client import run_with_supabase
from .trailing_stop import (
    SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN,
    calculate_point_trailing,
    silver_tsl_points,
    uses_silver_breakeven_stop,
)


class PaperBroker:
    def __init__(self, algo_id: str, starting_capital: float):
        self.algo_id = algo_id
        self.starting_capital = starting_capital
        self._ensure_state_row()

    def state_table_name(self) -> str:
        return "algo_state"

    def positions_table_name(self) -> str:
        return "positions"

    def trades_table_name(self) -> str:
        return "trades"

    def storage_algo_id(self) -> str:
        return namespaced_value(self.algo_id)

    def storage_algo_candidates(self) -> list[str]:
        return current_storage_values(self.algo_id)

    def _merge_storage_rows(self, rows_by_candidate: list[list[dict]], *, limit: int | None = None, order_key: str | None = None, reverse: bool = False) -> list[dict]:
        merged: list[dict] = []
        seen_ids: set[str] = set()
        for rows in rows_by_candidate:
            for row in rows or []:
                row_id = str(row.get("id") or "")
                dedupe_key = row_id or f"{row.get('algo_id')}::{row.get('symbol')}::{row.get('entry_time')}::{row.get('exit_time')}"
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                merged.append(row)
        if order_key:
            merged.sort(key=lambda row: str(row.get(order_key) or ""), reverse=reverse)
        if limit is not None:
            return merged[:limit]
        return merged

    def _ensure_state_row(self):
        existing = run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).select("*").eq("algo_id", self.storage_algo_id()).execute()
        )
        if not existing.data:
            run_with_supabase(
                lambda supabase, payload={
                    "algo_id": self.storage_algo_id(),
                    "cash": self.starting_capital,
                    "trade_count_today": 0,
                    "buy_count_today": 0,
                    "sell_count_today": 0,
                    "trading_date": datetime.date.today().isoformat(),
                }: supabase.table(self.state_table_name()).insert(payload).execute()
            )

    def _get_state(self) -> dict:
        row = run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).select("*").eq("algo_id", self.storage_algo_id()).execute()
        ).data[0]
        today = datetime.date.today().isoformat()
        if row["trading_date"] != today:
            # new trading day -- reset daily counters, keep cumulative cash/pnl
            run_with_supabase(
                lambda supabase: supabase.table(self.state_table_name()).update({
                    "trading_date": today,
                    "trade_count_today": 0,
                    "buy_count_today": 0,
                    "sell_count_today": 0,
                }).eq("algo_id", self.storage_algo_id()).execute()
            )
            row.update({"trading_date": today, "trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0})
        return row

    def open_positions(self, include_stale: bool = False) -> list[dict]:
        query_date = datetime.date.today().isoformat()

        def query_candidate(supabase, candidate: str):
            request = supabase.table(self.positions_table_name()).select("*").eq("algo_id", candidate).eq("status", "open")
            if not include_stale:
                request = request.gte("entry_time", query_date)
            return request.execute()

        rows_by_candidate = [
            run_with_supabase(lambda supabase, key=candidate: query_candidate(supabase, key)).data
            for candidate in self.storage_algo_candidates()
        ]
        return self._merge_storage_rows(rows_by_candidate, order_key="entry_time", reverse=True)

    def close_stale_open_positions(self) -> int:
        """Close previous-day open paper positions so they never appear as live positions."""
        today = datetime.date.today().isoformat()
        stale_positions = [
            position for position in self.open_positions(include_stale=True)
            if str(position.get("entry_time") or "")[:10] < today
        ]
        for position in stale_positions:
            self.close_trade(position, float(position.get("entry_price") or 0), "MISSED_EOD_STALE")
        return len(stale_positions)

    def recent_trades(self, limit: int = 200, today_only: bool = True) -> list[dict]:
        query_date = datetime.date.today().isoformat()

        def query_candidate(supabase, candidate: str):
            request = supabase.table(self.trades_table_name()).select("*").eq("algo_id", candidate)
            if today_only:
                request = request.gte("exit_time", query_date)
            return request.order("exit_time", desc=True).limit(limit).execute()

        rows_by_candidate = [
            run_with_supabase(lambda supabase, key=candidate: query_candidate(supabase, key)).data
            for candidate in self.storage_algo_candidates()
        ]
        return self._merge_storage_rows(rows_by_candidate, limit=limit, order_key="exit_time", reverse=True)

    def already_traded_today(self, symbol: str) -> bool:
        today = datetime.date.today().isoformat()
        for candidate in self.storage_algo_candidates():
            result = run_with_supabase(
                lambda supabase, key=candidate: supabase.table(self.trades_table_name()).select("id").eq("algo_id", key)
                .eq("symbol", symbol).gte("entry_time", today).execute()
            )
            if result.data:
                return True
        return False

    def can_open_new_trade(self, side: str, max_total: int, max_per_side: int) -> bool:
        counts = self.today_counts()
        if counts["trade_count_today"] >= max_total:
            return False
        side_count = counts["buy_count_today"] if side == "BUY" else counts["sell_count_today"]
        if side_count < max_per_side:
            return True
        # allow counter/overflow trades on this side if the OTHER side hasn't used its full quota
        # and the total cap isn't hit yet (fills the 10-trade cap even if one side has fewer signals)
        return counts["trade_count_today"] < max_total

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
        entry_time: str | None = None,
    ):
        # Stamp trailing-SL metadata into signal_snapshot so the dashboard
        # can show initial SL vs current SL, when trailing first activated,
        # and how many times SL was bumped. Kept in the JSONB blob to avoid
        # a schema migration.
        merged_snapshot = dict(signal_snapshot or {})
        merged_snapshot.setdefault("initial_sl_price", float(sl_price))
        if merged_snapshot.get("silver_exit_policy") == SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN:
            breakeven = dict(merged_snapshot.get("silver_breakeven") or {})
            # Four-input entries retain a separate activation milestone and
            # final target. These defaults preserve historical open rows that
            # used the target itself as their breakeven milestone.
            breakeven.setdefault("armed", False)
            breakeven.setdefault("activation_price", float(target_price))
            breakeven.setdefault("activation_points", abs(float(target_price) - float(entry_price)))
            breakeven.setdefault("target_price", float(target_price))
            breakeven.setdefault("final_target_enabled", False)
            breakeven.setdefault("initial_sl_price", float(sl_price))
            merged_snapshot["silver_breakeven"] = breakeven
        merged_snapshot.setdefault("trailing", {
            "activated": False,
            "first_activated_at": None,
            "last_updated_at": None,
            "update_count": 0,
            "last_sl_before_trail": float(sl_price),
            "current_sl": float(sl_price),
            "events": [],
        })

        position_row = {
            "algo_id": self.storage_algo_id(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "trailing_sl_active": False,
            "entry_trigger": entry_trigger or "Strategy entry conditions matched",
            "signal_snapshot": merged_snapshot,
            "status": "open",
            # Persist every new trade time as an offset-aware UTC timestamp.
            # The UI renders this in IST, avoiding server/browser timezone
            # drift and impossible entry/exit ordering.
            "entry_time": entry_time or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        try:
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name()).insert(position_row).execute()
            )
        except Exception as exc:
            if "entry_trigger" not in str(exc) and "signal_snapshot" not in str(exc):
                raise
            # Backward compatible until the Supabase audit migration is applied.
            position_row.pop("entry_trigger", None)
            position_row.pop("signal_snapshot", None)
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name()).insert(position_row).execute()
            )
        state = self._get_state()
        updates = {"trade_count_today": state["trade_count_today"] + 1}
        updates["buy_count_today" if side == "BUY" else "sell_count_today"] = \
            state["buy_count_today" if side == "BUY" else "sell_count_today"] + 1
        run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).update(updates).eq("algo_id", self.storage_algo_id()).execute()
        )
        from .broadcaster import broadcast_sync
        broadcast_sync({
            "event": "position_opened",
            "algo_id": self.algo_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "high_price": entry_price,
            "low_price": entry_price,
            "entry_trigger": entry_trigger or "Strategy entry conditions matched",
            "signal_snapshot": merged_snapshot,
        })

    def update_position_range(self, position: dict, ltp: float) -> dict:
        entry = float(position.get("entry_price") or ltp)
        highest = max(float(position.get("highest_price") or entry), float(ltp))
        lowest = min(float(position.get("lowest_price") or entry), float(ltp))
        updates = {}
        if highest != float(position.get("highest_price") or entry):
            updates["highest_price"] = highest
        if lowest != float(position.get("lowest_price") or entry):
            updates["lowest_price"] = lowest
        if updates:
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name()).update(updates).eq("id", position["id"]).execute()
            )
        return {**position, **updates}

    def apply_trailing_stop(self, position: dict, ltp: float, settings: dict) -> dict:
        if uses_silver_breakeven_stop(position, settings):
            return self._apply_silver_breakeven_stop(position, ltp)
        settings = self._legacy_silver_position_settings(position, settings)
        if not should_use_trailing_stop(settings):
            return position

        entry = float(position["entry_price"])
        side = position["side"]
        current_sl = float(position["sl_price"])
        previous_sl = current_sl
        trigger_pct = float(settings.get("trailing_sl_trigger_pct") or 0)
        distance_pct = float(settings.get("trailing_sl_distance_pct") or 0)

        highest = max(float(position.get("highest_price") or entry), float(ltp))
        lowest = min(float(position.get("lowest_price") or entry), float(ltp))
        active = bool(position.get("trailing_sl_active"))
        updates = {"highest_price": highest, "lowest_price": lowest}
        sl_moved = False  # true only when the SL numerically shifts

        point_model = any(
            key in settings
            for key in ("tsl_activate_points", "tsl_profit_step_points", "tsl_lock_step_points")
        )
        if point_model:
            activate_points, profit_step_points, lock_step_points = silver_tsl_points(settings)
            if activate_points <= 0 or profit_step_points <= 0:
                return position
            point_result = calculate_point_trailing(
                entry=entry,
                side=side,
                current_sl=current_sl,
                highest=highest,
                lowest=lowest,
                activate_points=activate_points,
                profit_step_points=profit_step_points,
                lock_step_points=lock_step_points,
            )
            highest = point_result["highest"]
            lowest = point_result["lowest"]
            active = bool(point_result["trailing_active"] or active)
            sl_moved = bool(point_result["sl_moved"])
            if sl_moved:
                previous_sl = float(point_result["previous_sl"])
                current_sl = float(point_result["sl_price"])
                updates["sl_price"] = current_sl
        else:
            if trigger_pct <= 0 or distance_pct <= 0:
                return position
            if side == "BUY":
                move_pct = (highest - entry) / entry * 100
                if move_pct >= trigger_pct:
                    active = True
                    new_sl = highest * (1 - distance_pct / 100)
                    if new_sl > current_sl:
                        previous_sl = current_sl
                        updates["sl_price"] = new_sl
                        current_sl = new_sl
                        sl_moved = True
            else:
                move_pct = (entry - lowest) / entry * 100
                if move_pct >= trigger_pct:
                    active = True
                    new_sl = lowest * (1 + distance_pct / 100)
                    if new_sl < current_sl:
                        previous_sl = current_sl
                        updates["sl_price"] = new_sl
                        current_sl = new_sl
                        sl_moved = True

        updates["trailing_sl_active"] = active

        # Stamp trailing metadata into signal_snapshot so the dashboard can
        # render "trailing activated at HH:MM, bumped N times, initial X ->
        # current Y". Only writes back when something meaningful changed
        # (first activation or an actual SL bump) to keep DB write volume
        # low — this runs on every LTP tick.
        merged_snapshot: dict | None = None
        current_snapshot = dict(position.get("signal_snapshot") or {})
        trailing_meta = dict(current_snapshot.get("trailing") or {})
        trailing_events = list(trailing_meta.get("events") or [])
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if active and not trailing_meta.get("first_activated_at"):
            trailing_meta["first_activated_at"] = now_iso
            trailing_meta["activated"] = True
            if point_model:
                activate_points, profit_step_points, lock_step_points = silver_tsl_points(settings)
                trailing_meta["model"] = "point_lock"
                trailing_meta["activate_points"] = activate_points
                trailing_meta["profit_step_points"] = profit_step_points
                trailing_meta["lock_step_points"] = lock_step_points
            merged_snapshot = current_snapshot  # need to write

        if sl_moved:
            trailing_meta["last_updated_at"] = now_iso
            trailing_meta["update_count"] = int(trailing_meta.get("update_count") or 0) + 1
            trailing_meta.setdefault("activated", True)
            trailing_events.append({
                "at": now_iso,
                "ltp": float(ltp),
                "previous_sl": float(previous_sl),
                "new_sl": float(current_sl),
                "delta": float(current_sl) - float(previous_sl),
            })
            merged_snapshot = current_snapshot  # need to write

        if merged_snapshot is not None:
            trailing_meta["current_sl"] = float(current_sl)
            trailing_meta["events"] = trailing_events
            merged_snapshot["trailing"] = trailing_meta
            # Preserve the original stop when this is the first point-lock
            # move to breakeven; do not mistake the new SL for the initial SL.
            if "initial_sl_price" not in merged_snapshot:
                merged_snapshot["initial_sl_price"] = float(
                    position.get("initial_sl_price") or position.get("sl_price") or current_sl
                )
            updates["signal_snapshot"] = merged_snapshot

        run_with_supabase(
            lambda supabase: supabase.table(self.positions_table_name()).update(updates).eq("id", position["id"]).execute()
        )
        return {**position, **updates, "sl_price": current_sl}

    def _apply_silver_breakeven_stop(self, position: dict, ltp: float) -> dict:
        """Arm a one-time breakeven stop after Silver reaches activation.

        This intentionally is not a point-lock trail: after target is touched
        the stop moves once to the actual entry and remains there.
        """
        entry = float(position["entry_price"])
        final_target = float(position["target_price"])
        side = str(position["side"] or "").upper()
        current_sl = float(position["sl_price"])
        highest = max(float(position.get("highest_price") or entry), float(ltp))
        lowest = min(float(position.get("lowest_price") or entry), float(ltp))
        snapshot = dict(position.get("signal_snapshot") or {})
        breakeven = dict(snapshot.get("silver_breakeven") or {})
        activation_price = float(breakeven.get("activation_price") or final_target)
        reached_target = highest >= activation_price if side == "BUY" else lowest <= activation_price
        already_armed = bool(breakeven.get("armed") or position.get("trailing_sl_active"))
        updates = {"highest_price": highest, "lowest_price": lowest}

        if reached_target and not already_armed:
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            updates.update({
                "sl_price": entry,
                "trailing_sl_active": True,
            })
            breakeven.update({
                "armed": True,
                "armed_at": now_iso,
                "armed_ltp": float(ltp),
                "activation_price": activation_price,
                "target_price": final_target,
                "initial_sl_price": float(snapshot.get("initial_sl_price") or current_sl),
                "current_sl": entry,
                "model": "target_to_breakeven",
            })
            snapshot["silver_breakeven"] = breakeven
            snapshot["trailing"] = {
                "activated": True,
                "first_activated_at": now_iso,
                "last_updated_at": now_iso,
                "update_count": 1,
                "last_sl_before_trail": current_sl,
                "current_sl": entry,
                "events": [{
                    "at": now_iso,
                    "ltp": float(ltp),
                    "previous_sl": current_sl,
                    "new_sl": entry,
                    "delta": entry - current_sl,
                    "reason": "target_to_breakeven",
                }],
            }
            updates["signal_snapshot"] = snapshot

        if updates:
            run_with_supabase(
                lambda supabase: supabase.table(self.positions_table_name()).update(updates).eq("id", position["id"]).execute()
            )
        return {**position, **updates, "sl_price": float(updates.get("sl_price", current_sl))}

    def _legacy_silver_position_settings(self, position: dict | None, settings: dict) -> dict:
        """Keep pre-simplification open Silver positions on their old rules.

        New positions always store ``silver_exit_policy`` in the signal
        snapshot. Only an older open row without that snapshot uses the
        temporary compatibility values carried by settings normalization.
        """
        snapshot = (position or {}).get("signal_snapshot") or {}
        if snapshot.get("silver_exit_policy"):
            return settings
        legacy_mode = settings.get("_legacy_silver_open_position_exit_mode")
        if legacy_mode not in {"trailing_sl_only", "fixed_target_trailing_sl"}:
            return settings
        return {
            **settings,
            "exit_mode": legacy_mode,
            "trailing_sl_enabled": bool(
                settings.get("_legacy_silver_open_position_trailing_enabled")
            ),
        }

    def should_exit_at_target(self, settings: dict, position: dict | None = None) -> bool:
        if uses_silver_breakeven_stop(position, settings):
            snapshot = (position or {}).get("signal_snapshot") or {}
            breakeven = snapshot.get("silver_breakeven") or {}
            # Pre-upgrade open positions do not have the explicit flag and
            # retain their old no-final-target behavior.
            return bool(breakeven.get("final_target_enabled"))
        settings = self._legacy_silver_position_settings(position, settings)
        return should_use_fixed_target(settings)

    def today_counts(self) -> dict:
        today = datetime.date.today().isoformat()
        trades = self._merge_storage_rows([
            run_with_supabase(
                # Include the row id: _merge_storage_rows de-duplicates across
                # namespaced/legacy storage candidates, not across real trades.
                lambda supabase, key=candidate: supabase.table(self.trades_table_name()).select("id,side").eq("algo_id", key)
                .gte("entry_time", today).execute()
            ).data
            for candidate in self.storage_algo_candidates()
        ])
        positions = self._merge_storage_rows([
            run_with_supabase(
                lambda supabase, key=candidate: supabase.table(self.positions_table_name()).select("id,side").eq("algo_id", key)
                .eq("status", "open").gte("entry_time", today).execute()
            ).data
            for candidate in self.storage_algo_candidates()
        ])
        rows = trades + positions
        buy_count = len([row for row in rows if row.get("side") == "BUY"])
        sell_count = len([row for row in rows if row.get("side") == "SELL"])
        return {
            "trade_count_today": len(rows),
            "buy_count_today": buy_count,
            "sell_count_today": sell_count,
        }

    def close_trade(
        self,
        position: dict,
        exit_price: float,
        exit_reason: str,
        exit_time: str | None = None,
    ):
        side = position["side"]
        qty = position["qty"]
        entry_price = position["entry_price"]

        buy_value = entry_price * qty if side == "BUY" else exit_price * qty
        sell_value = exit_price * qty if side == "BUY" else entry_price * qty

        config = get_charges_config()
        charges = calculate_charges(buy_value, sell_value, config)

        run_with_supabase(
            lambda supabase: supabase.table(self.positions_table_name()).update({"status": "closed"}).eq("id", position["id"]).execute()
        )
        trade_row = {
            "algo_id": self.storage_algo_id(),
            "symbol": position["symbol"],
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_time": position["entry_time"],
            "exit_time": exit_time or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "entry_trigger": position.get("entry_trigger"),
            "signal_snapshot": position.get("signal_snapshot"),
            "exit_reason": exit_reason,
            **charges,
        }
        try:
            run_with_supabase(
                lambda supabase: supabase.table(self.trades_table_name()).insert(trade_row).execute()
            )
        except Exception as exc:
            if "entry_trigger" not in str(exc) and "signal_snapshot" not in str(exc):
                raise
            trade_row.pop("entry_trigger", None)
            trade_row.pop("signal_snapshot", None)
            run_with_supabase(
                lambda supabase: supabase.table(self.trades_table_name()).insert(trade_row).execute()
            )

        state = self._get_state()
        run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).update({"cash": state["cash"] + charges["net_pnl"]}).eq("algo_id", self.storage_algo_id()).execute()
        )
        from .broadcaster import broadcast_sync
        broadcast_sync({
            "event": "position_closed",
            "algo_id": self.algo_id,
            "symbol": position["symbol"],
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "entry_trigger": position.get("entry_trigger"),
            "signal_snapshot": position.get("signal_snapshot"),
            "net_pnl": charges["net_pnl"],
            "gross_pnl": charges["gross_pnl"],
            "total_charges": charges["total_charges"],
        })
        on_position_closed = getattr(self, "on_position_closed", None)
        if callable(on_position_closed):
            try:
                on_position_closed(
                    position=position,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    exit_time=trade_row["exit_time"],
                )
            except Exception as exc:
                print(f"[paper_broker] on_position_closed callback failed: {exc}")

    def summary(self) -> dict:
        state = self._get_state()
        counts = self.today_counts()
        # Sum the same trades the UI shows in "Closed Trades Today" so the
        # Gross/Net tiles and the list stay in lockstep. recent_trades()
        # uses gte("exit_time", server-date) which, with a UTC server and
        # IST clients, keeps yesterday-IST rows visible past midnight IST
        # (which is what the user sees at 01:00 IST after MCX evening
        # trades). Whatever is visible in the list adds up in the tiles;
        # old months never pile up forever.
        closed_trades = self.recent_trades(limit=1000, today_only=True)
        realized_net = sum(float(t.get("net_pnl") or 0) for t in closed_trades)
        realized_gross = sum(float(t.get("gross_pnl") or 0) for t in closed_trades)
        realized_charges = sum(float(t.get("total_charges") or 0) for t in closed_trades)
        return {
            "cash": round(state["cash"], 2),
            "starting_capital": self.starting_capital,
            "trade_count_today": counts["trade_count_today"],
            "buy_count_today": counts["buy_count_today"],
            "sell_count_today": counts["sell_count_today"],
            "realized_gross_pnl": round(realized_gross, 2),
            "realized_charges": round(realized_charges, 2),
            "realized_net_pnl": round(realized_net, 2),
        }

    def modify_protection(
        self,
        position_id,
        *,
        sl_price: float | None = None,
        target_price: float | None = None,
    ) -> dict:
        """Update SL and/or Target on an already-open position.

        Paper: DB-only update. LiveBroker overrides this to ALSO amend the
        matching pending SL / target orders at Fyers before writing the DB
        so a UI edit reflects on both sides. Only the fields supplied are
        changed; the untouched leg keeps its current level.
        """
        position = self._find_open_position(position_id)
        if not position:
            raise ValueError(f"Position {position_id!r} is not open for this algo.")
        updates: dict = {}
        side = str(position.get("side") or "").upper()
        entry_price = float(position.get("entry_price") or 0)
        if sl_price is not None:
            sl_value = float(sl_price)
            if sl_value <= 0:
                raise ValueError("Stop loss must be greater than zero.")
            if side == "BUY" and entry_price and sl_value >= entry_price:
                raise ValueError("Stop loss for a BUY must be below the entry price.")
            if side == "SELL" and entry_price and sl_value <= entry_price:
                raise ValueError("Stop loss for a SELL must be above the entry price.")
            updates["sl_price"] = round(sl_value, 2)
        if target_price is not None:
            target_value = float(target_price)
            if target_value <= 0:
                raise ValueError("Target must be greater than zero.")
            if side == "BUY" and entry_price and target_value <= entry_price:
                raise ValueError("Target for a BUY must be above the entry price.")
            if side == "SELL" and entry_price and target_value >= entry_price:
                raise ValueError("Target for a SELL must be below the entry price.")
            updates["target_price"] = round(target_value, 2)
        if not updates:
            return position
        run_with_supabase(
            lambda supabase: supabase.table(self.positions_table_name())
            .update(updates).eq("id", position["id"]).execute()
        )
        print(
            f"[paper_broker] protection updated for position {position.get('id')} "
            f"{position.get('symbol')} {side}: {updates}"
        )
        return {**position, **updates}

    def _find_open_position(self, position_id) -> dict | None:
        target = str(position_id)
        for position in self.open_positions():
            if str(position.get("id")) == target:
                return position
        return None

    def set_available_cash(self, amount: float) -> float:
        """Set this algo's paper balance without changing trade history or limits."""
        cash = round(float(amount), 2)
        if cash < 0:
            raise ValueError("Available cash cannot be negative.")
        run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).update({"cash": cash}).eq("algo_id", self.storage_algo_id()).execute()
        )
        return cash

    def daily_history(self, days: int = 30) -> list[dict]:
        start_date = datetime.date.today() - datetime.timedelta(days=max(days - 1, 0))
        try:
            trades = self._merge_storage_rows([
                run_with_supabase(
                    lambda supabase, key=candidate: supabase.table(self.trades_table_name()).select(
                        "entry_time,exit_time,symbol,side,qty,entry_price,exit_price,entry_trigger,gross_pnl,total_charges,net_pnl"
                    ).eq("algo_id", key).gte("exit_time", start_date.isoformat()).order("exit_time").execute()
                ).data
                for candidate in self.storage_algo_candidates()
            ], order_key="exit_time")
        except Exception as exc:
            if "entry_trigger" not in str(exc):
                raise
            trades = self._merge_storage_rows([
                run_with_supabase(
                    lambda supabase, key=candidate: supabase.table(self.trades_table_name()).select(
                        "entry_time,exit_time,symbol,side,qty,entry_price,exit_price,gross_pnl,total_charges,net_pnl"
                    ).eq("algo_id", key).gte("exit_time", start_date.isoformat()).order("exit_time").execute()
                ).data
                for candidate in self.storage_algo_candidates()
            ], order_key="exit_time")

        grouped: dict[str, dict] = {}
        for trade in trades:
            day = trade["exit_time"][:10]
            bucket = grouped.setdefault(day, {
                "date": day,
                "trade_count": 0,
                "gross_pnl": 0.0,
                "charges": 0.0,
                "net_pnl": 0.0,
                "symbols": set(),
            })
            bucket["trade_count"] += 1
            bucket["gross_pnl"] += float(trade.get("gross_pnl") or 0)
            bucket["charges"] += float(trade.get("total_charges") or 0)
            bucket["net_pnl"] += float(trade.get("net_pnl") or 0)
            if trade.get("symbol"):
                bucket["symbols"].add(trade["symbol"])

        history = []
        for day in sorted(grouped.keys(), reverse=True):
            bucket = grouped[day]
            history.append({
                "date": bucket["date"],
                "trade_count": bucket["trade_count"],
                "gross_pnl": round(bucket["gross_pnl"], 2),
                "charges": round(bucket["charges"], 2),
                "net_pnl": round(bucket["net_pnl"], 2),
                "symbols": sorted(bucket["symbols"]),
            })
        return history


def should_use_trailing_stop(settings: dict) -> bool:
    mode = settings.get("exit_mode", "fixed_target_trailing_sl")
    return bool(settings.get("trailing_sl_enabled")) and mode in {"trailing_sl_only", "fixed_target_trailing_sl"}


def should_use_fixed_target(settings: dict) -> bool:
    mode = settings.get("exit_mode", "fixed_target_trailing_sl")
    return mode in {"fixed_target_sl", "fixed_target_trailing_sl"}
