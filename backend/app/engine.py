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
import uuid

from .broadcaster import broadcast_sync
from .timezone import IST
from .symbols import get_nse500_watchlist
from .candle_aggregator import CandleAggregator
from .broker_factory import create_broker
from .fyers_client import connect_live_feed
from .fyers_auth import get_stored_access_token, refresh_access_token_from_refresh_token
from .strategies.algo1_opening_range import Algo1OpeningRange
from .strategies.algo3_silver_micro import Algo3SilverMicro
from .strategies.un1_915_filtered import UN1915Filtered
from .config import ENTRY_CHECK_TIME, HIDDEN_TABS, SQUARE_OFF_TIME

# Tab-key → algo_id map for the HIDDEN_TABS backend gate. Only strategy
# tabs are here; UI-only tabs (backtest, compare, history, calendar,
# charges) are not part of this map because they never load a strategy.
_STRATEGY_TAB_MAP = {
    "simple": "algo1",
    "filter": "algo2",
    "silvermicro": "algo3",
    "silver": "algo3",
}
_HIDDEN_ALGO_IDS = {
    _STRATEGY_TAB_MAP[key]
    for key in HIDDEN_TABS
    if key in _STRATEGY_TAB_MAP
}
from .runtime_mode import get_runtime_trading_mode, normalize_trading_mode, set_runtime_trading_mode

aggregator = CandleAggregator()
last_ltp: dict[str, float] = {}
_symbol_last_tick_at: dict[str, str] = {}
SCAN_RESULTS: dict[str, dict] = {}

STRATEGIES = {}   # populated in start_engine() once the watchlist is known
WATCHLIST: list[str] = []
LIVE_FEED_SYMBOLS: list[str] = []
_scheduler_started = False
_live_feed_started = False
_live_feed_socket = None
_live_feed_lock = threading.Lock()
_engine_lock = threading.Lock()
_feed_retry_schedules: set[tuple[datetime.date, str]] = set()
_feed_watchdog_started = False
_feed_watchdog_last_restart_at = 0.0
_position_ltp_poll_started = False
_critical_rest_fallback_started = False
_live_broker_reconcile_started = False
# Reconnect strategy for the Fyers WebSocket. Two mechanisms stacked:
#
# 1) Exponential backoff — 5s, 10s, 20s, 40s, 60s between successive retries.
#    Each retry waits `_FEED_BACKOFF_SEQUENCE[failure_count]` seconds since the
#    last restart before trying again. Reset to 0 when a real tick arrives.
#
# 2) Circuit breaker — after _FEED_MAX_CONSECUTIVE_FAILURES retries all fail
#    to produce a first tick (or produce a 429), stop retrying entirely for
#    _FEED_CIRCUIT_OPEN_SECONDS (15 min). Prevents an all-day silent hammer
#    on the Fyers endpoint when the token / IP / account is fundamentally
#    unable to connect. Circuit is closed (reset to 0 failures) either by
#    a successful first tick or by a human action (mode switch, fresh OAuth).
_FEED_BACKOFF_SEQUENCE = (5, 10, 20, 40, 60)
_FEED_MAX_CONSECUTIVE_FAILURES = 5
_FEED_CIRCUIT_OPEN_SECONDS = 180.0  # 3 minutes — Fyers server-closes WS
# every ~30s regardless of quota state, so a 15-min silence just costs us
# signal candles for no upside. 3 min gives Fyers time to release the old
# session on their side, then we resume via the normal backoff schedule.

# F6 (2026-08-17): minimum delay between failure-triggered restart attempts.
# Even when the backoff ladder says "5s", we won't restart faster than this
# during a run of failures. Prevents the restart-storm behavior where
# subscribe→close→subscribe cycles hit Fyers a dozen times per minute.
_FEED_MIN_RESTART_INTERVAL_SECONDS = 30.0
# F6: after the circuit closes (via first tick or human action), we require
# this many seconds of stability before another failure is allowed to
# re-open it. Stops the "recover → immediately re-fail → circuit opens
# again" loop that stretched 2026-08-17 morning's outage.
_FEED_POST_RECOVERY_GRACE_SECONDS = 60.0
_feed_last_recovery_at = 0.0
_feed_reconnect_failure_count = 0
_feed_circuit_open_until = 0.0
# When the WS most recently transitioned from connected → disconnected.
# The frontend uses this to debounce the "Disconnected" banner so a normal
# 30s reconnect doesn't flash red at the user (F14, 2026-08-17).
_feed_disconnected_since: float = 0.0

# On every Railway deploy the old container is killed abruptly and the new
# container immediately tries a Fyers WS handshake — but Fyers has not yet
# released the old connection for the same client_id, so it 429s the
# handshake. A single 429 was tripping the 15-minute circuit breaker on
# every push during market hours. Waiting ~45s after process start before
# the first handshake gives Fyers time to release the old session and
# avoids the deploy-race.
_BOOT_WS_DELAY_SECONDS = 45.0
_process_started_at = time.time()
_RECOVERY_SETTLING_SECONDS = 45.0


def _boot_grace_remaining() -> float:
    return max(0.0, _BOOT_WS_DELAY_SECONDS - (time.time() - _process_started_at))


# Token-expired guard. Fyers returns {'type': 'cn', 'code': -99, 'message':
# 'Token is expired'} on WS handshakes when the stored access token has
# lapsed. Continuing to handshake with a known-expired token just burns
# Fyers's per-account WS quota (2026-08-13 09:05-09:10 IST: 10+ wasted
# attempts before the user's fresh login arrived). Whenever we see that
# error, freeze all watchdog handshake attempts for this many seconds
# or until a fresh OAuth callback resets the flag.
_TOKEN_EXPIRED_HOLD_SECONDS = 600.0  # 10 min; long enough that user has to log in
_token_known_expired_at = 0.0


def _token_expired_hold_remaining() -> float:
    if _token_known_expired_at <= 0:
        return 0.0
    return max(0.0, _TOKEN_EXPIRED_HOLD_SECONDS - (time.time() - _token_known_expired_at))


def _mark_token_expired(reason: str = "") -> None:
    """Called when Fyers replies 'Token is expired'. Blocks further WS
    handshakes until either the hold expires or a fresh OAuth clears it."""
    global _token_known_expired_at
    if _token_known_expired_at == 0:
        print(f"[engine] Fyers token flagged as EXPIRED ({reason}); pausing WS handshakes for {int(_TOKEN_EXPIRED_HOLD_SECONDS)}s or until fresh login")
    _token_known_expired_at = time.time()


def _clear_token_expired(reason: str = "") -> None:
    """Called on successful OAuth callback / fresh-token store, or when
    Fyers accepts a WS handshake (implicit proof the token is good)."""
    global _token_known_expired_at
    if _token_known_expired_at > 0:
        print(f"[engine] Fyers token expired-flag cleared ({reason})")
    _token_known_expired_at = 0.0


# Mode-toggle cooldown. Rapid trading_mode toggles trigger back-to-back
# restart_live_feed(ignore_backoff=True) calls that stack fresh handshakes
# on top of a warm socket → Fyers 429s and the circuit opens. On 2026-08-13
# the user toggled paper<->live 5 times in 7 minutes and burned quota.
# Reject a toggle within this many seconds of the previous one.
_MODE_TOGGLE_COOLDOWN_SECONDS = 30.0
_last_mode_switch_at = 0.0


def _mode_toggle_cooldown_remaining() -> float:
    if _last_mode_switch_at <= 0:
        return 0.0
    return max(0.0, _MODE_TOGGLE_COOLDOWN_SECONDS - (time.time() - _last_mode_switch_at))


def _current_backoff_seconds() -> float:
    """The wait time required between the last restart and the next one,
    based on how many consecutive failures we're in. Never returns less
    than _FEED_MIN_RESTART_INTERVAL_SECONDS during a failure run (F6).
    First attempt (zero failures) is exempt so a healthy start still fires
    immediately."""
    if _feed_reconnect_failure_count <= 0:
        return float(_FEED_BACKOFF_SEQUENCE[0])
    idx = min(_feed_reconnect_failure_count - 1, len(_FEED_BACKOFF_SEQUENCE) - 1)
    ladder_value = float(_FEED_BACKOFF_SEQUENCE[idx])
    return max(ladder_value, _FEED_MIN_RESTART_INTERVAL_SECONDS)


