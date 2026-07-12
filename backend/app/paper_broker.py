"""
paper_broker.py — same idea as the earlier local version, but now
reads/writes Supabase so the frontend can see live state from any
device, and both algos get their own isolated capital pool + trade
log (keyed by algo_id).
"""
import datetime
from app.supabase_client import supabase
from app.charges import calculate_charges, get_charges_config


class PaperBroker:
    def __init__(self, algo_id: str, starting_capital: float):
        self.algo_id = algo_id
        self.starting_capital = starting_capital
        self._ensure_state_row()

    def _ensure_state_row(self):
        existing = supabase.table("algo_state").select("*").eq("algo_id", self.algo_id).execute()
        if not existing.data:
            supabase.table("algo_state").insert({
                "algo_id": self.algo_id, "cash": self.starting_capital,
                "trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0,
                "trading_date": datetime.date.today().isoformat(),
            }).execute()

    def _get_state(self) -> dict:
        row = supabase.table("algo_state").select("*").eq("algo_id", self.algo_id).execute().data[0]
        today = datetime.date.today().isoformat()
        if row["trading_date"] != today:
            # new trading day -- reset daily counters, keep cumulative cash/pnl
            supabase.table("algo_state").update({
                "trading_date": today, "trade_count_today": 0,
                "buy_count_today": 0, "sell_count_today": 0,
            }).eq("algo_id", self.algo_id).execute()
            row.update({"trading_date": today, "trade_count_today": 0, "buy_count_today": 0, "sell_count_today": 0})
        return row

    def open_positions(self) -> list[dict]:
        return supabase.table("positions").select("*").eq("algo_id", self.algo_id).eq("status", "open").execute().data

    def already_traded_today(self, symbol: str) -> bool:
        today = datetime.date.today().isoformat()
        result = supabase.table("trades").select("id").eq("algo_id", self.algo_id) \
            .eq("symbol", symbol).gte("entry_time", today).execute()
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
        supabase.table("positions").insert({
            "algo_id": self.algo_id, "symbol": symbol, "side": side, "qty": qty,
            "entry_price": entry_price, "sl_price": sl_price, "target_price": target_price,
            "status": "open", "entry_time": datetime.datetime.now().isoformat(),
        }).execute()
        state = self._get_state()
        updates = {"trade_count_today": state["trade_count_today"] + 1}
        updates["buy_count_today" if side == "BUY" else "sell_count_today"] = \
            state["buy_count_today" if side == "BUY" else "sell_count_today"] + 1
        supabase.table("algo_state").update(updates).eq("algo_id", self.algo_id).execute()

    def close_trade(self, position: dict, exit_price: float, exit_reason: str):
        side = position["side"]
        qty = position["qty"]
        entry_price = position["entry_price"]

        buy_value = entry_price * qty if side == "BUY" else exit_price * qty
        sell_value = exit_price * qty if side == "BUY" else entry_price * qty

        config = get_charges_config()
        charges = calculate_charges(buy_value, sell_value, config)

        supabase.table("positions").update({"status": "closed"}).eq("id", position["id"]).execute()
        supabase.table("trades").insert({
            "algo_id": self.algo_id, "symbol": position["symbol"], "side": side, "qty": qty,
            "entry_price": entry_price, "exit_price": exit_price,
            "entry_time": position["entry_time"], "exit_time": datetime.datetime.now().isoformat(),
            "exit_reason": exit_reason, **charges,
        }).execute()

        state = self._get_state()
        supabase.table("algo_state").update({"cash": state["cash"] + charges["net_pnl"]}).eq("algo_id", self.algo_id).execute()

    def summary(self) -> dict:
        state = self._get_state()
        trades_today = supabase.table("trades").select("net_pnl,gross_pnl,total_charges") \
            .eq("algo_id", self.algo_id).gte("entry_time", datetime.date.today().isoformat()).execute().data
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
