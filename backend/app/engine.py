"""
engine.py — runs continuously in the background (started from main.py
at FastAPI startup). Connects one Fyers WebSocket, feeds every tick to
the shared candle_aggregator, and dispatches to both strategies. This
is what guarantees Algo 1 and Algo 2 (and any future algo you add to
STRATEGIES) see identical data for a fair side-by-side comparison.

To add a future Algo 3: write app/strategies/algo3_whatever.py
implementing the Strategy interface, then add one line to STRATEGIES
below. Nothing else in this file changes.
"""
import datetime
import threading
import time

from app.symbols import get_nse500_watchlist
from app.candle_aggregator import CandleAggregator
from app.fyers_client import connect_live_feed
from app.fyers_auth import refresh_access_token, get_stored_access_token
from app.strategies.algo1_opening_range import Algo1OpeningRange
from app.strategies.algo2_momentum import Algo2Momentum
from app.config import ENTRY_CHECK_TIME, SQUARE_OFF_TIME

aggregator = CandleAggregator()
last_ltp: dict[str, float] = {}

STRATEGIES = {}   # populated in start_engine() once the watchlist is known


def _on_tick(message: dict):
    symbol = message.get("symbol")
    ltp = message.get("ltp")
    day_volume = message.get("vol_traded_today", 0)
    if not symbol or not ltp:
        return

    last_ltp[symbol] = ltp
    now = datetime.datetime.now()

    def on_candle_close(sym, candle, indicators):
        for strategy in STRATEGIES.values():
            strategy.on_candle_close(sym, candle, indicators)

    aggregator.on_tick(symbol, ltp, day_volume, on_candle_close=on_candle_close)

    for strategy in STRATEGIES.values():
        strategy.on_tick(symbol, ltp, now)
        for position in strategy.broker.open_positions():
            if position["symbol"] == symbol:
                position["_last_ltp"] = ltp
        strategy.check_exits()


def _scheduler_loop():
    """Runs alongside the tick handler -- checks the clock for the
    9:16 entry trigger (algo1) and 3:15 square-off (both algos)."""
    entries_fired_date = None
    squareoff_fired_date = None

    while True:
        now = datetime.datetime.now()
        today = now.date()
        current_time = now.strftime("%H:%M")

        if current_time >= ENTRY_CHECK_TIME and entries_fired_date != today:
            algo1 = STRATEGIES.get("algo1")
            if algo1:
                algo1.evaluate_entries(get_ltp_fn=lambda s: last_ltp.get(s))
            entries_fired_date = today

        if current_time >= SQUARE_OFF_TIME and squareoff_fired_date != today:
            for strategy in STRATEGIES.values():
                strategy.square_off_all()
            squareoff_fired_date = today

        if current_time == "08:45":
            try:
                refresh_access_token()
            except Exception as e:
                print(f"[scheduler] token refresh failed: {e}")

        time.sleep(15)


def start_engine():
    """Called once from main.py's FastAPI startup event."""
    if not get_stored_access_token():
        try:
            refresh_access_token()
        except Exception as e:
            print(f"[engine] initial token refresh failed, engine will retry at 08:45 tomorrow: {e}")
            return

    watchlist = get_nse500_watchlist()
    STRATEGIES["algo1"] = Algo1OpeningRange(watchlist)
    STRATEGIES["algo2"] = Algo2Momentum(watchlist)

    threading.Thread(target=_scheduler_loop, daemon=True).start()
    threading.Thread(target=lambda: connect_live_feed(watchlist, _on_tick), daemon=True).start()
    print(f"[engine] started with {len(watchlist)} symbols, {len(STRATEGIES)} strategies")
