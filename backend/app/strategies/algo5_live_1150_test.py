import datetime
import threading

from .base import Strategy
from ..fyers_auth import get_stored_access_token
from ..fyers_client import get_previous_close
from ..paper_broker import PaperBroker

MIN_GAP_PCT = 0.5
MAX_GAP_PCT = 2.0
TICK_SIZE = 0.05
SIGNAL_CANDLE_TIME = "11:50"


class Algo5Live1150Test(Strategy):
    algo_id = "algo5"
    display_name = "Algo 5 - Live 11:50 Test"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.prev_close: dict[str, float] = {}
        self.candidates: dict[str, tuple[str, float]] = {}
        self.candidate_details: dict[str, dict] = {}
        self.selected_symbols: set[str] = set()
        self.entries_evaluated_today = None
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def _load_previous_closes_background(self):
        try:
            if not get_stored_access_token():
                print("[algo5] no Fyers access token yet, skipping previous-close preload")
                return
            for symbol in self.watchlist:
                try:
                    close = get_previous_close(symbol)
                    if close:
                        self.prev_close[symbol] = close
                except Exception as e:
                    print(f"[algo5] couldn't get prev close for {symbol}: {e}")
        except Exception as e:
            print(f"[algo5] error in background preload: {e}")

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if candle["time"].strftime("%H:%M") != SIGNAL_CANDLE_TIME:
            return

        side = self._signal_side(symbol, candle)
        if side:
            self.candidates[symbol] = (side, candle["close"])
            prev_close = self.prev_close[symbol]
            self.candidate_details[symbol] = {
                "symbol": symbol,
                "side": side,
                "open": candle["open"],
                "prev_close": prev_close,
                "gap_pct": abs(candle["open"] - prev_close) / prev_close * 100,
                "passed_indicators": True,
                "indicator_results": {},
                "selected_for_trade": False,
                "rejection_reason": None,
            }

        now = datetime.datetime.now()
        if side and self._is_entry_window(now):
            self._enter(symbol, side, indicators.get("last_ltp") or candle["close"])

    def evaluate_entries(self, get_ltp_fn):
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return
        self.entries_evaluated_today = today

        for symbol, (side, fallback_price) in list(self.candidates.items()):
            entry_price = get_ltp_fn(symbol) or fallback_price
            self._enter(symbol, side, entry_price)
        self._record_scan_results()

    def _signal_side(self, symbol: str, candle: dict) -> str | None:
        prev_close = self.prev_close.get(symbol)
        if not prev_close:
            return None

        open_price = candle["open"]
        if abs(open_price - candle["low"]) <= TICK_SIZE:
            gap = open_price - prev_close
            if MIN_GAP_PCT / 100 * prev_close <= gap <= MAX_GAP_PCT / 100 * prev_close:
                return "BUY"

        if abs(open_price - candle["high"]) <= TICK_SIZE:
            gap = prev_close - open_price
            if MIN_GAP_PCT / 100 * prev_close <= gap <= MAX_GAP_PCT / 100 * prev_close:
                return "SELL"

        return None

    def _is_entry_window(self, now: datetime.datetime) -> bool:
        start = now.replace(hour=11, minute=51, second=0, microsecond=0)
        end = now.replace(hour=11, minute=52, second=0, microsecond=0)
        return start <= now < end

    def _can_open_side(self, side: str) -> bool:
        state = self.broker.summary()
        if state["trade_count_today"] >= self.settings["max_trades_per_day"]:
            return False
        if side == "BUY":
            return state["buy_count_today"] < self.settings["max_buy_trades"] or state["sell_count_today"] == self.settings["max_sell_trades"]
        return state["sell_count_today"] < self.settings["max_sell_trades"] or state["buy_count_today"] == self.settings["max_buy_trades"]

    def _enter(self, symbol: str, side: str, entry_price: float):
        if not entry_price or self.broker.already_traded_today(symbol) or not self._can_open_side(side):
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
        buy_selected = 0
        sell_selected = 0
        for symbol, details in self.candidate_details.items():
            row = dict(details)
            row["selected_for_trade"] = symbol in self.selected_symbols
            if row["selected_for_trade"] and row["side"] == "BUY":
                buy_selected += 1
            if row["selected_for_trade"] and row["side"] == "SELL":
                sell_selected += 1
            rows.append(row)
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": len([r for r in rows if r["side"] == "BUY"]),
            "sell_candidates": len([r for r in rows if r["side"] == "SELL"]),
            "buy_selected": buy_selected,
            "sell_selected": sell_selected,
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": max(0, len(self.watchlist) - len(rows)),
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

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
