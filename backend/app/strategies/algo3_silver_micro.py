"""
algo3_silver_micro.py — Silver Micro (MCX:SILVERMIC*) strategy.

Rewritten 2026-08-19 to match the client's spec doc (Silver Mic_Volume.docx).
The previous 5-minute EMA+volume version has been retired.

Rules per spec:
  Instrument:   MCX Silver Micro (SILVERMIC)
  Timeframe:    15 minute candles
  Indicator:    20 EMA on close (no volume filter)

  BUY setup:    a green candle (close > open) closes above EMA20.
                Its close is stored as the BUY setup level. Each new
                qualifying candle OVERWRITES the stored level so it
                always reflects the most recent qualifier.
  BUY trigger:  live LTP crosses (setup_close + n) in an UPWARD
                direction. Default n = 150 points; configurable via
                settings["silver_breakout_points"].

  SELL setup:   a red candle (close < open) closes below EMA20.
                Its close is stored / overwritten same way.
  SELL trigger: live LTP crosses (setup_close - n) in a DOWNWARD
                direction.

  Reversal:     if a contra trigger fires while a position is open,
                close the current position at LTP and open the new
                one at the same tick.

  Previous-day carry: on warmup the strategy replays enough historical
                15-min candles (10 days) to bring EMA20 and the two
                setup levels up to date, so the very first live tick
                after boot can already fire an entry based on
                yesterday's qualifier.

  Exits:        fixed SL and target in POINTS from entry, plus optional
                points-based trailing SL (activate at N points profit,
                trail N points behind the extremum since activation).
                The paper broker's trailing engine speaks percentages,
                so we pass an on-the-fly settings dict with the point
                values converted at the position's entry price.
"""
from __future__ import annotations

import datetime
import threading
from collections import deque

from .base import Strategy
from ..fyers_client import get_intraday_candles_for_range
from ..broker_factory import create_broker
from ..mcx_symbols import get_active_mcx_contract
from ..strategy_settings import get_settings
from ..timezone import IST

EMA_PERIOD = 20
WARMUP_LOOKBACK_DAYS = 10
BUCKET_MINUTES = 15
SILVER_MICRO_ROOT = "SILVERMIC"
# Fallback if the MCX symbol-master download fails at boot AND the
# in-memory cache is empty. Client confirmed the front-month contract
# on 2026-08-18 is 31AUGFUT (matches Fyers naming: expiry-day + month).
# Long-term the correct symbol comes from get_active_mcx_contract which
# reads Fyers's live master file and picks the nearest expiry, so this
# constant only matters when both network calls fail.
SILVER_MICRO_SYMBOL = "MCX:SILVERMIC31AUGFUT"


def _resolve_silver_symbol() -> str:
    """Ask Fyers's MCX symbol master for the current front-month Silver
    Micro contract so the strategy auto-rolls each month. Falls back to
    the hardcoded SILVER_MICRO_SYMBOL only if the network fails."""
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


def _fmt(value: float | None) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "--"


