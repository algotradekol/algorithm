"""Delta PAXG EMA-price/EMA-volume references with paper-only execution."""
from __future__ import annotations

import copy
import datetime
import math
import time

from .algo3_silver_micro import Algo3SilverMicro, _ema_step, _entry_time_iso
from ..delta_log import delta_log
from ..timezone import IST


DELTA_STRATEGY_VERSION = "paxg_ema_volume_v1"
DELTA_EXIT_MODE_THREE_CANDLE = "three_candle_tsl"
DELTA_EXIT_MODE_CONTINUOUS_LADDER = "continuous_ladder_tsl"
DELTA_EXIT_MODES = (
    "fixed_target_sl",
    "target_to_breakeven_sl",
    DELTA_EXIT_MODE_THREE_CANDLE,
    DELTA_EXIT_MODE_CONTINUOUS_LADDER,
)
DELTA_TIMEFRAMES = (1, 3, 5, 7, 15, 30, 60, 120, 240)
DELTA_DEFAULTS = {
    "scan_enabled": True,
    "trading_enabled": False,
    "silver_breakout_points": 3.0,
    "sl_points": 15.0,
    "tsl_activate_points": 15.0,
    "target_points": 50.0,
    "tsl_buffer_points": 3.0,
    "tsl_profit_step_points": 15.0,
    "tsl_lock_step_points": 15.0,
    "silver_lots": 1,
    "size_mode": "lots",
    "pax_size": 0.001,
    "leverage": 50.0,
    "exit_mode": "fixed_target_sl",
    "manual_exit_reentry_enabled": False,
    "post_exit_cooldown_minutes": 5,
    "strategy_version": DELTA_STRATEGY_VERSION,
}


SILVER_STRATEGY_VERSION = 'silver_micro_delta_usd_v1'
SILVER_DEFAULTS = {
    **DELTA_DEFAULTS,
    'silver_breakout_points': 0.10,
    'sl_points': 0.30,
    'tsl_activate_points': 0.30,
    'target_points': 1.0,
    'exit_mode': 'target_to_breakeven_sl',
    'strategy_version': SILVER_STRATEGY_VERSION,
}


def defaults_for(asset='gold'):
    return SILVER_DEFAULTS if asset == 'silver' else DELTA_DEFAULTS


def validate_settings(settings, asset='gold'):
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
        "tsl_profit_step_points",
        "tsl_lock_step_points",
        "silver_lots",
        "pax_size",
        "leverage",
        "post_exit_cooldown_minutes",
    ):
        if isinstance(settings[key], bool):
            raise ValueError(f"{key} must be numeric")
        value = float(settings[key])
        if not math.isfinite(value) or value < 0 or (key != "post_exit_cooldown_minutes" and value <= 0):
            raise ValueError(f"{key} must be positive")
        settings[key] = value
    if not float(settings["silver_lots"]).is_integer():
        raise ValueError("Lots per trade must be a whole number")
    settings["silver_lots"] = int(settings["silver_lots"])
    if settings.get("size_mode") not in {"lots", "pax"}:
        raise ValueError("Size mode must be lots or pax")
    if asset != "gold" and settings["size_mode"] == "pax":
        raise ValueError("PAXG sizing is available only for Delta Gold")
    if not float(settings["post_exit_cooldown_minutes"]).is_integer():
        raise ValueError("Post-exit rest must be a whole number of minutes")
    settings["post_exit_cooldown_minutes"] = int(settings["post_exit_cooldown_minutes"])
    if not 0 <= settings["post_exit_cooldown_minutes"] <= 10_080:
        raise ValueError("Post-exit rest must be between 0 minutes and 7 days")
    if settings["strategy_version"] != defaults_for(asset)['strategy_version']:
        raise ValueError("Invalid Delta strategy version")
    if settings["exit_mode"] not in DELTA_EXIT_MODES:
        raise ValueError("Invalid Delta exit mode")
    if asset == 'silver' and settings['exit_mode'] in {DELTA_EXIT_MODE_THREE_CANDLE, DELTA_EXIT_MODE_CONTINUOUS_LADDER}:
        raise ValueError('Delta Silver uses normal Silver Micro fixed or breakeven exits')
    if (
        settings["exit_mode"] in {"target_to_breakeven_sl", DELTA_EXIT_MODE_CONTINUOUS_LADDER}
        and settings["tsl_activate_points"] >= settings["target_points"]
    ):
        raise ValueError("TSL activation must be below the final target")
    return settings


