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
from collections import defaultdict
from .base import Strategy
from ..paper_broker import PaperBroker
from ..fyers_client import get_previous_close
from ..fyers_auth import get_stored_access_token
from ..candidate_ranking import rank_candidates, select_ranked_candidates
GAP_LIMIT_PCT = 2.0
# A one-minute candle only exists for a symbol that traded during that minute.
# This floor detects a dead/broken feed without requiring every NSE 500 symbol
# to trade at the opening bell.
MIN_OPENING_READY_SYMBOLS = 10


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
        self.opening_candles: dict[str, list[dict]] = defaultdict(list)
        self.scan_seen_symbols: set[str] = set()
        self.prev_close_ready_symbols: set[str] = set()
        self.open_extreme_symbols: set[str] = set()
        self.selected_symbols: set[str] = set()
        self.selected_sides: dict[str, str] = {}
        # Keep the scan audit honest: a candidate selected for an entry slot can
        # still fail to open if its live entry price is unavailable.
        self.entry_failures: dict[str, str] = {}
        self.entries_evaluated_today = None
        self._previous_close_load_lock = threading.Lock()
        self._previous_close_loading = False
        # Load previous closes in background to avoid blocking startup
        self.refresh_market_data()

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
        finally:
            with self._previous_close_load_lock:
                self._previous_close_loading = False

    def refresh_market_data(self):
        """Retry preloading after a manual Fyers OAuth login supplies a token."""
        with self._previous_close_load_lock:
            if self._previous_close_loading or len(self.prev_close) >= len(self.watchlist):
                return
            self._previous_close_loading = True
        threading.Thread(target=self._load_previous_closes_background, daemon=True).start()

    def set_previous_close(self, symbol: str, previous_close: float):
        """Use the broker's live previous-close field when the feed provides it."""
        if symbol in self.watchlist and previous_close > 0:
            self.prev_close[symbol] = previous_close

    def scan_candle_time(self) -> str:
        return self.settings.get("test_candle_time", "11:10") if self.settings.get("test_schedule_enabled") else "09:15"

    def _schedule_time(self, minutes_after_start: int) -> str:
        return (datetime.datetime.strptime(self.scan_candle_time(), "%H:%M") + datetime.timedelta(minutes=minutes_after_start)).strftime("%H:%M")

    def _is_collection_candle(self, candle_time: str) -> bool:
        # The signal is the combined 9:15, 9:16 and 9:17 candles. The
        # preceding minute is only used to have the live feed ready.
        return self.scan_candle_time() <= candle_time < self._schedule_time(3)

    def entry_window(self, current_time: str) -> bool:
        entry = self._schedule_time(3)
        return entry <= current_time < (datetime.datetime.strptime(entry, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M")

    def entry_window_elapsed(self, current_time: str) -> bool:
        deadline = self._schedule_time(4)
        return current_time >= deadline

    def schedule_status(self, now: datetime.datetime) -> dict:
        if not self.settings.get("test_schedule_enabled"):
            return {"enabled": False}
        candle_time = self.scan_candle_time()
        entry_time = self._schedule_time(3)
        current_time = now.strftime("%H:%M")
        state = "waiting"
        if self._schedule_time(-1) <= current_time < entry_time:
            state = "collecting_candle"
        elif entry_time <= current_time < (datetime.datetime.strptime(entry_time, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M"):
            state = "evaluating_entries"
        elif current_time >= (datetime.datetime.strptime(entry_time, "%H:%M") + datetime.timedelta(minutes=1)).strftime("%H:%M"):
            state = "finished"
        return {"enabled": True, "candle_time": candle_time, "entry_time": entry_time, "state": state}

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass  # algo1 only acts on the 9:15 candle close and the 9:16 entry check

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if not self._is_collection_candle(candle["time"].strftime("%H:%M")):
            return

        self.scan_seen_symbols.add(symbol)
        self.opening_candles[symbol].append(candle)

    def _combined_opening_candle(self, candles: list[dict]) -> dict:
        return {
            "time": candles[0]["time"],
            "open": candles[0]["open"],
            "high": max(candle["high"] for candle in candles),
            "low": min(candle["low"] for candle in candles),
            "close": candles[-1]["close"],
            "volume": sum(float(candle.get("volume") or 0) for candle in candles),
            "window_candle_count": len(candles),
        }

    def _build_candidates_from_collection(self):
        self.buy_candidates = []
        self.sell_candidates = []
        self.candidate_details = {}
        self.open_extreme_symbols = set()
        self.prev_close_ready_symbols = set()
        for symbol, candles in self.opening_candles.items():
            prev_close = self.prev_close.get(symbol)
            if not prev_close:
                continue

            candle = self._combined_opening_candle(candles)
            open_price, high, low = candle["open"], candle["high"], candle["low"]
            self.prev_close_ready_symbols.add(symbol)
            buy_shape = open_price == low
            sell_shape = open_price == high
            # A flat opening window satisfies both shapes. It has no
            # directional information, so never let BUY win merely because it
            # is evaluated first.
            flat_shape = buy_shape and sell_shape
            gap_pct = abs(open_price - prev_close) / prev_close * 100
            self.candidate_details[symbol] = {
                "symbol": symbol,
                "side": "WATCH",
                "open": open_price,
                "high": high,
                "low": low,
                "close": candle["close"],
                "prev_close": prev_close,
                "gap_pct": gap_pct,
                "candle_received": True,
                "window_candle_count": candle["window_candle_count"],
                "shape_passed": (buy_shape or sell_shape) and not flat_shape,
                "signal_shape": "flat_ambiguous" if flat_shape else ("open_equals_low" if buy_shape else "open_equals_high" if sell_shape else "neither"),
                "gap_passed": False,
                "passed_indicators": True,
                "indicator_results": {},
                "selected_for_trade": False,
                "rejection_reason": "flat_ambiguous_opening_window" if flat_shape else ("failed_opening_shape" if not (buy_shape or sell_shape) else "failed_gap_filter"),
            }

            if flat_shape:
                continue
            if buy_shape:
                self.open_extreme_symbols.add(symbol)
                if gap_pct <= GAP_LIMIT_PCT:
                    self.buy_candidates.append(symbol)
                    self.candidate_details[symbol].update({"side": "BUY", "gap_passed": True, "rejection_reason": None})
            elif sell_shape:
                self.open_extreme_symbols.add(symbol)
                if gap_pct <= GAP_LIMIT_PCT:
                    self.sell_candidates.append(symbol)
                    self.candidate_details[symbol].update({"side": "SELL", "gap_passed": True, "rejection_reason": None})

    def evaluate_entries(self, get_ltp_fn):
        """Called at 9:18 after the three-minute opening collection window."""
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return True
        self._build_candidates_from_collection()
        if not self._opening_data_ready():
            self._record_scan_results([], [], scan_status="incomplete", scan_message=self._opening_data_message())
            return False
        self.entries_evaluated_today = today

        qualified = [
            self.candidate_details[symbol]
            for symbol in self.buy_candidates + self.sell_candidates
        ]
        ranked = rank_candidates(qualified, self.settings, profile="simple")
        selected = select_ranked_candidates(ranked, self.settings)
        buys = [row["symbol"] for row in selected if row["side"] == "BUY"]
        sells = [row["symbol"] for row in selected if row["side"] == "SELL"]

        self.entry_failures = {}
        planned_symbols = {row["symbol"] for row in selected}
        for symbol in buys:
            self._enter(symbol, "BUY", get_ltp_fn(symbol))
        for symbol in sells:
            self._enter(symbol, "SELL", get_ltp_fn(symbol))
        self._record_scan_results(buys, sells, planned_symbols=planned_symbols)
        return True

    def _opening_data_ready(self) -> bool:
        # A manually enabled Test Schedule is a paper-only pipeline check and
        # can run from any received symbol. Production still needs a small
        # non-zero sample to detect a dead or unhealthy market-data feed.
        if self.settings.get("test_schedule_enabled"):
            return bool(self.scan_seen_symbols and self.prev_close_ready_symbols)
        return self._opening_ready_symbol_count() >= min(MIN_OPENING_READY_SYMBOLS, len(self.watchlist))

    def _opening_ready_symbol_count(self) -> int:
        return len(self.scan_seen_symbols & self.prev_close_ready_symbols)

    def _opening_data_message(self) -> str:
        required = min(MIN_OPENING_READY_SYMBOLS, len(self.watchlist))
        return (
            "Opening scan was not eligible for entry: "
            f"received {len(self.scan_seen_symbols)}/{len(self.watchlist)} symbols during the {self.scan_candle_time()}-{self._schedule_time(2)} IST window and "
            f"matched {self._opening_ready_symbol_count()}/{len(self.watchlist)} symbols with previous closes "
            f"(requires at least {required} ready symbols to detect a healthy feed). No late trades will be placed."
        )

    def mark_opening_scan_missed(self):
        if self.entries_evaluated_today == datetime.date.today():
            return
        self._record_scan_results([], [], scan_status="missed_data", scan_message=self._opening_data_message())

    def mark_opening_scan_failed(self, error: str):
        """Expose a scheduler exception in the scan panel without placing trades."""
        self._record_scan_results(
            [], [], scan_status="error",
            scan_message=f"Opening scan failed before any entry was placed: {error}",
        )

    def _enter(self, symbol: str, side: str, entry_price: float):
        if not entry_price:
            self.entry_failures[symbol] = "entry_price_unavailable"
            return False
        if self.broker.already_traded_today(symbol):
            self.entry_failures[symbol] = "already_traded_today"
            return False
        qty = int(self.settings["capital_per_trade"] // entry_price)
        if qty < 1:
            self.entry_failures[symbol] = "capital_per_trade_below_share_price"
            return False
        if side == "BUY":
            sl_price = entry_price * (1 - self.settings["sl_pct"] / 100)
            target_price = entry_price * (1 + self.settings["target_pct"] / 100)
        else:
            sl_price = entry_price * (1 + self.settings["sl_pct"] / 100)
            target_price = entry_price * (1 - self.settings["target_pct"] / 100)
        try:
            self.broker.open_trade(
                symbol, side, qty, entry_price, sl_price, target_price,
                self._entry_trigger(symbol, side), self._signal_snapshot(symbol, side, entry_price),
            )
        except Exception as exc:
            self.entry_failures[symbol] = "paper_broker_open_failed"
            print(f"[algo1] paper entry failed for {symbol}: {exc}")
            return False
        self.selected_symbols.add(symbol)
        self.selected_sides[symbol] = side
        return True

    def _entry_trigger(self, symbol: str, side: str) -> str:
        details = self.candidate_details.get(symbol, {})
        open_price = details.get("open")
        prev_close = details.get("prev_close")
        gap_pct = details.get("gap_pct")
        candle_shape = "open = low" if side == "BUY" else "open = high"
        gap_text = f"{float(gap_pct):.2f}%" if gap_pct is not None else "--"
        rank = details.get("rank")
        score = details.get("composite_score")
        ranking_text = f"rank #{rank}, score {float(score):.2f}/100" if rank and score is not None else "unranked"
        return (
            f"{self.scan_candle_time()}-{self._schedule_time(2)} opening window {candle_shape}; gap {gap_text} within <= {GAP_LIMIT_PCT:.2f}%; "
            f"{ranking_text}; entered at {self._schedule_time(3)}. Open {open_price}, prev close {prev_close}."
        )

    def _signal_snapshot(self, symbol: str, side: str, entry_price: float) -> dict:
        """Immutable evidence for the candle that selected this paper trade."""
        details = self.candidate_details.get(symbol, {})
        return {
            "window": f"{self.scan_candle_time()}-{self._schedule_time(2)} IST",
            "side": side,
            "shape": details.get("signal_shape"),
            "open": details.get("open"),
            "high": details.get("high"),
            "low": details.get("low"),
            "close": details.get("close"),
            "volume": details.get("volume"),
            "previous_close": details.get("prev_close"),
            "gap_pct": details.get("gap_pct"),
            "rank": details.get("rank"),
            "composite_score": details.get("composite_score"),
            "entry_ltp": entry_price,
        }

    def _record_scan_results(
        self,
        buys: list[str],
        sells: list[str],
        scan_status: str = "complete",
        scan_message: str | None = None,
        planned_symbols: set[str] | None = None,
    ):
        planned_symbols = planned_symbols or set()
        rows = []
        # Keep one audit row per watchlist symbol.  A funnel total is only
        # useful when clicking it can show the same set of symbols it counted.
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
                    "passed_indicators": False,
                    "indicator_results": {},
                    "selected_for_trade": False,
                    "rejection_reason": "missing_previous_close" if has_candle else "missing_opening_candle",
                }
            else:
                row = dict(details)
            row["selected_for_trade"] = symbol in self.selected_symbols
            if row["selected_for_trade"]:
                # A flat candle can satisfy both open=low and open=high. The
                # paper broker permits only the first actual entry; show that
                # real entry side rather than a later overwritten candidate.
                row["side"] = self.selected_sides.get(symbol, row["side"])
            if row["selected_for_trade"]:
                row["rejection_reason"] = None
            elif symbol in self.entry_failures:
                row["rejection_reason"] = self.entry_failures[symbol]
            elif symbol in planned_symbols:
                row["rejection_reason"] = "entry_not_opened"
            elif row.get("gap_passed"):
                # This candidate passed its conditions but was outside the
                # configured total/side allocation for the opening scan.
                row["rejection_reason"] = "slots_full"
            rows.append(row)
        result = {
            "algo_id": self.algo_id,
            "scan_time": datetime.datetime.now().isoformat(),
            "total_scanned": len(self.watchlist),
            "passed_opening_range": rows,
            "buy_candidates": len(self.buy_candidates),
            "sell_candidates": len(self.sell_candidates),
            "buy_selected": sum(1 for side in self.selected_sides.values() if side == "BUY"),
            "sell_selected": sum(1 for side in self.selected_sides.values() if side == "SELL"),
            "overflow_buy": max(0, len(buys) - self.settings["max_buy_trades"]),
            "overflow_sell": max(0, len(sells) - self.settings["max_sell_trades"]),
            "total_filtered_out": max(0, len(self.watchlist) - sum(1 for row in rows if row.get("gap_passed"))),
            "scan_status": scan_status,
            "scan_message": scan_message,
            "ranking": {
                "method": "Gap strength only: closer to the 2% gap limit ranks higher.",
                "weights": {"gap_strength": 1.0},
            },
            "best_matches": sorted(
                (row for row in rows if row.get("composite_score") is not None),
                key=lambda row: row.get("rank", 999999),
            )[:4],
            "condition_breakdown": [
                {"label": "Scanned universe", "passed": len(self.watchlist), "total": len(self.watchlist)},
                {"label": f"Condition 1: {self.scan_candle_time()}-{self._schedule_time(2)} candles received", "passed": len(self.scan_seen_symbols), "total": len(self.watchlist)},
                {"label": "Condition 2: open equals low/high", "passed": len(self.open_extreme_symbols), "total": len(self.scan_seen_symbols)},
                {"label": "Condition 3: opening gap <= 2%", "passed": sum(1 for row in rows if row.get("gap_passed")), "total": len(self.open_extreme_symbols)},
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

