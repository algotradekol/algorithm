import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from .base import Strategy
from ..fyers_auth import get_stored_access_token
from ..fyers_client import get_previous_close, get_recent_intraday_candles
from ..paper_broker import PaperBroker

MIN_GAP_PCT = 0.5
MAX_GAP_PCT = 2.0
TICK_SIZE = 0.05
# A one-minute candle only exists for a symbol that traded during that minute.
# Keep a small health floor, rather than incorrectly requiring all NSE 500
# symbols to print an opening tick.
MIN_OPENING_READY_SYMBOLS = 10


class Algo4OpeningRangeIndicators(Strategy):
    algo_id = "algo4"
    display_name = "Algo 4 — Opening Range Gap (With Indicators)"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.prev_close: dict[str, float] = {}
        self.candles: dict[str, list[dict]] = defaultdict(list)
        self.total_value: dict[str, float] = defaultdict(float)
        self.warmup_loaded: set[str] = set()
        self.candidates: dict[str, tuple[str, float]] = {}
        self.candidate_details: dict[str, dict] = {}
        self.selected_symbols: set[str] = set()
        self.scan_seen_symbols: set[str] = set()
        self.entries_evaluated_today = None
        self._previous_close_load_lock = threading.Lock()
        self._previous_close_loading = False
        self.refresh_market_data()

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def _load_previous_closes_background(self):
        try:
            if not get_stored_access_token():
                print(f"[{self.algo_id}] no Fyers access token yet, skipping previous-close preload")
                return
            def load_symbol(symbol: str):
                try:
                    warmup = get_recent_intraday_candles(symbol, resolution="1", days=7, limit=120)
                    if warmup:
                        return symbol, warmup, float(warmup[-1]["close"])
                    else:
                        close = get_previous_close(symbol)
                        return symbol, None, close
                except Exception as e:
                    print(f"[{self.algo_id}] couldn't get prev close for {symbol}: {e}")
                    return symbol, None, None

            # Keep the pre-market preload bounded. Sequential history calls
            # make a post-deploy restart miss the 09:16 opening scan.
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(load_symbol, symbol) for symbol in self.watchlist]
                for future in as_completed(futures):
                    symbol, warmup, close = future.result()
                    if warmup:
                        self.candles[symbol] = warmup
                        self.warmup_loaded.add(symbol)
                    if close:
                        self.prev_close[symbol] = float(close)
            print(f"[{self.algo_id}] indicator warmup loaded for {len(self.warmup_loaded)}/{len(self.watchlist)} symbols")
        except Exception as e:
            print(f"[{self.algo_id}] error in background preload: {e}")
        finally:
            with self._previous_close_load_lock:
                self._previous_close_loading = False

    def refresh_market_data(self):
        """Retry the preload after manual OAuth has made a token available."""
        with self._previous_close_load_lock:
            if self._previous_close_loading or len(self.prev_close) >= len(self.watchlist):
                return
            self._previous_close_loading = True
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def set_previous_close(self, symbol: str, previous_close: float):
        if symbol in self.watchlist and previous_close > 0:
            self.prev_close[symbol] = previous_close

    def scan_candle_time(self) -> str:
        return self.settings.get("test_candle_time", "11:10") if self.settings.get("test_schedule_enabled") else "09:15"

    def entry_window(self, current_time: str) -> bool:
        entry = (datetime.datetime.strptime(self.scan_candle_time(), "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M")
        return entry <= current_time < (datetime.datetime.strptime(entry, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M")

    def entry_window_elapsed(self, current_time: str) -> bool:
        deadline = (datetime.datetime.strptime(self.scan_candle_time(), "%H:%M") + datetime.timedelta(minutes=2)).strftime("%H:%M")
        return current_time >= deadline

    def schedule_status(self, now: datetime.datetime) -> dict:
        if not self.settings.get("test_schedule_enabled"):
            return {"enabled": False}
        candle_time = self.scan_candle_time()
        entry_time = (datetime.datetime.strptime(candle_time, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M")
        current_time = now.strftime("%H:%M")
        state = "waiting"
        if candle_time <= current_time < entry_time:
            state = "collecting_candle"
        elif entry_time <= current_time < (datetime.datetime.strptime(entry_time, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M"):
            state = "evaluating_entries"
        elif current_time >= (datetime.datetime.strptime(entry_time, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M"):
            state = "finished"
        return {"enabled": True, "candle_time": candle_time, "entry_time": entry_time, "state": state}

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        history = self.candles[symbol]
        history.append(candle)
        if len(history) > 120:
            del history[:-120]
        self.total_value[symbol] += float(candle["close"]) * float(candle.get("volume") or 0)

        if candle["time"].strftime("%H:%M") != self.scan_candle_time():
            return

        self.scan_seen_symbols.add(symbol)

        side, details = self._signal_details(symbol, candle, indicators)
        if details:
            self.candidate_details[symbol] = details
        if side and details and details["passed_indicators"]:
            self.candidates[symbol] = (side, candle["close"])

    def evaluate_entries(self, get_ltp_fn):
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return True
        if not self._opening_data_ready():
            self._record_scan_results(scan_status="incomplete", scan_message=self._opening_data_message())
            return False
        self.entries_evaluated_today = today

        for symbol, (side, fallback_price) in list(self.candidates.items()):
            entry_price = get_ltp_fn(symbol) or fallback_price
            self._enter(symbol, side, entry_price)
        self._record_scan_results()
        return True

    def _opening_data_ready(self) -> bool:
        if self.settings.get("test_schedule_enabled"):
            return bool(self._opening_ready_symbols())
        return len(self._opening_ready_symbols()) >= min(MIN_OPENING_READY_SYMBOLS, len(self.watchlist))

    def _opening_ready_symbols(self) -> set[str]:
        return self.scan_seen_symbols & set(self.prev_close)

    def _opening_data_message(self) -> str:
        required = min(MIN_OPENING_READY_SYMBOLS, len(self.watchlist))
        ready_count = len(self._opening_ready_symbols())
        return (
            "Opening scan was not eligible for entry: "
            f"received {len(self.scan_seen_symbols)}/{len(self.watchlist)} {self.scan_candle_time()} IST candles and "
            f"matched {ready_count}/{len(self.watchlist)} symbols with previous closes "
            f"(requires at least {required} ready symbols to detect a healthy feed). No late trades will be placed."
        )

    def mark_opening_scan_missed(self):
        if self.entries_evaluated_today == datetime.date.today():
            return
        self._record_scan_results(scan_status="missed_data", scan_message=self._opening_data_message())

    def _signal_details(self, symbol: str, candle: dict, indicators: dict) -> tuple[str | None, dict | None]:
        prev_close = self.prev_close.get(symbol)
        if not prev_close:
            return None, None

        open_price = candle["open"]
        buy_gap = open_price - prev_close
        sell_gap = prev_close - open_price
        buy_shape = abs(open_price - candle["low"]) <= TICK_SIZE
        sell_shape = abs(open_price - candle["high"]) <= TICK_SIZE
        buy_base = (
            buy_shape and
            MIN_GAP_PCT / 100 * prev_close <= buy_gap <= MAX_GAP_PCT / 100 * prev_close
        )
        sell_base = (
            sell_shape and
            MIN_GAP_PCT / 100 * prev_close <= sell_gap <= MAX_GAP_PCT / 100 * prev_close
        )

        side = "BUY" if buy_base else "SELL" if sell_base else None
        indicator_results = self._indicator_results(symbol, candle, indicators, side) if side else {}
        passed_indicators = bool(side) and all(
            result["passed"] for result in indicator_results.values() if result["enabled"]
        )
        gap = buy_gap if side == "BUY" else sell_gap if side == "SELL" else abs(open_price - prev_close)
        return side, {
            "symbol": symbol,
            "side": side or "WATCH",
            "open": open_price,
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "prev_close": prev_close,
            "gap_pct": gap / prev_close * 100,
            "candle_received": True,
            "shape_passed": buy_shape or sell_shape,
            "gap_passed": bool(side),
            "opening_range_gap_passed": bool(side),
            "passed_indicators": passed_indicators,
            "indicator_results": indicator_results,
            "warmup_candles": max(0, len(self.candles[symbol]) - 1),
            "selected_for_trade": False,
            "rejection_reason": None if passed_indicators else ("failed_indicator_filter" if side else "failed_opening_range_gap"),
        }

    def _passes_indicator_filters(self, symbol: str, candle: dict, indicators: dict, side: str) -> bool:
        return all(
            result["passed"] for result in self._indicator_results(symbol, candle, indicators, side).values()
            if result["enabled"]
        )

    def _indicator_results(self, symbol: str, candle: dict, indicators: dict, side: str) -> dict:
        ltp = float(indicators.get("last_ltp") or candle["close"])
        vwap = indicators.get("vwap")
        candles = self.candles[symbol]
        ema20 = self._ema(candles, 20)
        ema50 = self._ema(candles, 50)
        rsi14 = self._rsi(candles, 14)
        adx14 = self._adx(candles, 14)
        supertrend = self._supertrend(candles, self.settings["supertrend_period"], self.settings["supertrend_multiplier"])
        is_buy = side == "BUY"

        def item(value, passed, enabled):
            return {"value": value, "passed": bool(passed), "enabled": bool(enabled)}

        return {
            "vwap": item(vwap, vwap is not None and (ltp > float(vwap) if is_buy else ltp < float(vwap)), self.settings.get("filter_vwap", True)),
            "rsi": item(rsi14, rsi14 is not None and (rsi14 > self.settings["rsi_buy_threshold"] if is_buy else rsi14 < self.settings["rsi_sell_threshold"]), self.settings.get("filter_rsi", True)),
            "adx": item(adx14, adx14 is not None and adx14 > self.settings["adx_threshold"], self.settings.get("filter_adx", True)),
            "supertrend": item(supertrend, supertrend is not None and (ltp > supertrend if is_buy else ltp < supertrend), self.settings.get("filter_supertrend", True)),
            "ema20": item(ema20, ema20 is not None and (ltp > ema20 if is_buy else ltp < ema20), self.settings.get("filter_ema20", False)),
            "ema50": item(ema50, ema20 is not None and ema50 is not None and (ema20 > ema50 if is_buy else ema20 < ema50), self.settings.get("filter_ema50", False)),
            "volume": item(float(candle.get("volume") or 0), float(candle.get("volume") or 0) > self.settings["min_volume"], self.settings.get("filter_volume", True)),
            "liquidity": item(self.total_value[symbol], self.total_value[symbol] > self.settings["min_total_value"], self.settings.get("filter_liquidity", True)),
            "price_range": item(ltp, self.settings["ltp_min"] < ltp < self.settings["ltp_max"], self.settings.get("filter_price_range", True)),
        }

    def active_filters(self) -> list[str]:
        return [k.replace("filter_", "") for k, v in self.settings.items() if k.startswith("filter_") and v]

    def _is_entry_window(self, now: datetime.datetime) -> bool:
        start = now.replace(hour=9, minute=16, second=0, microsecond=0)
        end = now.replace(hour=9, minute=17, second=0, microsecond=0)
        return start <= now < end

    def _can_open_side(self, side: str) -> bool:
        state = self.broker.summary()
        if state["trade_count_today"] >= self.settings["max_trades_per_day"]:
            return False
        if side == "BUY":
            return state["buy_count_today"] < self.settings["max_buy_trades"] or state["sell_count_today"] == self.settings["max_sell_trades"]
        return state["sell_count_today"] < self.settings["max_sell_trades"] or state["buy_count_today"] == self.settings["max_buy_trades"]

    def _has_open_position(self, symbol: str) -> bool:
        return any(position["symbol"] == symbol for position in self.broker.open_positions())

    def _enter(self, symbol: str, side: str, entry_price: float):
        if not entry_price or self.broker.already_traded_today(symbol) or not self._can_open_side(side):
            return

        qty = int(self.settings["capital_per_trade"] // entry_price)
        if qty < 1:
            return

        if side == "BUY":
            sl_price = entry_price * (1 - self.settings["sl_pct"] / 100)
            target_price = entry_price * (1 + self.settings["target_pct"] / 100)
        else:
            sl_price = entry_price * (1 + self.settings["sl_pct"] / 100)
            target_price = entry_price * (1 - self.settings["target_pct"] / 100)
        self.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price, self._entry_trigger(symbol, side))
        self.selected_symbols.add(symbol)

    def _entry_trigger(self, symbol: str, side: str) -> str:
        details = self.candidate_details.get(symbol, {})
        indicator_results = details.get("indicator_results") or {}
        enabled_filters = [
            name for name, result in indicator_results.items()
            if result.get("enabled") and result.get("passed")
        ]
        failed_filters = [
            name for name, result in indicator_results.items()
            if result.get("enabled") and not result.get("passed")
        ]
        candle_shape = "open ~= low" if side == "BUY" else "open ~= high"
        gap_pct = details.get("gap_pct")
        gap_text = f"{float(gap_pct):.2f}%" if gap_pct is not None else "--"
        filter_text = ", ".join(enabled_filters) if enabled_filters else "no enabled indicator filters"
        if failed_filters:
            filter_text += f"; failed: {', '.join(failed_filters)}"
        return (
            f"{self.scan_candle_time()} candle {candle_shape}; gap {gap_text} between {MIN_GAP_PCT:.2f}% and {MAX_GAP_PCT:.2f}%; "
            f"passed filters: {filter_text}. Entry during the following minute."
        )

    def _record_scan_results(self, scan_status: str = "complete", scan_message: str | None = None):
        rows = []
        buy_selected = 0
        sell_selected = 0
        for symbol in self.watchlist:
            details = self.candidate_details.get(symbol)
            if details is None:
                has_candle = symbol in self.scan_seen_symbols
                row = {
                    "symbol": symbol,
                    "side": "WATCH",
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": None,
                    "prev_close": self.prev_close.get(symbol),
                    "gap_pct": None,
                    "candle_received": has_candle,
                    "shape_passed": False,
                    "gap_passed": False,
                    "opening_range_gap_passed": False,
                    "passed_indicators": False,
                    "indicator_results": {},
                    "selected_for_trade": False,
                    "rejection_reason": "missing_previous_close" if has_candle else "missing_opening_candle",
                }
            else:
                row = dict(details)
            if symbol in self.selected_symbols:
                row["selected_for_trade"] = True
            else:
                row["selected_for_trade"] = False
                row["rejection_reason"] = row["rejection_reason"] or "not_selected"
            if row["selected_for_trade"] and row["side"] == "BUY":
                buy_selected += 1
            if row["selected_for_trade"] and row["side"] == "SELL":
                sell_selected += 1
            rows.append(row)

        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now().isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": len([r for r in rows if r["side"] == "BUY" and r.get("opening_range_gap_passed")]),
            "sell_candidates": len([r for r in rows if r["side"] == "SELL" and r.get("opening_range_gap_passed")]),
            "buy_selected": buy_selected,
            "sell_selected": sell_selected,
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": max(0, len(self.watchlist) - sum(1 for row in rows if row.get("opening_range_gap_passed"))),
            "scan_status": scan_status,
            "scan_message": scan_message,
            "condition_breakdown": self._condition_breakdown(rows),
            "warmup_loaded_symbols": len(self.warmup_loaded),
            "warmup_required_candles": {
                "ema20": 20,
                "ema50": 50,
                "rsi": 15,
                "adx": 28,
                "supertrend": int(self.settings["supertrend_period"]) + 1,
            },
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

    def _condition_breakdown(self, rows: list[dict]) -> list[dict]:
        steps = [
            {"label": "Scanned universe", "passed": len(self.watchlist), "total": len(self.watchlist)},
            {"label": f"Condition 1: {self.scan_candle_time()} opening range + gap", "passed": sum(1 for row in rows if row.get("opening_range_gap_passed")), "total": len(self.watchlist)},
        ]
        survivors = [row for row in rows if row.get("opening_range_gap_passed")]
        labels = {
            "vwap": "VWAP condition",
            "rsi": "RSI condition",
            "adx": "ADX condition",
            "supertrend": "Supertrend condition",
            "ema20": "EMA20 condition",
            "ema50": "EMA50 condition",
            "volume": "Volume condition",
            "liquidity": "Liquidity condition",
            "price_range": "Price range condition",
        }
        for key, label in labels.items():
            if not any(row.get("indicator_results", {}).get(key, {}).get("enabled") for row in survivors):
                continue
            passed_rows = [
                row for row in survivors
                if not row.get("indicator_results", {}).get(key, {}).get("enabled") or
                row.get("indicator_results", {}).get(key, {}).get("passed")
            ]
            steps.append({"label": f"Condition {len(steps)}: {label}", "passed": len(passed_rows), "total": len(survivors)})
            survivors = passed_rows
        selected = len([row for row in rows if row.get("selected_for_trade")])
        steps.append({"label": "Final: selected for trade", "passed": selected, "total": len(survivors)})
        return steps

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")
            if not ltp:
                continue
            position = self.broker.apply_trailing_stop(position, ltp, self.settings)
            side, sl, target = position["side"], position["sl_price"], position["target_price"]
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

    def _ema(self, candles: list[dict], period: int) -> float | None:
        if len(candles) < period:
            return None
        closes = [float(candle["close"]) for candle in candles]
        k = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for close in closes[period:]:
            ema = close * k + ema * (1 - k)
        return ema

    def _rsi(self, candles: list[dict], period: int) -> float | None:
        if len(candles) < period + 1:
            return None
        closes = [float(candle["close"]) for candle in candles]
        gains = []
        losses = []
        for index in range(1, period + 1):
            change = closes[index] - closes[index - 1]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        for index in range(period + 1, len(closes)):
            change = closes[index] - closes[index - 1]
            avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
            avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _true_ranges(self, candles: list[dict]) -> list[float]:
        ranges = []
        for index, candle in enumerate(candles):
            high = float(candle["high"])
            low = float(candle["low"])
            if index == 0:
                ranges.append(high - low)
            else:
                prev_close = float(candles[index - 1]["close"])
                ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        return ranges

    def _atr_series(self, candles: list[dict], period: int) -> list[float | None]:
        true_ranges = self._true_ranges(candles)
        atr_values: list[float | None] = [None] * len(true_ranges)
        if len(true_ranges) < period:
            return atr_values
        atr = sum(true_ranges[:period]) / period
        atr_values[period - 1] = atr
        for index in range(period, len(true_ranges)):
            atr = (atr * (period - 1) + true_ranges[index]) / period
            atr_values[index] = atr
        return atr_values

    def _adx(self, candles: list[dict], period: int) -> float | None:
        if len(candles) < period * 2:
            return None

        plus_dm = [0.0]
        minus_dm = [0.0]
        true_ranges = self._true_ranges(candles)
        for index in range(1, len(candles)):
            up_move = float(candles[index]["high"]) - float(candles[index - 1]["high"])
            down_move = float(candles[index - 1]["low"]) - float(candles[index]["low"])
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

        tr_smooth = sum(true_ranges[1:period + 1])
        plus_smooth = sum(plus_dm[1:period + 1])
        minus_smooth = sum(minus_dm[1:period + 1])
        dx_values = []

        for index in range(period + 1, len(candles)):
            tr_smooth = tr_smooth - (tr_smooth / period) + true_ranges[index]
            plus_smooth = plus_smooth - (plus_smooth / period) + plus_dm[index]
            minus_smooth = minus_smooth - (minus_smooth / period) + minus_dm[index]
            if tr_smooth == 0:
                dx_values.append(0.0)
                continue
            plus_di = 100 * plus_smooth / tr_smooth
            minus_di = 100 * minus_smooth / tr_smooth
            denominator = plus_di + minus_di
            dx_values.append(0.0 if denominator == 0 else 100 * abs(plus_di - minus_di) / denominator)

        if len(dx_values) < period:
            return None
        adx = sum(dx_values[:period]) / period
        for dx in dx_values[period:]:
            adx = (adx * (period - 1) + dx) / period
        return adx

    def _supertrend(self, candles: list[dict], period: int, multiplier: float) -> float | None:
        if len(candles) < period + 1:
            return None
        atr_values = self._atr_series(candles, period)
        final_upper = None
        final_lower = None
        supertrend = None

        for index, candle in enumerate(candles):
            atr = atr_values[index]
            if atr is None:
                continue
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
            prev_close = float(candles[index - 1]["close"]) if index > 0 else close
            basic_upper = (high + low) / 2 + multiplier * atr
            basic_lower = (high + low) / 2 - multiplier * atr

            if final_upper is None:
                final_upper = basic_upper
                final_lower = basic_lower
                supertrend = final_lower if close >= (high + low) / 2 else final_upper
                continue

            final_upper = basic_upper if basic_upper < final_upper or prev_close > final_upper else final_upper
            final_lower = basic_lower if basic_lower > final_lower or prev_close < final_lower else final_lower

            if supertrend == final_upper and close > final_upper:
                supertrend = final_lower
            elif supertrend == final_lower and close < final_lower:
                supertrend = final_upper
            elif supertrend == final_upper:
                supertrend = final_upper
            else:
                supertrend = final_lower

        return supertrend
