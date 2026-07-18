"""
algo1_opening_range.py

Implements the spec exactly as given:
- 9:15 hrs 1-min candle: open == low -> BUY candidate (if gap from
  prev close <= 2%); open == high -> SELL candidate (same gap check)
- Entry at 9:16 hrs, market price
- Target 2%, SL 1%, capital ₹50,000 per trade
- Max 10 trades total: 5 buy + 5 sell; if one side is short on
  qualifying candidates, the other side's overflow candidates fill
  the remaining slots up to 10 total (ASSUMPTION: candidates are
  processed in watchlist order -- change SORT_KEY below if you want
  them prioritized by gap size instead)

Mode is MIS/intraday funded via margin -- this paper version doesn't
model margin/leverage explicitly (see paper_broker.py notes), it just
tracks the ₹50,000-per-trade capital allocation and P&L against it.
"""
import datetime
import threading
from .base import Strategy
from ..paper_broker import PaperBroker
from ..fyers_client import get_previous_close
from ..fyers_auth import get_stored_access_token
GAP_LIMIT_PCT = 2.0


class Algo1OpeningRange(Strategy):
    algo_id = "algo1"
    display_name = "Algo 1 — Opening Range Gap"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.prev_close: dict[str, float] = {}
        self.buy_candidates: list[str] = []
        self.sell_candidates: list[str] = []
        self.entries_evaluated_today = None
        # Load previous closes in background to avoid blocking startup
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def _load_previous_closes_background(self):
        """Load previous closes in a background thread to avoid blocking initialization."""
        try:
            if not get_stored_access_token():
                print("[algo1] no Fyers access token yet, skipping previous-close preload")
                return
            for symbol in self.watchlist:
                try:
                    self.prev_close[symbol] = get_previous_close(symbol)
                except Exception as e:
                    print(f"[algo1] couldn't get prev close for {symbol}: {e}")
        except Exception as e:
            print(f"[algo1] error in background preload: {e}")

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass  # algo1 only acts on the 9:15 candle close and the 9:16 entry check

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        # Only care about the very first candle of the day (9:15-9:16 bar)
        if candle["time"].strftime("%H:%M") != "09:15":
            return

        prev_close = self.prev_close.get(symbol)
        if not prev_close:
            return

        open_price, high, low = candle["open"], candle["high"], candle["low"]

        if open_price == low:
            gap_pct = abs(open_price - prev_close) / prev_close * 100
            if gap_pct <= GAP_LIMIT_PCT:
                self.buy_candidates.append(symbol)

        if open_price == high:
            gap_pct = abs(prev_close - open_price) / prev_close * 100
            if gap_pct <= GAP_LIMIT_PCT:
                self.sell_candidates.append(symbol)

    def evaluate_entries(self, get_ltp_fn):
        """Called by the engine at 9:16. get_ltp_fn(symbol) -> current price."""
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return
        self.entries_evaluated_today = today

        max_total = self.settings["max_trades_per_day"]
        max_per_side = self.settings["max_buy_trades"]
        buys = self.buy_candidates[:max_per_side]
        sells = self.sell_candidates[:self.settings["max_sell_trades"]]

        # Fill remaining slots up to the configured daily max from whichever side has leftover candidates.
        remaining_slots = max_total - (len(buys) + len(sells))
        overflow_pool = self.buy_candidates[max_per_side:] + self.sell_candidates[self.settings["max_sell_trades"]:]
        for symbol in overflow_pool[:remaining_slots]:
            if symbol in self.buy_candidates and symbol not in buys:
                buys.append(symbol)
            elif symbol in self.sell_candidates and symbol not in sells:
                sells.append(symbol)

        for symbol in buys:
            self._enter(symbol, "BUY", get_ltp_fn(symbol))
        for symbol in sells:
            self._enter(symbol, "SELL", get_ltp_fn(symbol))

    def _enter(self, symbol: str, side: str, entry_price: float):
        if not entry_price or self.broker.already_traded_today(symbol):
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

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")  # engine sets this before calling check_exits
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

