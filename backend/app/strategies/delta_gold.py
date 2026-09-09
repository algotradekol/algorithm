"""Original Silver reference/entry rules with Delta candles and paper execution."""
from __future__ import annotations

import datetime
import math
import time

from .algo3_silver_micro import Algo3SilverMicro, _ema_step
from ..timezone import IST


DELTA_DEFAULTS = {
    "scan_enabled": True, "trading_enabled": False, "silver_breakout_points": 200.0,
    "sl_points": 200.0, "tsl_activate_points": 500.0, "target_points": 2000.0,
    "silver_lots": 1, "exit_mode": "fixed_target_sl", "manual_exit_reentry_enabled": False,
}


def validate_settings(settings):
    if set(settings) != set(DELTA_DEFAULTS):
        raise ValueError("Unknown or missing Delta settings")
    for key in ("scan_enabled", "trading_enabled", "manual_exit_reentry_enabled"):
        if not isinstance(settings[key], bool):
            raise ValueError(f"{key} must be true or false")
    for key in ("silver_breakout_points", "sl_points", "tsl_activate_points", "target_points", "silver_lots"):
        if isinstance(settings[key], bool):
            raise ValueError(f"{key} must be numeric")
        value = float(settings[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be positive")
        settings[key] = value
    if not float(settings["silver_lots"]).is_integer():
        raise ValueError("Contracts per trade must be a whole number")
    if settings["exit_mode"] not in {"fixed_target_sl", "target_to_breakeven_sl"}:
        raise ValueError("Invalid Delta exit mode")
    if settings["exit_mode"] == "target_to_breakeven_sl" and settings["tsl_activate_points"] >= settings["target_points"]:
        raise ValueError("TSL activation must be below the final target")
    return settings


class DeltaGold(Algo3SilverMicro):
    def __init__(self, minutes, symbol, broker):
        self.minutes = minutes
        self.algo_id = f"delta_gold_{minutes}m"
        self.display_name = f"Delta Gold {'15 min' if minutes == 15 else '1 hr'}"
        self.reference_history = []
        self.last_candle_epoch = None
        self.data_error = "Waiting for Delta history"
        super().__init__([symbol], settings=dict(broker.state["settings"]), broker=broker)
        self._sl_cooldown_until_monotonic = time.monotonic() + max(0, broker.state.get("cooldown_until", 0) - time.time())

    def refresh_market_data(self, force=False):
        # DeltaService owns history and feeds; never start a FYERS warmup.
        return False

    def square_off_all(self):
        # No daily session boundary in this 24/7 paper engine.
        return

    @staticmethod
    def _is_opening_gap_from_prior_session(event_time, setup_bar_at):
        return False

    def _persist_setup_event(self, side, bar, source, ema20_override=None):
        self.reference_history.insert(0, {"side": side, "time": bar["time"].replace(tzinfo=IST).isoformat(),
                                         "close": bar["close"], "ema20": self._ema20})
        self.reference_history = self.reference_history[:200]

    def ingest_history(self, rows, now):
        interval = self.minutes * 60
        current_bucket = int(now // interval) * interval
        by_time = {}
        for row in rows:
            stamp = int(row["time"])
            if stamp % interval or stamp >= current_bucket:
                continue
            prices = [float(row[key]) for key in ("open", "high", "low", "close")]
            if any(not math.isfinite(p) or p <= 0 for p in prices) or not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]:
                raise ValueError("Invalid Delta OHLC history")
            by_time[stamp] = row
        stamps = sorted(by_time)
        if not stamps or stamps[-1] != current_bucket - interval:
            raise ValueError("Delta latest completed candle is missing; new entries paused")
        if self.last_candle_epoch is None:
            if len(stamps) < 20:
                raise ValueError("Need at least 20 completed Delta candles for EMA20")
        else:
            stamps = [s for s in stamps if s > self.last_candle_epoch]
            if stamps and stamps[0] != self.last_candle_epoch + interval:
                raise ValueError("Delta history gap; new entries paused")
        if any(b - a != interval for a, b in zip(stamps, stamps[1:])):
            raise ValueError("Delta history contains missing candles; new entries paused")
        for stamp in stamps:
            row = by_time[stamp]
            bar = {**row, **{key: float(row[key]) for key in ("open", "high", "low", "close")},
                   "time": datetime.datetime.fromtimestamp(stamp, IST).replace(tzinfo=None)}
            self._ema20 = _ema_step(self._ema20, float(row["close"]))
            bar["ema20"] = self._ema20
            self._bars.append(bar)
            self._update_setups(bar, log=False)
            side = "BUY" if self._buy_setup_bar_at == bar["time"] else "SELL" if self._sell_setup_bar_at == bar["time"] else None
            if side:
                self._persist_setup_event(side, bar, "delta_history")
            self.last_candle_epoch = stamp
        # The current candle's open is used only to confirm SELL candle color.
        forming = next((r for r in rows if int(r["time"]) == current_bucket), None)
        if forming:
            self._current_bucket = datetime.datetime.fromtimestamp(current_bucket, IST).replace(tzinfo=None)
            self._minute_buffer = [{"open": float(forming["open"])}]
        self._history_ready = True
        self.data_error = None

    def process_price(self, price, timestamp):
        self._last_tick_ltp = price
        self._last_tick_at = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()
        # Exits remain active while reference history recovers.
        self.check_exits()
        interval = self.minutes * 60
        bucket = int(timestamp // interval) * interval
        bucket_at = datetime.datetime.fromtimestamp(bucket, IST).replace(tzinfo=None)
        if self._current_bucket != bucket_at:
            self._current_bucket = bucket_at
            self._minute_buffer = []  # Wait for Delta's actual opening price.
        if self.last_candle_epoch == bucket - interval and not self.data_error and self.scan_enabled():
            self._check_triggers(price, event_time=datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc))
        self._prev_ltp = price

    def _entry_trigger(self, side, entry_price, trigger_level):
        return f"{self.minutes}m Delta {side} reference breakout at {trigger_level:g}; paper fill {entry_price:g}"

    def _handle_broker_position_closed(self, **kwargs):
        if self.settings.get("manual_exit_reentry_enabled"):
            super()._handle_broker_position_closed(**kwargs)
        else:
            self._buy_reentry_after_exit = None
            self._sell_reentry_after_exit = None

    def _fire_entry(self, side, ltp, trigger_level, setup_bar_at_override=None, event_time=None):
        guard = self.broker.state.get("manual_guard")
        reference = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        reference_time = reference.replace(tzinfo=IST).isoformat() if reference else None
        if guard and guard["side"] == side and guard["setup_time"] == reference_time:
            prev = self._prev_ltp
            crossed = prev is not None and (prev < trigger_level <= ltp if side == "BUY" else prev > trigger_level >= ltp)
            if not crossed:
                return False
        return super()._fire_entry(side, ltp, trigger_level, setup_bar_at_override, event_time)

    def _signal_snapshot(self, side, entry_price, trigger_level):
        result = super()._signal_snapshot(side, entry_price, trigger_level)
        reference_time = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        result.update(timeframe=f"{self.minutes}m", broker="delta", execution="paper",
                      setup_time=reference_time.replace(tzinfo=IST).isoformat() if reference_time else None,
                      setup_close=self._buy_setup_close if side == "BUY" else self._sell_setup_close,
                      settings=dict(self.settings))
        return result
