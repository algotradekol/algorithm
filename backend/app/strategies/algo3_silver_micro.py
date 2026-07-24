"""
algo3_silver_micro.py

Single-instrument MCX strategy for Silver Micro (SILVERMIC).

Rules:
- 5-minute candles
- Buy setup: green candle closes above EMA20 and volume is above volume EMA20
- Sell setup: red candle closes below EMA20 and volume is above volume EMA20
- Confirmation: the very next 5-minute candle must continue in the same
  direction and close beyond the setup candle close
- Entry: next candle open (first live tick in the next 5-minute bucket)
- Reversal: if the opposite side confirms while a position is open, close the
  current position and flip on the next candle open
"""
from __future__ import annotations

import datetime
import threading
from collections import deque
from zoneinfo import ZoneInfo

from .base import Strategy
from ..fyers_client import get_intraday_candles_for_range
from ..mcx_symbols import get_active_mcx_contract
from ..paper_broker import PaperBroker
from ..strategy_settings import get_settings

IST = ZoneInfo("Asia/Kolkata")
EMA_PERIOD = 20
WARMUP_LOOKBACK_DAYS = 10


def _ema_step(previous: float | None, value: float, period: int = EMA_PERIOD) -> float:
    k = 2 / (period + 1)
    return float(value) if previous is None else float(value) * k + previous * (1 - k)


