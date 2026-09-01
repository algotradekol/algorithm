import datetime

from .algo3_silver_micro import Algo3SilverMicro, _fmt
from ..silver_setup_history import get_latest_setup_reference, record_setup_event
from ..trailing_stop import calculate_candle_pair_trailing, uses_silver_candle_pair_tsl


class Algo5SilverMicro2(Algo3SilverMicro):
    """Silver Micro experiment with current-first EMA-wick fallback setups."""

    algo_id = "algo5"
    display_name = "Silver Micro 2.0 - 15m reference BUY / red-chain SELL"

    _CURRENT_REFERENCE = "current"
    _FALLBACK_EMA_WICK_REFERENCE = "fallback_ema_wick"

    def __init__(self, watchlist: list[str] | None = None):
        # Parent startup starts its history warmup asynchronously, so define
        # the Algo5-only metadata before it can replay a completed candle.
        self._buy_setup_family: str | None = None
        self._sell_setup_family: str | None = None
        self._setup_references: dict[str, dict[str, dict | None]] = self._empty_setup_references()
        super().__init__(watchlist=watchlist)

    @staticmethod
    def _empty_setup_references() -> dict[str, dict[str, dict | None]]:
        return {
            "BUY": {"current": None, "fallback_ema_wick": None},
            "SELL": {"current": None, "fallback_ema_wick": None},
        }

    def _reset_aggregation_state(self):
        super()._reset_aggregation_state()
        self._buy_setup_family = None
        self._sell_setup_family = None
        self._setup_references = self._empty_setup_references()

    def _remember_reference(self, side: str, family: str, bar: dict) -> None:
        self._setup_references[side][family] = {
            "close": float(bar["close"]),
            "time": bar["time"],
            "ema20": float(self._ema20) if self._ema20 is not None else None,
            "family": family,
        }

    def _ema_wick_distance_points(self) -> float:
        return float(self.settings.get("ema_wick_distance_points", 300) or 300)

    def _buy_setup_family_for(self, bar: dict) -> str | None:
        if self._ema20 is None:
            return None
        open_price = float(bar.get("open") or 0)
        close = float(bar.get("close") or 0)
        low = float(bar.get("low", close) or close)

        # Existing Silver BUY is always preferred.
        if close > open_price and close > self._ema20:
            return self._CURRENT_REFERENCE
        # Fallback BUY: a red candle closes above EMA and its wick touches
        # EMA or comes within the configured distance above it.
        if (
            open_price > close
            and close > self._ema20
            and low <= self._ema20 + self._ema_wick_distance_points()
        ):
            return self._FALLBACK_EMA_WICK_REFERENCE
        return None

    def _sell_setup_family_for(self, bar: dict) -> str | None:
        if self._ema20 is None:
            return None
        open_price = float(bar.get("open") or 0)
        close = float(bar.get("close") or 0)
        high = float(bar.get("high", close) or close)

        # Existing Silver SELL is always preferred.
        if close < open_price and close < self._ema20:
            return self._CURRENT_REFERENCE
        # Fallback SELL: a green candle closes below EMA and its wick touches
        # EMA or comes within the configured distance below it.
        if (
            open_price < close
            and close < self._ema20
            and high >= self._ema20 - self._ema_wick_distance_points()
        ):
            return self._FALLBACK_EMA_WICK_REFERENCE
        return None

    def _qualifies_as_buy_setup(self, bar: dict) -> bool:
        return self._buy_setup_family_for(bar) is not None

    def _qualifies_as_sell_setup(self, bar: dict) -> bool:
        return self._sell_setup_family_for(bar) is not None

    def _check_triggers(self, ltp: float, event_time=None):
        """Add the Algo5-only SELL exception, then retain the parent flow.

        The standard red-chain path remains entirely in the parent. For an
        EMA-wick fallback SELL reference, the spec permits a later green (or
        red) candle to cross the trigger. We place that otherwise-blocked
        entry here, then delegate so normal BUY/reversal handling is intact.
        """
        if self._sell_setup_family != self._FALLBACK_EMA_WICK_REFERENCE:
            return super()._check_triggers(ltp, event_time=event_time)

        n = float(self.settings.get("silver_breakout_points", 150) or 0)
        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None
        current_bucket_open = (
            float(self._minute_buffer[0]["open"])
            if self._minute_buffer and self._current_bucket is not None
            else None
        )
        legacy_red_path = bool(
            sell_level is not None
            and self._ema20 is not None
            and self._sell_setup_bar_at is not None
            and self._current_bucket is not None
            and self._current_bucket > self._sell_setup_bar_at
            and current_bucket_open is not None
            and ltp < current_bucket_open
            and ltp < self._ema20
        )
        fallback_later_candle = bool(
            sell_level is not None
            and self._sell_setup_bar_at is not None
            and self._current_bucket is not None
            and self._current_bucket > self._sell_setup_bar_at
        )
        same_reference_downturn = bool(
            self._sell_reentry_after_exit
            and sell_level is not None
            and self._sell_reentry_after_exit.get("setup_bar_at") == self._sell_setup_bar_at
            and self._prev_ltp is not None
            and ltp <= sell_level
            and ltp < float(self._prev_ltp)
        )
        crossed_downward = bool(
            ltp <= sell_level
            and (self._prev_ltp is None or self._prev_ltp > sell_level)
        )
        if (
            fallback_later_candle
            and not legacy_red_path
            and (crossed_downward or same_reference_downturn)
            and not self._failed_attempt_blocks_setup("SELL", self._sell_setup_bar_at)
        ):
            print(
                f"[algo5] TRIGGER SELL (EMA-wick fallback): reference "
                f"{self._sell_setup_close:.2f} - n={n:.0f} = level {sell_level:.2f}; LTP={ltp:.2f}"
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

        return super()._check_triggers(ltp, event_time=event_time)

    def _update_setups(self, bar: dict, log: bool = False):
        if self._ema20 is None:
            return
        buy_family = self._buy_setup_family_for(bar)
        sell_family = self._sell_setup_family_for(bar)
        close = float(bar["close"])

        if buy_family is not None:
            old = self._buy_setup_close
            self._buy_setup_close = close
            self._buy_setup_bar_at = bar["time"]
            self._buy_setup_family = buy_family
            self._remember_reference("BUY", buy_family, bar)
            self._buy_reentry_after_exit = None
            if log:
                self._persist_setup_event("BUY", bar, source="live")
                print(
                    f"[algo5] BUY {buy_family} setup UPDATED {_fmt(old)} -> {close:.2f} "
                    f"(EMA20 {_fmt(self._ema20)} at {bar['time'].isoformat()})"
                )
            return

        if sell_family is not None:
            old = self._sell_setup_close
            self._sell_setup_close = close
            self._sell_setup_bar_at = bar["time"]
            self._sell_setup_family = sell_family
            self._remember_reference("SELL", sell_family, bar)
            self._sell_reentry_after_exit = None
            if log:
                self._persist_setup_event("SELL", bar, source="live")
                print(
                    f"[algo5] SELL {sell_family} setup UPDATED {_fmt(old)} -> {close:.2f} "
                    f"(EMA20 {_fmt(self._ema20)} at {bar['time'].isoformat()})"
                )
            return

        if log:
            print(
                f"[algo5] bar did NOT update setup: O={float(bar['open']):.2f} "
                f"H={float(bar.get('high') or 0):.2f} L={float(bar.get('low') or 0):.2f} "
                f"C={close:.2f} EMA20={_fmt(self._ema20)}"
            )

    def _persist_setup_event(
        self,
        side: str,
        bar: dict,
        source: str,
        ema20_override: float | None = None,
    ) -> None:
        if not self.symbol:
            return
        family = self._buy_setup_family if side == "BUY" else self._sell_setup_family
        ema20 = self._ema20 if ema20_override is None else ema20_override
        if family is None or ema20 is None:
            return
        event_source = source
        if family == self._FALLBACK_EMA_WICK_REFERENCE:
            event_source = f"{source}:fallback_ema_wick:{self._ema_wick_distance_points():g}"
        record_setup_event(
            algo_id=self.algo_id,
            symbol=self.symbol,
            side=side,
            bar=bar,
            ema20=ema20,
            breakout_points=float(self.settings.get("silver_breakout_points", 150) or 150),
            source=event_source,
        )

    def _entry_trigger(self, side: str, entry_price: float, trigger_level: float) -> str:
        family = self._buy_setup_family if side == "BUY" else self._sell_setup_family
        label = "current" if family == self._CURRENT_REFERENCE else "EMA-wick fallback"
        direction = "upward" if side == "BUY" else "downward"
        setup_close = self._buy_setup_close if side == "BUY" else self._sell_setup_close
        n = self.settings.get("silver_breakout_points", 200)
        operator = "+" if side == "BUY" else "-"
        return (
            f"15m Silver Micro 2.0 {side} {label} reference on {self.symbol}: "
            f"setup close {_fmt(setup_close)} {operator} n={n} = trigger {_fmt(trigger_level)}, "
            f"LTP crossed {direction} at {entry_price:.2f}. EMA20 {_fmt(self._ema20)}."
        )

    def _signal_snapshot(self, side: str, entry_price: float, trigger_level: float) -> dict:
        snapshot = super()._signal_snapshot(side, entry_price, trigger_level)
        snapshot["setup_family"] = self._buy_setup_family if side == "BUY" else self._sell_setup_family
        snapshot["ema_wick_distance_points"] = self._ema_wick_distance_points()
        # Capture policy at entry so turning the settings toggle off later
        # cannot unexpectedly square off an already carried paper position.
        snapshot["overnight_carry_enabled"] = bool(self.settings.get("overnight_carry_enabled"))
        return snapshot

    def square_off_all(self):
        """Keep opted-in 2.0 paper positions open across the session boundary."""
        for position in self.broker.open_positions():
            snapshot = position.get("signal_snapshot") or {}
            if bool(snapshot.get("overnight_carry_enabled")):
                continue
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")

    def _latest_candle_pair(self, side: str) -> tuple[dict, dict] | None:
        """Find the latest completed reversal pair, including pre-arm bars."""
        bars = list(self._bars)
        for index in range(len(bars) - 1, 0, -1):
            first_bar, second_bar = bars[index - 1], bars[index]
            if calculate_candle_pair_trailing(
                side=side,
                first_bar=first_bar,
                second_bar=second_bar,
                buffer_points=self._candle_pair_buffer_points(),
            ) is not None:
                return first_bar, second_bar
        return None

    def _candle_pair_buffer_points(self) -> float:
        return float(self.settings.get("tsl_lock_step_points", 100) or 100)

    def _apply_candle_pair_trailing(self, position: dict, ltp: float) -> dict | None:
        """Replace the stop from the newest completed pair once breakeven is armed."""
        if not uses_silver_candle_pair_tsl(position) or not position.get("trailing_sl_active"):
            return position
        pair = self._latest_candle_pair(str(position.get("side") or ""))
        if pair is None:
            return position
        first_bar, second_bar = pair
        candidate = calculate_candle_pair_trailing(
            side=position.get("side"),
            first_bar=first_bar,
            second_bar=second_bar,
            buffer_points=self._candle_pair_buffer_points(),
        )
        if candidate is None:
            return position
        candidate_sl = float(candidate["candidate_sl"])
        side = str(position.get("side") or "").upper()
        # A protective stop must remain on the safe side of the executable
        # price. If the historic pair level is already breached, flatten now
        # rather than send FYERS an invalid stop amendment.
        already_breached = candidate_sl >= ltp if side == "BUY" else candidate_sl <= ltp
        if already_breached:
            print(
                f"[algo5] candle-pair SL already crossed for {side}: "
                f"candidate={candidate_sl:.2f}, LTP={ltp:.2f}; closing safely"
            )
            self.broker.close_trade(position, ltp, "TRAILING_SL")
            if side == "BUY":
                self._arm_buy_reentry_after_exit("TRAILING_SL")
            else:
                self._arm_sell_reentry_after_exit("TRAILING_SL")
            return None
        return self.broker.apply_candle_pair_trailing_stop(
            position,
            ltp,
            first_bar,
            second_bar,
            self._candle_pair_buffer_points(),
        )

    def check_exits(self):
        """Keep parent exits, with Algo5's pair trail after breakeven arms."""
        position = self._open_position()
        if not position:
            return
        snapshot = position.get("signal_snapshot") or {}
        if isinstance(snapshot, dict) and (
            snapshot.get("fyers_app_managed")
            or snapshot.get("origin") in {"fyers_app_manual", "fyers_recovered_position"}
        ):
            return
        ltp = position.get("_last_ltp") or self._last_tick_ltp
        if not ltp:
            return
        ltp = float(ltp)
        effective_settings = self._trailing_settings_for(position)
        position = self.broker.apply_trailing_stop(position, ltp, effective_settings)
        position = self._apply_candle_pair_trailing(position, ltp)
        if not position:
            return
        side = position["side"]
        sl = float(position["sl_price"])
        target = float(position["target_price"])
        try:
            use_target = self.broker.should_exit_at_target(effective_settings, position)
        except TypeError:
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

    def _finalize_bar(self, allow_signals: bool, require_closed: bool = True):
        bars_before = len(self._bars)
        super()._finalize_bar(allow_signals=allow_signals, require_closed=require_closed)
        if len(self._bars) == bars_before:
            return
        position = self._open_position()
        ltp = (position or {}).get("_last_ltp") or self._last_tick_ltp
        if position and ltp:
            self._apply_candle_pair_trailing(position, float(ltp))

    def feed_status(self) -> dict:
        status = super().feed_status()
        n = float(self.settings.get("silver_breakout_points", 200) or 0)
        slots: dict[str, dict] = {}
        for side in ("BUY", "SELL"):
            for family in (self._CURRENT_REFERENCE, self._FALLBACK_EMA_WICK_REFERENCE):
                reference = self._setup_references[side][family]
                if reference is None:
                    persisted = get_latest_setup_reference(
                        self.algo_id,
                        side=side,
                        live_only=True,
                        setup_family=family,
                    )
                    if persisted:
                        raw_time = persisted.get("candle_time")
                        try:
                            reference_time = datetime.datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                        except Exception:
                            reference_time = None
                        reference = {
                            "close": float(persisted.get("candle_close") or 0),
                            "time": reference_time,
                            "ema20": float(persisted.get("ema20") or 0),
                            "family": family,
                        }
                close = reference.get("close") if reference else None
                slots[f"{side.lower()}_{family}"] = {
                    "close": close,
                    "time": reference["time"].isoformat() if reference and reference.get("time") else None,
                    "ema20": reference.get("ema20") if reference else None,
                    "trigger_level": (float(close) + n) if close is not None and side == "BUY" else ((float(close) - n) if close is not None else None),
                    "family": family,
                }
        status["reference_slots"] = slots
        status["ema_wick_distance_points"] = self._ema_wick_distance_points()
        # Expose the latest completed pair even before a position is open, so
        # the dashboard can show exactly which 15-minute bars a future pair
        # TSL move would use after breakeven arms.
        pair_slots: dict[str, dict | None] = {}
        for side, key in (("BUY", "buy_red_green"), ("SELL", "sell_green_red")):
            pair = self._latest_candle_pair(side)
            if pair is None:
                pair_slots[key] = None
                continue
            pair_slots[key] = calculate_candle_pair_trailing(
                side=side,
                first_bar=pair[0],
                second_bar=pair[1],
                buffer_points=self._candle_pair_buffer_points(),
            )
        status["candle_pair_slots"] = pair_slots
        return status
