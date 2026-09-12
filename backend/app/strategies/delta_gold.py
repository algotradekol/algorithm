"""Delta PAXG EMA-price/EMA-volume references with paper-only execution."""
from __future__ import annotations

import copy
import datetime
import math
import time

from .algo3_silver_micro import Algo3SilverMicro, _ema_step, _entry_time_iso
from ..timezone import IST


DELTA_STRATEGY_VERSION = "paxg_ema_volume_v1"
DELTA_EXIT_MODE_THREE_CANDLE = "three_candle_tsl"
DELTA_TIMEFRAMES = (5, 7, 15, 30, 60, 240)
DELTA_DEFAULTS = {
    "scan_enabled": True,
    "trading_enabled": False,
    "silver_breakout_points": 3.0,
    "sl_points": 15.0,
    "tsl_activate_points": 15.0,
    "target_points": 50.0,
    "tsl_buffer_points": 3.0,
    "silver_lots": 1,
    "exit_mode": "fixed_target_sl",
    "manual_exit_reentry_enabled": False,
    "post_exit_cooldown_minutes": 5,
    "strategy_version": DELTA_STRATEGY_VERSION,
}


def validate_settings(settings):
    if set(settings) != set(DELTA_DEFAULTS):
        raise ValueError("Unknown or missing Delta settings")
    for key in ("scan_enabled", "trading_enabled", "manual_exit_reentry_enabled"):
        if not isinstance(settings[key], bool):
            raise ValueError(f"{key} must be true or false")
    for key in (
        "silver_breakout_points",
        "sl_points",
        "tsl_activate_points",
        "target_points",
        "tsl_buffer_points",
        "silver_lots",
        "post_exit_cooldown_minutes",
    ):
        if isinstance(settings[key], bool):
            raise ValueError(f"{key} must be numeric")
        value = float(settings[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be positive")
        settings[key] = value
    if not float(settings["silver_lots"]).is_integer():
        raise ValueError("Lots per trade must be a whole number")
    if settings["post_exit_cooldown_minutes"] not in {5, 15, 30, 60, 240, 720}:
        raise ValueError("Post-exit rest must be 5, 15, 30, 60, 240 or 720 minutes")
    if settings["strategy_version"] != DELTA_STRATEGY_VERSION:
        raise ValueError("Invalid Delta strategy version")
    if settings["exit_mode"] not in {
        "fixed_target_sl",
        "target_to_breakeven_sl",
        DELTA_EXIT_MODE_THREE_CANDLE,
    }:
        raise ValueError("Invalid Delta exit mode")
    if (
        settings["exit_mode"] == "target_to_breakeven_sl"
        and settings["tsl_activate_points"] >= settings["target_points"]
    ):
        raise ValueError("TSL activation must be below the final target")
    return settings


def normalize_stored_settings(settings):
    """Upgrade the old price-only Delta settings exactly once."""
    raw = settings if isinstance(settings, dict) else {}
    if raw.get("strategy_version") != DELTA_STRATEGY_VERSION:
        normalized = dict(DELTA_DEFAULTS)
        for key in ("scan_enabled", "trading_enabled", "manual_exit_reentry_enabled"):
            if isinstance(raw.get(key), bool):
                normalized[key] = raw[key]
        if raw.get("exit_mode") in {"fixed_target_sl", "target_to_breakeven_sl"}:
            normalized["exit_mode"] = raw["exit_mode"]
        return validate_settings(normalized)
    return validate_settings({**DELTA_DEFAULTS, **raw})


class DeltaGold(Algo3SilverMicro):
    def __init__(self, minutes, symbol, broker):
        if minutes not in DELTA_TIMEFRAMES:
            raise ValueError("Delta timeframe must be 5, 7, 15, 30, 60 or 240 minutes")
        self.minutes = minutes
        self.algo_id = f"delta_gold_{minutes}m"
        labels = {60: "Delta Gold 1 hr", 240: "Delta Gold 4 hr"}
        self.display_name = labels.get(minutes, f"Delta Gold {minutes} min")
        self.reference_history = []
        self.last_candle_epoch = None
        self.data_error = "Waiting for Delta history"
        self._volume_ema20 = None
        self._current_candle_open = None

        original = copy.deepcopy(broker.state.get("settings") or {})
        normalized = normalize_stored_settings(original)
        if normalized != original:
            state = copy.deepcopy(broker.state)
            state["settings"] = normalized
            broker.commit(state)
        super().__init__([symbol], settings=dict(normalized), broker=broker)
        self._sl_cooldown_until_monotonic = time.monotonic() + max(
            0, broker.state.get("cooldown_until", 0) - time.time()
        )

    def refresh_market_data(self, force=False):
        return False

    def square_off_all(self):
        return

    @staticmethod
    def _is_opening_gap_from_prior_session(event_time, setup_bar_at):
        return False

    def _persist_setup_event(self, side, bar, source, ema20_override=None):
        self.reference_history.insert(
            0,
            {
                "side": side,
                "time": bar["time"].replace(tzinfo=IST).isoformat(),
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
                "ema20": self._ema20,
                "volume_ema20": self._volume_ema20,
            },
        )
        self.reference_history = self.reference_history[:200]

    def _qualifies_as_buy_setup(self, bar):
        return bool(
            self._ema20 is not None
            and self._volume_ema20 is not None
            and bar["close"] > bar["open"]
            and bar["close"] > self._ema20
            and bar["volume"] > self._volume_ema20
        )

    def _qualifies_as_sell_setup(self, bar):
        return bool(
            self._ema20 is not None
            and self._volume_ema20 is not None
            and bar["close"] < bar["open"]
            and bar["close"] < self._ema20
            and bar["volume"] > self._volume_ema20
        )

    def _update_setups(self, bar, log=False):
        if self._qualifies_as_buy_setup(bar):
            self._buy_setup_close = float(bar["close"])
            self._buy_setup_bar_at = bar["time"]
            self._buy_reentry_after_exit = None
            if log:
                self._persist_setup_event("BUY", bar, "delta_live")
        elif self._qualifies_as_sell_setup(bar):
            self._sell_setup_close = float(bar["close"])
            self._sell_setup_bar_at = bar["time"]
            self._sell_reentry_after_exit = None
            if log:
                self._persist_setup_event("SELL", bar, "delta_live")

    def ingest_history(self, rows, now):
        interval = self.minutes * 60
        current_bucket = int(now // interval) * interval
        by_time = {}
        forming = None
        for raw in rows:
            stamp = int(raw["time"])
            if stamp == current_bucket:
                forming = raw
                continue
            if stamp % interval or stamp >= current_bucket:
                continue
            prices = [float(raw[key]) for key in ("open", "high", "low", "close")]
            volume = float(raw.get("volume", 0))
            if (
                any(not math.isfinite(price) or price <= 0 for price in prices)
                or not math.isfinite(volume)
                or volume < 0
                or not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]
            ):
                raise ValueError("Invalid Delta OHLCV history")
            by_time[stamp] = {**raw, "volume": volume}
        stamps = sorted(by_time)
        if not stamps or stamps[-1] != current_bucket - interval:
            raise ValueError("Delta latest completed candle is missing; new entries paused")
        if self.last_candle_epoch is None:
            if len(stamps) < 20:
                raise ValueError("Need at least 20 completed Delta candles for EMA20")
        else:
            stamps = [stamp for stamp in stamps if stamp > self.last_candle_epoch]
            if stamps and stamps[0] != self.last_candle_epoch + interval:
                raise ValueError("Delta history gap; new entries paused")
        if any(right - left != interval for left, right in zip(stamps, stamps[1:])):
            raise ValueError("Delta history contains missing candles; new entries paused")

        for stamp in stamps:
            raw = by_time[stamp]
            bar = {
                **raw,
                **{key: float(raw[key]) for key in ("open", "high", "low", "close", "volume")},
                "time": datetime.datetime.fromtimestamp(stamp, IST).replace(tzinfo=None),
            }
            self._ema20 = _ema_step(self._ema20, bar["close"])
            self._volume_ema20 = _ema_step(self._volume_ema20, bar["volume"])
            bar.update(ema20=self._ema20, volume_ema20=self._volume_ema20)
            self._bars.append(bar)
            old_buy, old_sell = self._buy_setup_bar_at, self._sell_setup_bar_at
            self._update_setups(bar, log=False)
            if self._buy_setup_bar_at != old_buy:
                self._persist_setup_event("BUY", bar, "delta_history")
            if self._sell_setup_bar_at != old_sell:
                self._persist_setup_event("SELL", bar, "delta_history")
            self.last_candle_epoch = stamp
            self._apply_three_candle_tsl(bar)

        if forming:
            open_price = float(forming["open"])
            if not math.isfinite(open_price) or open_price <= 0:
                raise ValueError("Delta forming candle open is invalid")
            bucket_at = datetime.datetime.fromtimestamp(current_bucket, IST).replace(tzinfo=None)
            if self._current_bucket != bucket_at:
                self._prev_ltp = None
            self._current_bucket = bucket_at
            self._current_candle_open = open_price
            self._minute_buffer = [{"open": open_price}]
        self._history_ready = True
        self.data_error = None

    def process_price(self, price, timestamp):
        price = float(price)
        self._last_tick_ltp = price
        self._last_tick_at = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()
        self.check_exits()
        interval = self.minutes * 60
        bucket = int(timestamp // interval) * interval
        bucket_at = datetime.datetime.fromtimestamp(bucket, IST).replace(tzinfo=None)
        if self._current_bucket != bucket_at:
            self._current_bucket = bucket_at
            self._current_candle_open = None
            self._minute_buffer = []
            self._prev_ltp = None
        if self.last_candle_epoch == bucket - interval and not self.data_error and self.scan_enabled():
            self._check_triggers(price, event_time=datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc))
        self._prev_ltp = price

    def _check_triggers(self, ltp, event_time=None):
        if self._current_candle_open is None:
            return
        n = float(self.settings["silver_breakout_points"])
        baseline = self._prev_ltp if self._prev_ltp is not None else self._current_candle_open
        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None
        buy_level = self._buy_setup_close + n if self._buy_setup_close is not None else None

        if (
            sell_level is not None
            and self._sell_setup_bar_at is not None
            and self._current_bucket > self._sell_setup_bar_at
            and self._current_candle_open > sell_level
            and baseline > sell_level >= ltp
            and not self._failed_attempt_blocks_setup("SELL", self._sell_setup_bar_at)
        ):
            if self._fire_entry("SELL", sell_level, sell_level, self._sell_setup_bar_at, event_time):
                self._mark_fired("SELL", setup_bar_at=self._sell_setup_bar_at)

        if (
            buy_level is not None
            and self._buy_setup_bar_at is not None
            and self._current_bucket > self._buy_setup_bar_at
            and self._current_candle_open < buy_level
            and baseline < buy_level <= ltp
            and not self._failed_attempt_blocks_setup("BUY", self._buy_setup_bar_at)
        ):
            if self._fire_entry("BUY", buy_level, buy_level, self._buy_setup_bar_at, event_time):
                self._mark_fired("BUY", setup_bar_at=self._buy_setup_bar_at)

    def _entry_trigger(self, side, entry_price, trigger_level):
        return (
            f"{self.minutes}m Delta {side} EMA20 + volume EMA20 reference breakout "
            f"at {trigger_level:g}; paper fill {entry_price:g}"
        )

    def _handle_broker_position_closed(self, **kwargs):
        if self.settings.get("manual_exit_reentry_enabled"):
            super()._handle_broker_position_closed(**kwargs)
        else:
            self._buy_reentry_after_exit = None
            self._sell_reentry_after_exit = None

    def _post_sl_cooldown_remaining(self):
        return max(0.0, float(self.broker.state.get("cooldown_until") or 0) - time.time())

    def _arm_post_sl_cooldown(self, exit_reason):
        # Delta's broker persists the configured timer atomically with the exit.
        return

    def cooldown_status(self):
        remaining = self._post_sl_cooldown_remaining()
        return {
            "active": remaining > 0,
            "until": float(self.broker.state.get("cooldown_until") or 0) or None,
            "remaining_seconds": remaining,
            "reason": self.broker.state.get("cooldown_reason") if remaining > 0 else None,
        }

    def _fire_entry(self, side, ltp, trigger_level, setup_bar_at_override=None, event_time=None):
        guard = self.broker.state.get("manual_guard")
        reference = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        reference_time = reference.replace(tzinfo=IST).isoformat() if reference else None
        if guard and guard["side"] == side and guard["setup_time"] == reference_time:
            previous = self._prev_ltp
            crossed = previous is not None and (
                previous < trigger_level <= ltp if side == "BUY" else previous > trigger_level >= ltp
            )
            if not crossed:
                return False
        return super()._fire_entry(side, ltp, trigger_level, setup_bar_at_override, event_time)

    def _three_candle_window(self, entry_bucket=None):
        eligible = [bar for bar in self._bars if entry_bucket is None or bar["time"] != entry_bucket]
        return eligible[-3:] if len(eligible) >= 3 else []

    def _three_candle_details(self, side, window, buffer_points=None):
        if len(window) != 3:
            return None
        buffer_points = float(buffer_points if buffer_points is not None else self.settings["tsl_buffer_points"])
        reference = min(float(bar["low"]) for bar in window) if side == "BUY" else max(float(bar["high"]) for bar in window)
        candidate = reference - buffer_points if side == "BUY" else reference + buffer_points
        return {
            "calculated_at": window[-1]["time"].replace(tzinfo=IST).isoformat(),
            "reference_price": reference,
            "buffer_points": buffer_points,
            "candidate_sl": candidate,
            "candles": [
                {
                    key: (bar[key].replace(tzinfo=IST).isoformat() if key == "time" else bar[key])
                    for key in ("time", "open", "high", "low", "close", "volume", "ema20", "volume_ema20")
                }
                for bar in window
            ],
        }

    def _apply_three_candle_tsl(self, completed_bar):
        position = self._open_position()
        if not position:
            return
        policy = (position.get("signal_snapshot") or {}).get("delta_three_candle_tsl")
        if not isinstance(policy, dict):
            return
        entry_bucket_raw = policy.get("entry_bucket")
        entry_bucket = None
        if entry_bucket_raw:
            entry_bucket = datetime.datetime.fromisoformat(str(entry_bucket_raw)).replace(tzinfo=None)
        if completed_bar["time"] == entry_bucket:
            return
        details = self._three_candle_details(
            position["side"],
            self._three_candle_window(entry_bucket),
            policy.get("buffer_points"),
        )
        if details:
            self.broker.apply_three_candle_stop(position, details, float(completed_bar["close"]))

    def _enter(self, side, entry_price, trigger_level, event_time=None):
        if not self.symbol or not entry_price:
            return False
        lots = max(1, int(self.settings.get("silver_lots", 1) or 1))
        direction = 1 if side == "BUY" else -1
        configured_sl = float(entry_price) - direction * float(self.settings["sl_points"])
        target = float(entry_price) + direction * float(self.settings["target_points"])
        activation = float(entry_price) + direction * float(self.settings["tsl_activate_points"])
        if min(configured_sl, target) <= 0:
            return False

        snapshot = self._signal_snapshot(side, entry_price, trigger_level)
        mode = self.settings["exit_mode"]
        snapshot["silver_exit_policy"] = mode
        snapshot["configured_initial_sl_price"] = configured_sl
        if mode == "target_to_breakeven_sl":
            snapshot["silver_breakeven"] = {
                "armed": False,
                "activation_price": activation,
                "activation_points": self.settings["tsl_activate_points"],
                "target_price": target,
                "final_target_enabled": True,
                "initial_sl_price": configured_sl,
            }

        effective_sl = configured_sl
        if mode == DELTA_EXIT_MODE_THREE_CANDLE:
            event = event_time or datetime.datetime.now(datetime.timezone.utc)
            event_stamp = event.timestamp() if event.tzinfo else event.replace(tzinfo=IST).timestamp()
            entry_bucket_stamp = int(event_stamp // (self.minutes * 60)) * self.minutes * 60
            entry_bucket = datetime.datetime.fromtimestamp(entry_bucket_stamp, IST).replace(tzinfo=None)
            details = self._three_candle_details(side, self._three_candle_window(entry_bucket))
            accepted = bool(
                details
                and (configured_sl < details["candidate_sl"] < entry_price if side == "BUY" else entry_price < details["candidate_sl"] < configured_sl)
            )
            if accepted:
                effective_sl = float(details["candidate_sl"])
            snapshot["delta_three_candle_tsl"] = {
                "policy": DELTA_EXIT_MODE_THREE_CANDLE,
                "entry_bucket": entry_bucket.replace(tzinfo=IST).isoformat(),
                "buffer_points": self.settings["tsl_buffer_points"],
                "initial_window": details,
                "events": ([{**details, "status": "accepted", "previous_sl": configured_sl}] if accepted else []),
                "evaluations": ([{**details, "status": "accepted" if accepted else "not_tighter"}] if details else []),
            }

        try:
            args = (self.symbol, side, lots, float(entry_price), effective_sl, target, self._entry_trigger(side, entry_price, trigger_level), snapshot)
            if event_time is None:
                self.broker.open_trade(*args)
            else:
                self.broker.open_trade(*args, entry_time=_entry_time_iso(event_time))
            return True
        except Exception as exc:
            print(f"[delta-paper] entry failed for {self.symbol}: {exc}")
            return False

    def _signal_snapshot(self, side, entry_price, trigger_level):
        reference_time = self._buy_setup_bar_at if side == "BUY" else self._sell_setup_bar_at
        reference = next(
            (row for row in self.reference_history if row["side"] == side and reference_time and row["time"] == reference_time.replace(tzinfo=IST).isoformat()),
            None,
        )
        return {
            "symbol": self.symbol,
            "timeframe": f"{self.minutes}m",
            "broker": "delta",
            "execution": "paper",
            "strategy_version": DELTA_STRATEGY_VERSION,
            "side": side,
            "entry_ltp": entry_price,
            "trigger_level": trigger_level,
            "n_points": self.settings["silver_breakout_points"],
            "setup_time": reference_time.replace(tzinfo=IST).isoformat() if reference_time else None,
            "setup_close": self._buy_setup_close if side == "BUY" else self._sell_setup_close,
            "setup_reference": copy.deepcopy(reference),
            "ema20": self._ema20,
            "volume_ema20": self._volume_ema20,
            "entry_candle_open": self._current_candle_open,
            "settings": dict(self.settings),
        }
