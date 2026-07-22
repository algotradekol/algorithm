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
from .strategies.test_algo import TestAlgo
from .strategies.un1_915_filtered import UN1915Filtered
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
_feed_retry_schedules: set[tuple[datetime.date, str]] = set()
_feed_watchdog_started = False
_feed_watchdog_last_restart_at = 0.0
_engine_status = {
    "state": "new",
    "error": None,
    "last_token_refresh": None,
    "last_token_refresh_error": None,
    "live_feed_started": False,
    "fyers_ws_connected": False,
    "fyers_ws_error": None,
    "fyers_ws_last_event_at": None,
    "fyers_ws_subscribed_symbols": 0,
    "fyers_ws_first_tick_at": None,
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
        update = {
            "fyers_ws_connected": connected,
            "fyers_ws_error": None if connected else error,
            "fyers_ws_last_event_at": _utc_now(),
            "live_feed_started": connected or _engine_status.get("live_feed_started"),
        }
        if status.get("subscribed_symbols") is not None:
            update["fyers_ws_subscribed_symbols"] = int(status["subscribed_symbols"])
        if status.get("first_tick_received") and not _engine_status.get("fyers_ws_first_tick_at"):
            update["fyers_ws_first_tick_at"] = _utc_now()
        _engine_status.update(update)

    if not connected:
        with _live_feed_lock:
            _live_feed_started = False


def _on_candle_close(symbol: str, candle: dict, indicators: dict):
    with _engine_lock:
        _engine_status.update({
            "last_candle_close_at": _utc_now(),
            "closed_candle_count": int(_engine_status.get("closed_candle_count") or 0) + 1,
        })
    for strategy in STRATEGIES.values():
        strategy.on_candle_close(symbol, candle, indicators)


def _on_tick(message: dict):
    symbol = message.get("symbol")
    ltp = message.get("ltp")
    day_volume = message.get("vol_traded_today", 0)
    if not symbol or not ltp:
        return

    last_ltp[symbol] = ltp
    prev_close = (
        message.get("prev_close_price")
        or message.get("prev_close")
        or message.get("previous_close")
    )
    if prev_close:
        try:
            previous_close_value = float(prev_close)
        except (TypeError, ValueError):
            previous_close_value = None
        if previous_close_value:
            for strategy in STRATEGIES.values():
                set_previous_close = getattr(strategy, "set_previous_close", None)
                if set_previous_close:
                    set_previous_close(symbol, previous_close_value)
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

    aggregator.on_tick(symbol, ltp, day_volume, on_candle_close=_on_candle_close)

    for strategy in STRATEGIES.values():
        strategy.on_tick(symbol, ltp, now)
        for position in strategy.broker.open_positions():
            if position["symbol"] == symbol:
                position["_last_ltp"] = ltp
                strategy.broker.update_position_range(position, ltp)
        strategy.check_exits()


def _scheduler_loop():
    """Runs alongside the tick handler -- checks the clock for the
    9:16 entry trigger (algo1) and 3:15 square-off (both algos)."""
    entries_fired_date: dict[str, datetime.date] = {}
    entries_fired_schedule: dict[str, tuple[bool, str]] = {}
    squareoff_fired_date = None
    token_refresh_fired_date = None
    global _feed_retry_schedules

    while True:
        now = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
        today = now.date()
        current_time = now.strftime("%H:%M")

        if current_time >= "08:30" and token_refresh_fired_date != today:
            try_refresh_access_token(reason="scheduled_08_30")
            token_refresh_fired_date = today

        # A socket handshake is not market data. Retry once during whichever
        # candle minute a production or UI test schedule is using if no tick
        # has arrived today, leaving enough time to build that candle.
        for strategy in STRATEGIES.values():
            scan_time = getattr(strategy, "scan_candle_time", lambda: None)()
            retry_key = (today, scan_time) if scan_time else None
            if retry_key and current_time == scan_time and retry_key not in _feed_retry_schedules:
                last_tick_at = _engine_status.get("last_tick_at") or ""
                if not last_tick_at.startswith(today.isoformat()):
                    _feed_retry_schedules.add(retry_key)
                    print(f"[engine] no market tick at {scan_time}; restarting Fyers live feed once before scheduled scan")
                    restart_live_feed(reason=f"scheduled_{scan_time}_no_first_tick")

        # Each opening strategy can opt into a one-off test schedule without
        # changing the production 09:15/09:16 defaults for the other strategy.
        # Close the just-finished minute even for symbols that have not sent a
        # follow-up tick yet. This must happen before the 9:16 entry check.
        aggregator.flush_completed_candles(on_candle_close=_on_candle_close, now=now.replace(tzinfo=None))
        pending = []
        completed_any = False
        for strategy in STRATEGIES.values():
            if not hasattr(strategy, "entry_window"):
                continue
            schedule = (
                bool(strategy.settings.get("test_schedule_enabled")),
                strategy.scan_candle_time(),
            )
            # A later UI change from the production schedule to a test time is
            # a new run. Do not let the already-missed 09:16 window suppress it.
            if entries_fired_date.get(strategy.algo_id) == today and entries_fired_schedule.get(strategy.algo_id) == schedule:
                continue
            if strategy.entry_window(current_time):
                completed = strategy.evaluate_entries(get_ltp_fn=lambda s: last_ltp.get(s))
                if completed is False:
                    pending.append(strategy.algo_id)
                else:
                    entries_fired_date[strategy.algo_id] = today
                    entries_fired_schedule[strategy.algo_id] = schedule
                    completed_any = True
            elif strategy.entry_window_elapsed(current_time):
                strategy.mark_opening_scan_missed()
                entries_fired_date[strategy.algo_id] = today
                entries_fired_schedule[strategy.algo_id] = schedule
                print(f"[engine] entry window elapsed without complete data for {strategy.algo_id}; no late entries were placed")
        if pending:
            print(f"[engine] opening scan waiting for complete market data: {', '.join(pending)}")
        if completed_any:
            try:
                from app.calendar_store import save_dashboard_snapshot
                save_dashboard_snapshot(note="entry_scan")
            except Exception as exc:
                print(f"[engine] entry-scan calendar snapshot failed: {exc}")

        if current_time >= SQUARE_OFF_TIME and squareoff_fired_date != today:
            for strategy in STRATEGIES.values():
                strategy.square_off_all()
            try:
                from app.calendar_store import save_dashboard_snapshot
                save_dashboard_snapshot(note="eod_squareoff")
            except Exception as exc:
                print(f"[engine] EOD calendar snapshot failed: {exc}")
            squareoff_fired_date = today

        time.sleep(15)


def _live_feed_watchdog_loop():
    """Keep a real market-data stream alive before a scheduled scan.

    A websocket connection flag only means a socket was requested. The opening
    strategies need actual ticks to build their candle, so reconnect a stale
    stream during NSE hours with a cooldown to avoid duplicate SDK sessions.
    """
    global _feed_watchdog_last_restart_at

    while True:
        try:
            now = datetime.datetime.now(ZoneInfo("Asia/Kolkata"))
            market_open = "09:15" <= now.strftime("%H:%M") < "15:30"
            last_tick_at = _engine_status.get("last_tick_at")
            tick_is_fresh = False
            if last_tick_at:
                try:
                    tick_time = datetime.datetime.fromisoformat(last_tick_at.replace("Z", "+00:00"))
                    tick_is_fresh = (datetime.datetime.now(datetime.timezone.utc) - tick_time).total_seconds() < 45
                except (TypeError, ValueError):
                    tick_is_fresh = False
            stale_seconds = time.time() - _feed_watchdog_last_restart_at
            market_open_at = now.replace(hour=9, minute=15, second=0, microsecond=0)
            opening_grace_elapsed = (now - market_open_at).total_seconds() >= 60

            if market_open and opening_grace_elapsed and get_stored_access_token() and not tick_is_fresh and stale_seconds >= 60:
                _feed_watchdog_last_restart_at = time.time()
                print("[engine] Fyers market tick is missing or stale; restarting live feed watchdog")
                restart_live_feed(reason="watchdog_stale_or_missing_tick")
        except Exception as exc:
            print(f"[engine] live-feed watchdog error: {exc}")
        time.sleep(15)


def start_live_feed_if_ready(force: bool = False) -> bool:
    global _live_feed_started, _live_feed_socket, _feed_watchdog_last_restart_at

    if not WATCHLIST:
        print("[engine] watchlist not initialized yet, cannot start live feed")
        return False

    if get_stored_access_token() is None:
        print("[engine] no Fyers access token in Supabase yet, waiting for manual login")
        return False

    socket_to_close = None
    with _live_feed_lock:
        if _live_feed_started and not force:
            return True
        if force:
            # Close the old SDK connection outside the lock before starting the
            # replacement, otherwise a stale socket can keep the feed silent.
            socket_to_close = _live_feed_socket
            _live_feed_socket = None

    if socket_to_close is not None:
        close_connection = getattr(socket_to_close, "close_connection", None)
        if callable(close_connection):
            try:
                close_connection()
            except Exception as exc:
                print(f"[engine] old Fyers websocket close failed: {exc}")

    with _live_feed_lock:
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
        _feed_watchdog_last_restart_at = time.time()
        with _engine_lock:
            _engine_status.update({
                "live_feed_started": True,
                "fyers_ws_error": None,
                "fyers_ws_last_event_at": _utc_now(),
                "fyers_ws_subscribed_symbols": 0,
                "fyers_ws_first_tick_at": None,
            })
        print(f"[engine] live feed start requested for {len(WATCHLIST)} symbols")
        for strategy in STRATEGIES.values():
            refresh_market_data = getattr(strategy, "refresh_market_data", None)
            if refresh_market_data:
                refresh_market_data()
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
        "fyers_ws_subscribed_symbols": _engine_status.get("fyers_ws_subscribed_symbols"),
        "fyers_ws_first_tick_at": _engine_status.get("fyers_ws_first_tick_at"),
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


def enrich_positions_with_ltp(positions: list[dict]) -> list[dict]:
    enriched = []
    for position in positions:
        row = dict(position)
        ltp = last_ltp.get(row["symbol"])
        if ltp is not None:
            entry = float(row.get("entry_price") or 0)
            qty = int(row.get("qty") or 0)
            side = row.get("side")
            unrealized = (entry - ltp) * qty if side == "SELL" else (ltp - entry) * qty
            row["ltp"] = ltp
            row["unrealized_pnl"] = round(unrealized, 2)
            row["high_price"] = max(float(row.get("highest_price") or entry), float(ltp))
            row["low_price"] = min(float(row.get("lowest_price") or entry), float(ltp))
        return_row_ltp = row.get("ltp")
        if return_row_ltp is None:
            row["ltp"] = row.get("entry_price")
            row["unrealized_pnl"] = 0
            row["high_price"] = row.get("highest_price") or row.get("entry_price")
            row["low_price"] = row.get("lowest_price") or row.get("entry_price")
        enriched.append(row)
    return enriched


def attach_entry_triggers(algo_id: str, rows: list[dict]) -> list[dict]:
    enriched = []
    scan_rows = SCAN_RESULTS.get(algo_id, {}).get("passed_opening_range") or []
    scan_by_symbol = {row.get("symbol"): row for row in scan_rows}
    for row in rows:
        item = dict(row)
        if not item.get("entry_trigger"):
            item["entry_trigger"] = _infer_entry_trigger(algo_id, item, scan_by_symbol.get(item.get("symbol")))
        enriched.append(item)
    return enriched


def _infer_entry_trigger(algo_id: str, row: dict, scan_row: dict | None) -> str:
    side = row.get("side") or "--"
    if scan_row:
        if scan_row.get("entry_trigger"):
            return scan_row["entry_trigger"]
        if algo_id == "test_algo":
            move_pct = scan_row.get("gap_pct")
            candle_time = str(scan_row.get("candle_time") or "")[11:16] or "live"
            move_text = f"{float(move_pct):.3f}%" if move_pct is not None else "--"
            return f"{candle_time} closed 1-minute candle moved {move_text}; test algo threshold matched for {side}."
        gap_pct = scan_row.get("gap_pct")
        gap_text = f"{float(gap_pct):.2f}%" if gap_pct is not None else "--"
        open_price = scan_row.get("open")
        prev_close = scan_row.get("prev_close")
        if algo_id == "algo2":
            passed_filters = [
                name for name, result in (scan_row.get("indicator_results") or {}).items()
                if result.get("enabled") and result.get("passed")
            ]
            filters = ", ".join(passed_filters) if passed_filters else "base filters only"
            return (
                f"9:15 filtered opening-range trigger for {side}; gap {gap_text}; "
                f"passed filters: {filters}. Open {open_price}, prev close {prev_close}."
            )
        return f"9:15 simple opening-range trigger for {side}; gap {gap_text}. Open {open_price}, prev close {prev_close}."

    labels = {
        "algo1": "Legacy trade before trigger storage: likely 9:15 simple opening-range condition matched.",
        "algo2": "Legacy trade before trigger storage: likely 9:15 filtered opening-range conditions matched.",
        "test_algo": "Legacy trade before trigger storage: likely live 1-minute test candle move matched.",
    }
    return labels.get(algo_id, "Legacy trade before trigger storage; exact trigger was not saved.")


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
    global WATCHLIST, _scheduler_started, _feed_watchdog_started

    with _engine_lock:
        if _engine_status["state"] in {"starting", "running"}:
            return
        _engine_status.update({"state": "starting", "error": None})

    try:
        watchlist = get_nse500_watchlist()
        strategies = {
            "algo1": Algo1OpeningRange(watchlist),
            "algo2": UN1915Filtered(watchlist),
            "test_algo": TestAlgo(watchlist),
        }
        for algo_id, strategy in strategies.items():
            stale_count = strategy.broker.close_stale_open_positions()
            if stale_count:
                print(f"[engine] closed {stale_count} stale open positions for {algo_id}")

        with _engine_lock:
            WATCHLIST = watchlist
            STRATEGIES.clear()
            STRATEGIES.update(strategies)
            _engine_status.update({"state": "running", "error": None})

        if not _scheduler_started:
            threading.Thread(target=_scheduler_loop, daemon=True).start()
            _scheduler_started = True
        if not _feed_watchdog_started:
            threading.Thread(target=_live_feed_watchdog_loop, daemon=True).start()
            _feed_watchdog_started = True

        try_refresh_access_token(reason="startup")
        if not start_live_feed_if_ready():
            print("[engine] started without live feed; complete manual Fyers login to enable it")
        print(f"[engine] started with {len(WATCHLIST)} symbols, {len(STRATEGIES)} strategies")
    except Exception as exc:
        with _engine_lock:
            _engine_status.update({"state": "failed", "error": str(exc)})
        print(f"[engine] startup failed: {exc}")
