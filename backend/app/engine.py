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
from zoneinfo import ZoneInfo

from app.broadcaster import broadcast_sync
from .symbols import get_nse500_watchlist
from .candle_aggregator import CandleAggregator
from .fyers_client import connect_live_feed
from .fyers_auth import get_stored_access_token, refresh_access_token_from_refresh_token
from .strategies.algo1_opening_range import Algo1OpeningRange
from .strategies.algo2_momentum import Algo2Momentum
from .config import ENTRY_CHECK_TIME, SQUARE_OFF_TIME

aggregator = CandleAggregator()
last_ltp: dict[str, float] = {}
last_price_broadcast: dict[str, float] = {}
SCAN_RESULTS: dict[str, dict] = {}

STRATEGIES = {}   # populated in start_engine() once the watchlist is known
WATCHLIST: list[str] = []
_scheduler_started = False
_live_feed_started = False
_live_feed_socket = None
_live_feed_lock = threading.Lock()
_engine_lock = threading.Lock()
_engine_status = {
    "state": "new",
    "error": None,
    "last_token_refresh": None,
    "last_token_refresh_error": None,
    "live_feed_started": False,
    "fyers_ws_connected": False,
    "fyers_ws_error": None,
    "fyers_ws_last_event_at": None,
    "last_tick_at": None,
    "last_tick_symbol": None,
    "last_tick_ltp": None,
    "tick_count": 0,
    "last_candle_close_at": None,
    "closed_candle_count": 0,
}


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _on_live_feed_status(status: dict):
    global _live_feed_started

    connected = bool(status.get("connected"))
    error = status.get("error")
    with _engine_lock:
        _engine_status.update({
            "fyers_ws_connected": connected,
            "fyers_ws_error": None if connected else error,
            "fyers_ws_last_event_at": _utc_now(),
            "live_feed_started": connected or _engine_status.get("live_feed_started"),
        })

    if not connected:
        with _live_feed_lock:
            _live_feed_started = False


def _on_tick(message: dict):
    symbol = message.get("symbol")
    ltp = message.get("ltp")
    day_volume = message.get("vol_traded_today", 0)
    if not symbol or not ltp:
        return

    last_ltp[symbol] = ltp
    now = datetime.datetime.now()
    now_ts = time.time()
    with _engine_lock:
        _engine_status.update({
            "last_tick_at": _utc_now(),
            "last_tick_symbol": symbol,
            "last_tick_ltp": ltp,
            "tick_count": int(_engine_status.get("tick_count") or 0) + 1,
        })
    if now_ts - last_price_broadcast.get(symbol, 0) >= 1:
        broadcast_sync({"event": "price_update", "symbol": symbol, "ltp": ltp})
        last_price_broadcast[symbol] = now_ts

    def on_candle_close(sym, candle, indicators):
        with _engine_lock:
            _engine_status.update({
                "last_candle_close_at": _utc_now(),
                "closed_candle_count": int(_engine_status.get("closed_candle_count") or 0) + 1,
            })
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
    token_refresh_fired_date = None

    while True:
        now = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.date()
        current_time = now.strftime("%H:%M")

        if current_time >= "08:30" and token_refresh_fired_date != today:
            try_refresh_access_token(reason="scheduled_08_30")
            token_refresh_fired_date = today

        if current_time >= ENTRY_CHECK_TIME and entries_fired_date != today:
            for strategy in STRATEGIES.values():
                if getattr(strategy, "algo_id", None) == "algo5":
                    continue
                if hasattr(strategy, "evaluate_entries"):
                    strategy.evaluate_entries(get_ltp_fn=lambda s: last_ltp.get(s))
            entries_fired_date = today

        if current_time >= SQUARE_OFF_TIME and squareoff_fired_date != today:
            for strategy in STRATEGIES.values():
                strategy.square_off_all()
            squareoff_fired_date = today

        time.sleep(15)