def _circuit_open_remaining() -> float:
    return max(0.0, _feed_circuit_open_until - time.time())


def _reset_feed_circuit(reason: str) -> None:
    """Close the circuit and clear the backoff counter — called on real tick
    or on explicit human action so the next restart can fire immediately."""
    global _feed_reconnect_failure_count, _feed_circuit_open_until, _feed_last_recovery_at
    if _feed_reconnect_failure_count > 0 or _feed_circuit_open_until > 0:
        print(f"[engine] Fyers WS reconnect state reset ({reason})")
        _feed_last_recovery_at = time.time()
    _feed_reconnect_failure_count = 0
    _feed_circuit_open_until = 0.0


def _record_feed_failure(reason: str) -> None:
    """Increment failure count and open the circuit if we've crossed the
    max-attempts threshold. Called from on_error / on_close and from the
    30-second missing-first-tick detector.

    F6: during the post-recovery grace window we don't count failures at
    all — this stops the "we just recovered, then failed again 5s later,
    circuit opens instantly" oscillation that stretched 2026-08-17.
    """
    global _feed_reconnect_failure_count, _feed_circuit_open_until
    if _feed_last_recovery_at > 0:
        since_recovery = time.time() - _feed_last_recovery_at
        if since_recovery < _FEED_POST_RECOVERY_GRACE_SECONDS:
            print(
                f"[engine] Fyers WS failure ignored during {int(_FEED_POST_RECOVERY_GRACE_SECONDS)}s "
                f"post-recovery grace ({reason}); {int(since_recovery)}s since last recovery"
            )
            return
    _feed_reconnect_failure_count += 1
    if _feed_reconnect_failure_count >= _FEED_MAX_CONSECUTIVE_FAILURES:
        _feed_circuit_open_until = time.time() + _FEED_CIRCUIT_OPEN_SECONDS
        print(
            f"[engine] Fyers WS circuit breaker OPEN after "
            f"{_feed_reconnect_failure_count} consecutive failures ({reason}); "
            f"suppressing reconnects for {int(_FEED_CIRCUIT_OPEN_SECONDS)}s"
        )
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
    "fyers_session_state": "token_missing",
    "fyers_recovery_id": None,
    "fyers_recovery_owner": None,
    "fyers_recovery_reason": None,
    "fyers_recovery_started_at": None,
    "fyers_recovery_settling_until": None,
    "fyers_recovery_last_event": None,
}


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _iso_after(seconds: float) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=max(0.0, seconds))
    ).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _recovery_owner_from_reason(reason: str) -> str:
    lowered = str(reason or "manual").lower()
    if lowered.startswith("fyers_oauth_callback"):
        return "oauth_callback"
    if lowered.startswith("token_refresh"):
        return "token_refresh"
    if lowered.startswith("trading_mode"):
        return "mode_switch"
    if lowered.startswith("watchdog") or lowered.startswith("scheduled_"):
        return "watchdog"
    if lowered.startswith("startup") or lowered.startswith("boot"):
        return "startup"
    if lowered.startswith("manual_disconnect"):
        return "manual_disconnect"
    return "manual"


