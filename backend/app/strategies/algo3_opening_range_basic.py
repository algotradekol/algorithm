import datetime
import threading

from .base import Strategy
from ..fyers_auth import get_stored_access_token
from ..fyers_client import get_previous_close
from ..paper_broker import PaperBroker

CAPITAL_PER_TRADE = 50_000
TARGET_PCT = 2.0
SL_PCT = 1.0
MIN_GAP_PCT = 0.5
MAX_GAP_PCT = 2.0
MAX_TOTAL_TRADES = 10
MAX_PER_SIDE = 5
TICK_SIZE = 0.05


class Algo3OpeningRangeBasic(Strategy):
    algo_id = "algo3"
    display_name = "Algo 3 — Opening Range Gap (Basic)"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=CAPITAL_PER_TRADE * MAX_TOTAL_TRADES)
        self.prev_close: dict[str, float] = {}
        self.candidates: dict[str, tuple[str, float]] = {}
        self.entries_evaluated_today = None
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def _load_previous_closes_background(self):
        try:
            if not get_stored_access_token():
                print("[algo3] no Fyers access token yet, skipping previous-close preload")
                return
            for symbol in self.watchlist:
                try:
                    close = get_previous_close(symbol)
                    if close:
                        self.prev_close[symbol] = close
                except Exception as e:
                    print(f"[algo3] couldn't get prev close for {symbol}: {e}")
        except Exception as e:
            print(f"[algo3] error in background preload: {e}")

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if candle["time"].strftime("%H:%M") != "09:15":
            return

        side = self._signal_side(symbol, candle)
        if side:
            self.candidates[symbol] = (side, candle["close"])

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
        start = now.replace(hour=9, minute=16, second=0, microsecond=0)
        end = now.replace(hour=9, minute=17, second=0, microsecond=0)
        return start <= now < end

    def _can_open_side(self, side: str) -> bool:
        state = self.broker.summary()
        if state["trade_count_today"] >= MAX_TOTAL_TRADES:
            return False
        if side == "BUY":
            return state["buy_count_today"] < MAX_PER_SIDE or state["sell_count_today"] == MAX_PER_SIDE
        return state["sell_count_today"] < MAX_PER_SIDE or state["buy_count_today"] == MAX_PER_SIDE

    def _enter(self, symbol: str, side: str, entry_price: float):
        if not entry_price or self.broker.already_traded_today(symbol) or not self._can_open_side(side):
            return

        qty = int(CAPITAL_PER_TRADE // entry_price)
        if qty < 1:
            return

        if side == "BUY":
            sl_price = entry_price * (1 - SL_PCT / 100)
            target_price = entry_price * (1 + TARGET_PCT / 100)
        else:
            sl_price = entry_price * (1 + SL_PCT / 100)
            target_price = entry_price * (1 - TARGET_PCT / 100)
        self.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price)

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")
            if not ltp:
                continue
            side, sl, target = position["side"], position["sl_price"], position["target_price"]
            if side == "BUY":
                if ltp <= sl:
                    self.broker.close_trade(position, ltp, "SL")
                elif ltp >= target:
                    self.broker.close_trade(position, ltp, "TARGET")
            else:
                if ltp >= sl:
                    self.broker.close_trade(position, ltp, "SL")
                elif ltp <= target:
                    self.broker.close_trade(position, ltp, "TARGET")

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")
