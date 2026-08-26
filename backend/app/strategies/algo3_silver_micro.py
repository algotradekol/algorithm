"""
algo3_silver_micro.py — Silver Micro (MCX:SILVERMIC*) strategy.

Rewritten 2026-08-19 to match the client's spec doc (Silver Mic_Volume.docx).
The live BUY path uses the finalized 15-minute reference-breakout model.

Rules per spec:
  Instrument:   MCX Silver Micro (SILVERMIC)
  Timeframe:    15 minute candles
  Indicator:    20 EMA on close (no volume filter)

  BUY setup:    a finalized green 15-minute candle closes above EMA20 and its
                close becomes the reference. BUY fires when price crosses
                reference + n. After a BUY SL/TARGET, renewed upward movement
                may re-enter against the same reference until a newer
                qualifying 15-minute candle replaces it.

  SELL setup:   a red candle (close < open) closes below EMA20.
                Its close is stored as the SELL reference close.
                On the NEXT qualifying red candle, compare its forming
                price against the PREVIOUS stored red reference close.
                If price crosses reference - n while that candle is
                forming, SELL triggers immediately at the crossing price.
                When the candle closes, its close becomes the next stored
                sell reference. Green candles in between do not clear the
                stored red reference.

  Reversal:     if a contra trigger fires while a position is open,
                close the current position at LTP and open the new
                one at the same tick.

  Previous-day carry: on warmup the strategy replays enough historical
                15-min candles (10 days) to bring EMA20 and the two
                setup levels up to date, so the very first live tick
                after boot can already fire an entry based on
                yesterday's qualifier.

  Exits:        either fixed target + fixed SL, or a separate profit
                milestone that moves the SL once to breakeven while the
                final target remains active.
"""
from __future__ import annotations

import datetime
import threading
import time
from collections import deque

from .base import Strategy
from ..config import SILVER_MICRO_SYMBOL_OVERRIDE
from ..fyers_client import get_intraday_candle_at, get_intraday_candles_for_range
from ..broker_factory import create_broker
from ..mcx_symbols import get_active_mcx_contract
from ..silver_setup_history import record_setup_event
from ..strategy_settings import get_settings
from ..timezone import IST
from ..trailing_stop import SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN, normalize_silver_exit_mode

EMA_PERIOD = 20
WARMUP_LOOKBACK_DAYS = 10
BUCKET_MINUTES = 15
# FYERS may expose a just-closed historical candle before its final close has
# settled. Waiting briefly prevents a first boundary snapshot from becoming a
# different setup reference than the candle ultimately shown by FYERS.
REST_BAR_SETTLE_SECONDS = 30
SILVER_BUY_PLAN_REFERENCE_BREAKOUT = "reference_breakout"
SILVER_MICRO_ROOT = "SILVERMIC"
# MCX Silver Micro contract size: 1 lot = 1 kg = 1 unit on Fyers.
# Qty is therefore lots * 1. Kept as a constant so it's easy to adjust
# if the exchange ever changes the lot size.
SILVER_MICRO_LOT_SIZE = 1

# Minimum seconds between successful warmups. Prevents the WS watchdog /
# mode-switch loop from re-fetching 7000+ candles every few minutes,
# which was thrashing the algo3 state (setups reset to yesterday's
# values every restart, so today's live bars were repeatedly discarded).
# 5 minutes is short enough to pick up a genuine restart, long enough
# to not run more than 12 times per hour in a worst-case restart storm.
WARMUP_DEBOUNCE_SECONDS = 300
ENTRY_ATTEMPT_COOLDOWN_SECONDS = 180
# Fallback if the MCX symbol-master download fails at boot AND the
# in-memory cache is empty. Client confirmed the front-month contract
# on 2026-08-18 is 31AUGFUT (matches Fyers naming: expiry-day + month).
# Long-term the correct symbol comes from get_active_mcx_contract which
# reads Fyers's live master file and picks the nearest expiry, so this
# constant only matters when both network calls fail.
SILVER_MICRO_SYMBOL = "MCX:SILVERMIC31AUGFUT"


def _resolve_silver_symbol() -> str:
    """Pick the Silver Micro contract to trade.

    Precedence:
      1. SILVER_MICRO_SYMBOL_OVERRIDE env var (client's explicit choice)
      2. Fyers MCX symbol master, nearest expiry (auto-rollover)
      3. Hardcoded SILVER_MICRO_SYMBOL fallback (only if the network fails)

    The env override exists because Fyers's "nearest expiry" pick can
    differ from what the client actually trades — e.g. Fyers lists a
    weekly Silver Micro variant that expires before the monthly contract
    the client uses. On 2026-08-18 the client (Bumba Da) asked for
    31AUGFUT even though Fyers's master listed 26AUGFUT as nearest.
    """
    if SILVER_MICRO_SYMBOL_OVERRIDE:
        print(f"[algo3] using SILVER_MICRO_SYMBOL_OVERRIDE={SILVER_MICRO_SYMBOL_OVERRIDE}")
        return SILVER_MICRO_SYMBOL_OVERRIDE
    try:
        return get_active_mcx_contract(SILVER_MICRO_ROOT)
    except Exception as exc:
        print(f"[algo3] active MCX contract lookup failed ({exc}); using fallback {SILVER_MICRO_SYMBOL}")
        return SILVER_MICRO_SYMBOL


def _ema_step(previous: float | None, value: float, period: int = EMA_PERIOD) -> float:
    k = 2 / (period + 1)
    return float(value) if previous is None else float(value) * k + previous * (1 - k)


