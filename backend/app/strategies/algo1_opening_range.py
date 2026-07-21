"""
algo1_opening_range.py

Implements the spec exactly as given:
- 9:15 hrs 1-min candle: open == low -> BUY candidate (if gap from
  prev close <= 2%); open == high -> SELL candidate (same gap check)
- Entry at 9:16 hrs, market price
- Target 2%, SL 1%, capital ₹50,000 per trade
- Max 10 trades total: 5 buy + 5 sell; if one side is short on
  qualifying candidates, the other side's overflow candidates fill
  the remaining slots up to 10 total (ASSUMPTION: candidates are
  processed in watchlist order -- change SORT_KEY below if you want
  them prioritized by gap size instead)

Mode is MIS/intraday funded via margin -- this paper version doesn't
model margin/leverage explicitly (see paper_broker.py notes), it just
tracks the ₹50,000-per-trade capital allocation and P&L against it.
"""
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from .base import Strategy
from ..paper_broker import PaperBroker
from ..fyers_client import get_previous_close
from ..fyers_auth import get_stored_access_token
GAP_LIMIT_PCT = 2.0


class Algo1OpeningRange(Strategy):
    algo_id = "algo1"
    display_name = "UN1 9:15 v15 — Simple"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = PaperBroker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.prev_close: dict[str, float] = {}
        self.buy_candidates: list[str] = []
        self.sell_candidates: list[str] = []
        self.candidate_details: dict[str, dict] = {}
        self.scan_seen_symbols: set[str] = set()
        self.prev_close_ready_symbols: set[str] = set()
        self.open_extreme_symbols: set[str] = set()
        self.selected_symbols: set[str] = set()
        self.entries_evaluated_today = None
        # Load previous closes in background to avoid blocking startup
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def reload_settings(self):
        from app.strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]

    def _load_previous_closes_background(self):
        """Load previous closes in a background thread to avoid blocking initialization."""
        try:
            if not get_stored_access_token():
                print("[algo1] no Fyers access token yet, skipping previous-close preload")
                return
            # A sequential 500-symbol history preload can still be running at
            # 09:16 after a restart. Keep concurrency deliberately modest to
            # finish pre-market without flooding the broker API.
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = {pool.submit(get_previous_close, symbol): symbol for symbol in self.watchlist}
                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        close = future.result()
                        if close:
                            self.prev_close[symbol] = close
                    except Exception as e:
                        print(f"[algo1] couldn't get prev close for {symbol}: {e}")
            print(f"[algo1] previous closes loaded for {len(self.prev_close)}/{len(self.watchlist)} symbols")
        except Exception as e:
            print(f"[algo1] error in background preload: {e}")

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass  # algo1 only acts on the 9:15 candle close and the 9:16 entry check

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        # Only care about the very first candle of the day (9:15-9:16 bar)
        if candle["time"].strftime("%H:%M") != "09:15":
            return

        self.scan_seen_symbols.add(symbol)
        prev_close = self.prev_close.get(symbol)
        if not prev_close:
            return

        open_price, high, low = candle["open"], candle["high"], candle["low"]
        self.prev_close_ready_symbols.add(symbol)

        if open_price == low:
            self.open_extreme_symbols.add(symbol)
            gap_pct = abs(open_price - prev_close) / prev_close * 100
            if gap_pct <= GAP_LIMIT_PCT:
                self.buy_candidates.append(symbol)
                self.candidate_details[symbol] = {
                    "symbol": symbol,
                    "side": "BUY",
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": candle["close"],
                    "prev_close": prev_close,
                    "gap_pct": gap_pct,
                    "passed_indicators": True,
                    "indicator_results": {},
                    "selected_for_trade": False,
                    "rejection_reason": None,
                }

        if open_price == high:
            self.open_extreme_symbols.add(symbol)
            gap_pct = abs(prev_close - open_price) / prev_close * 100
            if gap_pct <= GAP_LIMIT_PCT:
                self.sell_candidates.append(symbol)
                self.candidate_details[symbol] = {
                    "symbol": symbol,
                    "side": "SELL",
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": candle["close"],
                    "prev_close": prev_close,
                    "gap_pct": gap_pct,
                    "passed_indicators": True,
                    "indicator_results": {},
                    "selected_for_trade": False,
                    "rejection_reason": None,
                }

    def evaluate_entries(self, get_ltp_fn):
        """Called by the engine at 9:16. get_ltp_fn(symbol) -> current price."""
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return True
        if not self._opening_data_ready():
            self._record_scan_results([], [], scan_status="incomplete", scan_message=self._opening_data_message())
            return False
        self.entries_evaluated_today = today

        max_total = self.settings["max_trades_per_day"]
        max_per_side = self.settings["max_buy_trades"]
        buys = self.buy_candidates[:max_per_side]
        sells = self.sell_candidates[:self.settings["max_sell_trades"]]

        # Fill remaining slots up to the configured daily max from whichever side has leftover candidates.
        remaining_slots = max_total - (len(buys) + len(sells))
        overflow_pool = self.buy_candidates[max_per_side:] + self.sell_candidates[self.settings["max_sell_trades"]:]
        for symbol in overflow_pool[:remaining_slots]:
            if symbol in self.buy_candidates and symbol not in buys:
                buys.append(symbol)
            elif symbol in self.sell_candidates and symbol not in sells:
                sells.append(symbol)

        for symbol in buys:
            self._enter(symbol, "BUY", get_ltp_fn(symbol))
        for symbol in sells:
            self._enter(symbol, "SELL", get_ltp_fn(symbol))
        self._record_scan_results(buys, sells)
        return True

    def _opening_data_ready(self) -> bool:
        required = max(1, int(len(self.watchlist) * 0.98))
        return len(self.scan_seen_symbols) >= required and len(self.prev_close_ready_symbols) >= required

    def _opening_data_message(self) -> str:
        required = max(1, int(len(self.watchlist) * 0.98))
        return (
            "Opening scan was not eligible for entry: "
            f"received {len(self.scan_seen_symbols)}/{len(self.watchlist)} 09:15 IST candles and "
            f"loaded {len(self.prev_close_ready_symbols)}/{len(self.watchlist)} previous closes "
            f"(requires {required} of each). No late trades will be placed."
        )

    def mark_opening_scan_missed(self):
        if self.entries_evaluated_today == datetime.date.today():
            return
        self._record_scan_results([], [], scan_status="missed_data", scan_message=self._opening_data_message())

    def _enter(self, symbol: str, side: str, entry_price: float):
        if not entry_price or self.broker.already_traded_today(symbol):
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
        open_price = details.get("open")
        prev_close = details.get("prev_close")
        gap_pct = details.get("gap_pct")
        candle_shape = "open = low" if side == "BUY" else "open = high"
        gap_text = f"{float(gap_pct):.2f}%" if gap_pct is not None else "--"
        return (
            f"9:15 candle {candle_shape}; gap {gap_text} within <= {GAP_LIMIT_PCT:.2f}%; "
            f"entry at 9:16 LTP. Open {open_price}, prev close {prev_close}."
        )

    def _record_scan_results(self, buys: list[str], sells: list[str], scan_status: str = "complete", scan_message: str | None = None):
        rows = []
        for symbol, details in self.candidate_details.items():
            row = dict(details)
            row["selected_for_trade"] = symbol in self.selected_symbols
            row["rejection_reason"] = None if row["selected_for_trade"] else "slots_full"
            rows.append(row)
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now().isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": len(self.buy_candidates),
            "sell_candidates": len(self.sell_candidates),
            "buy_selected": len(buys),
            "sell_selected": len(sells),
            "overflow_buy": max(0, len(buys) - self.settings["max_buy_trades"]),
            "overflow_sell": max(0, len(sells) - self.settings["max_sell_trades"]),
            "total_filtered_out": max(0, len(self.watchlist) - len(rows)),
            "scan_status": scan_status,
            "scan_message": scan_message,
            "condition_breakdown": [
                {"label": "Scanned universe", "passed": len(self.watchlist), "total": len(self.watchlist)},
                {"label": "Condition 1: 9:15 candle received", "passed": len(self.scan_seen_symbols), "total": len(self.watchlist)},
                {"label": "Condition 2: open equals low/high", "passed": len(self.open_extreme_symbols), "total": len(self.scan_seen_symbols)},
                {"label": "Condition 3: opening gap <= 2%", "passed": len(rows), "total": len(self.open_extreme_symbols)},
                {"label": "Final: selected for trade", "passed": len(self.selected_symbols), "total": len(rows)},
            ],
        }
        from app.engine import SCAN_RESULTS
        from app.broadcaster import broadcast_sync
        SCAN_RESULTS[self.algo_id] = result
        broadcast_sync({"event": "scan_complete", "algo_id": self.algo_id, "results": result})

    def check_exits(self):
        for position in self.broker.open_positions():
            ltp = position.get("_last_ltp")  # engine sets this before calling check_exits
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