def delta_ladder_snapshot(position, settings):
    side = position['side']
    direction = 1 if side == 'BUY' else -1
    entry_price = float(position['entry_price'])
    activation_points = float(settings['tsl_activate_points'])
    return {
        'policy': DELTA_EXIT_MODE_CONTINUOUS_LADDER,
        'armed': False,
        'status': 'waiting_for_initial_target',
        'activation_price': entry_price + direction * activation_points,
        'activation_points': activation_points,
        'target_price': float(position['target_price']),
        'final_target_enabled': True,
        'initial_sl_price': float(position['sl_price']),
        'profit_step_points': float(settings.get('tsl_profit_step_points') or activation_points),
        'lock_step_points': float(settings.get('tsl_lock_step_points') or 0),
        'highest': entry_price,
        'lowest': entry_price,
        'step_index': -1,
        'protected_points': 0.0,
        'events': [],
        'evaluations': [],
    }


def exit_mode_snapshot_patch(new_mode, position, settings, minutes, asset, now_utc):
    """Compute a signal_snapshot patch that switches an open position's
    exit_mode without touching the entry, side or current sl/target prices.

    Returns None when the position already uses `new_mode`. Raises ValueError
    for an unknown mode or a Silver+three_candle combination that the
    strategy validator also blocks.

    None values in the returned dict mean "delete this key from the snapshot"
    — the caller applies the patch via apply_exit_mode_patch which honours
    that convention. Kept as a pure function so it can be unit-tested and so
    both the paper (delta_engine.edit_protection) and live (DeltaLiveBroker
    .update_protection) paths call the same code.
    """
    if asset == 'silver' and new_mode in {DELTA_EXIT_MODE_THREE_CANDLE, DELTA_EXIT_MODE_CONTINUOUS_LADDER}:
        raise ValueError('Delta Silver does not support this TSL mode')
    if new_mode not in DELTA_EXIT_MODES:
        raise ValueError(f'Invalid exit_mode: {new_mode!r}')
    snapshot = position.get('signal_snapshot') or {}
    if snapshot.get('silver_exit_policy') == new_mode:
        return None
    patch: dict = {
        'silver_exit_policy': new_mode,
        'exit_mode_previous': snapshot.get('silver_exit_policy'),
        'exit_mode_edited_at': now_utc.isoformat(),
        # Both keys reset by default; the branch below re-adds whichever one
        # the new mode needs. Keeps the patch tiny and idempotent.
        'silver_breakeven': None,
        'delta_three_candle_tsl': None,
        'delta_ladder_tsl': None,
    }
    if new_mode == 'target_to_breakeven_sl':
        side = position['side']
        direction = 1 if side == 'BUY' else -1
        activation_points = float(settings['tsl_activate_points'])
        entry_price = float(position['entry_price'])
        patch['silver_breakeven'] = {
            'armed': False,
            'activation_price': entry_price + direction * activation_points,
            'activation_points': activation_points,
            'target_price': float(position['target_price']),
            'final_target_enabled': True,
            'initial_sl_price': float(position['sl_price']),
        }
    elif new_mode == DELTA_EXIT_MODE_THREE_CANDLE:
        # Three-candle window starts from NOW, not from the original entry —
        # the operator consciously activated this policy at this moment, so
        # any pre-edit candles must not retroactively drive the trail.
        ist_stamp = now_utc.astimezone(IST).timestamp()
        bucket_stamp = int(ist_stamp // (minutes * 60)) * minutes * 60
        entry_bucket = datetime.datetime.fromtimestamp(bucket_stamp, IST).replace(tzinfo=None)
        patch['delta_three_candle_tsl'] = {
            'policy': DELTA_EXIT_MODE_THREE_CANDLE,
            'entry_bucket': entry_bucket.replace(tzinfo=IST).isoformat(),
            'status': 'waiting_for_three_post_entry_candles',
            'window_rule': 'rolling_latest_3_closed_strategy_candles_after_edit',
            'buffer_points': float(settings.get('tsl_buffer_points', 0) or 0),
            'events': [],
            'evaluations': [],
        }
    elif new_mode == DELTA_EXIT_MODE_CONTINUOUS_LADDER:
        patch['delta_ladder_tsl'] = delta_ladder_snapshot(position, settings)
    return patch


def apply_exit_mode_patch(position, patch):
    """Merge a patch returned by exit_mode_snapshot_patch into position in
    place. `None` values in the patch delete their key from signal_snapshot.
    Also clears any live trailing flag — the new mode restarts its own trail
    logic from a clean slate so an old armed breakeven cannot leak forward.
    """
    if patch is None:
        return
    snapshot = position.setdefault('signal_snapshot', {})
    for key, value in patch.items():
        if value is None:
            snapshot.pop(key, None)
        else:
            snapshot[key] = value
    position.pop('trailing_sl_active', None)


def effective_delta_lots(settings, product, asset="gold"):
    """Return whole Delta exchange quantity from the configured size mode."""
    mode = settings.get("size_mode", "lots")
    if mode == "pax" and asset == "gold":
        contract_value = float(product.get("contract_value") or 0)
        if not math.isfinite(contract_value) or contract_value <= 0:
            raise ValueError("Delta contract value unavailable; cannot convert PAXG size")
        pax_size = float(settings.get("pax_size") or 0)
        if not math.isfinite(pax_size) or pax_size <= 0:
            raise ValueError("PAXG per trade must be positive")
        return max(1, int(math.ceil(pax_size / contract_value)))
    return max(1, int(settings.get("silver_lots", 1) or 1))


def normalize_stored_settings(settings, asset='gold'):
    """Upgrade the old price-only Delta settings exactly once."""
    raw = settings if isinstance(settings, dict) else {}
    defaults = defaults_for(asset)
    if raw.get("strategy_version") != defaults['strategy_version']:
        normalized = dict(defaults)
        for key in ("scan_enabled", "trading_enabled", "manual_exit_reentry_enabled"):
            if isinstance(raw.get(key), bool):
                normalized[key] = raw[key]
        if raw.get("exit_mode") in DELTA_EXIT_MODES:
            normalized["exit_mode"] = raw["exit_mode"]
        return validate_settings(normalized, asset)
    return validate_settings({**defaults, **raw}, asset)


class DeltaGold(Algo3SilverMicro):
    asset = 'gold'
    def __init__(self, minutes, symbol, broker):
        if minutes not in DELTA_TIMEFRAMES:
            raise ValueError("Delta timeframe must be 5, 7, 15, 30, 60, 120 or 240 minutes")
        self.minutes = minutes
        self.algo_id = f"delta_gold_{minutes}m"
        labels = {60: "Delta Gold 1 hr", 120: "Delta Gold 2 hr", 240: "Delta Gold 4 hr"}
        self.display_name = labels.get(minutes, f"Delta Gold {minutes} min")
        self.reference_history = []
        self.last_candle_epoch = None
        self.data_error = "Waiting for Delta history"
        self._volume_ema20 = None
        self._current_candle_open = None

        original = copy.deepcopy(broker.state.get("settings") or {})
        normalized = normalize_stored_settings(original, self.asset)
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
        delta_log(
            "reference_saved",
            strategy=self.algo_id,
            asset=self.asset,
            minutes=self.minutes,
            side=side,
            source=source,
            candle_time=bar["time"].replace(tzinfo=IST).isoformat(),
            close=bar["close"],
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            volume=bar["volume"],
            ema20=self._ema20,
            volume_ema20=self._volume_ema20,
            trigger=(float(bar["close"]) + float(self.settings["silver_breakout_points"]) if side == "BUY" else float(bar["close"]) - float(self.settings["silver_breakout_points"])),
        )
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
        if not stamps:
            raise ValueError("Delta returned no completed candles; new entries paused")
        if stamps[-1] != current_bucket - interval:
            # Custom-aggregated timeframes (7m, 120m) drop a bucket when Delta
            # is briefly missing one of its source 1m candles, and even native
            # timeframes see a fresh bar arrive a tick late. Silently skip
            # this refresh — nothing new to ingest — and let the next poll
            # pick up the completed bucket once Delta delivers it.
            return
        if self.last_candle_epoch is None:
            if len(stamps) < 20:
                raise ValueError("Need at least 20 completed Delta candles for EMA20")
        else:
            stamps = [stamp for stamp in stamps if stamp > self.last_candle_epoch]
            if stamps and stamps[0] != self.last_candle_epoch + interval:
                # Delta may drop a bucket that had zero trades (common around the
                # IST-midnight contract rollover). The "latest completed" guard
                # above already confirmed the feed is live, so bump forward past
                # the empty bucket instead of pausing the strategy forever.
                self.last_candle_epoch = stamps[0] - interval
        if any(right - left != interval for left, right in zip(stamps, stamps[1:])):
            # On the very first ingest we still refuse a gappy warmup — the
            # 20-bar EMA seed must be clean. Once last_candle_epoch is set,
            # incremental refreshes should tolerate an interior hole (usually
            # a 7m/120m aggregation dropping a bucket for a missing 1m bar);
            # keep only the contiguous suffix ending at the newest stamp so
            # we still advance state on the fresh candles and skip the gap.
            if self.last_candle_epoch is None:
                raise ValueError("Delta history contains missing candles; new entries paused")
            contiguous_start = len(stamps) - 1
            while contiguous_start > 0 and stamps[contiguous_start] - stamps[contiguous_start - 1] == interval:
                contiguous_start -= 1
            stamps = stamps[contiguous_start:]
            if stamps and stamps[0] != self.last_candle_epoch + interval:
                self.last_candle_epoch = stamps[0] - interval

        live_completed_bars = self.last_candle_epoch is not None
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
            delta_log(
                "completed_candle",
                strategy=self.algo_id,
                asset=self.asset,
                minutes=self.minutes,
                candle_time=bar["time"].replace(tzinfo=IST).isoformat(),
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                volume=bar["volume"],
                ema20=self._ema20,
                volume_ema20=self._volume_ema20,
                buy_qualifies=self._qualifies_as_buy_setup(bar),
                sell_qualifies=self._qualifies_as_sell_setup(bar),
            )
            if live_completed_bars and self.asset == 'silver' and self.scan_enabled():
                self._check_candle_close_trigger(bar)
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
            delta_log(
                "forming_candle",
                strategy=self.algo_id,
                asset=self.asset,
                minutes=self.minutes,
                bucket=bucket_at.replace(tzinfo=IST).isoformat(),
                open=open_price,
                current_bucket=current_bucket,
            )
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
            delta_log(
                "bucket_changed_on_tick",
                strategy=self.algo_id,
                asset=self.asset,
                minutes=self.minutes,
                previous_bucket=self._current_bucket.replace(tzinfo=IST).isoformat() if self._current_bucket else None,
                new_bucket=bucket_at.replace(tzinfo=IST).isoformat(),
                price=price,
                timestamp=timestamp,
            )
            self._current_bucket = bucket_at
            self._current_candle_open = None
            self._minute_buffer = []
            self._prev_ltp = None
        ready_for_triggers = self.last_candle_epoch == bucket - interval and not self.data_error and self.scan_enabled()
        delta_log(
            "price_seen_by_strategy",
            mode=getattr(self.broker, "mode", "paper"),
            strategy=self.algo_id,
            asset=self.asset,
            minutes=self.minutes,
            price=price,
            timestamp=timestamp,
            bucket=bucket_at.replace(tzinfo=IST).isoformat(),
            previous_ltp=self._prev_ltp,
            current_candle_open=self._current_candle_open,
            last_candle_epoch=self.last_candle_epoch,
            scan_enabled=self.scan_enabled(),
            trading_enabled=bool(self.settings.get("trading_enabled", True)),
            data_error=self.data_error,
            ready_for_triggers=ready_for_triggers,
        )
        if ready_for_triggers:
            self._check_triggers(price, event_time=datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc))
        self._prev_ltp = price

    def _check_triggers(self, ltp, event_time=None, baseline_override=None, reason="tick"):
        if self._current_candle_open is None:
            delta_log("trigger_check_skipped_no_open", strategy=self.algo_id, minutes=self.minutes, ltp=ltp)
            return
        n = float(self.settings["silver_breakout_points"])
        baseline = baseline_override if baseline_override is not None else (self._prev_ltp if self._prev_ltp is not None else self._current_candle_open)
        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None
        buy_level = self._buy_setup_close + n if self._buy_setup_close is not None else None
        sell_checks = {
            "has_level": sell_level is not None,
            "has_reference": self._sell_setup_bar_at is not None,
            "after_reference": bool(self._sell_setup_bar_at is not None and self._current_bucket > self._sell_setup_bar_at),
            "opened_valid_side": bool(sell_level is not None and self._current_candle_open > sell_level),
            "crossed": bool(sell_level is not None and baseline > sell_level >= ltp),
            "failed_attempt_block": bool(self._sell_setup_bar_at is not None and self._failed_attempt_blocks_setup("SELL", self._sell_setup_bar_at)),
        }
        buy_checks = {
            "has_level": buy_level is not None,
            "has_reference": self._buy_setup_bar_at is not None,
            "after_reference": bool(self._buy_setup_bar_at is not None and self._current_bucket > self._buy_setup_bar_at),
            "opened_valid_side": bool(buy_level is not None and self._current_candle_open < buy_level),
            "crossed": bool(buy_level is not None and baseline < buy_level <= ltp),
            "failed_attempt_block": bool(self._buy_setup_bar_at is not None and self._failed_attempt_blocks_setup("BUY", self._buy_setup_bar_at)),
        }
        delta_log(
            "trigger_check",
            mode=getattr(self.broker, "mode", "paper"),
            strategy=self.algo_id,
            asset=self.asset,
            minutes=self.minutes,
            reason=reason,
            settings=self.settings,
            ltp=ltp,
            baseline=baseline,
            current_candle_open=self._current_candle_open,
            current_bucket=self._current_bucket.replace(tzinfo=IST).isoformat() if self._current_bucket else None,
            sell_level=sell_level,
            sell_reference_time=self._sell_setup_bar_at.replace(tzinfo=IST).isoformat() if self._sell_setup_bar_at else None,
            sell_checks=sell_checks,
            buy_level=buy_level,
            buy_reference_time=self._buy_setup_bar_at.replace(tzinfo=IST).isoformat() if self._buy_setup_bar_at else None,
            buy_checks=buy_checks,
        )

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

    def recheck_triggers_after_settings_change(self, ltp, event_time=None):
        """Re-evaluate the active candle when a new offset moves a trigger."""
        if ltp is None:
            return
        try:
            self._check_triggers(
                float(ltp),
                event_time=event_time,
                baseline_override=self._current_candle_open,
                reason="settings_change",
            )
        except TypeError as exc:
            if "baseline_override" not in str(exc) and "reason" not in str(exc):
                raise
            self._check_triggers(float(ltp), event_time=event_time)

    def _entry_trigger(self, side, entry_price, trigger_level):
        mode = getattr(self.broker, "mode", "paper")
        return (
            f"{self.minutes}m Delta {side} EMA20 + volume EMA20 reference breakout "
            f"at {trigger_level:g}; {mode} fill {entry_price:g}"
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
        delta_log(
            "entry_fire_attempt",
            mode=getattr(self.broker, "mode", "paper"),
            settings=self.settings,
            strategy=self.algo_id,
            asset=self.asset,
            minutes=self.minutes,
            side=side,
            ltp=ltp,
            trigger_level=trigger_level,
            setup_time=reference_time,
            manual_guard=guard,
            current_position=bool(self._open_position()),
            cooldown=self.cooldown_status(),
            trading_enabled=bool(self.settings.get("trading_enabled", True)),
        )
        if guard and guard["side"] == side and guard["setup_time"] == reference_time:
            previous = self._prev_ltp
            crossed = previous is not None and (
                previous < trigger_level <= ltp if side == "BUY" else previous > trigger_level >= ltp
            )
            if not crossed:
                delta_log("entry_blocked_manual_guard", strategy=self.algo_id, minutes=self.minutes, side=side, previous_ltp=previous, trigger_level=trigger_level, ltp=ltp)
                return False
        return super()._fire_entry(side, ltp, trigger_level, setup_bar_at_override, event_time)

    def _three_candle_window(self, entry_bucket=None):
        eligible = [bar for bar in self._bars if entry_bucket is None or bar["time"] > entry_bucket]
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
            delta_log("entry_aborted_missing_symbol_or_price", strategy=self.algo_id, symbol=self.symbol, entry_price=entry_price)
            return False
        lots = effective_delta_lots(self.settings, getattr(self.broker, "product", {}), self.asset)
        direction = 1 if side == "BUY" else -1
        configured_sl = float(entry_price) - direction * float(self.settings["sl_points"])
        target = float(entry_price) + direction * float(self.settings["target_points"])
        activation = float(entry_price) + direction * float(self.settings["tsl_activate_points"])
        if min(configured_sl, target) <= 0:
            delta_log("entry_aborted_invalid_prices", strategy=self.algo_id, side=side, entry_price=entry_price, sl=configured_sl, target=target)
            return False

        snapshot = self._signal_snapshot(side, entry_price, trigger_level)
        snapshot["size_mode"] = self.settings.get("size_mode", "lots")
        snapshot["configured_lots"] = int(self.settings.get("silver_lots", 1) or 1)
        snapshot["configured_pax_size"] = self.settings.get("pax_size")
        snapshot["effective_lots"] = lots
        snapshot["leverage"] = self.settings.get("leverage")
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
        if mode == DELTA_EXIT_MODE_CONTINUOUS_LADDER:
            snapshot["delta_ladder_tsl"] = delta_ladder_snapshot(
                {"side": side, "entry_price": float(entry_price), "sl_price": configured_sl, "target_price": target},
                self.settings,
            )

        if mode == DELTA_EXIT_MODE_THREE_CANDLE:
            event = event_time or datetime.datetime.now(datetime.timezone.utc)
            event_stamp = event.timestamp() if event.tzinfo else event.replace(tzinfo=IST).timestamp()
            entry_bucket_stamp = int(event_stamp // (self.minutes * 60)) * self.minutes * 60
            entry_bucket = datetime.datetime.fromtimestamp(entry_bucket_stamp, IST).replace(tzinfo=None)
            snapshot["delta_three_candle_tsl"] = {
                "policy": DELTA_EXIT_MODE_THREE_CANDLE,
                "entry_bucket": entry_bucket.replace(tzinfo=IST).isoformat(),
                "status": "waiting_for_three_post_entry_candles",
                "window_rule": "rolling_latest_3_closed_strategy_candles_after_entry_bucket",
                "buffer_points": self.settings["tsl_buffer_points"],
                "events": [],
                "evaluations": [],
            }

        try:
            args = (self.symbol, side, lots, float(entry_price), effective_sl, target, self._entry_trigger(side, entry_price, trigger_level), snapshot)
            delta_log(
                "entry_submit",
                strategy=self.algo_id,
                asset=self.asset,
                mode=getattr(self.broker, "mode", "paper"),
                symbol=self.symbol,
                side=side,
                lots=lots,
                entry_price=float(entry_price),
                sl=effective_sl,
                target=target,
                trigger_level=trigger_level,
                exit_mode=mode,
                setup_time=snapshot.get("setup_time"),
            )
            if event_time is None:
                self.broker.open_trade(*args)
            else:
                self.broker.open_trade(*args, entry_time=_entry_time_iso(event_time))
            self.data_error = None
            delta_log("entry_submit_ok", strategy=self.algo_id, side=side, mode=getattr(self.broker, "mode", "paper"))
            return True
        except Exception as exc:
            self.data_error = str(exc)
            delta_log("entry_submit_failed", strategy=self.algo_id, side=side, error=self.data_error, mode=getattr(self.broker, "mode", "paper"))
            print(f"[delta-{getattr(self.broker, 'mode', 'paper')}] entry failed for {self.symbol}: {self.data_error}")
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
            "execution": getattr(self.broker, "mode", "paper"),
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