def _recovery_window_active(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    settling_until = _parse_iso_datetime(_engine_status.get("fyers_recovery_settling_until"))
    started_at = _parse_iso_datetime(_engine_status.get("fyers_recovery_started_at"))
    if settling_until and settling_until > now:
        return True
    if started_at and (now - started_at).total_seconds() <= _RECOVERY_SETTLING_SECONDS:
        return True
    return False


def _set_fyers_session_state(state: str, *, note: str | None = None):
    with _engine_lock:
        _engine_status["fyers_session_state"] = state
        _engine_status["fyers_recovery_last_event"] = note or state


def _begin_fyers_recovery(owner: str, reason: str, *, settling_seconds: float = _RECOVERY_SETTLING_SECONDS) -> tuple[bool, str | None]:
    now = datetime.datetime.now(datetime.timezone.utc)
    with _engine_lock:
        current_owner = _engine_status.get("fyers_recovery_owner")
        current_reason = _engine_status.get("fyers_recovery_reason")
        current_id = _engine_status.get("fyers_recovery_id")
        if current_owner and _recovery_window_active(now):
            if current_owner == owner and current_reason == reason:
                print(
                    f"[engine] fyers recovery suppressed owner={owner} reason={reason} "
                    f"recovery_id={current_id} (duplicate in settling window)"
                )
                return False, current_id
            print(
                f"[engine] fyers recovery suppressed owner={owner} reason={reason} "
                f"recovery_id={current_id} active_owner={current_owner} active_reason={current_reason}"
            )
            return False, current_id
        recovery_id = uuid.uuid4().hex[:8]
        session_state = (
            "token_present_ws_recovering"
            if owner == "watchdog"
            else "token_present_settling"
        )
        _engine_status.update({
            "fyers_session_state": session_state,
            "fyers_recovery_id": recovery_id,
            "fyers_recovery_owner": owner,
            "fyers_recovery_reason": reason,
            "fyers_recovery_started_at": now.isoformat(),
            "fyers_recovery_settling_until": _iso_after(settling_seconds),
            "fyers_recovery_last_event": f"recovery_started:{reason}",
        })
    print(f"[engine] fyers recovery start recovery_id={recovery_id} owner={owner} reason={reason}")
    return True, recovery_id


def _complete_fyers_recovery(state: str, note: str):
    with _engine_lock:
        recovery_id = _engine_status.get("fyers_recovery_id")
        _engine_status.update({
            "fyers_session_state": state,
            "fyers_recovery_last_event": note,
        })
        if state in {"token_present_connected", "token_missing", "token_present_degraded"}:
            _engine_status.update({
                "fyers_recovery_id": None,
                "fyers_recovery_owner": None,
                "fyers_recovery_reason": None,
                "fyers_recovery_started_at": None,
                "fyers_recovery_settling_until": None,
            })
    print(f"[engine] fyers recovery complete recovery_id={recovery_id} state={state} note={note}")


def _hhmm_minus_minutes(hhmm: str, minutes: int) -> str:
    base = datetime.datetime.strptime(hhmm, "%H:%M")
    return (base - datetime.timedelta(minutes=minutes)).strftime("%H:%M")


def _strategy_session_bounds(strategy) -> tuple[str, str]:
    start_fn = getattr(strategy, "market_session_start", None)
    end_fn = getattr(strategy, "market_session_end", None)
    start = start_fn() if callable(start_fn) else "09:15"
    end = end_fn() if callable(end_fn) else "15:30"
    return start, end


def _strategy_square_off_time(strategy) -> str:
    square_off_fn = getattr(strategy, "square_off_time", None)
    if callable(square_off_fn):
        return square_off_fn()
    return SQUARE_OFF_TIME


def _strategy_session_active(strategy, hhmm: str) -> bool:
    start, end = _strategy_session_bounds(strategy)
    return start <= hhmm < end


def _strategy_feed_permitted(strategy, hhmm: str) -> bool:
    start, end = _strategy_session_bounds(strategy)
    warmup_start = _hhmm_minus_minutes(start, 10)
    return warmup_start <= hhmm < end


def _any_strategy_active(hhmm: str) -> bool:
    return any(_strategy_session_active(strategy, hhmm) for strategy in STRATEGIES.values())


def _any_strategy_feed_permitted(hhmm: str) -> bool:
    return any(_strategy_feed_permitted(strategy, hhmm) for strategy in STRATEGIES.values())


def _on_live_feed_status(status: dict):
    global _live_feed_started, _feed_circuit_open_until

    connected = bool(status.get("connected"))
    error = status.get("error")
    error_text = str(error or "").lower()
    is_rate_limited = "429" in error_text or "too many requests" in error_text
    is_token_expired = "token is expired" in error_text or "token expired" in error_text

    global _feed_disconnected_since
    with _engine_lock:
        was_connected = bool(_engine_status.get("fyers_ws_connected"))
        current_session_state = _engine_status.get("fyers_session_state")
        in_recovery_window = _recovery_window_active()
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
        if connected:
            update.update({
                "fyers_session_state": "token_present_connected",
                "fyers_recovery_last_event": "ws_open",
                "fyers_recovery_id": None,
                "fyers_recovery_owner": None,
                "fyers_recovery_reason": None,
                "fyers_recovery_started_at": None,
                "fyers_recovery_settling_until": None,
            })
        elif current_session_state != "token_missing":
            update["fyers_session_state"] = (
                "token_present_settling"
                if in_recovery_window and current_session_state == "token_present_settling"
                else "token_present_ws_recovering"
            )
            update["fyers_recovery_last_event"] = "ws_close"
        _engine_status.update(update)
        # Track the disconnected-since timestamp so /api/engine/status can
        # tell the frontend how long we've been down (F14 debounce).
        if connected:
            _feed_disconnected_since = 0.0
        elif was_connected or _feed_disconnected_since == 0.0:
            _feed_disconnected_since = time.time()

    # Real tick arrived → Fyers is fine with us, close the circuit and reset backoff.
    if status.get("first_tick_received"):
        _reset_feed_circuit("first tick received")
        _clear_token_expired("first tick received")

    if is_token_expired:
        _mark_token_expired("WS handshake reported token expired")

    # 429 counts as a failure AND immediately opens the circuit — we are being
    # explicitly told to back off, don't wait for the count to accumulate.
    if is_rate_limited:
        _feed_circuit_open_until = max(
            _feed_circuit_open_until,
            time.time() + _FEED_CIRCUIT_OPEN_SECONDS,
        )
        print(
            f"[engine] Fyers WS 429; circuit breaker OPEN for "
            f"{int(_FEED_CIRCUIT_OPEN_SECONDS)}s"
        )
        _record_feed_failure("rate_limited")
    elif not connected and error:
        # F4 (2026-08-17): before market open (09:15 IST) Fyers doesn't
        # broadcast ticks, so the "no market tick within 30s" watchdog
        # WILL fire — it's not a failure, it's the market being closed.
        # Skip failure counting for that specific message during the
        # 09:05-09:15 warmup window; otherwise the circuit breaker opens
        # right when we need it warmed up.
        now_ist = datetime.datetime.now(IST).strftime("%H:%M")
        in_premarket = "09:05" <= now_ist < "09:15"
        is_no_tick_watchdog = "no fyers market tick" in error_text or "not delivering market data" in error_text
        if in_premarket and is_no_tick_watchdog:
            print(f"[engine] Fyers WS no-tick during pre-market ({now_ist}) — expected, not counted as failure")
        else:
            # Any other disconnect (close, error, missing first tick) adds to
            # the circuit-breaker failure count.
            _record_feed_failure(error_text[:60] or "disconnect")

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
        watchlist = getattr(strategy, "watchlist", [])
        if watchlist and symbol not in watchlist:
            continue
        strategy.on_candle_close(symbol, candle, indicators)


# Tick diagnostics — track first tick per symbol + periodic summary so
# "why is my MCX feed silent?" is answerable from Railway logs alone.
# Without this, _on_tick is silent and a broken subscription looks
# identical to "no market activity." Reset on live feed restart.
_first_tick_seen_symbols: set[str] = set()
_tick_stats_lock = threading.Lock()
_tick_stats = {
    "window_started_at": None,  # monotonic seconds
    "window_ticks": 0,
    "window_symbols": set(),
    "by_exchange": {},
    "window_seconds": 60,
}


def _reset_tick_diagnostics():
    with _tick_stats_lock:
        _first_tick_seen_symbols.clear()
        _tick_stats["window_started_at"] = None
        _tick_stats["window_ticks"] = 0
        _tick_stats["window_symbols"] = set()
        _tick_stats["by_exchange"] = {}


def _record_tick_diagnostics(symbol: str, ltp) -> None:
    """First-tick-per-non-NSE-symbol log + rolling ~5-minute summary.

    NSE is by far the noisiest exchange (500+ symbols each ticking
    multiple times per second during market hours) and the client's
    only real concern in Silver-only mode is 'is MCX arriving?'.
    So NSE is silenced:
      - No first-tick log for individual NSE symbols
      - No summary while ONLY NSE is ticking (nothing interesting to say)
      - A single 'NSE feed alive: N ticks' line every ~5 min proves
        the socket is up without flooding
    Non-NSE (MCX / BSE / any commodity) still gets first-tick per symbol
    AND a summary whenever any arrive.
    """
    now = time.time()
    exch = symbol.split(":", 1)[0].upper() if ":" in symbol else "?"
    with _tick_stats_lock:
        # Only log first-tick for non-NSE symbols — that's the
        # signal the user actually cares about.
        if exch != "NSE" and symbol not in _first_tick_seen_symbols:
            _first_tick_seen_symbols.add(symbol)
            print(f"[feed] first tick for {symbol} @ {ltp}")
        elif exch == "NSE" and not any(s.startswith("NSE:") for s in _first_tick_seen_symbols):
            # A single line confirms NSE stream came alive at all.
            _first_tick_seen_symbols.add(symbol)
            print(f"[feed] NSE stream alive (first NSE tick: {symbol} @ {ltp})")
        if _tick_stats["window_started_at"] is None:
            _tick_stats["window_started_at"] = now
        _tick_stats["window_ticks"] += 1
        _tick_stats["window_symbols"].add(symbol)
        _tick_stats["by_exchange"][exch] = _tick_stats["by_exchange"].get(exch, 0) + 1
        elapsed = now - _tick_stats["window_started_at"]
        # Summary cadence:
        #  - Every 60s if any non-NSE (MCX/BSE) ticks arrived (what we care about)
        #  - Every 300s (5 min) if only NSE ticks (heartbeat only)
        non_nse = sum(v for k, v in _tick_stats["by_exchange"].items() if k != "NSE")
        cadence = 60 if non_nse > 0 else 300
        if elapsed >= cadence:
            by_exch = ", ".join(f"{k}={v}" for k, v in sorted(_tick_stats["by_exchange"].items()))
            if non_nse > 0:
                print(
                    f"[feed] tick summary last {int(elapsed)}s: "
                    f"{_tick_stats['window_ticks']} ticks / "
                    f"{len(_tick_stats['window_symbols'])} symbols ({by_exch})"
                )
            else:
                # NSE-only heartbeat — one short line every 5 min so we
                # know the socket is still delivering something.
                print(f"[feed] NSE-only feed alive: {_tick_stats['window_ticks']} ticks / {len(_tick_stats['window_symbols'])} symbols in {int(elapsed)}s (no MCX/BSE)")
            _tick_stats["window_started_at"] = now
            _tick_stats["window_ticks"] = 0
            _tick_stats["window_symbols"] = set()
            _tick_stats["by_exchange"] = {}


def _on_tick(message: dict):
    symbol = message.get("symbol")
    ltp = message.get("ltp")
    day_volume = message.get("vol_traded_today", 0)
    if not symbol or not ltp:
        return
    _record_tick_diagnostics(symbol, ltp)

    last_ltp[symbol] = ltp
    _symbol_last_tick_at[symbol] = _utc_now()
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
    now = datetime.datetime.now(IST)
    with _engine_lock:
        _engine_status.update({
            "last_tick_at": _utc_now(),
            "last_tick_symbol": symbol,
            "last_tick_ltp": ltp,
            "tick_count": int(_engine_status.get("tick_count") or 0) + 1,
        })
    # Broadcast every market tick immediately so the dashboard and any live
    # selected rows move in lockstep with the feed instead of a throttled view.
    broadcast_sync({"event": "price_update", "symbol": symbol, "ltp": ltp})

    aggregator.on_tick(symbol, ltp, day_volume, on_candle_close=_on_candle_close)

    for strategy in STRATEGIES.values():
        watchlist = getattr(strategy, "watchlist", [])
        if watchlist and symbol not in watchlist:
            continue
        strategy.on_tick(symbol, ltp, now)
        for position in strategy.broker.open_positions():
            if position["symbol"] == symbol:
                position["_last_ltp"] = ltp
                strategy.broker.update_position_range(position, ltp)
        strategy.check_exits()


def _critical_live_feed_symbols() -> list[str]:
    """Symbols that should get a REST fallback if the WS starves them."""
    return [
        symbol
        for symbol in (LIVE_FEED_SYMBOLS or WATCHLIST)
        if symbol and not str(symbol).upper().startswith("NSE:")
    ]


def _symbol_tick_age_seconds(symbol: str) -> float | None:
    value = _symbol_last_tick_at.get(symbol)
    if not value:
        return None
    try:
        tick_time = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - tick_time).total_seconds()