class Algo3SilverMicro(Strategy):
    algo_id = "algo3"
    display_name = "Silver Micro - 15m EMA breakout"

    def __init__(self, watchlist: list[str] | None = None):
        # Prefer an explicit override (used by backtest); otherwise resolve
        # the live MCX front-month symbol so we auto-roll as contracts expire.
        self.symbol = (watchlist or [None])[0] if watchlist else _resolve_silver_symbol()
        self.watchlist = [self.symbol] if self.symbol else []
        self.settings = get_settings(self.algo_id)
        self.broker = create_broker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])

        self._history_lock = threading.Lock()
        self._history_loading = False
        self._history_ready = False
        self._history_error: str | None = None
        self._warmup_minute_candles = 0

        # 15-min bucket aggregation from 1-min inputs.
        self._minute_buffer: list[dict] = []
        self._current_bucket: datetime.datetime | None = None
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

    def scan_enabled(self) -> bool:
        return bool(self.settings.get("scan_enabled", True))

    def refresh_market_data(self):
        with self._history_lock:
            if self._history_loading or not self.symbol:
                return
            self._history_loading = True
        threading.Thread(target=self._load_history_background, daemon=True).start()

    def _load_history_background(self):
        try:
            if not self.symbol:
                return
            end_date = datetime.date.today() - datetime.timedelta(days=1)
            start_date = end_date - datetime.timedelta(days=WARMUP_LOOKBACK_DAYS)
            history = get_intraday_candles_for_range(self.symbol, start_date, end_date)
            # Preserve the last successful warmup count instead of blindly
            # overwriting with 0 on a transient API failure — the diagnostic
            # panel becomes useless if a later 0-result warmup wipes the
            # earlier "loaded 6090 candles" number even though the deque
            # (self._bars) still has all of them.
            if history:
                self._warmup_minute_candles = len(history)
                self._history_ready = True
                self._history_error = None
                for candle in history:
                    self._ingest_minute_candle(candle, allow_signals=False)
                self._finalize_bar(allow_signals=False)
            else:
                # Only stamp 0 if we've never had a successful warmup yet.
                if self._warmup_minute_candles == 0:
                    self._history_error = "history call returned 0 candles"
            print(
                f"[algo3] warmup loaded {len(history)} 1m candles → "
                f"{len(self._bars)} 15m bars, EMA20={_fmt(self._ema20)}, "
                f"buy_setup={_fmt(self._buy_setup_close)}, "
                f"sell_setup={_fmt(self._sell_setup_close)}"
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
            self._check_triggers(ltp)

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
        use_target = self.broker.should_exit_at_target(effective_settings)

        if side == "BUY":
            if ltp <= sl:
                self.broker.close_trade(position, ltp, "SL")
            elif use_target and ltp >= target:
                self.broker.close_trade(position, ltp, "TARGET")
        else:
            if ltp >= sl:
                self.broker.close_trade(position, ltp, "SL")
            elif use_target and ltp <= target:
                self.broker.close_trade(position, ltp, "TARGET")

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")

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

    def _finalize_bar(self, allow_signals: bool):
        if not self._minute_buffer or self._current_bucket is None:
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
        self._bars.append(bar)
        self._ema20 = _ema_step(self._ema20, bar["close"])
        self._last_bar_at = bar["time"].isoformat()
        self._minute_buffer = []
        self._update_setups(bar)

    def _update_setups(self, bar: dict):
        """Per spec: setup level is the CLOSE of the most recent
        qualifying candle. Overwrite on every new qualifier so we always
        track the latest one."""
        if self._ema20 is None:
            return
        is_green = bar["close"] > bar["open"]
        is_red = bar["close"] < bar["open"]
        close = bar["close"]
        if is_green and close > self._ema20:
            self._buy_setup_close = close
            self._buy_setup_bar_at = bar["time"]
        elif is_red and close < self._ema20:
            self._sell_setup_close = close
            self._sell_setup_bar_at = bar["time"]

    # ── tick-based trigger detection ─────────────────────────────────
    def _check_triggers(self, ltp: float):
        prev = self._prev_ltp
        if prev is None:
            return  # first tick — no cross to detect

        n = float(self.settings.get("silver_breakout_points", 150))
        if n <= 0:
            return

        buy_level = self._buy_setup_close + n if self._buy_setup_close is not None else None
        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None

        # Upward cross for BUY: previous below level, current at/above.
        if buy_level is not None and prev < buy_level <= ltp:
            self._fire_entry("BUY", ltp, buy_level)
            return
        # Downward cross for SELL: previous above level, current at/below.
        if sell_level is not None and prev > sell_level >= ltp:
            self._fire_entry("SELL", ltp, sell_level)

    def _fire_entry(self, side: str, ltp: float, trigger_level: float):
        current = self._open_position()
        if current and current["side"] == side:
            return  # already positioned same-side; nothing to do
        if current and current["side"] != side:
            # Reversal: close existing at current LTP, then open contra.
            self.broker.close_trade(current, ltp, "REVERSAL_CONTRA_SIGNAL")

        self._enter(side, ltp, trigger_level)

    def _enter(self, side: str, entry_price: float, trigger_level: float) -> bool:
        if not self.symbol or not entry_price:
            return False
        qty = int(self.settings["capital_per_trade"] // float(entry_price))
        if qty < 1:
            return False

        sl_pts = float(self.settings.get("sl_points", 100))
        target_pts = float(self.settings.get("target_points", 300))
        if side == "BUY":
            sl_price = float(entry_price) - sl_pts
            target_price = float(entry_price) + target_pts
        else:
            sl_price = float(entry_price) + sl_pts
            target_price = float(entry_price) - target_pts

        trigger = self._entry_trigger(side, entry_price, trigger_level)
        snapshot = self._signal_snapshot(side, entry_price, trigger_level)
        try:
            self.broker.open_trade(
                self.symbol, side, qty, float(entry_price),
                sl_price, target_price, trigger, snapshot,
            )
            print(
                f"[algo3] ENTERED {side} {self.symbol} @ {entry_price:.2f} "
                f"qty={qty} (trigger {_fmt(trigger_level)}, "
                f"sl={_fmt(sl_price)}, tgt={_fmt(target_price)})"
            )
            return True
        except Exception as exc:
            print(f"[algo3] entry failed for {self.symbol}: {exc}")
            return False

    def _entry_trigger(self, side: str, entry_price: float, trigger_level: float) -> str:
        n = self.settings.get("silver_breakout_points", 150)
        setup_close = self._buy_setup_close if side == "BUY" else self._sell_setup_close
        direction = "upward" if side == "BUY" else "downward"
        return (
            f"15m silver {side.lower()} breakout on {self.symbol}: setup close "
            f"{_fmt(setup_close)} + n={n} = trigger {_fmt(trigger_level)}, "
            f"LTP crossed {direction} through the level at {entry_price:.2f}. "
            f"EMA20 {_fmt(self._ema20)}."
        )

    def _signal_snapshot(self, side: str, entry_price: float, trigger_level: float) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": "15m",
            "side": side,
            "entry_ltp": entry_price,
            "trigger_level": trigger_level,
            "n_points": self.settings.get("silver_breakout_points", 150),
            "ema20": self._ema20,
            "buy_setup_close": self._buy_setup_close,
            "sell_setup_close": self._sell_setup_close,
        }

    def _open_position(self) -> dict | None:
        for position in self.broker.open_positions():
            if position.get("symbol") == self.symbol:
                return position
        return None

    # ── points → percent conversion for the shared trailing engine ───
    def _trailing_settings_for(self, position: dict) -> dict:
        entry = float(position.get("entry_price") or 0) or 1.0
        trigger_pts = float(self.settings.get("tsl_trigger_points", 0))
        distance_pts = float(self.settings.get("tsl_distance_points", 0))
        merged = dict(self.settings)
        if trigger_pts > 0:
            merged["trailing_sl_trigger_pct"] = (trigger_pts / entry) * 100.0
        if distance_pts > 0:
            merged["trailing_sl_distance_pct"] = (distance_pts / entry) * 100.0
        return merged

    # ── diagnostics ──────────────────────────────────────────────────
    def feed_status(self) -> dict:
        return {
            "algo_id": self.algo_id,
            "display_name": self.display_name,
            "symbol": self.symbol,
            "history_ready": self._history_ready,
            "history_loading": self._history_loading,
            "history_error": self._history_error,
            "warmup_minute_candles": self._warmup_minute_candles,
            "bars_15m": len(self._bars),
            "minute_buffer_count": len(self._minute_buffer),
            "current_bucket": self._current_bucket.isoformat() if self._current_bucket else None,
            "ema20": self._ema20,
            "buy_setup_close": self._buy_setup_close,
            "sell_setup_close": self._sell_setup_close,
            "buy_setup_bar_at": self._buy_setup_bar_at.isoformat() if self._buy_setup_bar_at else None,
            "sell_setup_bar_at": self._sell_setup_bar_at.isoformat() if self._sell_setup_bar_at else None,
            "n_points": self.settings.get("silver_breakout_points", 150),
            "last_tick_at": self._last_tick_at,
            "last_tick_ltp": self._last_tick_ltp,
            "last_minute_candle_at": self._last_minute_candle_at,
            "last_bar_at": self._last_bar_at,
        }
