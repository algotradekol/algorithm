"""
paper_broker.py — same idea as the earlier local version, but now
reads/writes Supabase so the frontend can see live state from any
device, and both algos get their own isolated capital pool + trade
log (keyed by algo_id).
"""
import datetime
from .supabase_client import run_with_supabase
from .charges import calculate_charges, get_charges_config


class PaperBroker:
    def __init__(self, algo_id: str, starting_capital: float):
        self.algo_id = algo_id
        self.starting_capital = starting_capital
        self._ensure_state_row()

    def _ensure_state_row(self):
        existing = run_with_supabase(
            lambda supabase: supabase.table("algo_state").select("*").eq("algo_id", self.algo_id).execute()
        )
        if not existing.data:
            run_with_supabase(
                lambda supabase: supabase.table("algo_state").insert({
                    "algo_id": self.algo_id, "cash": self.starting_capital,
                    "trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0,
                    "trading_date": datetime.date.today().isoformat(),
                }).execute()
            )

    def _get_state(self) -> dict:
        row = run_with_supabase(
            lambda supabase: supabase.table("algo_state").select("*").eq("algo_id", self.algo_id).execute()
        ).data[0]
        today = datetime.date.today().isoformat()
        if row["trading_date"] != today:
            # new trading day -- reset daily counters, keep cumulative cash/pnl
            run_with_supabase(
                lambda supabase: supabase.table("algo_state").update({
                    "trading_date": today, "trade_count_today": 0,
                    "buy_count_today": 0, "sell_count_today": 0,
                }).eq("algo_id", self.algo_id).execute()
            )
            row.update({"trading_date": today, "trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0})
        return row

    def open_positions(self) -> list[dict]:
        return run_with_supabase(
            lambda supabase: supabase.table("positions").select("*").eq("algo_id", self.algo_id).eq("status", "open").execute()
        ).data

    def recent_trades(self, limit: int = 200) -> list[dict]:
        result = run_with_supabase(
            lambda supabase: supabase.table("trades").select("*").eq("algo_id", self.algo_id)
            .order("exit_time", desc=True).limit(limit).execute()
        )
        return result.data

    def already_traded_today(self, symbol: str) -> bool:
        today = datetime.date.today().isoformat()
        result = run_with_supabase(
            lambda supabase: supabase.table("trades").select("id").eq("algo_id", self.algo_id)
            .eq("symbol", symbol).gte("entry_time", today).execute()
        )
        return len(result.data) > 0

    def can_open_new_trade(self, side: str, max_total: int, max_per_side: int) -> bool:
        state = self._get_state()
        if state["trade_count_today"] >= max_total:
            return False
        side_count = state["buy_count_today"] if side == "BUY" else state["sell_count_today"]
        other_count = state["sell_count_today"] if side == "BUY" else state["buy_count_today"]
        if side_count < max_per_side:
            return True
        # allow counter/overflow trades on this side if the OTHER side hasn't used its full quota
        # and the total cap isn't hit yet (fills the 10-trade cap even if one side has fewer signals)
        return state["trade_count_today"] < max_total

    def open_trade(self, symbol: str, side: str, qty: int, entry_price: float, sl_price: float, target_price: float):
        run_with_supabase(
            lambda supabase: supabase.table("positions").insert({
                "algo_id": self.algo_id, "symbol": symbol, "side": side, "qty": qty,
                "entry_price": entry_price, "sl_price": sl_price, "target_price": target_price,
                "status": "open", "entry_time": datetime.datetime.now().isoformat(),
            }).execute()
        )
        state = self._get_state()
        updates = {"trade_count_today": state["trade_count_today"] + 1}
        updates["buy_count_today" if side == "BUY" else "sell_count_today"] = \
            state["buy_count_today" if side == "BUY" else "sell_count_today"] + 1
        run_with_supabase(
            lambda supabase: supabase.table("algo_state").update(updates).eq("algo_id", self.algo_id).execute()
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
        })

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        side = position["side"]
        qty = position["qty"]
        entry_price = position["entry_price"]

        buy_value = entry_price * qty if side == "BUY" else exit_price * qty
        sell_value = exit_price * qty if side == "BUY" else entry_price * qty

        config = get_charges_config()
        charges = calculate_charges(buy_value, sell_value, config)

        run_with_supabase(
            lambda supabase: supabase.table("positions").update({"status": "closed"}).eq("id", position["id"]).execute()
        )
        run_with_supabase(
            lambda supabase: supabase.table("trades").insert({
                "algo_id": self.algo_id, "symbol": position["symbol"], "side": side, "qty": qty,
                "entry_price": entry_price, "exit_price": exit_price,
                "entry_time": position["entry_time"], "exit_time": datetime.datetime.now().isoformat(),
                "exit_reason": exit_reason, **charges,
            }).execute()
        )

        state = self._get_state()
        run_with_supabase(
            lambda supabase: supabase.table("algo_state").update({"cash": state["cash"] + charges["net_pnl"]}).eq("algo_id", self.algo_id).execute()
        )
        from .broadcaster import broadcast_sync
        broadcast_sync({
            "event": "position_closed",
            "algo_id": self.algo_id,
            "symbol": position["symbol"],
            "exit_reason": exit_reason,
            "net_pnl": charges["net_pnl"],
            "gross_pnl": charges["gross_pnl"],
            "total_charges": charges["total_charges"],
        })

    def summary(self) -> dict:
        state = self._get_state()
        trades_today = run_with_supabase(
            lambda supabase: supabase.table("trades").select("net_pnl,gross_pnl,total_charges")
            .eq("algo_id", self.algo_id).gte("entry_time", datetime.date.today().isoformat()).execute()
        ).data
        realized_net = sum(t["net_pnl"] for t in trades_today)
        realized_gross = sum(t["gross_pnl"] for t in trades_today)
        realized_charges = sum(t["total_charges"] for t in trades_today)
        return {
            "cash": round(state["cash"], 2),
            "starting_capital": self.starting_capital,
            "trade_count_today": state["trade_count_today"],
            "buy_count_today": state["buy_count_today"],
            "sell_count_today": state["sell_count_today"],
            "realized_gross_pnl": round(realized_gross, 2),
            "realized_charges": round(realized_charges, 2),
            "realized_net_pnl": round(realized_net, 2),
        }

    def daily_history(self, days: int = 30) -> list[dict]:
        start_date = datetime.date.today() - datetime.timedelta(days=max(days - 1, 0))
        trades = run_with_supabase(
            lambda supabase: supabase.table("trades").select(
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