def _symbol_needs_rest_fallback(symbol: str, max_age_seconds: float = 15.0) -> bool:
    age = _symbol_tick_age_seconds(symbol)
    return age is None or age >= max_age_seconds


def _stale_open_position_symbols(max_age_seconds: float = 10.0) -> set[str]:
    """Return open-position symbols whose own feed has gone stale.

    Important: this must be per-symbol, not based on the global engine
    last-tick time. A fresh NSE tick must not suppress REST protection for
    an open MCX Silver position whose own ticks have gone quiet.
    """
    symbols: set[str] = set()
    for strategy in STRATEGIES.values():
        brokers = getattr(strategy, "_active_brokers", None)
        broker_list = brokers() if callable(brokers) else [strategy.broker]
        for broker in broker_list:
            try:
                for position in broker.open_positions():
                    sym = position.get("symbol")
                    if sym and _symbol_needs_rest_fallback(sym, max_age_seconds=max_age_seconds):
                        symbols.add(sym)
            except Exception:
                continue
    return symbols


def _inject_rest_tick(symbol: str, ltp: float) -> None:
    _on_tick({
        "symbol": symbol,
        "ltp": float(ltp),
        "vol_traded_today": 0,
        "source": "rest_fallback",
    })


def _critical_symbol_rest_fallback_loop():
    """Keep non-NSE strategy symbols alive via REST when WS is silent.

    For today's production issue this mainly means the one MCX Silver Micro
    symbol: if the websocket subscribes successfully but no tick arrives,
    poll Fyers Quotes for LTP and the completed 1-minute candle so algo3 can
    still update last price, build 15m bars, and trigger entries/exits.
    """
    from .fyers_client import get_live_ltp_batch, get_single_minute_candle

    last_candle_seen: dict[str, str] = {}

    while True:
        try:
            now_ist = datetime.datetime.now(IST)
            hhmm = now_ist.strftime("%H:%M")
            # NSE closes at 15:30, but MCX Silver Micro continues into the
            # evening session. Keep the REST fallback alive for the full MCX
            # window so post-15:30 algo3 can still trade.
            if not ("09:15" <= hhmm < "23:30"):
                time.sleep(5)
                continue

            critical_symbols = [symbol for symbol in _critical_live_feed_symbols() if _symbol_needs_rest_fallback(symbol)]
            if not critical_symbols:
                time.sleep(5)
                continue

            try:
                ltps = get_live_ltp_batch(critical_symbols, mode="live")
            except Exception as exc:
                print(f"[engine] critical-symbol REST LTP fallback failed: {exc}")
                time.sleep(5)
                continue

            for symbol, ltp in ltps.items():
                if not ltp:
                    continue
                _inject_rest_tick(symbol, ltp)

                # Rebuild the just-completed 1-minute candle from history so
                # algo3 can keep forming 15m bars even while WS is dead.
                previous_minute = (now_ist.replace(second=0, microsecond=0) - datetime.timedelta(minutes=1))
                candle_hhmm = previous_minute.strftime("%H:%M")
                candle_key = f"{symbol}|{previous_minute.isoformat()}"
                if last_candle_seen.get(symbol) == candle_key:
                    continue
                try:
                    candles = get_single_minute_candle(symbol, candle_hhmm)
                except Exception as exc:
                    print(f"[engine] critical-symbol 1m candle fallback failed for {symbol} {candle_hhmm}: {exc}")
                    continue
                if not candles:
                    continue
                candle = candles[-1]
                if candle.get("time") and candle["time"].date() == previous_minute.date():
                    _on_candle_close(symbol, candle, {})
                    last_candle_seen[symbol] = candle_key

        except Exception as exc:
            print(f"[engine] critical-symbol REST fallback loop error: {exc}")

        time.sleep(5)


def _recover_scheduled_candle_from_buffer(strategy, scan_time: str, today: datetime.date) -> int:
    """Replay an already-closed test candle into one strategy.

    A test schedule can be saved after its target minute has started, or the
    scheduler can be delayed while the feed thread is busy. The shared candle
    aggregator still retains the exact one-minute OHLC bar, so use that before
    falling back to a failed test instead of retrying forever.
    """
    seen_symbols = getattr(strategy, "scan_seen_symbols", set())
    watchlist = set(getattr(strategy, "watchlist", []) or [])
    recovered = 0
    for symbol, state in list(aggregator.symbols.items()):
        if watchlist and symbol not in watchlist:
            continue
        if symbol in seen_symbols:
            continue
        candle = next(
            (
                item
                for item in reversed(list(state.closed_candles))
                if item.get("time")
                and item["time"].date() == today
                and item["time"].strftime("%H:%M") == scan_time
            ),
            None,
        )
        if candle is None:
            continue
        strategy.on_candle_close(symbol, dict(candle), aggregator.get_indicators(symbol))
        recovered += 1
    if recovered:
        print(
            f"[engine] recovered {recovered} buffered {scan_time}:00 IST candles "
            f"for scheduled test {strategy.algo_id}"
        )
    return recovered


