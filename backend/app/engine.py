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

from .symbols import get_nse500_watchlist
from .candle_aggregator import CandleAggregator
from .fyers_client import connect_live_feed
from .fyers_auth import get_stored_access_token
from .strategies.algo1_opening_range import Algo1OpeningRange
from .strategies.algo2_momentum import Algo2Momentum
from .config import ENTRY_CHECK_TIME, SQUARE_OFF_TIME

aggregator = CandleAggregator()
last_ltp: dict[str, float] = {}

STRATEGIES = {}   # populated in start_engine() once the watchlist is known
WATCHLIST: list[str] = []
_scheduler_started = False
_live_feed_started = False
_live_feed_lock = threading.Lock()
_engine_lock = threading.Lock()
_engine_status = {
    "state": "new",
    "error": None,
}


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

        time.sleep(15)


def start_live_feed_if_ready() -> bool:
    global _live_feed_started

    if not WATCHLIST:
        print("[engine] watchlist not initialized yet, cannot start live feed")
        return False

    if get_stored_access_token() is None:
        print("[engine] no Fyers access token in Supabase yet, waiting for manual login")
        return False

    with _live_feed_lock:
        if _live_feed_started:
            return True
        threading.Thread(target=lambda: connect_live_feed(WATCHLIST, _on_tick), daemon=True).start()
        _live_feed_started = True
        print(f"[engine] live feed start requested for {len(WATCHLIST)} symbols")
        return True


def get_engine_status() -> dict:
    return {
        "state": _engine_status["state"],
        "error": _engine_status["error"],
        "watchlist_count": len(WATCHLIST),
        "strategies_running": list(STRATEGIES.keys()),
    }


def start_engine():
    """Called once from main.py's FastAPI startup event."""
    global WATCHLIST, _scheduler_started

    with _engine_lock:
        if _engine_status["state"] in {"starting", "running"}:
            return
        _engine_status.update({"state": "starting", "error": None})

    try:
        watchlist = get_nse500_watchlist()
        from app.strategies.algo3_opening_range_basic import Algo3OpeningRangeBasic
        from app.strategies.algo4_opening_range_indicators import Algo4OpeningRangeIndicators
        strategies = {
            "algo1": Algo1OpeningRange(watchlist),
            "algo2": Algo2Momentum(watchlist),
            "algo3": Algo3OpeningRangeBasic(watchlist),
            "algo4": Algo4OpeningRangeIndicators(watchlist),
        }

        with _engine_lock:
            WATCHLIST = watchlist
            STRATEGIES.clear()
            STRATEGIES.update(strategies)
            _engine_status.update({"state": "running", "error": None})

        if not _scheduler_started:
            threading.Thread(target=_scheduler_loop, daemon=True).start()
            _scheduler_started = True

        if not start_live_feed_if_ready():
            print("[engine] started without live feed; complete manual Fyers login to enable it")
        print(f"[engine] started with {len(WATCHLIST)} symbols, {len(STRATEGIES)} strategies")
    except Exception as exc:
        with _engine_lock:
            _engine_status.update({"state": "failed", "error": str(exc)})
        print(f"[engine] startup failed: {exc}")