def _bucket_start(ts: datetime.datetime) -> datetime.datetime:
    minute = (ts.minute // 5) * 5
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
    display_name = "Silver Micro - 5m EMA/Volume"

    def __init__(self, watchlist: list[str] | None = None):
        self.symbol = (watchlist or [None])[0] if watchlist else get_active_mcx_contract("SILVERMIC")
        self.watchlist = [self.symbol] if self.symbol else []
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])

        self._history_lock = threading.Lock()
        self._history_loading = False
        self._history_ready = False

        self._minute_buffer: list[dict] = []
        self._current_bucket: datetime.datetime | None = None
        self._five_minute_candles: deque[dict] = deque(maxlen=300)
        self._ema20_price: float | None = None
        self._ema20_volume: float | None = None

        self._pending_setup: dict | None = None
        self._pending_entry: dict | None = None

        self.refresh_market_data()

    def reload_settings(self):
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

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
            for candle in history:
                self._ingest_minute_candle(candle, allow_signals=False)
            self._finalize_five_minute_candle(allow_signals=False)
            self._history_ready = True
            print(f"[algo3] warm-up loaded for {self.symbol}: {len(history)} one-minute candles")
        except Exception as exc:
            print(f"[algo3] warm-up failed for {self.symbol}: {exc}")
        finally:
            with self._history_lock:
                self._history_loading = False

    def on_tick(self, symbol: str, ltp: float, timestamp):
        if symbol != self.symbol:
            return
        self._maybe_execute_pending_entry(symbol, float(ltp), timestamp)

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if symbol != self.symbol:
            return
        self._ingest_minute_candle(candle, allow_signals=True)

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
            self._finalize_five_minute_candle(allow_signals=allow_signals)
            self._current_bucket = bucket
            self._minute_buffer = [minute_candle]
            return

        self._minute_buffer.append(minute_candle)

    def _finalize_five_minute_candle(self, allow_signals: bool):
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
        self._five_minute_candles.append(bar)
        self._ema20_price = _ema_step(self._ema20_price, bar["close"])
        self._ema20_volume = _ema_step(self._ema20_volume, bar["volume"])
        self._minute_buffer = []

        if allow_signals:
            self._evaluate_completed_bar(bar)

    def _evaluate_completed_bar(self, bar: dict):
        current_bucket = bar["time"]

        if self._pending_setup:
            expected_bucket = self._pending_setup["confirmation_bucket"]
            if current_bucket == expected_bucket:
                if self._confirmation_passed(bar, self._pending_setup):
                    self._pending_entry = {
                        "side": self._pending_setup["side"],
                        "entry_bucket": current_bucket + datetime.timedelta(minutes=5),
                        "setup_candle": self._pending_setup["setup_candle"],
                        "confirmation_candle": bar,
                    }
                self._pending_setup = None
            elif current_bucket > expected_bucket:
                self._pending_setup = None

        if len(self._five_minute_candles) < EMA_PERIOD:
            return

        side = self._condition_one_side(bar)
        if not side:
            return

        current_position = self._open_position()
        if current_position and current_position["side"] == side:
            return

        if current_position and current_position["side"] != side:
            self.broker.close_trade(current_position, bar["close"], "REVERSAL_CONTRA_SIGNAL")

        self._pending_setup = {
            "side": side,
            "setup_candle": bar,
            "setup_close": bar["close"],
            "confirmation_bucket": current_bucket + datetime.timedelta(minutes=5),
        }

    def _condition_one_side(self, bar: dict) -> str | None:
        if self._ema20_price is None or self._ema20_volume is None:
            return None

        is_green = bar["close"] > bar["open"]
        is_red = bar["close"] < bar["open"]
        above_ema = bar["close"] > self._ema20_price
        below_ema = bar["close"] < self._ema20_price
        strong_volume = bar["volume"] > self._ema20_volume

        if is_green and above_ema and strong_volume:
            return "BUY"
        if is_red and below_ema and strong_volume:
            return "SELL"
        return None

    def _confirmation_passed(self, bar: dict, pending_setup: dict) -> bool:
        side = pending_setup["side"]
        setup_close = float(pending_setup["setup_close"])
        if side == "BUY":
            return bar["close"] > bar["open"] and bar["close"] > setup_close
        return bar["close"] < bar["open"] and bar["close"] < setup_close

    def _maybe_execute_pending_entry(self, symbol: str, ltp: float, timestamp):
        if not self._pending_entry or symbol != self.symbol:
            return

        now = timestamp if isinstance(timestamp, datetime.datetime) else datetime.datetime.now(IST)
        if now.tzinfo is not None:
            now = now.astimezone(IST).replace(tzinfo=None)
        current_bucket = _bucket_start(now)
        if current_bucket < self._pending_entry["entry_bucket"]:
            return

        current_position = self._open_position()
        if current_position and current_position["side"] == self._pending_entry["side"]:
            self._pending_entry = None
            return

        if current_position and current_position["side"] != self._pending_entry["side"]:
            self.broker.close_trade(current_position, ltp, "REVERSAL_CONTRA_SIGNAL")

        entered = self._enter(
            side=self._pending_entry["side"],
            entry_price=ltp,
            setup_candle=self._pending_entry["setup_candle"],
            confirmation_candle=self._pending_entry["confirmation_candle"],
        )
        if entered:
            self._pending_entry = None

    def _enter(self, side: str, entry_price: float, setup_candle: dict | None = None, confirmation_candle: dict | None = None) -> bool:
        if not self.symbol or not entry_price:
            return False
        qty = int(self.settings["capital_per_trade"] // float(entry_price))
        if qty < 1:
            return False

        if side == "BUY":
            sl_price = float(entry_price) * (1 - float(self.settings["sl_pct"]) / 100)
            target_price = float(entry_price) * (1 + float(self.settings["target_pct"]) / 100)
        else:
            sl_price = float(entry_price) * (1 + float(self.settings["sl_pct"]) / 100)
            target_price = float(entry_price) * (1 - float(self.settings["target_pct"]) / 100)

        trigger = self._entry_trigger(side, entry_price, setup_candle, confirmation_candle)
        snapshot = self._signal_snapshot(side, entry_price, setup_candle, confirmation_candle)
        try:
            self.broker.open_trade(self.symbol, side, qty, float(entry_price), sl_price, target_price, trigger, snapshot)
            return True
        except Exception as exc:
            print(f"[algo3] paper entry failed for {self.symbol}: {exc}")
            return False

    def _entry_trigger(self, side: str, entry_price: float, setup_candle: dict | None, confirmation_candle: dict | None) -> str:
        setup = setup_candle or {}
        confirm = confirmation_candle or {}
        setup_close = setup.get("close")
        confirm_close = confirm.get("close")
        ema_price = _fmt(self._ema20_price)
        ema_volume = _fmt(self._ema20_volume)
        setup_close_text = _fmt(setup_close)
        confirm_close_text = _fmt(confirm_close)
        return (
            f"5m {side.lower()} setup on {self.symbol}: "
            f"condition-1 candle closed {'above' if side == 'BUY' else 'below'} EMA20 with volume above volume EMA20; "
            f"immediate confirmation candle closed {'higher' if side == 'BUY' else 'lower'} than setup close; "
            f"entered on next candle open near {entry_price:.2f}. "
            f"Setup close {setup_close_text}, confirmation close {confirm_close_text}, EMA20 {ema_price}, volume EMA20 {ema_volume}."
        )

    def _signal_snapshot(self, side: str, entry_price: float, setup_candle: dict | None, confirmation_candle: dict | None) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": "5m",
            "side": side,
            "entry_ltp": entry_price,
            "ema20_price": self._ema20_price,
            "ema20_volume": self._ema20_volume,
            "setup_candle": setup_candle,
            "confirmation_candle": confirmation_candle,
        }

    def _open_position(self) -> dict | None:
        for position in self.broker.open_positions():
            if position.get("symbol") == self.symbol:
                return position
        return None

    def check_exits(self):
        position = self._open_position()
        if not position:
            return
        ltp = position.get("_last_ltp")
        if not ltp:
            return
        position = self.broker.apply_trailing_stop(position, float(ltp), self.settings)
        side, sl, target = position["side"], float(position["sl_price"]), float(position["target_price"])
        use_target = self.broker.should_exit_at_target(self.settings)

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
