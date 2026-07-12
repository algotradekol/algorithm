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

CAPITAL_PER_TRADE = 50_000       # configurable -- change here or wire to a frontend setting later
TARGET_PCT = 2.0
INITIAL_SL_PCT = 1.0
VOLUME_MULTIPLIER = 1.5
TRAIL_SL_ENABLED = True
TRAIL_TRIGGER_PCT = 1.0   # once price moves 1% in favor, start trailing
TRAIL_STEP_PCT = 0.5      # trail SL to lock in 0.5% once triggered


class Algo2Momentum(Strategy):
    algo_id = "algo2"
    display_name = "Algo 2 — VWAP/EMA/Volume Momentum"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=CAPITAL_PER_TRADE * 20)

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
        qty = int(CAPITAL_PER_TRADE // entry_price)
        if qty < 1:
            return
        sl_price = entry_price * (1 - INITIAL_SL_PCT / 100)
        target_price = entry_price * (1 + TARGET_PCT / 100)
        self.broker.open_trade(symbol, "BUY", qty, entry_price, sl_price, target_price)

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")
            if not ltp:
                continue
            entry, sl, target = position["entry_price"], position["sl_price"], position["target_price"]

            if TRAIL_SL_ENABLED:
                move_pct = (ltp - entry) / entry * 100
                if move_pct >= TRAIL_TRIGGER_PCT:
                    new_sl = entry * (1 + TRAIL_STEP_PCT / 100)
                    if new_sl > sl:
                        from ..supabase_client import run_with_supabase
                        run_with_supabase(
                            lambda supabase: supabase.table("positions").update({"sl_price": new_sl}).eq("id", position["id"]).execute()
                        )
                        sl = new_sl

            if ltp <= sl:
                self.broker.close_trade(position, ltp, "SL")
            elif ltp >= target:
                self.broker.close_trade(position, ltp, "TARGET")

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")
