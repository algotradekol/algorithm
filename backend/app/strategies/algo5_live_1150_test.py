import datetime

from .base import Strategy
from ..paper_broker import PaperBroker

MIN_CANDLE_MOVE_PCT = 0.05
DIAGNOSTIC_TARGET_PCT = 0.10
DIAGNOSTIC_SL_PCT = 0.05
MAX_SCAN_ROWS = 100


class Algo5Live1150Test(Strategy):
    algo_id = "algo5"
    display_name = "Algo 5 - Live Candle Movement Test"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.latest_ltp: dict[str, float] = {}
        self.latest_candles: dict[str, dict] = {}
        self.selected_symbols: set[str] = set()
        self.selected_sides: dict[str, str] = {}

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def on_tick(self, symbol: str, ltp: float, timestamp):
        if ltp:
            self.latest_ltp[symbol] = float(ltp)

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        side = self._signal_side(candle)
        move_pct = self._candle_move_pct(candle)
        self.latest_candles[symbol] = {
            "symbol": symbol,
            "side": side or "WATCH",
            "open": candle["open"],
            "prev_close": candle["open"],
            "gap_pct": move_pct,
            "passed_indicators": bool(side),
            "indicator_results": {
                "vwap": {"value": candle["close"], "passed": bool(side), "enabled": False},
                "rsi": {"value": move_pct, "passed": bool(side), "enabled": True},
                "adx": {"value": abs(move_pct), "passed": abs(move_pct) >= MIN_CANDLE_MOVE_PCT, "enabled": True},
                "supertrend": {"value": candle["open"], "passed": bool(side), "enabled": False},
                "volume": {"value": candle.get("volume", 0), "passed": True, "enabled": False},
            },
            "selected_for_trade": symbol in self.selected_symbols,
            "rejection_reason": None if side else "waiting_for_0.05pct_closed_candle_move",
            "candle_time": candle["time"].isoformat() if hasattr(candle["time"], "isoformat") else str(candle["time"]),
            "close": candle["close"],
            "high": candle["high"],
            "low": candle["low"],
            "volume": candle.get("volume", 0),
        }

        if side:
            self._enter(symbol, side, float(candle["close"]))
            self.latest_candles[symbol]["selected_for_trade"] = symbol in self.selected_symbols
            if self.latest_candles[symbol]["selected_for_trade"]:
                self.latest_candles[symbol]["rejection_reason"] = None
            else:
                self.latest_candles[symbol]["rejection_reason"] = "trade_slots_or_duplicate_check"

        self._record_scan_results()

    def evaluate_entries(self, get_ltp_fn):
        self._record_scan_results()

    def _signal_side(self, candle: dict) -> str | None:
        move_pct = self._candle_move_pct(candle)
        if move_pct >= MIN_CANDLE_MOVE_PCT:
            return "BUY"
        if move_pct <= -MIN_CANDLE_MOVE_PCT:
            return "SELL"
        return None

    def _candle_move_pct(self, candle: dict) -> float:
        open_price = float(candle.get("open") or 0)
        close_price = float(candle.get("close") or 0)
        if open_price <= 0:
            return 0.0
        return (close_price - open_price) / open_price * 100

    def _can_open_side(self, side: str) -> bool:
        state = self.broker.summary()
        if state["trade_count_today"] >= self.settings["max_trades_per_day"]:
            return False
        if side == "BUY":
            return state["buy_count_today"] < self.settings["max_buy_trades"] or state["sell_count_today"] == self.settings["max_sell_trades"]
        return state["sell_count_today"] < self.settings["max_sell_trades"] or state["buy_count_today"] == self.settings["max_buy_trades"]

    def _enter(self, symbol: str, side: str, entry_price: float):
        open_symbols = {position["symbol"] for position in self.broker.open_positions()}
        if symbol in open_symbols or symbol in self.selected_symbols or not entry_price or self.broker.already_traded_today(symbol) or not self._can_open_side(side):
            return

        qty = int(self.settings["capital_per_trade"] // entry_price)
        if qty < 1:
            return

        if side == "BUY":
            sl_price = entry_price * (1 - DIAGNOSTIC_SL_PCT / 100)
            target_price = entry_price * (1 + DIAGNOSTIC_TARGET_PCT / 100)
        else:
            sl_price = entry_price * (1 + DIAGNOSTIC_SL_PCT / 100)
            target_price = entry_price * (1 - DIAGNOSTIC_TARGET_PCT / 100)
        self.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price)
        self.selected_symbols.add(symbol)
        self.selected_sides[symbol] = side

    def _record_scan_results(self):
        open_positions = self.broker.open_positions()
        open_position_sides = {position["symbol"]: position["side"] for position in open_positions}
        selected_symbols = self.selected_symbols | set(open_position_sides.keys())
        selected_sides = {**self.selected_sides, **open_position_sides}
        for position in open_positions:
            symbol = position["symbol"]
            if symbol not in self.latest_candles:
                entry = float(position["entry_price"])
                self.latest_candles[symbol] = self._position_row(position, entry)

        prepared_rows = []
        for row in self.latest_candles.values():
            prepared = dict(row)
            symbol = prepared["symbol"]
            if symbol in selected_symbols:
                prepared["side"] = selected_sides.get(symbol) or prepared.get("side") or "WATCH"
                prepared["selected_for_trade"] = True
                prepared["rejection_reason"] = None
            prepared_rows.append(prepared)

        rows = sorted(
            prepared_rows,
            key=lambda row: (0 if row.get("selected_for_trade") else 1, -abs(float(row.get("gap_pct") or 0))),
        )[:MAX_SCAN_ROWS]
        buy_candidates = len([row for row in rows if row["side"] == "BUY"])
        sell_candidates = len([row for row in rows if row["side"] == "SELL"])
        buy_selected = len([row for row in rows if row["selected_for_trade"] and row["side"] == "BUY"])
        sell_selected = len([row for row in rows if row["selected_for_trade"] and row["side"] == "SELL"])
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": buy_candidates,
            "sell_candidates": sell_candidates,
            "buy_selected": buy_selected,
            "sell_selected": sell_selected,
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": max(0, len(self.watchlist) - len(self.latest_candles)),
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

    def _position_row(self, position: dict, ltp: float) -> dict:
        side = position["side"]
        entry = float(position["entry_price"])
        move_pct = ((ltp - entry) / entry * 100) if entry else 0.0
        if side == "SELL":
            move_pct = -move_pct
        return {
            "symbol": position["symbol"],
            "side": side,
            "open": entry,
            "prev_close": entry,
            "gap_pct": move_pct,
            "passed_indicators": True,
            "indicator_results": {
                "vwap": {"value": ltp, "passed": True, "enabled": False},
                "rsi": {"value": move_pct, "passed": True, "enabled": True},
                "adx": {"value": abs(move_pct), "passed": True, "enabled": True},
                "supertrend": {"value": entry, "passed": True, "enabled": False},
                "volume": {"value": 0, "passed": True, "enabled": False},
            },
            "selected_for_trade": True,
            "rejection_reason": None,
            "candle_time": position.get("entry_time"),
            "close": ltp,
            "high": max(entry, ltp),
            "low": min(entry, ltp),
            "volume": 0,
        }

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = self.latest_ltp.get(position["symbol"]) or position.get("_last_ltp")
            if not ltp:
                continue
            side = position["side"]
            entry = float(position["entry_price"])
            if side == "BUY":
                sl = entry * (1 - DIAGNOSTIC_SL_PCT / 100)
                target = entry * (1 + DIAGNOSTIC_TARGET_PCT / 100)
            else:
                sl = entry * (1 + DIAGNOSTIC_SL_PCT / 100)
                target = entry * (1 - DIAGNOSTIC_TARGET_PCT / 100)
            if side == "BUY":
                if ltp <= sl:
                    self.broker.close_trade(position, ltp, "SL")
                elif ltp >= target:
                    self.broker.close_trade(position, ltp, "TARGET")
            else:
                if ltp >= sl:
                    self.broker.close_trade(position, ltp, "SL")
                elif ltp <= target:
                    self.broker.close_trade(position, ltp, "TARGET")

    def square_off_all(self):
        for position in self.broker.open_positions():
            ltp = self.latest_ltp.get(position["symbol"]) or position.get("_last_ltp", position["entry_price"])
            self.broker.close_trade(position, ltp, "EOD_SQUAREOFF")