def _scheduler_loop():
    """Runs alongside the tick handler -- checks the clock for the
    9:16 entry trigger (algo1) and 3:15 square-off (both algos)."""
    entries_fired_date: dict[str, datetime.date] = {}
    entries_fired_schedule: dict[str, tuple[bool, str]] = {}
    test_schedule_attempt_minute: dict[str, tuple[datetime.date, str]] = {}
    # Dedupe noisy per-poll status prints so retry cycles do not spam the console.
    last_pending_msg: dict[str, str] = {}
    # Track (date, algo_id) for the "collecting_candle" broadcast so we only
    # push it once per scan-day per strategy, not every 5s during the minute.
    collecting_broadcast: set[tuple[datetime.date, str]] = set()
    squareoff_fired_dates: dict[str, datetime.date] = {}
    token_refresh_fired_date = None
    global _feed_retry_schedules

    while True:
        now = datetime.datetime.now(IST)
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
                    # Delegate to restart_live_feed so the same
                    # backoff + circuit-breaker rules apply here too.
                    if not restart_live_feed(reason=f"scheduled_{scan_time}_no_first_tick"):
                        print(
                            f"[engine] no market tick at {scan_time}; restart skipped "
                            "by backoff/circuit — scan will run on LTP fallback"
                        )

            # Broadcast a visible "collecting_candle" status when the scan
            # minute starts, so the frontend banner shows the app is working
            # instead of just displaying yesterday's stale scan results while
            # the 60-second candle-collection window ticks.
            broadcast_key = (today, strategy.algo_id)
            if (
                scan_time
                and current_time == scan_time
                and broadcast_key not in collecting_broadcast
                and hasattr(strategy, "mark_collecting_candle")
            ):
                collecting_broadcast.add(broadcast_key)
                try:
                    strategy.mark_collecting_candle()
                except Exception as exc:
                    print(f"[engine] mark_collecting_candle failed for {strategy.algo_id}: {exc}")

        # Each opening strategy can opt into a one-off test schedule without
        # changing the production 09:15/09:16 defaults for the other strategy.
        # Close the just-finished minute even for symbols that have not sent a
        # follow-up tick yet. This must happen before the 9:16 entry check.
        aggregator.flush_completed_candles(on_candle_close=_on_candle_close, now=now.replace(tzinfo=None))
        for strategy in STRATEGIES.values():
            flush_due = getattr(strategy, "flush_clock_closed_bar", None)
            if callable(flush_due):
                try:
                    flush_due()
                except Exception as exc:
                    print(f"[engine] flush_clock_closed_bar failed for {strategy.algo_id}: {exc}")

        pending = []
        completed_any = False
        for strategy in STRATEGIES.values():
            if not hasattr(strategy, "entry_window"):
                continue
            schedule = (
                bool(strategy.settings.get("test_schedule_enabled")),
                strategy.scan_candle_time(),
            )
            test_schedule_enabled = bool(strategy.settings.get("test_schedule_enabled"))
            scan_time = getattr(strategy, "scan_candle_time", lambda: None)()
            # Test mode fires 2 minutes after the candle (production 09:15
            # still fires at +1 min = 09:16). +2 gives:
            #   - +1 min: the candle minute itself finishes closing
            #   - +1 min: brief buffer for WS-collected data to settle
            # First-come-first-serve: whatever data is available at the
            # evaluate moment (WS candles + immediate Fyers history) is
            # used for trades. Symbols still missing at that moment are
            # audited as missing — no fabrication, no long wait.
            # Fyers history API returns a PARTIAL flat candle (open=high=low
            # =close) if you query before the minute is fully indexed on
            # their side — typically 3-5 minutes after the candle closes.
            # 2 minutes was too short and produced fake-flat-candle scans.
            # Production 09:15 still fires at +1 min because Fyers bulk-
            # indexes the 09:15 candle at market open (no lag there).
            # Test schedules wait for Fyers to index the signal-minute candle
            # into their history endpoint. 3 min is the low end of the 3-5 min
            # indexing window per prior observation — trades off some safety
            # for faster feedback in test runs. Production 09:15 keeps +1 min
            # because Fyers bulk-indexes at market open (no lag there).
            entry_delay_min = 3 if test_schedule_enabled else 1
            entry_time = None
            if scan_time:
                try:
                    entry_time = (
                        datetime.datetime.strptime(scan_time, "%H:%M")
                        + datetime.timedelta(minutes=entry_delay_min)
                    ).strftime("%H:%M")
                except ValueError:
                    entry_time = None
            # A later UI change from the production schedule to a test time is
            # a new run. Do not let the already-missed 09:16 window suppress it.
            if entries_fired_date.get(strategy.algo_id) == today and entries_fired_schedule.get(strategy.algo_id) == schedule:
                continue
            if test_schedule_enabled:
                if entry_time and current_time >= entry_time:
                    attempt_key = (today, current_time)
                    if test_schedule_attempt_minute.get(strategy.algo_id) == attempt_key:
                        continue
                    test_schedule_attempt_minute[strategy.algo_id] = attempt_key
                    print(
                        f"[engine] scheduled test starting for {strategy.algo_id}: "
                        f"signal={scan_time}:00 IST evaluation={entry_time}:00 IST"
                    )
                    _recover_scheduled_candle_from_buffer(strategy, scan_time, today)
                    try:
                        completed = strategy.evaluate_entries(get_ltp_fn=lambda s: last_ltp.get(s))
                    except Exception as exc:
                        print(f"[engine] opening scan failed for {strategy.algo_id}: {exc}")
                        mark_failed = getattr(strategy, "mark_opening_scan_failed", None)
                        if callable(mark_failed):
                            try:
                                mark_failed(str(exc))
                            except Exception as record_exc:
                                print(f"[engine] could not record failed scan for {strategy.algo_id}: {record_exc}")
                        entries_fired_date[strategy.algo_id] = today
                        entries_fired_schedule[strategy.algo_id] = schedule
                        completed_any = True
                        continue
                    if completed is False:
                        # Deadline = entry_time + 2 min. In test mode with the
                        # 3-min entry delay this is scan_time + 5 min total;
                        # in production it stays scan_time + 3 min. Gives the
                        # retry loop enough headroom for Fyers history to
                        # finalize the candle.
                        deadline_time = (
                            datetime.datetime.strptime(scan_time, "%H:%M")
                            + datetime.timedelta(minutes=entry_delay_min + 2)
                        ).strftime("%H:%M")
                        actual_time = datetime.datetime.now(IST).strftime("%H:%M")
                        if actual_time >= deadline_time:
                            mark_missed = getattr(strategy, "mark_opening_scan_missed", None)
                            if callable(mark_missed):
                                mark_missed()
                            entries_fired_date[strategy.algo_id] = today
                            entries_fired_schedule[strategy.algo_id] = schedule
                            completed_any = True
                            print(
                                f"[engine] scheduled test ended for {strategy.algo_id}; "
                                f"the {scan_time}:00 IST candle was not usable by {deadline_time}:00 IST"
                            )
                        else:
                            pending.append(strategy.algo_id)
                            pending_msg = f"pending:{strategy.algo_id}:test"
                            if last_pending_msg.get(strategy.algo_id) != pending_msg:
                                last_pending_msg[strategy.algo_id] = pending_msg
                                print(
                                    f"[engine] scheduled test pending for {strategy.algo_id}; "
                                    "waiting for candle or previous-close data (silent until resolved)"
                                )
                    else:
                        entries_fired_date[strategy.algo_id] = today
                        entries_fired_schedule[strategy.algo_id] = schedule
                        completed_any = True
                        print(f"[engine] scheduled test completed for {strategy.algo_id}")
                continue
            # ── Production path: 09:16 entry window ──────────────────────────
            if strategy.entry_window(current_time):
                print(
                    f"[engine][{now.strftime('%H:%M:%S')} IST] ENTRY WINDOW OPEN for {strategy.algo_id} "
                    f"(scan={scan_time} entry={entry_time}) — calling evaluate_entries()"
                )
                try:
                    completed = strategy.evaluate_entries(get_ltp_fn=lambda s: last_ltp.get(s))
                except Exception as exc:
                    # A single strategy must never terminate the scheduler thread.
                    print(f"[engine][{now.strftime('%H:%M:%S')} IST] opening scan FAILED for {strategy.algo_id}: {exc}")
                    mark_failed = getattr(strategy, "mark_opening_scan_failed", None)
                    if callable(mark_failed):
                        try:
                            mark_failed(str(exc))
                        except Exception as record_exc:
                            print(f"[engine] could not record failed scan for {strategy.algo_id}: {record_exc}")
                    entries_fired_date[strategy.algo_id] = today
                    entries_fired_schedule[strategy.algo_id] = schedule
                    continue
                if completed is False:
                    pending.append(strategy.algo_id)
                    pending_msg = f"pending:{strategy.algo_id}:prod"
                    if last_pending_msg.get(strategy.algo_id) != pending_msg:
                        last_pending_msg[strategy.algo_id] = pending_msg
                        print(
                            f"[engine] evaluate_entries pending for {strategy.algo_id} "
                            "(silent until resolved)"
                        )
                else:
                    entries_fired_date[strategy.algo_id] = today
                    entries_fired_schedule[strategy.algo_id] = schedule
                    completed_any = True
                    print(
                        f"[engine][{now.strftime('%H:%M:%S')} IST] evaluate_entries COMPLETED for {strategy.algo_id}"
                    )
            elif strategy.entry_window_elapsed(current_time):
                strategy.mark_opening_scan_missed()
                entries_fired_date[strategy.algo_id] = today
                entries_fired_schedule[strategy.algo_id] = schedule
                print(
                    f"[engine][{now.strftime('%H:%M:%S')} IST] entry window elapsed for {strategy.algo_id}; "
                    "no late entries placed"
                )
        # Individual pending prints above are already deduped; the aggregate
        # line was the loudest source of 5-second console spam. Drop it.
        _ = pending
        if completed_any:
            try:
                from .calendar_store import save_dashboard_snapshot
                save_dashboard_snapshot(note="entry_scan")
            except Exception as exc:
                print(f"[engine] entry-scan calendar snapshot failed: {exc}")

        squared_any = False
        for strategy in STRATEGIES.values():
            cutoff = _strategy_square_off_time(strategy)
            if current_time < cutoff or squareoff_fired_dates.get(strategy.algo_id) == today:
                continue
            strategy.square_off_all()
            squareoff_fired_dates[strategy.algo_id] = today
            squared_any = True
        if squared_any:
            try:
                from .calendar_store import save_dashboard_snapshot
                save_dashboard_snapshot(note="eod_squareoff")
            except Exception as exc:
                print(f"[engine] EOD calendar snapshot failed: {exc}")

        time.sleep(5)


