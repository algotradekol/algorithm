import datetime
import threading
from collections import defaultdict

from .base import Strategy
from ..fyers_auth import get_stored_access_token
from ..fyers_client import get_previous_close
from ..paper_broker import PaperBroker

MIN_GAP_PCT = 0.5
MAX_GAP_PCT = 2.0
TICK_SIZE = 0.05


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
        self.candidates: dict[str, tuple[str, float]] = {}
        self.candidate_details: dict[str, dict] = {}
        self.selected_symbols: set[str] = set()
        self.entries_evaluated_today = None
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def _load_previous_closes_background(self):
        try:
            if not get_stored_access_token():
                print(f"[{self.algo_id}] no Fyers access token yet, skipping previous-close preload")
                return
            for symbol in self.watchlist:
                try:
                    close = get_previous_close(symbol)
                    if close:
                        self.prev_close[symbol] = close
                except Exception as e:
                    print(f"[{self.algo_id}] couldn't get prev close for {symbol}: {e}")
        except Exception as e:
            print(f"[{self.algo_id}] error in background preload: {e}")

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        history = self.candles[symbol]
        history.append(candle)
        if len(history) > 120:
            del history[:-120]
        self.total_value[symbol] += float(candle["close"]) * float(candle.get("volume") or 0)

        if candle["time"].strftime("%H:%M") != "09:15":
            return

        side, details = self._signal_details(symbol, candle, indicators)
        if details:
            self.candidate_details[symbol] = details
        if side and details and details["passed_indicators"]:
            self.candidates[symbol] = (side, candle["close"])

        now = datetime.datetime.now()
        if side and self._is_entry_window(now):
            self._enter(symbol, side, indicators.get("last_ltp") or candle["close"])

    def evaluate_entries(self, get_ltp_fn):
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return
        self.entries_evaluated_today = today

        for symbol, (side, fallback_price) in list(self.candidates.items()):
            entry_price = get_ltp_fn(symbol) or fallback_price
            self._enter(symbol, side, entry_price)
        self._record_scan_results()

    def _signal_details(self, symbol: str, candle: dict, indicators: dict) -> tuple[str | None, dict | None]:
        prev_close = self.prev_close.get(symbol)
        if not prev_close:
            return None, None

        open_price = candle["open"]
        buy_gap = open_price - prev_close
        sell_gap = prev_close - open_price
        buy_base = (
            abs(open_price - candle["low"]) <= TICK_SIZE and
            MIN_GAP_PCT / 100 * prev_close <= buy_gap <= MAX_GAP_PCT / 100 * prev_close
        )
        sell_base = (
            abs(open_price - candle["high"]) <= TICK_SIZE and
            MIN_GAP_PCT / 100 * prev_close <= sell_gap <= MAX_GAP_PCT / 100 * prev_close
        )

        side = "BUY" if buy_base else "SELL" if sell_base else None
        if not side:
            return None, None

        indicator_results = self._indicator_results(symbol, candle, indicators, side)
        passed_indicators = all(
            result["passed"] for result in indicator_results.values() if result["enabled"]
        )
        gap = buy_gap if side == "BUY" else sell_gap
        return side, {
            "symbol": symbol,
            "side": side,
            "open": open_price,
            "prev_close": prev_close,
            "gap_pct": gap / prev_close * 100,
            "passed_indicators": passed_indicators,
            "indicator_results": indicator_results,
            "selected_for_trade": False,
            "rejection_reason": None if passed_indicators else "failed_indicator_filter",
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
        self.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price)
        self.selected_symbols.add(symbol)

    def _record_scan_results(self):
        rows = []
        buy_selected = 0
        sell_selected = 0
        for symbol, details in self.candidate_details.items():
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
            "buy_candidates": len([r for r in rows if r["side"] == "BUY"]),
            "sell_candidates": len([r for r in rows if r["side"] == "SELL"]),
            "buy_selected": buy_selected,
            "sell_selected": sell_selected,
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": max(0, len(self.watchlist) - len(rows)),
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

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
