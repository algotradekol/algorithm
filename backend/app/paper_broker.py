"""
paper_broker.py — same idea as the earlier local version, but now
reads/writes Supabase so the frontend can see live state from any
device, and both algos get their own isolated capital pool + trade
log (keyed by algo_id).
"""
from __future__ import annotations

import datetime

from .charges import calculate_charges, get_charges_config
from .supabase_client import run_with_supabase


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

    def _ensure_state_row(self):
        existing = run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).select("*").eq("algo_id", self.algo_id).execute()
        )
        if not existing.data:
            run_with_supabase(
                lambda supabase: supabase.table(self.state_table_name()).insert({
                    "algo_id": self.algo_id,
                    "cash": self.starting_capital,
                    "trade_count_today": 0,
                    "buy_count_today": 0,
                    "sell_count_today": 0,
                    "trading_date": datetime.date.today().isoformat(),
                }).execute()
            )

    def _get_state(self) -> dict:
        row = run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).select("*").eq("algo_id", self.algo_id).execute()
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
                }).eq("algo_id", self.algo_id).execute()
            )
            row.update({"trading_date": today, "trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0})
        return row

    def open_positions(self, include_stale: bool = False) -> list[dict]:
        query_date = datetime.date.today().isoformat()

        def query(supabase):
            request = supabase.table(self.positions_table_name()).select("*").eq("algo_id", self.algo_id).eq("status", "open")
            if not include_stale:
                request = request.gte("entry_time", query_date)
            return request.execute()

        return run_with_supabase(query).data

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

        def query(supabase):
            request = supabase.table(self.trades_table_name()).select("*").eq("algo_id", self.algo_id)
            if today_only:
                request = request.gte("entry_time", query_date)
            return request.order("exit_time", desc=True).limit(limit).execute()

        result = run_with_supabase(query)
        return result.data

    def already_traded_today(self, symbol: str) -> bool:
        today = datetime.date.today().isoformat()
        result = run_with_supabase(
            lambda supabase: supabase.table(self.trades_table_name()).select("id").eq("algo_id", self.algo_id)
            .eq("symbol", symbol).gte("entry_time", today).execute()
        )
        return len(result.data) > 0

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
        merged_snapshot.setdefault("trailing", {
            "activated": False,
            "first_activated_at": None,
            "last_updated_at": None,
            "update_count": 0,
            "last_sl_before_trail": float(sl_price),
        })

        position_row = {
            "algo_id": self.algo_id,
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
            "entry_time": entry_time or datetime.datetime.now().isoformat(),
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
            lambda supabase: supabase.table(self.state_table_name()).update(updates).eq("algo_id", self.algo_id).execute()
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
        if not should_use_trailing_stop(settings):
            return position

        entry = float(position["entry_price"])
        side = position["side"]
        current_sl = float(position["sl_price"])
        trigger_pct = float(settings.get("trailing_sl_trigger_pct") or 0)
        distance_pct = float(settings.get("trailing_sl_distance_pct") or 0)
        if trigger_pct <= 0 or distance_pct <= 0:
            return position

        highest = max(float(position.get("highest_price") or entry), float(ltp))
        lowest = min(float(position.get("lowest_price") or entry), float(ltp))
        active = bool(position.get("trailing_sl_active"))
        updates = {"highest_price": highest, "lowest_price": lowest}
        sl_moved = False  # true only when the SL numerically shifts

        if side == "BUY":
            move_pct = (highest - entry) / entry * 100
            if move_pct >= trigger_pct:
                active = True
                new_sl = highest * (1 - distance_pct / 100)
                if new_sl > current_sl:
                    updates["sl_price"] = new_sl
                    current_sl = new_sl
                    sl_moved = True
        else:
            move_pct = (entry - lowest) / entry * 100
            if move_pct >= trigger_pct:
                active = True
                new_sl = lowest * (1 + distance_pct / 100)
                if new_sl < current_sl:
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
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if active and not trailing_meta.get("first_activated_at"):
            trailing_meta["first_activated_at"] = now_iso
            trailing_meta["activated"] = True
            merged_snapshot = current_snapshot  # need to write

        if sl_moved:
            trailing_meta["last_updated_at"] = now_iso
            trailing_meta["update_count"] = int(trailing_meta.get("update_count") or 0) + 1
            trailing_meta.setdefault("activated", True)
            merged_snapshot = current_snapshot  # need to write

        if merged_snapshot is not None:
            merged_snapshot["trailing"] = trailing_meta
            # Preserve initial_sl_price if it existed; older positions
            # written before this feature won't have it — stamp it now
            # using the SL that was in play when trailing first activated.
            if "initial_sl_price" not in merged_snapshot:
                merged_snapshot["initial_sl_price"] = float(
                    position.get("sl_price") if not sl_moved else current_sl
                )
            updates["signal_snapshot"] = merged_snapshot

        run_with_supabase(
            lambda supabase: supabase.table(self.positions_table_name()).update(updates).eq("id", position["id"]).execute()
        )
        return {**position, **updates, "sl_price": current_sl}

    def should_exit_at_target(self, settings: dict) -> bool:
        return should_use_fixed_target(settings)

    def today_counts(self) -> dict:
        today = datetime.date.today().isoformat()
        trades = run_with_supabase(
            lambda supabase: supabase.table(self.trades_table_name()).select("side").eq("algo_id", self.algo_id)
            .gte("entry_time", today).execute()
        ).data
        positions = run_with_supabase(
            lambda supabase: supabase.table(self.positions_table_name()).select("side").eq("algo_id", self.algo_id)
            .eq("status", "open").gte("entry_time", today).execute()
        ).data
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
            "algo_id": self.algo_id,
            "symbol": position["symbol"],
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_time": position["entry_time"],
            "exit_time": exit_time or datetime.datetime.now().isoformat(),
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
            lambda supabase: supabase.table(self.state_table_name()).update({"cash": state["cash"] + charges["net_pnl"]}).eq("algo_id", self.algo_id).execute()
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

    def summary(self) -> dict:
        state = self._get_state()
        counts = self.today_counts()
        trades_today = run_with_supabase(
            lambda supabase: supabase.table(self.trades_table_name()).select("net_pnl,gross_pnl,total_charges")
            .eq("algo_id", self.algo_id).gte("entry_time", datetime.date.today().isoformat()).execute()
        ).data
        realized_net = sum(t["net_pnl"] for t in trades_today)
        realized_gross = sum(t["gross_pnl"] for t in trades_today)
        realized_charges = sum(t["total_charges"] for t in trades_today)
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

    def set_available_cash(self, amount: float) -> float:
        """Set this algo's paper balance without changing trade history or limits."""
        cash = round(float(amount), 2)
        if cash < 0:
            raise ValueError("Available cash cannot be negative.")
        run_with_supabase(
            lambda supabase: supabase.table(self.state_table_name()).update({"cash": cash}).eq("algo_id", self.algo_id).execute()
        )
        return cash

    def daily_history(self, days: int = 30) -> list[dict]:
        start_date = datetime.date.today() - datetime.timedelta(days=max(days - 1, 0))
        try:
            trades = run_with_supabase(
                lambda supabase: supabase.table(self.trades_table_name()).select(
                    "entry_time,exit_time,symbol,side,qty,entry_price,exit_price,entry_trigger,gross_pnl,total_charges,net_pnl"
                ).eq("algo_id", self.algo_id).gte("exit_time", start_date.isoformat()).order("exit_time").execute()
            ).data
        except Exception as exc:
            if "entry_trigger" not in str(exc):
                raise
            trades = run_with_supabase(
                lambda supabase: supabase.table(self.trades_table_name()).select(
                    "entry_time,exit_time,symbol,side,qty,entry_price,exit_price,gross_pnl,total_charges,net_pnl"
                ).eq("algo_id", self.algo_id).gte("exit_time", start_date.isoformat()).order("exit_time").execute()
            ).data

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