def _live_broker_reconcile_loop():
    """Poll Fyers orderbook every 30s during market hours to detect
    when hard SL or Target orders have filled. When a fill is detected,
    the LiveBroker updates our positions/trades tables and cancels the
    sibling protective order so it doesn't accidentally reverse the
    position later.

    Only runs against strategies whose broker is a LiveBroker instance
    (paper mode is a no-op). Silently skipped when no open live
    positions exist.
    """
    from .live_broker import LiveBroker

    while True:
        try:
            now = datetime.datetime.now(IST)
            current_time = now.strftime("%H:%M")
            if not _any_strategy_active(current_time):
                time.sleep(30)
                continue

            for strategy in STRATEGIES.values():
                broker = getattr(strategy, "broker", None)
                if not isinstance(broker, LiveBroker):
                    continue
                try:
                    result = broker.reconcile_open_positions()
                    if result.get("reconciled"):
                        print(
                            f"[engine] LiveBroker reconciled {result['reconciled']} positions "
                            f"for {strategy.algo_id}"
                        )
                except Exception as exc:
                    print(f"[engine] LiveBroker reconcile failed for {strategy.algo_id}: {exc}")

        except Exception as exc:
            print(f"[engine] LiveBroker reconcile loop error: {exc}")

        time.sleep(30)


def _open_position_ltp_poll_loop():
    """Keep open positions' LTP fresh when the Fyers WebSocket is dead.

    Without this loop, if the WS drops (or Fyers closes it mid-session
    with `code 200 Connection Closed`), positions on the dashboard get
    stuck at their last-known LTP and check_exits stops firing because
    no ticks arrive to trigger it. That means SL/Target won't hit until
    a WS reconnect — potentially never in a single trading day.

    This loop polls the Fyers Quotes REST API (which works independent
    of the WS) every 10 seconds for the union of every open position
    across all strategies, updates the shared last_ltp dict, pushes a
    synthetic 'tick' through each strategy's on_tick + check_exits so
    SL/Target logic fires normally. When the WS is healthy, this loop
    is a no-op (WS ticks arrive faster than the poll interval so
    last_ltp is always fresh).
    """
    from .fyers_client import get_live_ltp_batch

    while True:
        try:
            now = datetime.datetime.now(IST)
            current_time = now.strftime("%H:%M")
            if not _any_strategy_active(current_time):
                time.sleep(10)
                continue

            # Only protect OPEN symbols whose own feed has gone stale.
            # Fresh NSE traffic must not disable REST protection for MCX.
            symbols_needing_ltp = _stale_open_position_symbols(max_age_seconds=10.0)

            if not symbols_needing_ltp:
                time.sleep(10)
                continue

            try:
                ltps = get_live_ltp_batch(list(symbols_needing_ltp), mode="live")
            except Exception as exc:
                print(f"[engine] open-position LTP poll failed: {exc}")
                time.sleep(10)
                continue

            for symbol, ltp in ltps.items():
                if not ltp:
                    continue
                # Reuse the normal tick pathway so UI, per-symbol last-tick
                # bookkeeping, trailing, and exit checks all move together.
                try:
                    _inject_rest_tick(symbol, ltp)
                except Exception as exc:
                    print(f"[engine] LTP-poll tick inject failed for {symbol}: {exc}")

        except Exception as exc:
            print(f"[engine] open-position LTP poll loop error: {exc}")

        time.sleep(10)


def _live_feed_watchdog_loop():
    """Keep a real market-data stream alive before a scheduled scan.

    Uses the exponential-backoff sequence + circuit breaker declared at the
    top of this file. Reconnects are gated by whichever is stricter:
      - The per-retry backoff (5s → 10s → 20s → 40s → 60s), so we don't
        hammer Fyers between attempts.
      - The circuit breaker (opens after 5 failures; suppresses reconnects
        for 15 minutes), so we don't loop forever when the account/token
        is fundamentally broken.
    """
    global _feed_watchdog_last_restart_at

    while True:
        try:
            now = datetime.datetime.now(IST)
            current_time = now.strftime("%H:%M")
            market_open = _any_strategy_active(current_time)
            # Per-strategy warmup: allow WS attempts 10 min before the
            # earliest active session so the opening bar can still be built
            # live instead of via delayed REST backfill.
            feed_permitted = _any_strategy_feed_permitted(current_time) and get_stored_access_token()
            premarket_warmup = feed_permitted and not market_open
            last_tick_at = _engine_status.get("last_tick_at")
            tick_is_fresh = False
            if last_tick_at:
                try:
                    tick_time = datetime.datetime.fromisoformat(last_tick_at.replace("Z", "+00:00"))
                    tick_is_fresh = (datetime.datetime.now(datetime.timezone.utc) - tick_time).total_seconds() < 45
                except (TypeError, ValueError):
                    tick_is_fresh = False
            stale_seconds = time.time() - _feed_watchdog_last_restart_at

            circuit_wait = _circuit_open_remaining()
            backoff_wait = max(0.0, _current_backoff_seconds() - stale_seconds)
            boot_wait = _boot_grace_remaining()
            token_expired_wait = _token_expired_hold_remaining()

            if not feed_permitted:
                pass  # off-hours for every strategy, or no token
            elif token_expired_wait > 0:
                # Fyers said the token is expired — no point handshaking
                # again until the user re-logs in. Silent (would spam every
                # 5s otherwise); the mark_token_expired() print already
                # fires once when detected.
                pass
            elif tick_is_fresh:
                pass  # feed is healthy
            elif boot_wait > 0:
                # Deploy-race guard: don't handshake while Fyers still holds
                # the old container's WS. Silent — logs once from start_engine.
                pass
            elif circuit_wait > 0:
                # Silent — this can loop for 15 minutes; don't log every 5s.
                pass
            elif backoff_wait > 0:
                pass
            else:
                # NB: do NOT bump _feed_watchdog_last_restart_at before the
                # call. restart_live_feed uses that same variable to decide
                # whether backoff has elapsed, and bumping it here would
                # cause every call to be "skipped: 59s backoff remaining"
                # even when the watchdog already waited the required time.
                # restart_live_feed will bump it itself if it actually
                # kicks off a new connection.
                next_backoff = _FEED_BACKOFF_SEQUENCE[
                    min(_feed_reconnect_failure_count, len(_FEED_BACKOFF_SEQUENCE) - 1)
                ]
                reason = "watchdog_premarket_warmup" if premarket_warmup and not market_open else "watchdog_stale_or_missing_tick"
                started = restart_live_feed(reason=reason)
                if started:
                    phase = "pre-market warmup" if premarket_warmup and not market_open else "market hours"
                    print(
                        f"[engine] Fyers WS restart during {phase} "
                        f"(attempt #{_feed_reconnect_failure_count + 1}, "
                        f"next backoff {next_backoff}s)"
                    )
        except Exception as exc:
            print(f"[engine] live-feed watchdog error: {exc}")
        time.sleep(5)


