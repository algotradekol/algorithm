import datetime

from .base import Strategy
from ..paper_broker import PaperBroker

START_TIME = "09:20"
STOP_ENTRY_TIME = "15:00"
MIN_MOVE_PCT = 0.03
TARGET_PCT = 0.15
SL_PCT = 0.10
MAX_TRADES = 6
MAX_PER_SIDE = 3
MAX_SCAN_ROWS = 100


class TestAlgo(Strategy):
    algo_id = "test_algo"
    display_name = "Test Algo - Live Feature Check"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.latest_ltp: dict[str, float] = {}
        self.candidates: dict[str, dict] = {}
        self.selected_symbols: set[str] = set()

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def on_tick(self, symbol: str, ltp: float, timestamp):
        if ltp:
            self.latest_ltp[symbol] = float(ltp)

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        candle_time = candle["time"].strftime("%H:%M")
        if candle_time < START_TIME or candle_time >= STOP_ENTRY_TIME:
            return

        side, rejection_reason = self._signal_side(candle)
        row = self._candidate_row(symbol, candle, side, rejection_reason)
        self.candidates[symbol] = row

        if side:
            entry_price = indicators.get("last_ltp") or self.latest_ltp.get(symbol) or candle["close"]
            opened = self._enter(symbol, side, float(entry_price))
            if opened:
                row["selected_for_trade"] = True
                row["rejection_reason"] = None
            elif row["rejection_reason"] is None:
                row["rejection_reason"] = "test_cap_full_or_duplicate"

        self._record_scan_results()

    def _signal_side(self, candle: dict) -> tuple[str | None, str | None]:
        move_pct = self._candle_move_pct(candle)
        if move_pct >= MIN_MOVE_PCT:
            return "BUY", None
        if move_pct <= -MIN_MOVE_PCT:
            return "SELL", None
        return None, "waiting_for_0.03pct_candle_move"

    def _candidate_row(self, symbol: str, candle: dict, side: str | None, rejection_reason: str | None) -> dict:
        move_pct = self._candle_move_pct(candle)
        passed = bool(side)
        return {
            "symbol": symbol,
            "side": side or "WATCH",
            "open": candle["open"],
            "prev_close": candle["open"],
            "gap_pct": move_pct,
            "passed_indicators": passed,
            "indicator_results": {
                "rsi": {"value": move_pct, "passed": abs(move_pct) >= MIN_MOVE_PCT, "enabled": True},
                "volume": {"value": candle.get("volume", 0), "passed": True, "enabled": True},
            },
            "selected_for_trade": False,
            "rejection_reason": rejection_reason,
            "candle_time": candle["time"].isoformat() if hasattr(candle["time"], "isoformat") else str(candle["time"]),
            "close": candle["close"],
            "high": candle["high"],
            "low": candle["low"],
            "volume": candle.get("volume", 0),
        }

    def _candle_move_pct(self, candle: dict) -> float:
        open_price = float(candle.get("open") or 0)
        close_price = float(candle.get("close") or 0)
        if open_price <= 0:
            return 0.0
        return (close_price - open_price) / open_price * 100

    def _can_open_side(self, side: str) -> bool:
        state = self.broker.summary()
        if state["trade_count_today"] >= min(MAX_TRADES, self.settings["max_trades_per_day"]):
            return False
        if side == "BUY":
            return state["buy_count_today"] < min(MAX_PER_SIDE, self.settings["max_buy_trades"])
        return state["sell_count_today"] < min(MAX_PER_SIDE, self.settings["max_sell_trades"])

    def _enter(self, symbol: str, side: str, entry_price: float) -> bool:
        open_symbols = {position["symbol"] for position in self.broker.open_positions()}
        if (
            symbol in open_symbols or
            symbol in self.selected_symbols or
            not entry_price or
            self.broker.already_traded_today(symbol) or
            not self._can_open_side(side)
        ):
            return False

        qty = int(min(10000, self.settings["capital_per_trade"]) // entry_price)
        if qty < 1:
            return False

        if side == "BUY":
            sl_price = entry_price * (1 - SL_PCT / 100)
            target_price = entry_price * (1 + TARGET_PCT / 100)
        else:
            sl_price = entry_price * (1 + SL_PCT / 100)
            target_price = entry_price * (1 - TARGET_PCT / 100)
        self.broker.open_trade(symbol, side, qty, entry_price, sl_price, target_price)
        self.selected_symbols.add(symbol)
        return True

    def _record_scan_results(self):
        rows = sorted(
            self.candidates.values(),
            key=lambda row: (0 if row.get("selected_for_trade") else 1, -abs(float(row.get("gap_pct") or 0))),
        )[:MAX_SCAN_ROWS]
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": len([row for row in rows if row["side"] == "BUY"]),
            "sell_candidates": len([row for row in rows if row["side"] == "SELL"]),
            "buy_selected": len([row for row in rows if row["selected_for_trade"] and row["side"] == "BUY"]),
            "sell_selected": len([row for row in rows if row["selected_for_trade"] and row["side"] == "SELL"]),
            "overflow_buy": 0,
            "overflow_sell": 0,
            "total_filtered_out": max(0, len(self.watchlist) - len(self.candidates)),
            "condition_breakdown": [
                {"label": "Scanned universe", "passed": len(self.watchlist), "total": len(self.watchlist)},
                {"label": "Condition 1: live candle received", "passed": len(self.candidates), "total": len(self.watchlist)},
                {
                    "label": "Condition 2: +/-0.03% candle move",
                    "passed": len([row for row in rows if row["side"] in {"BUY", "SELL"}]),
                    "total": len(rows),
                },
                {
                    "label": "Final: selected for trade",
                    "passed": len([row for row in rows if row["selected_for_trade"]]),
                    "total": len([row for row in rows if row["side"] in {"BUY", "SELL"}]),
                },
            ],
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = self.latest_ltp.get(position["symbol"]) or position.get("_last_ltp")
            if not ltp:
                continue
            side, sl, target = position["side"], float(position["sl_price"]), float(position["target_price"])
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
