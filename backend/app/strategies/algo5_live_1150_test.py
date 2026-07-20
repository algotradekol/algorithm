import datetime
import time

from .base import Strategy
from ..paper_broker import PaperBroker

MIN_MOVE_PCT = 0.10
SCAN_BROADCAST_SECONDS = 5
MAX_SCAN_ROWS = 100


class Algo5Live1150Test(Strategy):
    algo_id = "algo5"
    display_name = "Algo 5 - Live Tick Smoke Test"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.first_ltp: dict[str, float] = {}
        self.latest_ltp: dict[str, float] = {}
        self.latest_seen_at: dict[str, str] = {}
        self.selected_symbols: set[str] = set()
        self.last_scan_broadcast_at = 0.0

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def on_tick(self, symbol: str, ltp: float, timestamp):
        if not ltp:
            return

        self.first_ltp.setdefault(symbol, float(ltp))
        self.latest_ltp[symbol] = float(ltp)
        self.latest_seen_at[symbol] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        side = self._signal_side(symbol)
        if side:
            self._enter(symbol, side, float(ltp))

        now = time.time()
        if now - self.last_scan_broadcast_at >= SCAN_BROADCAST_SECONDS:
            self.last_scan_broadcast_at = now
            self._record_scan_results()

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        pass

    def evaluate_entries(self, get_ltp_fn):
        self._record_scan_results()

    def _signal_side(self, symbol: str) -> str | None:
        first = self.first_ltp.get(symbol)
        latest = self.latest_ltp.get(symbol)
        if not first or not latest:
            return None

        move_pct = (latest - first) / first * 100
        if move_pct >= MIN_MOVE_PCT:
            return "BUY"
        if move_pct <= -MIN_MOVE_PCT:
            return "SELL"
        return None

    def _can_open_side(self, side: str) -> bool:
        state = self.broker.summary()
        if state["trade_count_today"] >= self.settings["max_trades_per_day"]:
            return False
        if side == "BUY":
            return state["buy_count_today"] < self.settings["max_buy_trades"] or state["sell_count_today"] == self.settings["max_sell_trades"]
        return state["sell_count_today"] < self.settings["max_sell_trades"] or state["buy_count_today"] == self.settings["max_buy_trades"]

    def _enter(self, symbol: str, side: str, entry_price: float):
        open_symbols = {position["symbol"] for position in self.broker.open_positions()}
        if symbol in open_symbols or symbol in self.selected_symbols or not entry_price or self.broker.already_traded_today(symbol) or not self._can_open_side(side):
            return

        qty = int(self.settings["capital_per_trade"] // entry_price)
        if qty < 1:
            return

        if side == "BUY":
            sl_price = entry_price * (1 - self.settings["sl_pct"] / 100)
            target_price = entry_price * (1 + self.settings["target_pct"] / 100)
        else:
            sl_price = entry_price * (1 + self.settings["sl_pct"] / 100)
            target_price = entry_price * (1 - self.settings["target_pct"] / 100)
        self.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price)
        self.selected_symbols.add(symbol)

    def _record_scan_results(self):
        rows = []
        for symbol, latest in sorted(self.latest_ltp.items(), key=lambda item: abs(self._move_pct(item[0])), reverse=True)[:MAX_SCAN_ROWS]:
            first = self.first_ltp.get(symbol) or latest
            move_pct = self._move_pct(symbol)
            side = self._signal_side(symbol) or "WATCH"
            selected = symbol in self.selected_symbols
            rows.append({
                "symbol": symbol,
                "side": side,
                "open": first,
                "prev_close": first,
                "gap_pct": move_pct,
                "passed_indicators": side != "WATCH",
                "indicator_results": {
                    "vwap": {"value": latest, "passed": True, "enabled": False},
                    "rsi": {"value": move_pct, "passed": abs(move_pct) >= MIN_MOVE_PCT, "enabled": True},
                    "adx": {"value": abs(move_pct), "passed": abs(move_pct) >= MIN_MOVE_PCT, "enabled": True},
                    "supertrend": {"value": first, "passed": True, "enabled": False},
                    "volume": {"value": 0, "passed": True, "enabled": False},
                },
                "selected_for_trade": selected,
                "rejection_reason": None if selected else ("waiting_for_0.10pct_move" if side == "WATCH" else "trade_slots_or_duplicate_check"),
                "last_seen_at": self.latest_seen_at.get(symbol),
            })

        buy_candidates = len([row for row in rows if row["side"] == "BUY"])
        sell_candidates = len([row for row in rows if row["side"] == "SELL"])
        buy_selected = len([row for row in rows if row["selected_for_trade"] and row["side"] == "BUY"])
        sell_selected = len([row for row in rows if row["selected_for_trade"] and row["side"] == "SELL"])
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": buy_candidates,
            "sell_candidates": sell_candidates,
            "buy_selected": buy_selected,
            "sell_selected": sell_selected,
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": max(0, len(self.watchlist) - len(self.latest_ltp)),
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

    def _move_pct(self, symbol: str) -> float:
        first = self.first_ltp.get(symbol)
        latest = self.latest_ltp.get(symbol)
        if not first or not latest:
            return 0.0
        return (latest - first) / first * 100

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")
            if not ltp:
                continue
            position = self.broker.apply_trailing_stop(position, ltp, self.settings)
            side, sl, target = position["side"], position["sl_price"], position["target_price"]
            use_target = self.broker.should_exit_at_target(self.settings)
            if side == "BUY":
                if ltp <= sl:
                    self.broker.close_trade(position, ltp, "SL")
                elif use_target and ltp >= target:
                    self.broker.close_trade(position, ltp, "TARGET")
            else:
                if ltp >= sl:
                    self.broker.close_trade(position, ltp, "SL")
                elif use_target and ltp <= target:
                    self.broker.close_trade(position, ltp, "TARGET")

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")