def start_live_feed_if_ready(force: bool = False) -> bool:
    global _live_feed_started, _live_feed_socket, _feed_watchdog_last_restart_at

    feed_symbols = LIVE_FEED_SYMBOLS or WATCHLIST
    if not feed_symbols:
        print("[engine] watchlist not initialized yet, cannot start live feed")
        return False

    if get_stored_access_token() is None:
        _complete_fyers_recovery("token_missing", "start_skipped:no_token")
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
                socket = connect_live_feed(feed_symbols, _on_tick, _on_live_feed_status)
                with _live_feed_lock:
                    _live_feed_socket = socket
            except Exception as exc:
                with _engine_lock:
                    _engine_status.update({
                        "fyers_ws_connected": False,
                        "fyers_ws_error": str(exc),
                        "fyers_ws_last_event_at": _utc_now(),
                        "live_feed_started": False,
                        "fyers_session_state": "token_present_degraded",
                        "fyers_recovery_last_event": f"ws_start_failed:{exc}",
                        "fyers_recovery_id": None,
                        "fyers_recovery_owner": None,
                        "fyers_recovery_reason": None,
                        "fyers_recovery_started_at": None,
                        "fyers_recovery_settling_until": None,
                    })
                with _live_feed_lock:
                    _live_feed_started = False
                print(f"[engine] live feed failed: {exc}")

        threading.Thread(target=run_live_feed, daemon=True).start()
        _live_feed_started = True
        _feed_watchdog_last_restart_at = time.time()
        # Fresh tick counters so first-tick-per-symbol logs fire again after
        # a WS restart; otherwise a symbol seen in the previous session
        # never appears in logs again and it looks like nothing is arriving.
        _reset_tick_diagnostics()
        with _engine_lock:
            _engine_status.update({
                "live_feed_started": True,
                "fyers_ws_error": None,
                "fyers_ws_last_event_at": _utc_now(),
                "fyers_ws_subscribed_symbols": 0,
                "fyers_ws_first_tick_at": None,
                "fyers_recovery_last_event": "feed_restart_requested",
            })
        print(f"[engine] live feed start requested for {len(feed_symbols)} symbols")
        for strategy in STRATEGIES.values():
            refresh_market_data = getattr(strategy, "refresh_market_data", None)
            if refresh_market_data:
                refresh_market_data()
        return True


def restart_live_feed(reason: str = "manual", ignore_backoff: bool = False) -> bool:
    """Restart the Fyers WS. Automated callers (watchdog, scheduler, token
    refresh) must respect both the exponential-backoff wait AND the circuit
    breaker; human/UI actions can bypass with ignore_backoff=True, which also
    resets the failure counter since a fresh login/mode-switch/OAuth means
    the previous run is no longer representative."""
    owner = _recovery_owner_from_reason(reason)
    began, recovery_id = _begin_fyers_recovery(owner, reason)
    if not began:
        return False
    if ignore_backoff:
        _reset_feed_circuit(f"human action: {reason}")
    else:
        circuit_wait = _circuit_open_remaining()
        if circuit_wait > 0:
            _set_fyers_session_state("token_present_ws_recovering", note=f"restart_suppressed:circuit:{reason}")
            print(
                f"[engine] restart_live_feed skipped ({reason}): "
                f"circuit breaker open, {int(circuit_wait)}s remaining"
            )
            return False
        since_last = time.time() - _feed_watchdog_last_restart_at
        backoff_needed = _current_backoff_seconds()
        if since_last < backoff_needed:
            wait_left = int(backoff_needed - since_last)
            _set_fyers_session_state("token_present_ws_recovering", note=f"restart_suppressed:backoff:{reason}")
            print(f"[engine] restart_live_feed skipped ({reason}): {wait_left}s backoff remaining")
            return False
    print(f"[engine] restarting Fyers live feed ({reason}) recovery_id={recovery_id} owner={owner}")
    return start_live_feed_if_ready(force=True)


def stop_live_feed(reason: str = "manual") -> bool:
    """Stop the active FYERS live feed and close any open websocket."""
    global _live_feed_started, _live_feed_socket

    socket_to_close = None
    with _live_feed_lock:
        socket_to_close = _live_feed_socket
        _live_feed_socket = None
        _live_feed_started = False

    if socket_to_close is not None:
        close_connection = getattr(socket_to_close, "close_connection", None)
        if callable(close_connection):
            try:
                close_connection()
            except Exception as exc:
                print(f"[engine] live feed stop close failed ({reason}): {exc}")

    with _engine_lock:
        _engine_status.update({
            "live_feed_started": False,
            "fyers_ws_connected": False,
            "fyers_ws_error": f"Stopped ({reason})",
            "fyers_session_state": "token_missing" if "disconnect" in reason else "token_present_degraded",
            "fyers_recovery_last_event": f"stopped:{reason}",
            "fyers_recovery_id": None,
            "fyers_recovery_owner": None,
            "fyers_recovery_reason": None,
            "fyers_recovery_started_at": None,
            "fyers_recovery_settling_until": None,
        })

    print(f"[engine] stopped Fyers live feed ({reason})")
    return True


def get_engine_status() -> dict:
    circuit_left = int(_circuit_open_remaining())
    ws_connected = bool(_engine_status.get("fyers_ws_connected"))
    live_feed_started = bool(_engine_status.get("live_feed_started"))
    # F14: auto_recovering means "we're trying to be up and haven't given up
    # yet" — the frontend uses this to show "Reconnecting…" instead of
    # scaring the user with "Disconnected". A hard disconnect (live feed
    # stopped by user / token expired hard) is NOT auto-recovering.
    auto_recovering = (
        (not ws_connected)
        and live_feed_started
        and (_feed_reconnect_failure_count > 0 or circuit_left > 0)
    )
    disconnected_since_s: int | None = None
    if _feed_disconnected_since > 0 and not ws_connected:
        disconnected_since_s = int(time.time() - _feed_disconnected_since)
    return {
        "state": _engine_status["state"],
        "error": _engine_status["error"],
        "trading_mode": get_runtime_trading_mode(),
        "fyers_session_state": _engine_status.get("fyers_session_state"),
        "fyers_recovery_id": _engine_status.get("fyers_recovery_id"),
        "fyers_recovery_owner": _engine_status.get("fyers_recovery_owner"),
        "fyers_recovery_reason": _engine_status.get("fyers_recovery_reason"),
        "fyers_recovery_started_at": _engine_status.get("fyers_recovery_started_at"),
        "fyers_recovery_settling_until": _engine_status.get("fyers_recovery_settling_until"),
        "fyers_recovery_last_event": _engine_status.get("fyers_recovery_last_event"),
        "ws_reconnect_failure_count": _feed_reconnect_failure_count,
        "ws_circuit_open_seconds_remaining": circuit_left,
        "ws_next_backoff_seconds": int(_current_backoff_seconds()),
        "auto_recovering": auto_recovering,
        "disconnected_since_seconds": disconnected_since_s,
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
        "live_feed_symbol_count": len(LIVE_FEED_SYMBOLS or WATCHLIST),
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
    }
    return labels.get(algo_id, "Legacy trade before trigger storage; exact trigger was not saved.")


def try_refresh_access_token(reason: str = "manual_or_startup") -> bool:
    try:
        refresh_access_token_from_refresh_token()
        with _engine_lock:
            _engine_status.update({
                "last_token_refresh": _utc_now(),
                "last_token_refresh_error": None,
                "fyers_session_state": "token_present_settling",
                "fyers_recovery_last_event": f"token_refresh_ok:{reason}",
            })
        print(f"[engine] Fyers access token refreshed via refresh token ({reason})")
        # A brand new token invalidates whatever 429 backoff we were sitting in
        # (Fyers rate-limits by session, not by token), so let this bypass.
        restart_live_feed(reason=f"token_refresh_{reason}", ignore_backoff=True)
        return True
    except Exception as exc:
        with _engine_lock:
            _engine_status["last_token_refresh_error"] = str(exc)
            if get_stored_access_token() is None:
                _engine_status["fyers_session_state"] = "token_missing"
            else:
                _engine_status["fyers_session_state"] = "token_present_degraded"
            _engine_status["fyers_recovery_last_event"] = f"token_refresh_failed:{reason}"
        print(f"[engine] Fyers refresh-token refresh skipped/failed ({reason}): {exc}")
        return False


