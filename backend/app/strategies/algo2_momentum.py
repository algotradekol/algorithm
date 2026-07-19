"""
algo2_momentum.py

Implements the spec as given:
Every 1-min candle close, for each watchlist symbol not already
traded today:
  - price > VWAP
  - price > EMA20
  - volume > 1.5x average volume
  -> Paper BUY

Risk: configurable capital per trade, SL 1%, target 2%, optional
trailing SL. Exit on target/SL or 3:15pm square-off.

ASSUMPTION: "average volume" is taken as the rolling average of the
last 20 closed 1-min candles for that symbol (candle_aggregator.py's
avg_volume_20) -- the spec doesn't say over what window, so this is a
reasonable default. Change VOLUME_LOOKBACK logic in
candle_aggregator.py if you want a different definition (e.g. a
20-day historical average at the same time-of-day).
"""
import datetime
from .base import Strategy
from ..paper_broker import PaperBroker

VOLUME_MULTIPLIER = 1.5


class Algo2Momentum(Strategy):
    algo_id = "algo2"
    display_name = "Algo 2 — VWAP/EMA/Volume Momentum"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def evaluate_entries(self, get_ltp_fn):
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now().isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": [],
            "buy_candidates": 0,
            "sell_candidates": 0,
            "buy_selected": 0,
            "sell_selected": 0,
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": len(self.watchlist),
            "message": "Algo 2 is momentum-based and does not use the 9:16 opening-range scan.",
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass  # acts on candle close, not raw ticks

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        now = datetime.datetime.now()
        if now.strftime("%H:%M") >= "15:15":
            return  # no new entries after square-off time

        if self.broker.already_traded_today(symbol):
            return

        vwap, ema20, avg_volume = indicators["vwap"], indicators["ema20"], indicators["avg_volume_20"]
        if vwap is None or ema20 is None or avg_volume is None:
            return

        price = candle["close"]
        conditions_met = (
            price > vwap and
            price > ema20 and
            candle["volume"] > VOLUME_MULTIPLIER * avg_volume
        )
        if conditions_met:
            self._enter(symbol, price)

    def _enter(self, symbol: str, entry_price: float):
        qty = int(self.settings["capital_per_trade"] // entry_price)
        if qty < 1:
            return
        sl_price = entry_price * (1 - self.settings["sl_pct"] / 100)
        target_price = entry_price * (1 + self.settings["target_pct"] / 100)
        self.broker.open_trade(symbol, "BUY", qty, entry_price, sl_price, target_price)

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")
            if not ltp:
                continue
            position = self.broker.apply_trailing_stop(position, ltp, self.settings)
            sl, target = position["sl_price"], position["target_price"]
            use_target = self.broker.should_exit_at_target(self.settings)

            if ltp <= sl:
                self.broker.close_trade(position, ltp, "SL")
            elif use_target and ltp >= target:
                self.broker.close_trade(position, ltp, "TARGET")

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")