def _bucket_start(ts: datetime.datetime, minutes: int = BUCKET_MINUTES) -> datetime.datetime:
    minute = (ts.minute // minutes) * minutes
    return ts.replace(minute=minute, second=0, microsecond=0)


def _latest_closed_minute_cutoff(now: datetime.datetime | None = None) -> datetime.datetime:
    """Return the earliest minute timestamp that is still forming.

    A 1-minute candle stamped with the CURRENT minute (e.g. 19:58 while the
    wall clock is still 19:58:xx IST) is not closed yet and must never be fed
    into the 15m builder, or the 19:45 bucket can finalize before 20:00.
    """
    current = now.astimezone(IST) if now and now.tzinfo is not None else now
    current = current or datetime.datetime.now(IST)
    return current.replace(second=0, microsecond=0, tzinfo=None)


def _is_bucket_closed(
    bucket_start: datetime.datetime,
    now: datetime.datetime | None = None,
    minutes: int = BUCKET_MINUTES,
) -> bool:
    """Return True only when the full bucket window has elapsed.

    Example: the 20:15 bucket is NOT closed until wall-clock time reaches
    20:30 and the first closed minute from that next bucket exists.
    """
    return bucket_start + datetime.timedelta(minutes=minutes) <= _latest_closed_minute_cutoff(now)


def _is_bucket_settled(
    bucket_start: datetime.datetime,
    now: datetime.datetime | None = None,
    minutes: int = BUCKET_MINUTES,
) -> bool:
    """Return True once FYERS has had a short post-close settle window."""
    current = now.astimezone(IST) if now and now.tzinfo is not None else now
    current = current or datetime.datetime.now(IST)
    current = current.replace(tzinfo=None)
    return bucket_start + datetime.timedelta(
        minutes=minutes,
        seconds=REST_BAR_SETTLE_SECONDS,
    ) <= current


def _fmt(value: float | None) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "--"


def _entry_time_iso(value) -> str:
    """Serialize a market event time consistently for broker audit rows."""
    if isinstance(value, datetime.datetime):
        # Engine market timestamps are naive IST. Persist an explicit UTC
        # offset so Supabase and the browser cannot interpret them differently.
        if value.tzinfo is None:
            value = value.replace(tzinfo=IST)
        return value.astimezone(datetime.timezone.utc).isoformat()
    return str(value)


class Algo3SilverMicro(Strategy):
    algo_id = "algo3"
    display_name = "Silver Micro - reference BUY / red-chain SELL"
    _MCX_SESSION_START = "09:00"
    _MCX_SESSION_END = "23:30"
    _MCX_SQUARE_OFF_TIME = "23:25"

    def __init__(self, watchlist: list[str] | None = None):
        # Prefer an explicit override (used by backtest); otherwise resolve
        # the live MCX front-month symbol so we auto-roll as contracts expire.
        self.symbol = (watchlist or [None])[0] if watchlist else _resolve_silver_symbol()
        self.watchlist = [self.symbol] if self.symbol else []
        self.settings = get_settings(self.algo_id)
        self.broker = create_broker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.broker.on_position_closed = self._handle_broker_position_closed

        self._history_lock = threading.Lock()
        self._history_loading = False
        self._history_ready = False
        self._history_error: str | None = None
        self._warmup_minute_candles = 0
        self._last_warmup_at: float | None = None  # monotonic seconds; None = never
        self._last_manual_history_refresh_at: float | None = None

        # 15-min bucket aggregation from 1-min inputs.
        self._minute_buffer: list[dict] = []
        self._current_bucket: datetime.datetime | None = None
        self._last_ingested_minute_at: datetime.datetime | None = None
        self._bars: deque[dict] = deque(maxlen=500)
        self._ema20: float | None = None

        # Stored setup levels — most recent qualifying candle's close.
        # Persist across candles until overwritten by a fresh qualifier.
        self._buy_setup_close: float | None = None
        self._sell_setup_close: float | None = None
        self._buy_setup_bar_at: datetime.datetime | None = None
        self._sell_setup_bar_at: datetime.datetime | None = None

        # Tick-cross detection needs the previous tick's LTP.
        self._prev_ltp: float | None = None

        # Entry markers distinguish successful entries from failed attempts.
        # A failed order consumes that setup so recovery cannot create a retry
        # storm, but a successful setup may re-enter after its position exits.
        # This intentionally allows unlimited same-reference re-entries while
        # the next 15m candle is still forming.
        self._last_fired_buy_bar_at: datetime.datetime | None = None
        self._last_fired_sell_bar_at: datetime.datetime | None = None
        self._last_attempted_buy_bar_at: datetime.datetime | None = None
        self._last_attempted_sell_bar_at: datetime.datetime | None = None
        # A completed SELL may re-enter on the next tick if the carried
        # reference threshold is still crossed. This is deliberately a
        # one-position/one-tick handoff, not a signal-arrow retry loop.
        self._sell_reentry_after_exit: dict | None = None
        self._buy_reentry_after_exit: dict | None = None
        self._entry_attempt_in_flight = False
        self._entry_guard_lock = threading.Lock()
        self._entry_cooldown_until_monotonic = 0.0

        # Diagnostics.
        self._last_tick_at: str | None = None
        self._last_tick_ltp: float | None = None
        self._last_minute_candle_at: str | None = None
        self._last_bar_at: str | None = None

        self.refresh_market_data()

    # ── settings / lifecycle ─────────────────────────────────────────
    def reload_settings(self):
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]
        self.broker.on_position_closed = self._handle_broker_position_closed
        self._buy_reentry_after_exit = None
        self._sell_reentry_after_exit = None

    def on_trading_mode_switched(self, new_mode: str, previous_mode: str | None = None) -> None:
        """Keep warmed Silver references, but clear per-mode entry consumption.

        Paper and live intentionally share the same warmed 15-minute BUY/SELL
        references. What must *not* carry across a mode switch is the memory
        that a given setup already fired in the previous mode, otherwise the
        freshly selected mode inherits a false "already used this candle"
        state and waits for a brand-new setup before it can trade.
        """
        self.broker.on_position_closed = self._handle_broker_position_closed
        self._last_fired_buy_bar_at = None
        self._last_fired_sell_bar_at = None
        self._last_attempted_buy_bar_at = None
        self._last_attempted_sell_bar_at = None
        self._buy_reentry_after_exit = None
        self._sell_reentry_after_exit = None
        self._entry_attempt_in_flight = False
        self._entry_cooldown_until_monotonic = 0.0

        if self._open_position():
            return
        if self._last_tick_ltp is None or float(self._last_tick_ltp) <= 0:
            return

        event_time = datetime.datetime.now(IST).replace(tzinfo=None)
        try:
            self._check_triggers(float(self._last_tick_ltp), event_time=event_time)
        except Exception as exc:
            print(
                f"[algo3] mode-switch recheck failed for "
                f"{previous_mode or 'unknown'} -> {new_mode}: {exc}"
            )

    def _handle_broker_position_closed(
        self,
        *,
        position: dict,
        exit_price: float,
        exit_reason: str,
        exit_time: str | None = None,
    ) -> None:
        """Manual closes should reset handoff state, then re-check live logic.

        Earlier fail-safe behavior could treat a manual flatten as if the
        strategy itself had decided to reverse. For Silver we want the
        opposite: a manual close clears any carried re-entry handoff, then
        allows only a fresh normal trigger to re-open a trade.
        """
        if str(exit_reason or "").upper() not in {"MANUAL_EXIT", "MANUAL_EXTERNAL_EXIT"}:
            return
        self._buy_reentry_after_exit = None
        self._sell_reentry_after_exit = None

        symbol = str(position.get("symbol") or "").strip().upper()
        active_symbol = str(self.symbol or "").strip().upper()
        if not symbol or symbol != active_symbol:
            return

        try:
            ltp = float(exit_price)
        except (TypeError, ValueError):
            return
        if ltp <= 0:
            return
        if self._open_position():
            return

        self._last_tick_ltp = ltp
        if exit_time:
            self._last_tick_at = str(exit_time)

        event_time = None
        if exit_time:
            try:
                event_time = datetime.datetime.fromisoformat(str(exit_time).replace("Z", "+00:00"))
            except ValueError:
                event_time = None
        try:
            self._check_triggers(ltp, event_time=event_time)
        except Exception as exc:
            print(f"[algo3] manual-close recheck failed: {exc}")

    def market_session_start(self) -> str:
        return self._MCX_SESSION_START

    def market_session_end(self) -> str:
        return self._MCX_SESSION_END

    def square_off_time(self) -> str:
        return self._MCX_SQUARE_OFF_TIME

    def scan_enabled(self) -> bool:
        return bool(self.settings.get("scan_enabled", True))

    def refresh_market_data(self, force: bool = False) -> bool:
        """Trigger a background warmup. Debounced by default — a fresh
        warmup within WARMUP_DEBOUNCE_SECONDS of the last successful one
        is a no-op unless `force=True`.

        Debounce matters because the WS watchdog restarts the live feed
        every few minutes on flaky connections. Each restart used to
        wipe today's live-built 15m bars and reload only up-to-yesterday
        from history, which is why the client's BUY setup stayed frozen
        at yesterday's 2,38,000 despite today's 09:00 close at 2,41,104.
        """
        now_monotonic = time.monotonic()
        with self._history_lock:
            if self._history_loading or not self.symbol:
                return False
            if (
                not force
                and self._last_warmup_at is not None
                and (now_monotonic - self._last_warmup_at) < WARMUP_DEBOUNCE_SECONDS
            ):
                remaining = int(WARMUP_DEBOUNCE_SECONDS - (now_monotonic - self._last_warmup_at))
                print(f"[algo3] warmup debounced ({remaining}s remaining); pass force=True to override")
                return False
            self._history_loading = True
        threading.Thread(target=self._load_history_background, daemon=True).start()
        return True

    def request_manual_history_refresh(self) -> tuple[bool, str]:
        """Start one explicitly requested warm-up without touching FYERS WS.

        A manual retry is useful after a transient FYERS history 429, but it
        must not become a refresh storm when several browser tabs are open.
        This guard is intentionally local to the strategy: it protects only
        the Silver history request and never clears tokens or restarts feeds.
        """
        cooldown_seconds = 60
        now_monotonic = time.monotonic()
        with self._history_lock:
            if not self.symbol:
                return False, "Silver symbol is not configured."
            if self._history_loading:
                return False, "Silver history refresh is already running."
            if self._last_manual_history_refresh_at is not None:
                elapsed = now_monotonic - self._last_manual_history_refresh_at
                if elapsed < cooldown_seconds:
                    remaining = max(1, int(cooldown_seconds - elapsed))
                    return False, f"Please wait {remaining}s before requesting Silver history again."

        if not self.refresh_market_data(force=True):
            return False, "Silver history refresh could not be started; another warm-up is already running."

        with self._history_lock:
            self._last_manual_history_refresh_at = now_monotonic
        return True, "Silver history refresh started. Existing references remain in place until FYERS returns fresh candles."

    def _reset_aggregation_state(self):
        """Wipe intra-day aggregation state before a warmup replay.

        Without this, a mid-day warmup would append historical (older)
        candles on top of a live-built _current_bucket, triggering a
        spurious finalize of today's incomplete bucket and mixing old
        history into today's bars. The setups deque + EMA20 are all
        derived state, so wiping and re-computing from scratch is
        cheaper than trying to reconcile.
        """
        self._minute_buffer = []
        self._current_bucket = None
        self._last_ingested_minute_at = None
        self._bars.clear()
        self._ema20 = None
        self._buy_setup_close = None
        self._sell_setup_close = None
        self._buy_setup_bar_at = None
        self._sell_setup_bar_at = None
        self._last_fired_buy_bar_at = None
        self._last_fired_sell_bar_at = None
        self._last_attempted_buy_bar_at = None
        self._last_attempted_sell_bar_at = None
        self._sell_reentry_after_exit = None
        self._buy_reentry_after_exit = None
        self._entry_attempt_in_flight = False
        self._entry_cooldown_until_monotonic = 0.0

    def _silver_buy_plan(self) -> str:
        """Silver BUY always uses the finalized 15-minute EMA reference."""
        return SILVER_BUY_PLAN_REFERENCE_BREAKOUT

    def _load_history_background(self):
        try:
            if not self.symbol:
                return
            # end_date is TODAY (inclusive) so the very first warmup on a
            # mid-day restart replays today's completed 1m candles too.
            # Previously end_date was today - 1, which meant every restart
            # reset the algo to yesterday's setups and today's live bars
            # were repeatedly discarded by later warmups.
            end_date = datetime.date.today()
            start_date = end_date - datetime.timedelta(days=WARMUP_LOOKBACK_DAYS)
            print(f"[algo3] warmup START: symbol={self.symbol} range={start_date} to {end_date}")
            # Keep the exact FYERS error instead of collapsing an expired or
            # throttled session into the misleading "0 candles" state.  A
            # successful OAuth callback forces this warmup again.
            history = get_intraday_candles_for_range(
                self.symbol,
                start_date,
                end_date,
                raise_on_error=True,
            )
            # Preserve the last successful warmup count instead of blindly
            # overwriting with 0 on a transient API failure — the diagnostic
            # panel becomes useless if a later 0-result warmup wipes the
            # earlier "loaded 6090 candles" number even though the deque
            # (self._bars) still has all of them.
            if history:
                # Reset state ONLY when we have data to replace it with;
                # a transient 0-result must NOT wipe good state.
                self._reset_aggregation_state()
                self._warmup_minute_candles = len(history)
                self._history_ready = True
                self._history_error = None
                for candle in history:
                    self._ingest_minute_candle(candle, allow_signals=False)
                self._finalize_bar(allow_signals=False)
                self._last_warmup_at = time.monotonic()
            else:
                # Only stamp 0 if we've never had a successful warmup yet.
                if self._warmup_minute_candles == 0:
                    self._history_error = "history call returned 0 candles"
            last_bar_ts = self._last_bar_at or "(none)"
            buy_ts = self._buy_setup_bar_at.isoformat() if self._buy_setup_bar_at else "(none)"
            sell_ts = self._sell_setup_bar_at.isoformat() if self._sell_setup_bar_at else "(none)"
            print(
                f"[algo3] warmup DONE: {len(history)} 1m candles -> "
                f"{len(self._bars)} 15m bars, last_bar={last_bar_ts}, "
                f"EMA20={_fmt(self._ema20)}, "
                f"buy_setup={_fmt(self._buy_setup_close)}@{buy_ts}, "
                f"sell_setup={_fmt(self._sell_setup_close)}@{sell_ts}"
            )
        except Exception as exc:
            self._history_error = str(exc)
            print(f"[algo3] warmup failed for {self.symbol}: {exc}")
        finally:
            with self._history_lock:
                self._history_loading = False

    # ── engine hooks ─────────────────────────────────────────────────
    def on_tick(self, symbol: str, ltp: float, timestamp):
        if symbol != self.symbol:
            return
        ltp = float(ltp)
        self._last_tick_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._last_tick_ltp = ltp

        if self.scan_enabled():
            self._check_triggers(ltp, event_time=timestamp)

        # Track prev_ltp AFTER using it so the first tick after boot
        # doesn't fire a spurious cross.
        self._prev_ltp = ltp

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if symbol != self.symbol:
            return
        candle_time = candle["time"]
        if candle_time.tzinfo is not None:
            candle_time = candle_time.astimezone(IST).replace(tzinfo=None)
        self._last_minute_candle_at = candle_time.isoformat()
        self._ingest_minute_candle(candle, allow_signals=self.scan_enabled())

    def check_exits(self):
        position = self._open_position()
        if not position:
            return
        ltp = position.get("_last_ltp") or self._last_tick_ltp
        if not ltp:
            return
        ltp = float(ltp)
        # Convert points-based TSL to the % settings the paper broker
        # expects. Computed per-position using the actual entry price
        # so points -> % is honest.
        effective_settings = self._trailing_settings_for(position)
        position = self.broker.apply_trailing_stop(position, ltp, effective_settings)
        side = position["side"]
        sl = float(position["sl_price"])
        target = float(position["target_price"])
        try:
            use_target = self.broker.should_exit_at_target(effective_settings, position)
        except TypeError:
            # Keep older lightweight smoke-test brokers compatible.
            use_target = self.broker.should_exit_at_target(effective_settings)

        if side == "BUY":
            if ltp <= sl:
                exit_reason = self._stop_exit_reason(position)
                self.broker.close_trade(position, ltp, exit_reason)
                self._arm_buy_reentry_after_exit(exit_reason)
            elif use_target and ltp >= target:
                self.broker.close_trade(position, ltp, "TARGET")
                self._arm_buy_reentry_after_exit("TARGET")
        else:
            if ltp >= sl:
                exit_reason = self._stop_exit_reason(position)
                self.broker.close_trade(position, ltp, exit_reason)
                self._arm_sell_reentry_after_exit(exit_reason)
            elif use_target and ltp <= target:
                self.broker.close_trade(position, ltp, "TARGET")
                self._arm_sell_reentry_after_exit("TARGET")

    def _arm_sell_reentry_after_exit(self, exit_reason: str) -> None:
        """Allow a closed SELL to continue the same red-chain move.

        The next tick still has to be a qualifying red-candle downturn at or
        below the carried trigger. The handoff can therefore re-enter
        immediately when the threshold is already crossed, without waiting
        for another full n-point move. Manual, reversal, and EOD exits never
        arm this handoff.
        """
        # Keep the reference alive for either protective exit. After a stop,
        # a qualifying downward tick can re-enter once it is at/below the
        # carried reference - n level; it cannot re-enter above that level.
        if exit_reason not in {"SL", "TRAILING_SL", "TARGET"}:
            return
        if self._sell_setup_close is None or self._sell_setup_bar_at is None:
            self._sell_reentry_after_exit = None
            return
        n = float(self.settings.get("silver_breakout_points", 150) or 150)
        self._sell_reentry_after_exit = {
            "setup_bar_at": self._sell_setup_bar_at,
            "trigger_level": float(self._sell_setup_close) - n,
            "exit_reason": exit_reason,
        }

    def _arm_buy_reentry_after_exit(self, exit_reason: str) -> None:
        """Keep a BUY reference eligible after its protective exit.

        Re-entry is not an unconditional retry: trigger processing still
        requires the next live price to move upward versus the immediately
        preceding tick. This prevents repeated orders on a flat/stale tick
        while allowing a renewed move above the same finalized reference.
        """
        if exit_reason not in {"SL", "TRAILING_SL", "TARGET"}:
            return
        if self._buy_setup_close is None or self._buy_setup_bar_at is None:
            self._buy_reentry_after_exit = None
            return
        n = float(self.settings.get("silver_breakout_points", 150) or 150)
        self._buy_reentry_after_exit = {
            "setup_bar_at": self._buy_setup_bar_at,
            "trigger_level": float(self._buy_setup_close) + n,
            "exit_reason": exit_reason,
        }

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")

    @staticmethod
    def _stop_exit_reason(position: dict) -> str:
        """Preserve whether the effective stop was a trailed stop.

        A point-lock TSL moves the stored SL only after activation. Recording
        that distinction at the source makes paper, live, and UI audit rows
        agree instead of relabeling a profitable trailing exit as a normal SL.
        """
        snapshot = position.get("signal_snapshot") or {}
        trailing = snapshot.get("trailing") if isinstance(snapshot, dict) else None
        if bool(position.get("trailing_sl_active")) or bool(isinstance(trailing, dict) and trailing.get("activated")):
            return "TRAILING_SL"
        return "SL"

    # ── aggregation ──────────────────────────────────────────────────
    def _ingest_minute_candle(self, candle: dict, allow_signals: bool):
        candle_time = candle["time"]
        if candle_time.tzinfo is not None:
            candle_time = candle_time.astimezone(IST).replace(tzinfo=None)

        minute_candle = {
            "time": candle_time,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume") or 0),
        }
        if candle_time >= _latest_closed_minute_cutoff():
            return
        if self._last_ingested_minute_at == candle_time:
            return
        self._last_ingested_minute_at = candle_time
        bucket = _bucket_start(candle_time)

        if self._current_bucket is None:
            self._current_bucket = bucket
            self._minute_buffer = [minute_candle]
            return

        if bucket != self._current_bucket:
            # New 15-min window started — finalize the completed one first.
            self._finalize_bar(allow_signals=allow_signals)
            self._current_bucket = bucket
            self._minute_buffer = [minute_candle]
            return

        self._minute_buffer.append(minute_candle)

    def _finalize_bar(self, allow_signals: bool, require_closed: bool = True):
        if not self._minute_buffer or self._current_bucket is None:
            return
        if require_closed and not _is_bucket_closed(self._current_bucket):
            return
        bar = {
            "time": self._current_bucket,
            "open": self._minute_buffer[0]["open"],
            "high": max(c["high"] for c in self._minute_buffer),
            "low": min(c["low"] for c in self._minute_buffer),
            "close": self._minute_buffer[-1]["close"],
            "volume": sum(c["volume"] for c in self._minute_buffer),
            "minute_count": len(self._minute_buffer),
        }
        bar = self._rest_verify_live_bar(bar, allow_signals=allow_signals)
        if allow_signals and bar.get("source") != "rest_verified_15m":
            # Never make a trade reference from a sparse local aggregation.
            # Exact FYERS 15m OHLC is mandatory before EMA, setup, or
            # persistence can move.
            print(
                f"[algo3] 15m setup SKIPPED: FYERS verification unavailable for "
                f"{self.symbol} @ {bar['time'].isoformat()}; local close "
                f"{bar['close']:.2f} was not used as a reference"
            )
            self._minute_buffer = []
            return
        self._bars.append(bar)
        self._ema20 = _ema_step(self._ema20, bar["close"])
        self._last_bar_at = bar["time"].isoformat()
        self._minute_buffer = []
        # Only log LIVE bars (allow_signals=True). Warmup replays thousands
        # of bars silently; logging each would flood Railway with 6000+ lines
        # per warmup and hide what actually happened live.
        if allow_signals:
            color = "GREEN" if bar["close"] > bar["open"] else ("RED" if bar["close"] < bar["open"] else "DOJI")
            side_of_ema = (
                "above-EMA" if self._ema20 is not None and bar["close"] > self._ema20
                else "below-EMA" if self._ema20 is not None and bar["close"] < self._ema20
                else "at-EMA"
            )
            print(
                f"[algo3] bar closed {bar['time'].isoformat()} "
                f"O={bar['open']:.2f} H={bar['high']:.2f} L={bar['low']:.2f} "
                f"C={bar['close']:.2f} EMA20={_fmt(self._ema20)} "
                f"{color} {side_of_ema} minutes={bar['minute_count']}"
            )
        # Order matters: check the trigger BEFORE updating setups. Otherwise
        # a strongly-directional candle would overwrite its own setup level
        # and then compare against itself, guaranteeing no fire. We want
        # the just-closed bar's CLOSE compared against the PREVIOUS setup
        # level (e.g. yesterday's or an earlier bar's).
        if allow_signals:
            self._check_candle_close_trigger(bar)
        self._update_setups(bar, log=allow_signals)

    def _rest_verify_live_bar(self, bar: dict, allow_signals: bool) -> dict:
        """Prefer FYERS's closed 15m candle over a thin local aggregate.

        Silver's local 15m builder depends on receiving enough 1m candles,
        which can lag or go sparse on MCX. For live setup updates we fetch
        the exact closed 15m candle from FYERS and overwrite the local
        OHLCV if it is available, so BUY/SELL setup levels always come
        from the real closed bar rather than a partial buffer.
        """
        if not allow_signals or not self.symbol:
            return bar
        try:
            authoritative = get_intraday_candle_at(self.symbol, bar["time"], resolution="15")
        except Exception as exc:
            print(
                f"[algo3] 15m REST verify FAILED for {self.symbol} @ {bar['time'].isoformat()}: {exc}; "
                f"using local close {bar['close']:.2f} (minutes={bar['minute_count']})"
            )
            return bar
        if not authoritative:
            print(
                f"[algo3] 15m REST verify MISSING for {self.symbol} @ {bar['time'].isoformat()}; "
                f"using local close {bar['close']:.2f} (minutes={bar['minute_count']})"
            )
            return bar
        if (
            float(authoritative["open"]) != float(bar["open"])
            or float(authoritative["high"]) != float(bar["high"])
            or float(authoritative["low"]) != float(bar["low"])
            or float(authoritative["close"]) != float(bar["close"])
            or float(authoritative["volume"]) != float(bar["volume"])
        ):
            print(
                f"[algo3] 15m REST verify OVERRIDE for {self.symbol} @ {bar['time'].isoformat()}: "
                f"local O={bar['open']:.2f} H={bar['high']:.2f} L={bar['low']:.2f} C={bar['close']:.2f} "
                f"V={bar['volume']:.0f} minutes={bar['minute_count']} -> "
                f"REST O={float(authoritative['open']):.2f} H={float(authoritative['high']):.2f} "
                f"L={float(authoritative['low']):.2f} C={float(authoritative['close']):.2f} "
                f"V={float(authoritative['volume']):.0f}"
            )
        verified = dict(bar)
        verified.update({
            "open": float(authoritative["open"]),
            "high": float(authoritative["high"]),
            "low": float(authoritative["low"]),
            "close": float(authoritative["close"]),
            "volume": float(authoritative["volume"]),
            "source": "rest_verified_15m",
        })
        return verified

    def flush_clock_closed_bar(self, allow_signals: bool | None = None) -> bool:
        """Finalize the current 15m bucket once its time window has elapsed.

        Normally a 15m bucket closes when the first 1m candle of the next
        bucket arrives. On thin MCX stretches that rollover minute can be
        delayed, leaving the previous setup stale even though the chart's
        15m candle has already closed. The scheduler calls this every few
        seconds so a due bucket is finalized by the wall clock, not only by
        next-bucket ingestion.
        """
        if not self._minute_buffer or self._current_bucket is None:
            return False
        if not _is_bucket_closed(self._current_bucket):
            return False
        if allow_signals is None:
            allow_signals = self.scan_enabled()
        if allow_signals and not _is_bucket_settled(self._current_bucket):
            return False
        bars_before = len(self._bars)
        self._finalize_bar(allow_signals=allow_signals, require_closed=True)
        return len(self._bars) > bars_before

    def _update_setups(self, bar: dict, log: bool = False):
        """Per spec: setup level is the CLOSE of the most recent
        qualifying candle. Overwrite on every new qualifier so we always
        track the latest one.

        `log=True` prints when the setup actually moves (only interesting
        for live bars; warmup silently rebuilds hundreds of setups).
        """
        if self._ema20 is None:
            return
        is_green = bar["close"] > bar["open"]
        is_red = bar["close"] < bar["open"]
        close = bar["close"]
        if is_green and close > self._ema20:
            old = self._buy_setup_close
            self._buy_setup_close = close
            self._buy_setup_bar_at = bar["time"]
            # A newer finalized green reference supersedes any re-entry
            # permission attached to the older reference.
            self._buy_reentry_after_exit = None
            if log:
                self._persist_setup_event("BUY", bar, source="live")
            if log:
                print(
                    f"[algo3] BUY setup UPDATED {_fmt(old)} -> {close:.2f} "
                    f"(green close > EMA20 {_fmt(self._ema20)} at {bar['time'].isoformat()})"
                )
        elif is_red and close < self._ema20:
            old = self._sell_setup_close
            self._sell_setup_close = close
            self._sell_setup_bar_at = bar["time"]
            # The finalized red candle is now the new comparison reference;
            # never carry a re-entry permission from the older reference.
            self._sell_reentry_after_exit = None
            if log:
                self._persist_setup_event("SELL", bar, source="live")
            if log:
                print(
                    f"[algo3] SELL setup UPDATED {_fmt(old)} -> {close:.2f} "
                    f"(red close < EMA20 {_fmt(self._ema20)} at {bar['time'].isoformat()})"
                )
        elif log:
            # Bar didn't qualify — say WHY so grepping logs answers "why
            # is my setup stuck?" without diving into code.
            reason = (
                "doji (close==open)" if not is_green and not is_red
                else "green but below EMA20" if is_green
                else "red but above EMA20"
            )
            print(
                f"[algo3] bar did NOT update setup: {reason} "
                f"(close={close:.2f}, EMA20={_fmt(self._ema20)})"
            )

    def _qualifies_as_buy_setup(self, bar: dict) -> bool:
        if self._ema20 is None:
            return False
        close = float(bar.get("close") or 0)
        open_price = float(bar.get("open") or 0)
        return close > open_price and close > self._ema20

    def _qualifies_as_sell_setup(self, bar: dict) -> bool:
        if self._ema20 is None:
            return False
        close = float(bar.get("close") or 0)
        open_price = float(bar.get("open") or 0)
        return close < open_price and close < self._ema20

    def _persist_setup_event(
        self,
        side: str,
        bar: dict,
        source: str,
        ema20_override: float | None = None,
    ) -> None:
        if not self.symbol:
            return
        close = float(bar.get("close") or 0)
        open_price = float(bar.get("open") or 0)
        is_green = close > open_price
        is_red = close < open_price
        ema20 = self._ema20 if ema20_override is None else ema20_override
        if ema20 is None:
            return
        if side == "BUY" and not (is_green and close > ema20):
            print(
                f"[algo3] setup history SKIPPED for BUY: non-qualifying candle "
                f"O={open_price:.2f} C={close:.2f} EMA20={_fmt(ema20)}"
            )
            return
        if side == "SELL" and not (is_red and close < ema20):
            print(
                f"[algo3] setup history SKIPPED for SELL: non-qualifying candle "
                f"O={open_price:.2f} C={close:.2f} EMA20={_fmt(ema20)}"
            )
            return
        record_setup_event(
            algo_id=self.algo_id,
            symbol=self.symbol,
            side=side,
            bar=bar,
            ema20=ema20,
            breakout_points=float(self.settings.get("silver_breakout_points", 150) or 150),
            source=source,
        )

    # ── tick-based trigger detection ─────────────────────────────────
    def _already_fired_this_setup(self, side: str, setup_bar_at: datetime.datetime | None = None) -> bool:
        """True if we've already fired an entry for the current stored setup.

        Keyed on the setup bar's timestamp: a fresh qualifying candle
        overwrites _buy_setup_bar_at / _sell_setup_bar_at, which re-arms
        the side. If the setup was injected manually (tests) without a
        bar_at, the guard is skipped so classic tick-cross logic still
        works.
        """
        if setup_bar_at is None:
            setup_bar_at = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        if side == "BUY":
            return (
                setup_bar_at is not None
                and self._last_fired_buy_bar_at == setup_bar_at
            )
        return (
            setup_bar_at is not None
            and self._last_fired_sell_bar_at == setup_bar_at
        )

    def _already_attempted_this_setup(self, side: str, setup_bar_at: datetime.datetime | None = None) -> bool:
        if setup_bar_at is None:
            setup_bar_at = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        if side == "BUY":
            return (
                setup_bar_at is not None
                and self._last_attempted_buy_bar_at == setup_bar_at
            )
        return (
            setup_bar_at is not None
            and self._last_attempted_sell_bar_at == setup_bar_at
        )

    def _already_consumed_this_setup(self, side: str, setup_bar_at: datetime.datetime | None = None) -> bool:
        return self._already_attempted_this_setup(side, setup_bar_at) or self._already_fired_this_setup(side, setup_bar_at)

    def _failed_attempt_blocks_setup(self, side: str, setup_bar_at: datetime.datetime | None = None) -> bool:
        """Block only a setup whose previous order attempt failed.

        A successful entry is allowed to fire again after its position is
        gone. The broker/open-position check remains the protection against
        duplicate same-side positions while the first trade is still open.
        """
        if setup_bar_at is None:
            setup_bar_at = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        if setup_bar_at is None:
            return False
        if side == "BUY":
            return self._last_attempted_buy_bar_at == setup_bar_at and self._last_fired_buy_bar_at != setup_bar_at
        return self._last_attempted_sell_bar_at == setup_bar_at and self._last_fired_sell_bar_at != setup_bar_at

    def _mark_fired(self, side: str, setup_bar_at: datetime.datetime | None = None) -> None:
        if setup_bar_at is None:
            setup_bar_at = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        if side == "BUY":
            self._last_fired_buy_bar_at = setup_bar_at
        else:
            self._last_fired_sell_bar_at = setup_bar_at

    def _mark_attempted(self, side: str, setup_bar_at: datetime.datetime | None = None) -> None:
        if setup_bar_at is None:
            setup_bar_at = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        if side == "BUY":
            self._last_attempted_buy_bar_at = setup_bar_at
        else:
            self._last_attempted_sell_bar_at = setup_bar_at
        if not self._is_live_broker():
            return
        cooldown = float(self.settings.get("silver_entry_cooldown_seconds", ENTRY_ATTEMPT_COOLDOWN_SECONDS) or 0)
        self._entry_cooldown_until_monotonic = max(
            self._entry_cooldown_until_monotonic,
            time.monotonic() + max(0.0, cooldown),
        )

    def _entry_cooldown_remaining(self) -> float:
        return max(0.0, self._entry_cooldown_until_monotonic - time.monotonic())

    def _is_live_broker(self) -> bool:
        return (
            getattr(self.broker, "_algo3_treat_as_live", False)
            or self.broker.__class__.__name__ == "LiveBroker"
        )

    def _live_broker_symbol_busy(self, current_position: dict | None = None) -> bool:
        """Best-effort live broker guard against duplicate Silver entries.

        If the app has no local open Silver position but Fyers still reports
        an open/pending symbol state, fail closed and skip a fresh entry.
        This prevents retry storms after partial entry/protection failures.
        """
        if not self._is_live_broker() or current_position is not None:
            return False
        try:
            from ..fyers_client import get_broker_orders, get_broker_positions

            positions_result = get_broker_positions("live")
            if not positions_result.get("available"):
                print(
                    f"[algo3] entry SKIPPED: live positions unavailable for broker guard "
                    f"({positions_result.get('warning') or 'unknown error'})"
                )
                return True
            for row in positions_result.get("positions", []):
                if row.get("symbol") == self.symbol:
                    print(f"[algo3] entry SKIPPED: broker already reports open {self.symbol} position")
                    return True

            orders_result = get_broker_orders("live")
            if not orders_result.get("available"):
                print(
                    f"[algo3] entry SKIPPED: live orders unavailable for broker guard "
                    f"({orders_result.get('warning') or 'unknown error'})"
                )
                return True
            # Fyers status codes: 1=CANCELLED, 2=FILLED/TRADED, 3=REJECTED
            # (some SDK versions), 4=TRANSIT, 5=REJECTED, 6=PENDING. Only
            # TRANSIT (4) and PENDING (6) actually reserve capacity — a
            # FILLED order from earlier today is HISTORY, not a live
            # blocker. Prior code filtered by symbol only and treated
            # today's already-filled rows as pending, blocking every
            # subsequent re-entry (client incident 2026-08-26 12:00 IST:
            # SELL trigger fired, blocked on a status=2 row from the
            # morning BUY that had long since filled + closed).
            _PENDING_STATUSES = {4, 6}
            for row in orders_result.get("orders", []):
                if row.get("symbol") != self.symbol:
                    continue
                try:
                    status_int = int(row.get("status")) if row.get("status") is not None else None
                except (TypeError, ValueError):
                    status_int = None
                if status_int not in _PENDING_STATUSES:
                    continue
                print(
                    f"[algo3] entry SKIPPED: broker already has pending {self.symbol} "
                    f"{row.get('side')} order (status={status_int})"
                )
                return True
            return False
        except Exception as exc:
            print(f"[algo3] entry SKIPPED: live broker guard failed for {self.symbol}: {exc}")
            return True

    def _check_triggers(self, ltp: float, event_time=None):
        n = float(self.settings.get("silver_breakout_points", 150))
        if n <= 0:
            return

        # Current red-chain behavior: keep the previous confirmed red close
        # as the anchor, then enter as soon as the next forming red 15m candle
        # crosses anchor - n. This must run from ticks, not from the candle's
        # eventual close, otherwise a fast move can give away the entry.
        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None
        opening_sell_gap = bool(
            sell_level is not None
            and self._sell_setup_bar_at is not None
            and ltp <= sell_level
            and self._is_opening_gap_from_prior_session(event_time, self._sell_setup_bar_at)
        )
        sell_reentry = self._sell_reentry_after_exit
        same_sell_reference = bool(
            sell_reentry
            and sell_level is not None
            and self._sell_setup_bar_at is not None
            and sell_reentry.get("setup_bar_at") == self._sell_setup_bar_at
            and abs(float(sell_reentry.get("trigger_level") or 0) - float(sell_level)) < 1e-9
        )
        same_reference_downturn = bool(
            same_sell_reference
            and self._prev_ltp is not None
            and ltp <= sell_level
            and ltp < float(self._prev_ltp)
        )
        current_bucket_open = (
            float(self._minute_buffer[0]["open"])
            if self._minute_buffer and self._current_bucket is not None
            else None
        )
        current_sell_candle = (
            sell_level is not None
            and self._ema20 is not None
            and self._sell_setup_bar_at is not None
            and self._current_bucket is not None
            and self._current_bucket > self._sell_setup_bar_at
            and current_bucket_open is not None
            and ltp < current_bucket_open
            and ltp < self._ema20
        )
        if (
            (opening_sell_gap or current_sell_candle)
            and (
                opening_sell_gap
                or (
                (
                    ltp <= sell_level
                    and (self._prev_ltp is None or self._prev_ltp > sell_level)
                )
                or same_reference_downturn
                )
            )
            and not self._failed_attempt_blocks_setup("SELL", self._sell_setup_bar_at)
        ):
            print(
                f"[algo3] TRIGGER SELL ({'opening gap' if opening_sell_gap else 'red-chain tick-cross'}): previous red reference "
                f"{self._sell_setup_close:.2f} - n={n:.0f} = level {sell_level:.2f}; "
                f"forming red candle LTP={ltp:.2f}"
            )
            if self._fire_entry(
                "SELL",
                ltp,
                sell_level,
                setup_bar_at_override=self._sell_setup_bar_at,
                event_time=event_time,
            ):
                self._sell_reentry_after_exit = None
                self._mark_fired("SELL", setup_bar_at=self._sell_setup_bar_at)

        buy_level = self._buy_setup_close + n if self._buy_setup_close is not None else None
        prev = self._prev_ltp
        buy_reentry = getattr(self, "_buy_reentry_after_exit", None)
        same_buy_reference = bool(
            buy_reentry
            and buy_level is not None
            and self._buy_setup_bar_at is not None
            and buy_reentry.get("setup_bar_at") == self._buy_setup_bar_at
            and abs(float(buy_reentry.get("trigger_level") or 0) - float(buy_level)) < 1e-9
        )
        renewed_buy_move = bool(same_buy_reference and prev is not None and ltp > float(prev))

        # Gap-through case: first live tick after warmup (or after a
        # fresh setup) arrives with LTP ALREADY past the trigger. Client's
        # ask (2026-08-20): fire immediately instead of waiting for a
        # downward tick to re-cross upward. Only runs when we have a
        # real setup_bar_at so failed attempts can be tied to the correct
        # setup candle.
        if (
            buy_level is not None
            and ltp >= buy_level
            and self._buy_setup_bar_at is not None
            and (not same_buy_reference or renewed_buy_move)
            and (self._prev_ltp is not None or self._is_opening_gap_from_prior_session(event_time, self._buy_setup_bar_at))
            and not self._failed_attempt_blocks_setup("BUY")
        ):
            print(f"[algo3] TRIGGER BUY (gap-through): LTP {ltp:.2f} >= level {buy_level:.2f} (setup {self._buy_setup_close:.2f} + n={n:.0f})")
            if self._fire_entry("BUY", ltp, buy_level, event_time=event_time):
                self._buy_reentry_after_exit = None
                self._mark_fired("BUY")
                return

        # A normal intraday tick still needs the usual crossing path. This is
        # separate from the deliberately narrow prior-session opening-gap path.
        if prev is None:
            return
        if (
            buy_level is not None
            and ((prev < buy_level <= ltp) or (same_buy_reference and renewed_buy_move and ltp >= buy_level))
            and not self._failed_attempt_blocks_setup("BUY")
        ):
            print(f"[algo3] TRIGGER BUY (tick-cross): prev {prev:.2f} -> LTP {ltp:.2f} crossed level {buy_level:.2f}")
            if self._fire_entry("BUY", ltp, buy_level, event_time=event_time):
                self._buy_reentry_after_exit = None
                self._mark_fired("BUY")
                return

    @staticmethod
    def _is_opening_gap_from_prior_session(event_time, setup_bar_at: datetime.datetime) -> bool:
        """Allow the first 09:00-09:14 IST market window to use a carried setup.

        Restarting a feed later in the day must not convert a stale first tick
        into a fake gap entry. This exception is intentionally limited to a
        prior-session setup during the real opening 15-minute window.
        ``None`` remains accepted for lightweight legacy smoke doubles.
        """
        if event_time is None:
            return True
        if not isinstance(event_time, datetime.datetime):
            return False
        local = event_time.astimezone(IST).replace(tzinfo=None) if event_time.tzinfo else event_time
        start = local.replace(hour=9, minute=0, second=0, microsecond=0)
        return setup_bar_at.date() < local.date() and start <= local < start + datetime.timedelta(minutes=15)
    def _check_candle_close_trigger(self, bar: dict):
        """Fallback trigger check on every completed 15m bar.

        Client's ask (2026-08-20): if a candle closes past the trigger
        level (e.g. 09:00 opens at 240K, closes 241K while BUY trigger
        was 238.2K), fire the entry at that close price. Handles the
        case where live ticks were sparse or WS dropped and the tick
        crossing never registered.
        """
        n = float(self.settings.get("silver_breakout_points", 150))
        if n <= 0:
            return
        close = float(bar["close"])
        bar_at = bar["time"]
        buy_qualifies = self._qualifies_as_buy_setup(bar)
        sell_qualifies = self._qualifies_as_sell_setup(bar)

        buy_level = self._buy_setup_close + n if self._buy_setup_close is not None else None
        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None
        buy_setup_identity = bar_at if buy_qualifies else self._buy_setup_bar_at
        # A red-chain entry is anchored to the PREVIOUS red setup. The
        # current qualifying bar is the candle that crossed that anchor.
        sell_setup_identity = self._sell_setup_bar_at if sell_qualifies else None

        if (
            buy_level is not None
            and close >= buy_level
            and buy_setup_identity is not None
            and not self._failed_attempt_blocks_setup("BUY", buy_setup_identity)
        ):
            print(f"[algo3] TRIGGER BUY (candle-close): bar close {close:.2f} >= level {buy_level:.2f} (setup {self._buy_setup_close:.2f} + n={n:.0f})")
            if self._fire_entry("BUY", close, buy_level, setup_bar_at_override=buy_setup_identity):
                self._buy_reentry_after_exit = None
                self._mark_fired("BUY", setup_bar_at=buy_setup_identity)
                return
        if (
            sell_level is not None
            and sell_qualifies
            and float(bar.get("low") or close) <= sell_level
            and sell_setup_identity is not None
            and not self._failed_attempt_blocks_setup("SELL", sell_setup_identity)
            and not self._already_fired_this_setup("SELL", sell_setup_identity)
        ):
            print(
                f"[algo3] TRIGGER SELL (red-chain candle fallback): qualifying red low "
                f"{float(bar.get('low') or close):.2f} "
                f"<= previous red reference {self._sell_setup_close:.2f} - n={n:.0f} "
                f"(level {sell_level:.2f})"
            )
            if self._fire_entry("SELL", sell_level, sell_level, setup_bar_at_override=sell_setup_identity):
                self._mark_fired("SELL", setup_bar_at=sell_setup_identity)

    def _fire_entry(
        self,
        side: str,
        ltp: float,
        trigger_level: float,
        setup_bar_at_override: datetime.datetime | None = None,
        event_time=None,
    ) -> bool:
        # Never submit an entry on the wrong side of its breakout level. This
        # is especially important for SELL re-entry after an exit: a renewed
        # downturn must already be at/below reference - n, not merely lower
        # than the previous tick while still above the trigger.
        if side == "SELL" and float(ltp) > float(trigger_level):
            print(
                f"[algo3] entry SKIPPED for SELL: LTP {float(ltp):.2f} "
                f"is above trigger {float(trigger_level):.2f}"
            )
            return False
        if side == "BUY" and float(ltp) < float(trigger_level):
            print(
                f"[algo3] entry SKIPPED for BUY: LTP {float(ltp):.2f} "
                f"is below trigger {float(trigger_level):.2f}"
            )
            return False
        current = self._open_position()
        if current and current["side"] == side:
            # Log so "trigger fired but no entry" is answerable from logs.
            print(f"[algo3] entry SKIPPED for {side}: already positioned same-side (qty={current.get('qty')})")
            return False
        with self._entry_guard_lock:
            if self._entry_attempt_in_flight:
                print(f"[algo3] entry SKIPPED for {side}: another Silver entry attempt is already in flight")
                return False
            if self._failed_attempt_blocks_setup(side, setup_bar_at_override):
                print(f"[algo3] entry SKIPPED for {side}: previous order attempt failed for this setup")
                return False
            if self._live_broker_symbol_busy(current):
                self._mark_attempted(side, setup_bar_at_override)
                return False
            cooldown_left = self._entry_cooldown_remaining()
            if cooldown_left > 0:
                print(f"[algo3] entry SKIPPED for {side}: cooldown active ({cooldown_left:.0f}s remaining)")
                return False
            self._mark_attempted(side, setup_bar_at_override)
            self._entry_attempt_in_flight = True
        try:
            if current and current["side"] != side:
                print(f"[algo3] REVERSAL: closing existing {current['side']} at {ltp:.2f} before opening {side}")
                self.broker.close_trade(current, ltp, "REVERSAL_CONTRA_SIGNAL")

            entered = self._enter(side, ltp, trigger_level, event_time=event_time)
            if entered:
                # A successful entry should not inherit the failed-attempt
                # cooldown when its position exits and the same reference is
                # legitimately crossed again during this 15m window.
                with self._entry_guard_lock:
                    self._entry_cooldown_until_monotonic = 0.0
            return entered
        finally:
            with self._entry_guard_lock:
                self._entry_attempt_in_flight = False

    def _enter(self, side: str, entry_price: float, trigger_level: float, event_time=None) -> bool:
        if not self.symbol or not entry_price:
            return False
        # Silver Micro is sized in LOTS, not by dividing capital by price.
        # 1 lot = 1 kg = SILVER_MICRO_LOT_SIZE units. Client trades whole
        # lots (default 1). Capital-per-trade is retained for UI preview
        # only; the algo does not divide by it.
        lots = int(self.settings.get("silver_lots", 1) or 1)
        qty = max(1, lots) * SILVER_MICRO_LOT_SIZE
        if qty < 1:
            return False

        sl_pts = float(self.settings.get("sl_points", 200))
        target_pts = float(self.settings.get("target_points", 2000))
        activation_pts = float(self.settings.get("tsl_activate_points", 500))
        if side == "BUY":
            sl_price = float(entry_price) - sl_pts
            target_price = float(entry_price) + target_pts
            activation_price = float(entry_price) + activation_pts
        else:
            sl_price = float(entry_price) + sl_pts
            target_price = float(entry_price) - target_pts
            activation_price = float(entry_price) - activation_pts

        trigger = self._entry_trigger(side, entry_price, trigger_level)
        snapshot = self._signal_snapshot(side, entry_price, trigger_level)
        snapshot["silver_exit_policy"] = normalize_silver_exit_mode(
            self.settings.get("exit_mode")
        )
        if snapshot["silver_exit_policy"] == SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN:
            snapshot["silver_breakeven"] = {
                "armed": False,
                "activation_price": activation_price,
                "activation_points": activation_pts,
                "target_price": target_price,
                "final_target_enabled": True,
                "initial_sl_price": sl_price,
            }
        try:
            open_trade_args = (
                self.symbol, side, qty, float(entry_price),
                sl_price, target_price, trigger, snapshot,
            )
            if event_time is None:
                self.broker.open_trade(*open_trade_args)
            else:
                try:
                    self.broker.open_trade(*open_trade_args, entry_time=_entry_time_iso(event_time))
                except TypeError as exc:
                    # Keep lightweight test/dummy brokers compatible while
                    # production PaperBroker/LiveBroker accept the timestamp.
                    if "entry_time" not in str(exc):
                        raise
                    self.broker.open_trade(*open_trade_args)
            print(
                f"[algo3] ENTERED {side} {self.symbol} @ {entry_price:.2f} "
                f"qty={qty} (trigger {_fmt(trigger_level)}, "
                f"sl={_fmt(sl_price)}, activation={_fmt(activation_price)}, "
                f"final_target={_fmt(target_price)})"
            )
            return True
        except Exception as exc:
            print(f"[algo3] entry failed for {self.symbol}: {exc}")
            return False

    def _entry_trigger(self, side: str, entry_price: float, trigger_level: float) -> str:
        n = self.settings.get("silver_breakout_points", 150)
        setup_close = self._buy_setup_close if side == "BUY" else self._sell_setup_close
        if side == "BUY":
            return (
                f"15m silver buy reference breakout on {self.symbol}: green setup close "
                f"{_fmt(setup_close)} + n={n} = trigger {_fmt(trigger_level)}, "
                f"LTP crossed upward through trigger {_fmt(trigger_level)}; "
                f"order submitted at {entry_price:.2f}. "
                f"EMA20 {_fmt(self._ema20)}."
            )
        return (
            f"15m silver sell red-chain on {self.symbol}: previous red reference close "
            f"{_fmt(setup_close)} - n={n} = trigger {_fmt(trigger_level)}, "
            f"forming qualifying red candle crossed downward through trigger "
            f"{_fmt(trigger_level)}; order submitted at {entry_price:.2f}. "
            f"EMA20 {_fmt(self._ema20)}."
        )

    def _signal_snapshot(self, side: str, entry_price: float, trigger_level: float) -> dict:
        snapshot = {
            "symbol": self.symbol,
            "timeframe": "15m",
            "side": side,
            "entry_ltp": entry_price,
            "trigger_level": trigger_level,
            "n_points": self.settings.get("silver_breakout_points", 150),
            "ema20": self._ema20,
            "silver_buy_plan": self._silver_buy_plan(),
            "buy_setup_close": self._buy_setup_close,
            "sell_setup_close": self._sell_setup_close,
        }
        if side == "BUY":
            snapshot.update({
                "buy_plan": SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
                "buy_setup_time": self._buy_setup_bar_at.isoformat() if self._buy_setup_bar_at else None,
                "buy_reference_close": self._buy_setup_close,
                "buy_price_ema20": self._ema20,
            })
        return snapshot

    def _open_position(self) -> dict | None:
        for position in self.broker.open_positions():
            if position.get("symbol") == self.symbol:
                return position
        return None

    # ── points → percent conversion for the shared trailing engine ───
    def _trailing_settings_for(self, position: dict) -> dict:
        merged = dict(self.settings)
        # PaperBroker and LiveBroker share the same point-lock arithmetic for
        # Silver. Keep the values in points; converting to percentages loses
        # the breakeven/step-lock semantics and creates live/backtest drift.
        if "tsl_activate_points" not in merged:
            merged["tsl_activate_points"] = merged.get("tsl_trigger_points", 0)
        if "tsl_profit_step_points" not in merged:
            merged["tsl_profit_step_points"] = merged.get("tsl_activate_points", 0)
        if "tsl_lock_step_points" not in merged:
            merged["tsl_lock_step_points"] = merged.get("tsl_distance_points", 0)
        return merged

    # ── diagnostics ──────────────────────────────────────────────────
    def feed_status(self) -> dict:
        self.flush_clock_closed_bar()
        return {
            "algo_id": self.algo_id,
            "display_name": self.display_name,
            "symbol": self.symbol,
            "history_ready": self._history_ready,
            "history_loading": self._history_loading,
            "history_error": self._history_error,
            "last_manual_history_refresh_at": self._last_manual_history_refresh_at,
            "warmup_minute_candles": self._warmup_minute_candles,
            "bars_15m": len(self._bars),
            "minute_buffer_count": len(self._minute_buffer),
            "current_bucket": self._current_bucket.isoformat() if self._current_bucket else None,
            "ema20": self._ema20,
            "silver_buy_plan": self._silver_buy_plan(),
            "buy_setup_close": self._buy_setup_close,
            "sell_setup_close": self._sell_setup_close,
            "buy_setup_bar_at": self._buy_setup_bar_at.isoformat() if self._buy_setup_bar_at else None,
            "sell_setup_bar_at": self._sell_setup_bar_at.isoformat() if self._sell_setup_bar_at else None,
            "n_points": self.settings.get("silver_breakout_points", 150),
            "last_tick_at": self._last_tick_at,
            "last_tick_ltp": self._last_tick_ltp,
            "last_minute_candle_at": self._last_minute_candle_at,
            "last_bar_at": self._last_bar_at,
            "silver_lots": int(self.settings.get("silver_lots", 1) or 1),
            "last_fired_buy_bar_at": self._last_fired_buy_bar_at.isoformat() if self._last_fired_buy_bar_at else None,
            "last_fired_sell_bar_at": self._last_fired_sell_bar_at.isoformat() if self._last_fired_sell_bar_at else None,
            "last_attempted_buy_bar_at": self._last_attempted_buy_bar_at.isoformat() if self._last_attempted_buy_bar_at else None,
            "last_attempted_sell_bar_at": self._last_attempted_sell_bar_at.isoformat() if self._last_attempted_sell_bar_at else None,
            "entry_attempt_in_flight": self._entry_attempt_in_flight,
            "entry_cooldown_remaining_seconds": int(self._entry_cooldown_remaining()),
        }