def apply_trading_mode(mode: str) -> dict:
    """Switch the active broker mode without restarting the process."""
    global _last_mode_switch_at
    normalized_mode = normalize_trading_mode(mode)
    current_mode = get_runtime_trading_mode()
    if normalized_mode == current_mode:
        return {
            "trading_mode": current_mode,
            "message": f"Trading mode already set to {current_mode}.",
        }

    # Cooldown guard: rapid toggles burn Fyers WS quota. If the previous
    # toggle was within _MODE_TOGGLE_COOLDOWN_SECONDS, reject with the
    # remaining time so the caller can show a friendly wait message.
    cooldown_remaining = _mode_toggle_cooldown_remaining()
    if cooldown_remaining > 0:
        raise RuntimeError(
            f"Mode toggle cooldown active — please wait {int(cooldown_remaining)}s "
            "before switching again (prevents Fyers WS quota burn)."
        )

    open_positions: dict[str, int] = {}
    for algo_id, strategy in STRATEGIES.items():
        try:
            positions = strategy.broker.open_positions()
        except Exception as exc:
            raise RuntimeError(f"Unable to inspect open positions for {algo_id}: {exc}") from exc
        if positions:
            open_positions[algo_id] = len(positions)

    # Paper and live state already live in separate broker tables. That means
    # switching modes does not need to close positions first; the positions stay
    # preserved in the mode they were opened in and the opposite mode gets its
    # own broker instance. Keep the information as a warning instead of blocking
    # the user's request with a 409.
    warning = None
    if open_positions:
        warning = (
            "Existing positions stay preserved in the previous mode: "
            f"{', '.join(f'{algo_id} ({count})' for algo_id, count in open_positions.items())}."
        )

    set_runtime_trading_mode(normalized_mode)

    for strategy in STRATEGIES.values():
        refresh_settings = getattr(strategy, "reload_settings", None)
        if callable(refresh_settings):
            refresh_settings()
        starting_capital = float(getattr(strategy, "settings", {}).get("starting_capital") or 500000)
        strategy.broker = create_broker(algo_id=strategy.algo_id, starting_capital=starting_capital)
        # Rebuild shadow paper broker under the new mode (paper mode: no
        # shadow needed; live mode + parallel: shadow is a fresh PaperBroker).
        rebuild_shadow = getattr(strategy, "_rebuild_shadow_paper_broker", None)
        if callable(rebuild_shadow):
            rebuild_shadow()
        refresh_market_data = getattr(strategy, "refresh_market_data", None)
        if callable(refresh_market_data):
            refresh_market_data()

    with _engine_lock:
        _engine_status["trading_mode"] = normalized_mode
        if get_stored_access_token(normalized_mode):
            _engine_status["fyers_session_state"] = "token_present_settling"
            _engine_status["fyers_recovery_last_event"] = f"mode_switch:{normalized_mode}"
        else:
            _engine_status["fyers_session_state"] = "token_missing"
            _engine_status["fyers_recovery_last_event"] = f"mode_switch_no_token:{normalized_mode}"

    _last_mode_switch_at = time.time()
    # Mode switch is a human UI action and uses a different token/client_id,
    # so it must not be silently skipped by any live 429 backoff cooldown.
    # Delayed 15s via Timer for the same reason as the OAuth callback path:
    # stacking a fresh handshake on top of a still-warm socket triggers 429
    # + circuit-open on Fyers's side.
    threading.Timer(
        15.0,
        restart_live_feed,
        kwargs={"reason": f"trading_mode_{normalized_mode}", "ignore_backoff": True},
    ).start()
    result = {
        "trading_mode": normalized_mode,
        "message": f"Trading mode switched to {normalized_mode}.",
    }
    if warning:
        result["warning"] = warning
        result["preserved_open_positions"] = open_positions
    return result


def start_engine():
    """Called once from main.py's FastAPI startup event."""
    global WATCHLIST, LIVE_FEED_SYMBOLS, _scheduler_started, _feed_watchdog_started, _critical_rest_fallback_started

    with _engine_lock:
        if _engine_status["state"] in {"starting", "running"}:
            return
        _engine_status.update({"state": "starting", "error": None})

    try:
        watchlist = get_nse500_watchlist()
        # Deployment-level strategy gate: HIDDEN_TABS env var can exclude
        # strategies entirely so a client-only build never risks loading
        # (or accidentally running) a strategy that isn't offered on that
        # deployment. Missing/empty env = load all three (dev default).
        all_strategy_ctors = {
            "algo1": lambda: Algo1OpeningRange(watchlist),
            "algo2": lambda: UN1915Filtered(watchlist),
            "algo3": lambda: Algo3SilverMicro(),
        }
        strategies = {
            algo_id: ctor()
            for algo_id, ctor in all_strategy_ctors.items()
            if algo_id not in _HIDDEN_ALGO_IDS
        }
        if _HIDDEN_ALGO_IDS:
            print(f"[engine] HIDDEN_TABS skipped strategies: {sorted(_HIDDEN_ALGO_IDS)}")
        live_feed_symbols = sorted({
            symbol
            for strategy in strategies.values()
            for symbol in getattr(strategy, "watchlist", [])
            if symbol
        })
        for algo_id, strategy in strategies.items():
            stale_count = strategy.broker.close_stale_open_positions()
            if stale_count:
                print(f"[engine] closed {stale_count} stale open positions for {algo_id}")

        with _engine_lock:
            WATCHLIST = watchlist
            LIVE_FEED_SYMBOLS = live_feed_symbols
            STRATEGIES.clear()
            STRATEGIES.update(strategies)
            _engine_status.update({"state": "running", "error": None})
            if get_stored_access_token() is None:
                _engine_status.update({
                    "fyers_session_state": "token_missing",
                    "fyers_recovery_last_event": "boot:no_token",
                })
            else:
                _engine_status.update({
                    "fyers_session_state": "token_present_settling",
                    "fyers_recovery_id": uuid.uuid4().hex[:8],
                    "fyers_recovery_owner": "startup",
                    "fyers_recovery_reason": "startup_boot",
                    "fyers_recovery_started_at": _utc_now(),
                    "fyers_recovery_settling_until": _iso_after(_BOOT_WS_DELAY_SECONDS + _RECOVERY_SETTLING_SECONDS),
                    "fyers_recovery_last_event": "boot:token_found",
                })

        if not _scheduler_started:
            threading.Thread(target=_scheduler_loop, daemon=True).start()
            _scheduler_started = True
        if not _feed_watchdog_started:
            threading.Thread(target=_live_feed_watchdog_loop, daemon=True).start()
            _feed_watchdog_started = True
        global _position_ltp_poll_started
        if not _position_ltp_poll_started:
            threading.Thread(target=_open_position_ltp_poll_loop, daemon=True).start()
            _position_ltp_poll_started = True
        if not _critical_rest_fallback_started:
            threading.Thread(target=_critical_symbol_rest_fallback_loop, daemon=True).start()
            _critical_rest_fallback_started = True
        global _live_broker_reconcile_started
        if not _live_broker_reconcile_started:
            threading.Thread(target=_live_broker_reconcile_loop, daemon=True).start()
            _live_broker_reconcile_started = True

        try_refresh_access_token(reason="startup")
        # Deploy-race guard: skip the immediate WS handshake during the boot
        # grace window. The watchdog will pick it up automatically once the
        # window elapses, using its normal backoff/circuit rules. If market
        # is already closed there is no urgency either way.
        boot_wait = _boot_grace_remaining()
        if boot_wait > 0:
            print(
                f"[engine] deferring first WS handshake for {int(boot_wait)}s "
                f"(boot grace to avoid deploy-race 429 from Fyers)"
            )
        elif not start_live_feed_if_ready():
            print("[engine] started without live feed; complete manual Fyers login to enable it")
        print(f"[engine] started in {get_runtime_trading_mode()} mode with {len(LIVE_FEED_SYMBOLS)} live symbols, {len(STRATEGIES)} strategies")
    except Exception as exc:
        with _engine_lock:
            _engine_status.update({"state": "failed", "error": str(exc)})
        print(f"[engine] startup failed: {exc}")
