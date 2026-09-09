from __future__ import annotations

import datetime
import time

from .algo3_silver_micro import (
    Algo3SilverMicro,
    _ema_step,
    _fmt,
    _is_bucket_closed,
    _is_bucket_settled,
    _latest_closed_minute_cutoff,
)
from ..runtime_mode import get_runtime_trading_mode
from ..silver_setup_history import get_latest_setup_reference, record_setup_event
from ..timezone import IST


BUCKET_MINUTES = 9


def _bucket_start_9m(ts: datetime.datetime) -> datetime.datetime:
    """Return the continuous 9-minute bucket anchored from 09:00 IST."""
    anchor = ts.replace(hour=9, minute=0, second=0, microsecond=0)
    elapsed_minutes = int((ts - anchor).total_seconds() // 60)
    if elapsed_minutes < 0:
        return anchor
    return anchor + datetime.timedelta(minutes=(elapsed_minutes // BUCKET_MINUTES) * BUCKET_MINUTES)


def _parse_volume_ema_from_source(source: str | None) -> float | None:
    marker = "9m_volume_ema:"
    raw = str(source or "")
    if marker not in raw:
        return None
    try:
        return float(raw.rsplit(marker, 1)[1].split(":", 1)[0])
    except (TypeError, ValueError):
        return None


class Algo6SilverVMicro(Algo3SilverMicro):
    """Paper-only Silver V Micro experiment using 9m price+volume EMA references."""

    algo_id = "algo6"
    display_name = "Silver V Micro - 9m EMA volume breakout"

    def __init__(self, watchlist: list[str] | None = None):
        super().__init__(watchlist=watchlist)
        self._ensure_volume_state()

    @staticmethod
    def _is_paper_mode_active() -> bool:
        return get_runtime_trading_mode() == "paper"

    def reload_settings(self, mode: str | None = None):
        # Keep the experiment isolated from live settings while we iterate.
        return super().reload_settings(mode="paper")

    def refresh_market_data(self, force: bool = False):
        if self._is_paper_mode_active():
            return super().refresh_market_data(force=force)

    def _load_history_background(self):
        # A data rebuild must not re-arm a previously fired reference or erase
        # the stop-loss cooldown. Entry checks remain blocked during replay.
        names = (
            "_last_fired_buy_bar_at", "_last_fired_sell_bar_at",
            "_last_attempted_buy_bar_at", "_last_attempted_sell_bar_at",
            "_entry_cooldown_until_monotonic", "_sl_cooldown_until_monotonic",
            "_buy_reentry_after_exit", "_sell_reentry_after_exit",
        )
        saved = {name: getattr(self, name, None) for name in names}
        self._reference_rebuilding = True
        try:
            super()._load_history_background()
        finally:
            for name, value in saved.items():
                setattr(self, name, value)
            self._reference_rebuilding = False

    def _references_current(self, now):
        if self._history_loading or getattr(self, "_reference_rebuilding", False):
            return False
        if not self._history_ready or self._history_error or not self._last_bar_at:
            return False
        bucket = _bucket_start_9m(now)
        last = datetime.datetime.fromisoformat(self._last_bar_at)
        if bucket.hour == 9 and bucket.minute == 0:
            # Opening-gap entries use the prior session's verified reference.
            return last < bucket and last.date() >= (now.date() - datetime.timedelta(days=4))
        return last == bucket - datetime.timedelta(minutes=BUCKET_MINUTES)

    def _request_reference_repair(self):
        now = time.monotonic()
        if now - getattr(self, "_last_reference_repair_at", float("-inf")) >= 30:
            self._last_reference_repair_at = now
            self.refresh_market_data(force=True)

    def _reset_aggregation_state(self) -> None:
        super()._reset_aggregation_state()
        self._ensure_volume_state(reset=True)

    def _ensure_volume_state(self, reset: bool = False) -> None:
        if reset or not hasattr(self, "_volume_ema20"):
            self._volume_ema20: float | None = None
        if reset or not hasattr(self, "_buy_setup_volume"):
            self._buy_setup_volume: float | None = None
        if reset or not hasattr(self, "_buy_setup_volume_ema20"):
            self._buy_setup_volume_ema20: float | None = None
        if reset or not hasattr(self, "_sell_setup_volume"):
            self._sell_setup_volume: float | None = None
        if reset or not hasattr(self, "_sell_setup_volume_ema20"):
            self._sell_setup_volume_ema20: float | None = None

    def on_trading_mode_switched(self, new_mode: str, previous_mode: str | None = None) -> None:
        if new_mode != "paper":
            self._buy_reentry_after_exit = None
            self._sell_reentry_after_exit = None
            self._entry_attempt_in_flight = False
            return
        super().on_trading_mode_switched(new_mode, previous_mode)

    def on_tick(self, symbol: str, ltp: float, timestamp):
        if not self._is_paper_mode_active():
            return
        if symbol != self.symbol:
            return
        now = datetime.datetime.now(IST).replace(tzinfo=None)
        if not self._references_current(now):
            self._last_tick_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._last_tick_ltp = float(ltp)
            self._prev_ltp = float(ltp)
            self._request_reference_repair()
            return
        super().on_tick(symbol, ltp, timestamp)

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if not self._is_paper_mode_active():
            return
        if symbol != self.symbol:
            return
        # Sparse WS/REST observations are unsuitable for volume references.
        self._last_minute_candle_at = candle["time"].isoformat()
        if not self._references_current(datetime.datetime.now(IST).replace(tzinfo=None)):
            self._request_reference_repair()

    def check_exits(self):
        if not self._is_paper_mode_active():
            return
        return super().check_exits()

    def square_off_all(self):
        for position in self.broker.open_positions():
            snapshot = position.get("signal_snapshot") or {}
            if bool(snapshot.get("overnight_carry_enabled")):
                continue
            ltp = position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")

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
        if self._last_ingested_minute_at is not None and candle_time <= self._last_ingested_minute_at:
            return
        self._last_ingested_minute_at = candle_time
        bucket = _bucket_start_9m(candle_time)

        if self._current_bucket is None:
            self._current_bucket = bucket
            self._minute_buffer = [minute_candle]
            return

        if bucket != self._current_bucket:
            self._finalize_bar(allow_signals=allow_signals)
            self._current_bucket = bucket
            self._minute_buffer = [minute_candle]
            return

        self._minute_buffer.append(minute_candle)

    def _finalize_bar(self, allow_signals: bool, require_closed: bool = True):
        if not self._minute_buffer or self._current_bucket is None:
            return
        if require_closed and not _is_bucket_closed(self._current_bucket, minutes=BUCKET_MINUTES):
            return
        expected = {self._current_bucket + datetime.timedelta(minutes=i) for i in range(BUCKET_MINUTES)}
        if {c["time"] for c in self._minute_buffer} != expected:
            self._minute_buffer = []
            return
        bar = {
            "time": self._current_bucket,
            "open": self._minute_buffer[0]["open"],
            "high": max(c["high"] for c in self._minute_buffer),
            "low": min(c["low"] for c in self._minute_buffer),
            "close": self._minute_buffer[-1]["close"],
            "volume": sum(c["volume"] for c in self._minute_buffer),
            "minute_count": len(self._minute_buffer),
            "source": "local_9m_volume_ema",
        }
        self._bars.append(bar)
        self._ema20 = _ema_step(self._ema20, bar["close"])
        self._volume_ema20 = _ema_step(self._volume_ema20, bar["volume"])
        bar["ema20"] = float(self._ema20) if self._ema20 is not None else None
        bar["volume_ema20"] = float(self._volume_ema20) if self._volume_ema20 is not None else None
        self._last_bar_at = bar["time"].isoformat()
        self._minute_buffer = []

        if allow_signals:
            color = "GREEN" if bar["close"] > bar["open"] else ("RED" if bar["close"] < bar["open"] else "DOJI")
            print(
                f"[algo6] 9m bar closed {bar['time'].isoformat()} "
                f"O={bar['open']:.2f} H={bar['high']:.2f} L={bar['low']:.2f} "
                f"C={bar['close']:.2f} EMA20={_fmt(self._ema20)} "
                f"V={bar['volume']:.0f} VEMA20={_fmt(self._volume_ema20)} "
                f"{color} minutes={bar['minute_count']}"
            )
        self._update_setups(bar, log=allow_signals)

    def flush_clock_closed_bar(self, allow_signals: bool | None = None) -> bool:
        if self._history_loading or getattr(self, "_reference_rebuilding", False):
            return False
        if not self._minute_buffer or self._current_bucket is None:
            return False
        if not _is_bucket_closed(self._current_bucket, minutes=BUCKET_MINUTES):
            return False
        if allow_signals is None:
            allow_signals = self.scan_enabled()
        if allow_signals and not _is_bucket_settled(self._current_bucket, minutes=BUCKET_MINUTES):
            return False
        bars_before = len(self._bars)
        self._finalize_bar(allow_signals=allow_signals, require_closed=True)
        return len(self._bars) > bars_before

    def _qualifies_as_buy_setup(self, bar: dict) -> bool:
        if self._ema20 is None or self._volume_ema20 is None:
            return False
        close = float(bar.get("close") or 0)
        open_price = float(bar.get("open") or 0)
        volume = float(bar.get("volume") or 0)
        return close > open_price and close > self._ema20 and volume > self._volume_ema20

    def _qualifies_as_sell_setup(self, bar: dict) -> bool:
        if self._ema20 is None or self._volume_ema20 is None:
            return False
        close = float(bar.get("close") or 0)
        open_price = float(bar.get("open") or 0)
        volume = float(bar.get("volume") or 0)
        return close < open_price and close < self._ema20 and volume > self._volume_ema20

    def _update_setups(self, bar: dict, log: bool = False):
        if self._ema20 is None or self._volume_ema20 is None:
            return
        close = float(bar.get("close") or 0)
        if self._qualifies_as_buy_setup(bar):
            old = self._buy_setup_close
            self._buy_setup_close = close
            self._buy_setup_bar_at = bar["time"]
            self._buy_setup_volume = float(bar.get("volume") or 0)
            self._buy_setup_volume_ema20 = float(self._volume_ema20)
            self._buy_reentry_after_exit = None
            if log:
                self._persist_setup_event("BUY", bar, source="live")
                print(
                    f"[algo6] BUY setup UPDATED {_fmt(old)} -> {close:.2f} "
                    f"(green 9m close > EMA20 {_fmt(self._ema20)}, "
                    f"volume {self._buy_setup_volume:.0f} > VEMA20 {_fmt(self._volume_ema20)} "
                    f"at {bar['time'].isoformat()})"
                )
        elif self._qualifies_as_sell_setup(bar):
            old = self._sell_setup_close
            self._sell_setup_close = close
            self._sell_setup_bar_at = bar["time"]
            self._sell_setup_volume = float(bar.get("volume") or 0)
            self._sell_setup_volume_ema20 = float(self._volume_ema20)
            self._sell_reentry_after_exit = None
            if log:
                self._persist_setup_event("SELL", bar, source="live")
                print(
                    f"[algo6] SELL setup UPDATED {_fmt(old)} -> {close:.2f} "
                    f"(red 9m close < EMA20 {_fmt(self._ema20)}, "
                    f"volume {self._sell_setup_volume:.0f} > VEMA20 {_fmt(self._volume_ema20)} "
                    f"at {bar['time'].isoformat()})"
                )
        elif log:
            print(
                f"[algo6] 9m bar did NOT update setup: O={float(bar.get('open') or 0):.2f} "
                f"C={close:.2f} EMA20={_fmt(self._ema20)} V={float(bar.get('volume') or 0):.0f} "
                f"VEMA20={_fmt(self._volume_ema20)}"
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
        ema20 = self._ema20 if ema20_override is None else ema20_override
        if ema20 is None or self._volume_ema20 is None:
            return
        if side == "BUY" and not self._qualifies_as_buy_setup(bar):
            return
        if side == "SELL" and not self._qualifies_as_sell_setup(bar):
            return
        record_setup_event(
            algo_id=self.algo_id,
            symbol=self.symbol,
            side=side,
            bar=bar,
            ema20=ema20,
            breakout_points=float(self.settings.get("silver_breakout_points", 200) or 200),
            source=f"{source}:9m_volume_ema:{float(self._volume_ema20):.8f}",
        )

    def _check_candle_close_trigger(self, bar: dict):
        return

    def _check_triggers(self, ltp: float, event_time=None):
        if self._history_loading or getattr(self, "_reference_rebuilding", False):
            return
        trigger_bucket = self._current_bucket
        if isinstance(event_time, datetime.datetime):
            local = event_time.astimezone(IST).replace(tzinfo=None) if event_time.tzinfo else event_time
            trigger_bucket = _bucket_start_9m(local)
        n = float(self.settings.get("silver_breakout_points", 200))
        if n <= 0:
            return
        prev = float(self._prev_ltp) if self._prev_ltp is not None else None
        buy_level = self._buy_setup_close + n if self._buy_setup_close is not None else None
        buy_opening_gap = bool(
            buy_level is not None
            and self._buy_setup_bar_at is not None
            and ltp >= buy_level
            and self._is_opening_gap_from_prior_session(event_time, self._buy_setup_bar_at)
        )
        buy_later_bucket = bool(
            buy_level is not None
            and self._buy_setup_bar_at is not None
            and trigger_bucket is not None
            and trigger_bucket > self._buy_setup_bar_at
        )
        if (
            buy_level is not None
            and (buy_opening_gap or buy_later_bucket)
            and (buy_opening_gap or (prev is not None and prev < buy_level <= ltp))
            and not self._already_fired_this_setup("BUY", self._buy_setup_bar_at)
            and not self._failed_attempt_blocks_setup("BUY")
        ):
            print(
                f"[algo6] TRIGGER BUY ({'opening gap' if buy_opening_gap else 'fresh upward cross'}): prev {_fmt(prev)} -> "
                f"LTP {ltp:.2f} crossed {buy_level:.2f}"
            )
            if self._fire_entry("BUY", buy_level, buy_level, event_time=event_time):
                self._buy_reentry_after_exit = None
                self._mark_fired("BUY", setup_bar_at=self._buy_setup_bar_at)
                return

        sell_level = self._sell_setup_close - n if self._sell_setup_close is not None else None
        sell_opening_gap = bool(
            sell_level is not None
            and self._sell_setup_bar_at is not None
            and ltp <= sell_level
            and self._is_opening_gap_from_prior_session(event_time, self._sell_setup_bar_at)
        )
        sell_later_bucket = bool(
            sell_level is not None
            and self._sell_setup_bar_at is not None
            and trigger_bucket is not None
            and trigger_bucket > self._sell_setup_bar_at
        )
        if (
            sell_level is not None
            and (sell_opening_gap or sell_later_bucket)
            and (sell_opening_gap or (prev is not None and prev > sell_level >= ltp))
            and not self._already_fired_this_setup("SELL", self._sell_setup_bar_at)
            and not self._failed_attempt_blocks_setup("SELL")
        ):
            print(
                f"[algo6] TRIGGER SELL ({'opening gap' if sell_opening_gap else 'fresh downward cross'}): prev {_fmt(prev)} -> "
                f"LTP {ltp:.2f} crossed {sell_level:.2f}"
            )
            if self._fire_entry("SELL", sell_level, sell_level, event_time=event_time):
                self._sell_reentry_after_exit = None
                self._mark_fired("SELL", setup_bar_at=self._sell_setup_bar_at)

    def _signal_snapshot(self, side: str, entry_price: float, trigger_level: float) -> dict:
        snapshot = super()._signal_snapshot(side, entry_price, trigger_level)
        snapshot.update({
            "timeframe": "9m",
            "logic_code": "V1",
            "algo_variant": "silver_v_micro",
            "volume_ema20": self._volume_ema20,
            "overnight_carry_enabled": bool(self.settings.get("overnight_carry_enabled")),
        })
        if side == "BUY":
            snapshot.update({
                "buy_setup_volume": self._buy_setup_volume,
                "buy_setup_volume_ema20": self._buy_setup_volume_ema20,
            })
        else:
            snapshot.update({
                "sell_setup_volume": self._sell_setup_volume,
                "sell_setup_volume_ema20": self._sell_setup_volume_ema20,
            })
        return snapshot

    def _entry_trigger(self, side: str, entry_price: float, trigger_level: float) -> str:
        n = float(self.settings.get("silver_breakout_points", 200) or 200)
        if side == "BUY":
            return (
                f"9m Silver V Micro BUY on {self.symbol}: green reference close "
                f"{_fmt(self._buy_setup_close)} + n={n:.0f} = trigger {_fmt(trigger_level)}, "
                f"fresh upward cross submitted at {entry_price:.2f}. EMA20 {_fmt(self._ema20)}, "
                f"volume {_fmt(self._buy_setup_volume)} > volume EMA20 {_fmt(self._buy_setup_volume_ema20)}."
            )
        return (
            f"9m Silver V Micro SELL on {self.symbol}: red reference close "
            f"{_fmt(self._sell_setup_close)} - n={n:.0f} = trigger {_fmt(trigger_level)}, "
            f"fresh downward cross submitted at {entry_price:.2f}. EMA20 {_fmt(self._ema20)}, "
            f"volume {_fmt(self._sell_setup_volume)} > volume EMA20 {_fmt(self._sell_setup_volume_ema20)}."
        )

    def _load_persisted_reference(self, side: str) -> tuple[float | None, datetime.datetime | None, float | None, float | None]:
        persisted = get_latest_setup_reference(self.algo_id, side=side, live_only=True)
        if not persisted:
            return None, None, None, None
        close = float(persisted.get("candle_close") or 0)
        raw_time = persisted.get("candle_time")
        bar_at = None
        if raw_time:
            try:
                bar_at = datetime.datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            except Exception:
                bar_at = None
        return (
            close,
            bar_at,
            float(persisted.get("candle_volume") or 0),
            _parse_volume_ema_from_source(persisted.get("source")),
        )

    def feed_status(self) -> dict:
        now = datetime.datetime.now(IST).replace(tzinfo=None)
        current = self._references_current(now)
        market_hours = datetime.time(9) <= now.time() < datetime.time(23, 30)
        history_error = self._history_error
        if market_hours and not current and not history_error:
            history_error = "Waiting for complete FYERS 9m history; new entries paused."
        buy_setup_close = self._buy_setup_close
        buy_setup_bar_at = self._buy_setup_bar_at
        buy_volume = self._buy_setup_volume
        buy_volume_ema = self._buy_setup_volume_ema20
        if not self._history_ready and (buy_setup_close is None or buy_setup_bar_at is None):
            buy_setup_close, buy_setup_bar_at, buy_volume, buy_volume_ema = self._load_persisted_reference("BUY")

        sell_setup_close = self._sell_setup_close
        sell_setup_bar_at = self._sell_setup_bar_at
        sell_volume = self._sell_setup_volume
        sell_volume_ema = self._sell_setup_volume_ema20
        if not self._history_ready and (sell_setup_close is None or sell_setup_bar_at is None):
            sell_setup_close, sell_setup_bar_at, sell_volume, sell_volume_ema = self._load_persisted_reference("SELL")

        return {
            "algo_id": self.algo_id,
            "display_name": self.display_name,
            "symbol": self.symbol,
            "history_ready": self._history_ready and (current or not market_hours),
            "history_loading": self._history_loading,
            "history_error": history_error,
            "reference_data_current": current,
            "reference_source": "fyers_1m_history",
            "last_manual_history_refresh_at": self._last_manual_history_refresh_at,
            "warmup_minute_candles": self._warmup_minute_candles,
            "timeframe": "9m",
            "timeframe_minutes": BUCKET_MINUTES,
            "bars_9m": len(self._bars),
            "bars_15m": len(self._bars),
            "minute_buffer_count": len(self._minute_buffer),
            "current_bucket": self._current_bucket.isoformat() if self._current_bucket else None,
            "ema20": self._ema20,
            "volume_ema20": self._volume_ema20,
            "silver_buy_plan": self._silver_buy_plan(),
            "buy_setup_close": buy_setup_close,
            "sell_setup_close": sell_setup_close,
            "buy_setup_bar_at": buy_setup_bar_at.isoformat() if buy_setup_bar_at else None,
            "sell_setup_bar_at": sell_setup_bar_at.isoformat() if sell_setup_bar_at else None,
            "buy_setup_volume": buy_volume,
            "buy_setup_volume_ema20": buy_volume_ema,
            "sell_setup_volume": sell_volume,
            "sell_setup_volume_ema20": sell_volume_ema,
            "n_points": self.settings.get("silver_breakout_points", 200),
            "last_tick_at": self._last_tick_at,
            "last_tick_ltp": self._last_tick_ltp,
            "last_minute_candle_at": self._last_minute_candle_at,
            "last_bar_at": self._last_bar_at,
            "silver_lots": int(self.settings.get("silver_lots", 1) or 1),
            "overnight_carry_enabled": bool(self.settings.get("overnight_carry_enabled")),
            "last_fired_buy_bar_at": self._last_fired_buy_bar_at.isoformat() if self._last_fired_buy_bar_at else None,
            "last_fired_sell_bar_at": self._last_fired_sell_bar_at.isoformat() if self._last_fired_sell_bar_at else None,
            "last_attempted_buy_bar_at": self._last_attempted_buy_bar_at.isoformat() if self._last_attempted_buy_bar_at else None,
            "last_attempted_sell_bar_at": self._last_attempted_sell_bar_at.isoformat() if self._last_attempted_sell_bar_at else None,
            "entry_attempt_in_flight": self._entry_attempt_in_flight,
            "entry_cooldown_remaining_seconds": int(self._entry_cooldown_remaining()),
            "post_sl_cooldown_remaining_seconds": int(self._post_sl_cooldown_remaining()),
        }
