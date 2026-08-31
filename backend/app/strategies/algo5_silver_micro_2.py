from .algo3_silver_micro import Algo3SilverMicro, _fmt
from ..silver_setup_history import record_setup_event


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
        super().__init__(watchlist=watchlist)

    def _reset_aggregation_state(self):
        super()._reset_aggregation_state()
        self._buy_setup_family = None
        self._sell_setup_family = None

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
        return snapshot