def start_live_feed_if_ready(force: bool = False) -> bool:
    global _live_feed_started, _live_feed_socket

    if not WATCHLIST:
        print("[engine] watchlist not initialized yet, cannot start live feed")
        return False

    if get_stored_access_token() is None:
        print("[engine] no Fyers access token in Supabase yet, waiting for manual login")
        return False

    with _live_feed_lock:
        if _live_feed_started and not force:
            return True
        if force:
            # Fyers' SDK may call close callbacks synchronously; do not close it
            # while holding our lock. The old token socket is expected to die.
            _live_feed_socket = None

        def run_live_feed():
            global _live_feed_socket, _live_feed_started
            try:
                socket = connect_live_feed(WATCHLIST, _on_tick, _on_live_feed_status)
                with _live_feed_lock:
                    _live_feed_socket = socket
            except Exception as exc:
                with _engine_lock:
                    _engine_status.update({
                        "fyers_ws_connected": False,
                        "fyers_ws_error": str(exc),
                        "fyers_ws_last_event_at": _utc_now(),
                        "live_feed_started": False,
                    })
                with _live_feed_lock:
                    _live_feed_started = False
                print(f"[engine] live feed failed: {exc}")

        threading.Thread(target=run_live_feed, daemon=True).start()
        _live_feed_started = True
        with _engine_lock:
            _engine_status.update({
                "live_feed_started": True,
                "fyers_ws_error": None,
                "fyers_ws_last_event_at": _utc_now(),
            })
        print(f"[engine] live feed start requested for {len(WATCHLIST)} symbols")
        return True


def restart_live_feed(reason: str = "manual") -> bool:
    print(f"[engine] restarting Fyers live feed ({reason})")
    return start_live_feed_if_ready(force=True)


def get_engine_status() -> dict:
    return {
        "state": _engine_status["state"],
        "error": _engine_status["error"],
        "last_token_refresh": _engine_status.get("last_token_refresh"),
        "last_token_refresh_error": _engine_status.get("last_token_refresh_error"),
        "live_feed_started": _engine_status.get("live_feed_started"),
        "fyers_ws_connected": _engine_status.get("fyers_ws_connected"),
        "fyers_ws_error": _engine_status.get("fyers_ws_error"),
        "fyers_ws_last_event_at": _engine_status.get("fyers_ws_last_event_at"),
        "last_tick_at": _engine_status.get("last_tick_at"),
        "last_tick_symbol": _engine_status.get("last_tick_symbol"),
        "last_tick_ltp": _engine_status.get("last_tick_ltp"),
        "tick_count": _engine_status.get("tick_count"),
        "symbols_with_ticks": len(last_ltp),
        "last_candle_close_at": _engine_status.get("last_candle_close_at"),
        "closed_candle_count": _engine_status.get("closed_candle_count"),
        "watchlist_count": len(WATCHLIST),
        "strategies_running": list(STRATEGIES.keys()),
    }


def try_refresh_access_token(reason: str = "manual_or_startup") -> bool:
    try:
        refresh_access_token_from_refresh_token()
        with _engine_lock:
            _engine_status.update({
                "last_token_refresh": _utc_now(),
                "last_token_refresh_error": None,
            })
        print(f"[engine] Fyers access token refreshed via refresh token ({reason})")
        restart_live_feed(reason=f"token_refresh_{reason}")
        return True
    except Exception as exc:
        with _engine_lock:
            _engine_status["last_token_refresh_error"] = str(exc)
        print(f"[engine] Fyers refresh-token refresh skipped/failed ({reason}): {exc}")
        return False


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
        from app.strategies.algo5_live_1150_test import Algo5Live1150Test
        strategies = {
            "algo1": Algo1OpeningRange(watchlist),
            "algo2": Algo2Momentum(watchlist),
            "algo3": Algo3OpeningRangeBasic(watchlist),
            "algo4": Algo4OpeningRangeIndicators(watchlist),
            "algo5": Algo5Live1150Test(watchlist),
        }

        with _engine_lock:
            WATCHLIST = watchlist
            STRATEGIES.clear()
            STRATEGIES.update(strategies)
            _engine_status.update({"state": "running", "error": None})

        if not _scheduler_started:
            threading.Thread(target=_scheduler_loop, daemon=True).start()
            _scheduler_started = True

        try_refresh_access_token(reason="startup")
        if not start_live_feed_if_ready():
            print("[engine] started without live feed; complete manual Fyers login to enable it")
        print(f"[engine] started with {len(WATCHLIST)} symbols, {len(STRATEGIES)} strategies")
    except Exception as exc:
        with _engine_lock:
            _engine_status.update({"state": "failed", "error": str(exc)})
        print(f"[engine] startup failed: {exc}")
