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
from ..broker_factory import create_broker
from ..fyers_client import get_previous_close, get_intraday_candles_for_range, get_single_minute_candle, get_live_ltp_batch, get_live_quotes_batch
from ..fyers_auth import get_stored_access_token
from ..candidate_ranking import build_sector_breakdown
from ..candidate_selection import execute_candidates_first_come
from ..symbols import get_nse500_sector_map
GAP_LIMIT_PCT = 2.0
TICK_SIZE = 0.05
# Concurrency for the 9:16 history backfill. IO-bound HTTP requests release
# the GIL, so more workers here only means more parallel network calls —
# CPU usage on Railway stays flat. Fyers allows ~10-50 req/s; 20 is safe.
_BACKFILL_MAX_WORKERS = 50
_PRELOAD_MAX_WORKERS = 12


class ScanDebugLogger:
    """Tracks symbol progression through scan stages for structured debugging."""
    def __init__(self, watchlist_size: int):
        self.total = watchlist_size
        self.stage_data = {
            "candles_received": {"count": 0, "symbols": []},
            "candles_live": {"count": 0, "symbols": []},
            "candles_backfilled": {"count": 0, "symbols": []},
            "candles_test_ltp": {"count": 0, "symbols": []},
            "candles_missing": {"count": 0, "symbols": []},
            "prev_close_loaded": {"count": 0, "symbols": []},
            "prev_close_missing": {"count": 0, "symbols": []},
            "shape_passed": {"count": 0, "symbols": {"buy": [], "sell": []}},
            "shape_failed_flat": {"count": 0, "symbols": []},
            "shape_failed_neither": {"count": 0, "symbols": []},
            "gap_passed": {"count": 0, "symbols": {"buy": [], "sell": []}},
            "gap_failed": {"count": 0, "symbols": []},
            "selected_trade": {"count": 0, "symbols": {"buy": [], "sell": []}},
        }

    def add_candle_received(self, symbol: str, source: str):
        self.stage_data["candles_received"]["count"] += 1
        self.stage_data[f"candles_{source}"]["count"] += 1
        self.stage_data[f"candles_{source}"]["symbols"].append(symbol)

    def add_candle_missing(self, symbol: str):
        self.stage_data["candles_missing"]["count"] += 1
        self.stage_data["candles_missing"]["symbols"].append(symbol)

    def add_prev_close(self, symbol: str, has_it: bool):
        if has_it:
            self.stage_data["prev_close_loaded"]["count"] += 1
            self.stage_data["prev_close_loaded"]["symbols"].append(symbol)
        else:
            self.stage_data["prev_close_missing"]["count"] += 1
            self.stage_data["prev_close_missing"]["symbols"].append(symbol)

    def add_shape_result(self, symbol: str, side: str, passed: bool, reason: str = ""):
        if passed:
            self.stage_data["shape_passed"]["count"] += 1
            self.stage_data["shape_passed"]["symbols"][side.lower()].append(symbol)
        else:
            if reason == "flat":
                self.stage_data["shape_failed_flat"]["count"] += 1
            else:
                self.stage_data["shape_failed_neither"]["count"] += 1
            self.stage_data[f"shape_failed_{reason}"]["symbols"].append(symbol)

    def add_gap_result(self, symbol: str, side: str, passed: bool):
        if passed:
            self.stage_data["gap_passed"]["count"] += 1
            self.stage_data["gap_passed"]["symbols"][side.lower()].append(symbol)
        else:
            self.stage_data["gap_failed"]["count"] += 1
            self.stage_data["gap_failed"]["symbols"].append(symbol)

    def add_selected(self, symbol: str, side: str):
        self.stage_data["selected_trade"]["count"] += 1
        self.stage_data["selected_trade"]["symbols"][side.lower()].append(symbol)

    def one_line_summary(self) -> str:
        """Single-line funnel: which stage killed the trades."""
        s = self.stage_data
        candles = s['candles_received']['count']
        return (
            f"[algo1] FUNNEL "
            f"universe={self.total} | "
            f"candles={candles} (live={s['candles_live']['count']} "
            f"bf={s['candles_backfilled']['count']} "
            f"ltp={s['candles_test_ltp']['count']} "
            f"miss={s['candles_missing']['count']}) | "
            f"prev_close={s['prev_close_loaded']['count']}/{candles} "
            f"(miss={s['prev_close_missing']['count']}) | "
            f"shape_ok={s['shape_passed']['count']} "
            f"(buy={len(s['shape_passed']['symbols']['buy'])} "
            f"sell={len(s['shape_passed']['symbols']['sell'])}) | "
            f"gap_ok={s['gap_passed']['count']} | "
            f"selected={s['selected_trade']['count']}"
        )

    def diagnose_bottleneck(self) -> str:
        """One line telling you exactly where the funnel died."""
        s = self.stage_data
        if s['candles_received']['count'] == 0:
            return "NO CANDLES — check Fyers websocket, backfill, and LTP preload"
        if s['prev_close_loaded']['count'] == 0:
            return "NO PREV_CLOSE — background loader not done or Fyers history API failing"
        if s['shape_passed']['count'] == 0:
            return f"SHAPE FAILED — {s['shape_failed_flat']['count']} flat + {s['shape_failed_neither']['count']} neither open==low nor ==high"
        if s['gap_passed']['count'] == 0:
            return f"GAP FAILED — all {s['shape_passed']['count']} symbols had gap > {GAP_LIMIT_PCT}%"
        if s['selected_trade']['count'] == 0:
            return f"NO ENTRIES — {s['gap_passed']['count']} qualified but broker.open_trade never succeeded (check entry_failures)"
        return f"OK — {s['selected_trade']['count']} trades placed"

    def print_report(self):
        """Compact one-liner + bottleneck diagnosis. Was 40 lines, now 2."""
        print(self.one_line_summary())
        print(f"[algo1] BOTTLENECK: {self.diagnose_bottleneck()}")


class Algo1OpeningRange(Strategy):
    algo_id = "algo1"
    display_name = "9:15 Opening Range"

    def __init__(self, watchlist: list[str]):
        self.watchlist = watchlist
        self.sector_map = get_nse500_sector_map()
        from ..strategy_settings import get_settings
        self.settings = get_settings(self.algo_id)
        self.broker = create_broker(algo_id=self.algo_id, starting_capital=self.settings["starting_capital"])
        self.prev_close: dict[str, float] = {}
        self.preloaded_ltps: dict[str, float] = {}
        self.test_mode_ltps: dict[str, float] = {}  # Store LTPs from test mode candles
        self.buy_candidates: list[str] = []
        self.sell_candidates: list[str] = []
        self.candidate_details: dict[str, dict] = {}
        self.opening_candles: dict[str, list[dict]] = defaultdict(list)
        self.history_verified_opening_symbols: set[str] = set()
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
        self._ltp_load_lock = threading.Lock()
        self._ltp_loading = False
        self.debug_logger = ScanDebugLogger(len(watchlist))
        # Load previous closes and LTPs in background to avoid blocking startup
        self.refresh_market_data()

    def reload_settings(self):
        from ..strategy_settings import get_settings
        previous_schedule = (
            bool(self.settings.get("test_schedule_enabled")),
            self.scan_candle_time(),
        )
        self.settings = get_settings(self.algo_id)
        self.broker.starting_capital = self.settings["starting_capital"]
        current_schedule = (
            bool(self.settings.get("test_schedule_enabled")),
            self.scan_candle_time(),
        )
        if current_schedule != previous_schedule:
            self._reset_scan_run_state()
            from ..engine import SCAN_RESULTS
            SCAN_RESULTS.pop(self.algo_id, None)
            print(
                f"[{self.algo_id}] scan schedule changed from {previous_schedule} "
                f"to {current_schedule}; cleared the previous scan-run state"
            )

    def _reset_scan_run_state(self):
        """Start a fresh scan without altering broker positions or trades."""
        self.buy_candidates = []
        self.sell_candidates = []
        self.candidate_details = {}
        self.opening_candles = defaultdict(list)
        self.history_verified_opening_symbols = set()
        self.scan_seen_symbols = set()
        self.prev_close_ready_symbols = set()
        self.open_extreme_symbols = set()
        self.selected_symbols = set()
        self.selected_sides = {}
        self.entry_failures = {}
        self.entries_evaluated_today = None
        self.test_mode_ltps = {}
        self.debug_logger = ScanDebugLogger(len(self.watchlist))

    def _load_previous_closes_background(self):
        """Load previous closes in a background thread to avoid blocking initialization."""
        try:
            if not get_stored_access_token():
                print("[algo1] no Fyers access token yet, skipping previous-close preload")
                return
            # A sequential 500-symbol history preload can still be running at
            # 09:16 after a restart. Keep concurrency deliberately modest to
            # finish pre-market without flooding the broker API.
            with ThreadPoolExecutor(max_workers=_PRELOAD_MAX_WORKERS) as pool:
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

    def _load_ltps_background(self):
        """Preload live LTPs in a background thread to avoid blocking initialization."""
        try:
            if not get_stored_access_token():
                print("[algo1] no Fyers access token yet, skipping LTP preload")
                return
            batch_size = 100
            for i in range(0, len(self.watchlist), batch_size):
                batch = self.watchlist[i:i+batch_size]
                try:
                    result = get_live_ltp_batch(batch)
                    if result:
                        self.preloaded_ltps.update(result)
                except Exception as e:
                    print(f"[algo1] couldn't fetch LTP batch {i//batch_size + 1}: {e}")
            print(f"[algo1] LTPs loaded for {len(self.preloaded_ltps)}/{len(self.watchlist)} symbols")
        except Exception as e:
            print(f"[algo1] error in LTP preload: {e}")
        finally:
            with self._ltp_load_lock:
                self._ltp_loading = False

    def refresh_market_data(self):
        """Retry preloading after a manual Fyers OAuth login supplies a token."""
        with self._previous_close_load_lock:
            if self._previous_close_loading or len(self.prev_close) >= len(self.watchlist):
                pass
            else:
                self._previous_close_loading = True
                threading.Thread(target=self._load_previous_closes_background, daemon=True).start()
        with self._ltp_load_lock:
            if self._ltp_loading or len(self.preloaded_ltps) >= len(self.watchlist):
                pass
            else:
                self._ltp_loading = True
                threading.Thread(target=self._load_ltps_background, daemon=True).start()

    def set_previous_close(self, symbol: str, previous_close: float):
        """Use the broker's live previous-close field when the feed provides it."""
        if symbol in self.watchlist and previous_close > 0:
            self.prev_close[symbol] = previous_close

    def scan_candle_time(self) -> str:
        return self.settings.get("test_candle_time", "11:10") if self.settings.get("test_schedule_enabled") else "09:15"

    def _display_time(self, value: str) -> str:
        return datetime.datetime.strptime(value, "%H:%M").strftime("%H:%M:%S")

    def _schedule_time(self, minutes_after_start: int) -> str:
        return (datetime.datetime.strptime(self.scan_candle_time(), "%H:%M") + datetime.timedelta(minutes=minutes_after_start)).strftime("%H:%M")

    def _is_collection_candle(self, candle_time: str) -> bool:
        # The signal is only the configured opening candle itself.
        return candle_time == self.scan_candle_time()

    def entry_window(self, current_time: str) -> bool:
        entry = self._schedule_time(1)
        # Keep the window open for 3 minutes so the history backfill (fetching
        # 400+ symbols from Fyers intraday API) has time to complete before we
        # give up. evaluate_entries() is idempotent — calling it multiple times
        # is safe because entries_evaluated_today guards against double-entry.
        return entry <= current_time < (datetime.datetime.strptime(entry, "%H:%M") + datetime.timedelta(minutes=3)).strftime("%H:%M")

    def entry_window_elapsed(self, current_time: str) -> bool:
        deadline = self._schedule_time(4)
        return current_time >= deadline

    def schedule_status(self, now: datetime.datetime) -> dict:
        if not self.settings.get("test_schedule_enabled"):
            return {"enabled": False}
        candle_time = self.scan_candle_time()
        entry_time = self._schedule_time(1)
        current_time = now.strftime("%H:%M")
        state = "waiting"
        scan_status = None
        scan_message = None
        try:
            from ..engine import SCAN_RESULTS
            latest = SCAN_RESULTS.get(self.algo_id) or {}
            scan_status = latest.get("scan_status")
            scan_message = latest.get("scan_message")
        except ImportError:
            pass
        if scan_status in {"incomplete", "missed_data", "error"}:
            state = scan_status
        elif scan_status == "evaluating":
            state = "evaluating_entries"
        elif self.entries_evaluated_today == now.date():
            state = "finished"
        elif current_time < candle_time:
            state = "waiting"
        elif current_time < entry_time:
            state = "collecting_candle"
        else:
            state = "evaluating_entries"
        return {
            "enabled": True,
            "candle_time": self._display_time(candle_time),
            "entry_time": self._display_time(entry_time),
            "state": state,
            "message": scan_message,
        }

    def on_tick(self, symbol: str, ltp: float, timestamp):
        pass  # algo1 acts on the signal candle close and enters on the next candle open/check

    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        if not self._is_collection_candle(candle["time"].strftime("%H:%M")):
            return

        self.scan_seen_symbols.add(symbol)
        self.opening_candles[symbol].append(candle)
        self.debug_logger.add_candle_received(symbol, "live")

    def _combined_opening_candle(self, candles: list[dict]) -> dict:
        candle = candles[0]
        return {
            "time": candle["time"],
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": float(candle.get("volume") or 0),
            "window_candle_count": 1,
        }

    def _build_candidates_from_collection(self):
        self.buy_candidates = []
        self.sell_candidates = []
        self.candidate_details = {}
        self.open_extreme_symbols = set()
        self.prev_close_ready_symbols = set()
        for symbol, candles in self.opening_candles.items():
            # Log candle received
            prev_close = self.prev_close.get(symbol)
            self.debug_logger.add_prev_close(symbol, bool(prev_close))
            if not prev_close:
                continue

            candle = self._combined_opening_candle(candles)
            open_price, high, low = candle["open"], candle["high"], candle["low"]
            self.prev_close_ready_symbols.add(symbol)
            # Use tick tolerance instead of exact float equality. Fyers history
            # often returns prices rounded to the instrument tick, and the live
            # feed can differ by tiny floating-point noise even when the candle
            # clearly printed at the low/high.
            buy_shape = abs(open_price - low) <= TICK_SIZE
            sell_shape = abs(open_price - high) <= TICK_SIZE
            flat_shape = buy_shape and sell_shape
            gap_pct = abs(open_price - prev_close) / prev_close * 100
            gap_up = open_price >= prev_close

            # Flat-candle handling depends on mode:
            #   - TEST mode: fabricated LTP-only candles are expected to be
            #     flat by design (that's the pipeline verification path).
            #     Resolve via gap direction so trades still fire.
            #   - PRODUCTION 9:15 mode: a flat candle means the aggregator
            #     saw ONE tick during the whole 09:15 minute (usually because
            #     WS was disconnected). This is NOT a real "open==low" or
            #     "open==high" signal — it's missing data. Reject and let
            #     phase-2 history backfill supply the real OHLC.
            is_test_schedule = bool(self.settings.get("test_schedule_enabled"))
            if flat_shape:
                if is_test_schedule:
                    buy_shape = gap_up
                    sell_shape = not gap_up
                else:
                    # Reject the flat candle so it fails the shape check.
                    # The history backfill in phase 2 will replace it with
                    # real OHLC and this row will be re-evaluated.
                    buy_shape = False
                    sell_shape = False

            # Log shape check result
            if buy_shape or sell_shape:
                side = "buy" if buy_shape else "sell"
                self.debug_logger.add_shape_result(symbol, side, True)
            else:
                self.debug_logger.add_shape_result(symbol, "none", False,
                                                   "flat" if flat_shape else "neither")

            self.candidate_details[symbol] = {
                "symbol": symbol,
                "sector": self.sector_map.get(symbol),
                "side": "WATCH",
                "open": open_price,
                "high": high,
                "low": low,
                "close": candle["close"],
                "prev_close": prev_close,
                "gap_pct": gap_pct,
                "candle_received": True,
                "window_candle_count": candle["window_candle_count"],
                "shape_passed": buy_shape or sell_shape,
                "signal_shape": "open_equals_low" if buy_shape else "open_equals_high" if sell_shape else "neither",
                "gap_passed": False,
                "passed_indicators": True,
                "indicator_results": {},
                "selected_for_trade": False,
                "rejection_reason": (
                    ("flat_candle_incomplete_data" if flat_shape else "failed_opening_shape")
                    if not (buy_shape or sell_shape)
                    else "failed_gap_filter"
                ),
            }

            if buy_shape:
                self.open_extreme_symbols.add(symbol)
                if gap_pct <= GAP_LIMIT_PCT:
                    self.buy_candidates.append(symbol)
                    self.debug_logger.add_gap_result(symbol, "BUY", True)
                    self.candidate_details[symbol].update({"side": "BUY", "gap_passed": True, "rejection_reason": None})
                else:
                    self.debug_logger.add_gap_result(symbol, "BUY", False)
            elif sell_shape:
                self.open_extreme_symbols.add(symbol)
                if gap_pct <= GAP_LIMIT_PCT:
                    self.sell_candidates.append(symbol)
                    self.debug_logger.add_gap_result(symbol, "SELL", True)
                    self.candidate_details[symbol].update({"side": "SELL", "gap_passed": True, "rejection_reason": None})
                else:
                    self.debug_logger.add_gap_result(symbol, "SELL", False)

    def _opening_candle_needs_history(self, symbol: str) -> bool:
        """Return true when the local signal bar is absent or only one price was observed."""
        candles = self.opening_candles.get(symbol, [])
        if not candles:
            return True
        candle = candles[0]
        return abs(float(candle["high"]) - float(candle["low"])) <= 1e-9

    def _backfill_opening_window_from_history(self, *, include_missing: bool = True) -> int:
        """Use the day-session history API to recover symbols whose live feed
        missed part of the opening window.

        A websocket restart at the bell can leave the live candle collector with
        only a partial opening universe.  The opening scan is still supposed to
        use the same opening candle, so we backfill missing symbols from the
        intraday history endpoint before evaluating entries.
        """
        if not get_stored_access_token():
            return 0

        today = datetime.date.today()
        filled = 0
        start_time = self.scan_candle_time()
        end_time = self._schedule_time(1)
        symbols_to_verify = [
            symbol for symbol in self.watchlist
            if symbol not in self.history_verified_opening_symbols
            and self._opening_candle_needs_history(symbol)
            and (include_missing or bool(self.opening_candles.get(symbol)))
        ]
        if not symbols_to_verify:
            return 0

        def load_symbol(symbol: str):
            try:
                # Use targeted single-minute fetch (2-min window) instead of
                # the full-day history to avoid rate limits and be ~375x faster.
                window = get_single_minute_candle(symbol, start_time)
            except Exception as exc:
                print(f"[algo1] could not backfill opening candle for {symbol}: {exc}")
                window = []
            return symbol, window

        with ThreadPoolExecutor(max_workers=_BACKFILL_MAX_WORKERS) as pool:
            futures = {pool.submit(load_symbol, symbol): symbol for symbol in symbols_to_verify}
            for future in as_completed(futures):
                symbol, window = future.result()
                if not self.prev_close.get(symbol):
                    try:
                        previous_close = get_previous_close(symbol)
                        if previous_close:
                            self.prev_close[symbol] = previous_close
                    except Exception as exc:
                        print(f"[algo1] could not backfill previous close for {symbol}: {exc}")
                if window:
                    # The completed history candle is authoritative even when a
                    # one-tick websocket bar has the same one-row list length.
                    self.opening_candles[symbol] = window
                    self.history_verified_opening_symbols.add(symbol)
                    self.scan_seen_symbols.add(symbol)
                    self.debug_logger.add_candle_received(symbol, "backfilled")
                    filled += 1
                else:
                    self.debug_logger.add_candle_missing(symbol)
        return filled

    def _prefetch_missing_ltps(self, candidates: list[dict], get_ltp_fn) -> dict[str, float]:
        """Batch-fetch LTPs via the Fyers quotes API for symbols not yet in the live feed."""
        missing = [row["symbol"] for row in candidates if not get_ltp_fn(row["symbol"])]
        if not missing:
            return {}
        try:
            result = get_live_ltp_batch(missing)
            return result
        except Exception as exc:
            print(f"[algo1] quotes-API LTP fallback failed: {exc}")
            return {}

    def _enter(self, symbol: str, side: str, entry_price: float) -> bool:
        """Place a live/paper trade for symbol. Returns True on success."""
        if not entry_price:
            self.entry_failures[symbol] = "entry_price_unavailable"
            return False
        if self.broker.already_traded_today(symbol):
            self.entry_failures[symbol] = "already_traded_today"
            return False

        capital = float(self.settings.get("capital_per_trade", 10000))
        qty = int(capital // entry_price)
        if qty < 1:
            self.entry_failures[symbol] = "capital_per_trade_below_share_price"
            return False

        sl_pct = float(self.settings.get("sl_pct", 1.0))
        target_pct = float(self.settings.get("target_pct", 2.0))
        if side == "BUY":
            sl_price = entry_price * (1 - sl_pct / 100)
            target_price = entry_price * (1 + target_pct / 100)
        else:
            sl_price = entry_price * (1 + sl_pct / 100)
            target_price = entry_price * (1 - target_pct / 100)

        try:
            self.broker.open_trade(
                symbol, side, qty, entry_price, sl_price, target_price,
                f"{self.scan_candle_time()} opening range {side}",
                self._signal_snapshot(symbol, side, entry_price),
            )
        except Exception as exc:
            self.entry_failures[symbol] = "broker_open_failed"
            print(f"[{self.algo_id}] entry failed for {symbol}: {exc}")
            return False

        self.selected_symbols.add(symbol)
        self.selected_sides[symbol] = side
        self.debug_logger.add_selected(symbol, side)
        print(f"[{self.algo_id}] ✅ entered {side} {symbol} @ {entry_price:.2f} qty={qty}")
        return True

    def evaluate_entries(self, get_ltp_fn):

        """Two-phase first-come-first-serve entry evaluation.

        Phase 1 (immediate, ~0s): evaluate every symbol whose 9:15 candle
        already arrived via the live feed.  These are the fastest arrivals
        and get the first trade slots.

        Phase 2 (backfill, ~8-15s): fetch Fyers intraday history for the
        symbols the live feed missed.  Any remaining open slots are filled
        from those in encounter order.

        Both phases use the Fyers quotes API as an LTP fallback so that a
        missing recent tick never blocks a valid entry in live mode.
        """
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return True

        is_test_schedule = bool(self.settings.get("test_schedule_enabled"))

        # Build candidates FIRST so the initial broadcast reflects real state
        # (open_extreme_symbols, buy/sell candidates, prev_close counts). Without
        # this the frontend polls scan-results during phase 1 and sees a stale
        # "0/N" for every condition until the scan finally completes 30-60s later.
        self.entry_failures = {}
        self._build_candidates_from_collection()

        self._record_scan_results(
            [], [],
            scan_status="evaluating",
            scan_message=(
                f"Phase 1: immediately evaluating {self._display_time(self.scan_candle_time())} IST "
                "live-feed candles — placing trades on first-come-first-served matches now."
            ),
        )

        # ── Phase 1: immediate ─────────────────────────────────────────────
        # Evaluate whatever the live WebSocket already delivered.  Do NOT wait
        # for the history backfill — these symbols arrived first and are served
        # first.
        phase1_qualified = [
            d for d in self.candidate_details.values() if d.get("gap_passed")
        ]
        all_attempted: set[str] = set()

        if phase1_qualified:
            fallback_ltp1 = self._prefetch_missing_ltps(phase1_qualified, get_ltp_fn)

            def enter_phase1(row: dict) -> bool:
                ltp = (get_ltp_fn(row["symbol"]) or fallback_ltp1.get(row["symbol"])
                       or self.test_mode_ltps.get(row["symbol"]))
                return self._enter(row["symbol"], row["side"], ltp)

            _, attempted1 = execute_candidates_first_come(
                phase1_qualified, self.settings, enter_phase1, self.broker.summary()
            )
            all_attempted |= attempted1

        # ── Phase 2: backfill + remaining slots ────────────────────────────
        summary = self.broker.summary()
        total_cap = max(0, int(self.settings.get("max_trades_per_day", 10)))
        trades_so_far = max(0, int(summary.get("trade_count_today") or 0))
        slots_left = total_cap - trades_so_far

        if slots_left > 0:
            # Real Fyers history backfill — this is the ONLY source in both
            # test and production. Fyers history takes 2-5 minutes to
            # finalize a just-closed candle; the scheduler now waits long
            # enough (5-min delay for test schedules) so history should
            # return real OHLC data. If a symbol still has no candle after
            # backfill, it's genuinely missing — the audit row says so and
            # the strategy simply doesn't trade it.
            filled = self._backfill_opening_window_from_history(
                include_missing=True
            )

            # For test mode, top up any prev_close that's missing so the
            # shape/gap check can actually run on symbols where history
            # DID return real OHLC. This does NOT fabricate candles —
            # only prev_close values for real candles that need it.
            if is_test_schedule:
                need_pc = [
                    s for s in self.watchlist
                    if s not in self.prev_close and s in self.opening_candles
                ]
                if need_pc:
                    try:
                        pc_quotes = get_live_quotes_batch(need_pc)
                        for sym, data in pc_quotes.items():
                            if data.get("prev_close"):
                                self.prev_close[sym] = float(data["prev_close"])
                    except Exception as exc:
                        print(f"[algo1] test mode: prev_close top-up failed: {exc}")

            if filled or not phase1_qualified:
                # Rebuild candidates now that history data is in.
                self._build_candidates_from_collection()
                # Broadcast the mid-scan state so the frontend funnel jumps
                # from the phase-1 numbers to the phase-2 numbers immediately,
                # instead of showing stale 0/N for 30-60s while orders slowly
                # succeed or fail one at a time.
                self._record_scan_results(
                    [], [],
                    scan_status="evaluating",
                    scan_message="Phase 2: LTP/prev-close top-up complete; placing remaining trades.",
                )
                phase2_qualified = [
                    d for d in self.candidate_details.values() if d.get("gap_passed")
                ]
                if phase2_qualified:
                    fallback_ltp2 = self._prefetch_missing_ltps(phase2_qualified, get_ltp_fn)

                    def enter_phase2(row: dict) -> bool:
                        ltp = (get_ltp_fn(row["symbol"]) or fallback_ltp2.get(row["symbol"])
                               or self.test_mode_ltps.get(row["symbol"]))
                        return self._enter(row["symbol"], row["side"], ltp)

                    _, attempted2 = execute_candidates_first_come(
                        phase2_qualified, self.settings, enter_phase2, self.broker.summary()
                    )
                    all_attempted |= attempted2

        # Final rebuild for reporting: the previous-close background loader
        # and the live feed keep delivering data throughout the (sometimes
        # slow, e.g. when live order placement stalls) entry evaluation above.
        # Without this, any symbol whose candle/prev_close arrived after the
        # last rebuild but before we report would show as
        # "candidate_not_evaluated" even though it now has everything needed
        # for a real shape/gap verdict. This is a pure local recompute over
        # data already fetched — no network calls, no effect on trades placed.
        self._build_candidates_from_collection()

        if not self._opening_data_ready():
            self._record_scan_results([], [], scan_status="incomplete", scan_message=self._opening_data_message())
            return False

        self.entries_evaluated_today = today
        buys_selected = [s for s, side in self.selected_sides.items() if side == "BUY"]
        sells_selected = [s for s, side in self.selected_sides.items() if side == "SELL"]

        self.debug_logger.print_report()
        failures_summary = ""
        if self.entry_failures:
            fails: dict = defaultdict(int)
            for reason in self.entry_failures.values():
                fails[reason] += 1
            failures_summary = f" | failures={dict(fails)}"
        print(
            f"[{self.algo_id}] RESULT: {len(self.selected_symbols)} trades "
            f"({sum(1 for s in self.selected_sides.values() if s=='BUY')} BUY, "
            f"{sum(1 for s in self.selected_sides.values() if s=='SELL')} SELL){failures_summary}"
        )

        self._record_scan_results(buys_selected, sells_selected, planned_symbols=all_attempted)
        return True

    def _opening_data_ready(self) -> bool:
        # Evaluate every complete symbol we have instead of blocking the whole
        # scan because quieter symbols did not print during the signal minute.
        # The funnel still reports missing rows for audit, and every available
        # row must pass the normal shape/gap conditions before an entry.
        return self._opening_ready_symbol_count() > 0

    def _opening_ready_symbol_count(self) -> int:
        return len({
            symbol
            for symbol in self.scan_seen_symbols & self.prev_close_ready_symbols
            if (
                symbol in self.history_verified_opening_symbols
                # A flat single-tick candle from the live feed is still a real
                # data point — count it so the scan is never permanently stuck.
                or symbol in self.opening_candles
            )
        })

    def _opening_data_message(self) -> str:
        return (
            "Opening scan was not eligible for entry: "
            f"received {len(self.scan_seen_symbols)}/{len(self.watchlist)} symbols for the {self._display_time(self.scan_candle_time())} IST signal candle and "
            f"matched {self._opening_ready_symbol_count()}/{len(self.watchlist)} symbols with previous closes "
            "(requires at least one symbol with completed OHLC and previous-close data). "
            "One-tick flat bars are retried from FYERS history instead of being treated as real flat candles."
        )

    def mark_collecting_candle(self):
        """Broadcast an early 'collecting_candle' status the moment the scan
        minute starts, so the frontend banner shows the app is actively
        working during the 60-second candle-collection window. Without this
        the UI displays stale yesterday's scan results and the user has no
        indication anything is happening until evaluate_entries fires at
        the end of the minute.
        """
        # Do not overwrite a completed or in-progress scan result with a
        # 'collecting_candle' placeholder — only broadcast when we're in a
        # neutral state (nothing recorded yet today, or last-recorded was
        # yesterday).
        today = datetime.date.today()
        if self.entries_evaluated_today == today:
            return
        candle_display = self._display_time(self.scan_candle_time())
        entry_display = self._display_time(self._schedule_time(1))
        self._record_scan_results(
            [], [],
            scan_status="collecting_candle",
            scan_message=(
                f"Collecting the {candle_display} IST signal candle. "
                f"Trades evaluate at {entry_display} IST."
            ),
        )

    def mark_opening_scan_missed(self):
        if self.entries_evaluated_today == datetime.date.today():
            return
        self.entries_evaluated_today = datetime.date.today()
        self._record_scan_results([], [], scan_status="missed_data", scan_message=self._opening_data_message())

    def mark_opening_scan_failed(self, error: str):
        """Expose a scheduler exception in the scan panel without placing trades."""
        self.entries_evaluated_today = datetime.date.today()
        self._record_scan_results(
            [], [],
            scan_status="incomplete",
            scan_message=f"Opening scan failed: {error}",
        )

    def _signal_snapshot(self, symbol: str, side: str, entry_price: float) -> dict:
        """Immutable evidence for the candle that selected this paper trade."""
        details = self.candidate_details.get(symbol, {})
        return {
            "window": f"{self._display_time(self.scan_candle_time())} IST",
            "side": side,
            "sector": self.sector_map.get(symbol),
            "shape": details.get("signal_shape"),
            "open": details.get("open"),
            "high": details.get("high"),
            "low": details.get("low"),
            "close": details.get("close"),
            "volume": details.get("volume"),
            "previous_close": details.get("prev_close"),
            "gap_pct": details.get("gap_pct"),
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
                observed_candles = self.opening_candles.get(symbol, [])
                observed_candle = (
                    self._combined_opening_candle(observed_candles)
                    if observed_candles
                    else None
                )
                has_candle = observed_candle is not None
                previous_close = self.prev_close.get(symbol)
                row = {
                    "symbol": symbol,
                    "side": "WATCH",
                    "open": observed_candle.get("open") if observed_candle else None,
                    "high": observed_candle.get("high") if observed_candle else None,
                    "low": observed_candle.get("low") if observed_candle else None,
                    "close": observed_candle.get("close") if observed_candle else None,
                    "volume": observed_candle.get("volume") if observed_candle else None,
                    "prev_close": previous_close,
                    "gap_pct": None,
                    "candle_received": has_candle,
                    "shape_passed": False,
                    "gap_passed": False,
                    "passed_indicators": False,
                    "indicator_results": {},
                    "selected_for_trade": False,
                    "rejection_reason": (
                        "missing_opening_candle"
                        if not has_candle
                        else "missing_previous_close"
                        if not previous_close
                        else "candidate_not_evaluated"
                    ),
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
            "sector_breakdown": build_sector_breakdown(rows),
            "condition_breakdown": [
                {"label": "Scanned universe", "passed": len(self.watchlist), "total": len(self.watchlist)},
                {"label": f"Condition 1: {self._display_time(self.scan_candle_time())} candle received", "passed": sum(1 for symbol in self.watchlist if self.opening_candles.get(symbol)), "total": len(self.watchlist)},
                {"label": "Condition 2: open equals low/high", "passed": len(self.open_extreme_symbols), "total": sum(1 for symbol in self.watchlist if self.opening_candles.get(symbol))},
                {"label": "Condition 3: opening gap <= 2%", "passed": sum(1 for row in rows if row.get("gap_passed")), "total": len(self.open_extreme_symbols)},
                {"label": "Final: selected for trade", "passed": len(self.selected_symbols), "total": sum(1 for row in rows if row.get("gap_passed"))},
            ],
        }
        from ..engine import SCAN_RESULTS
        from ..broadcaster import broadcast_sync
        # Dedupe: skip re-broadcasting an unchanged "incomplete" retry so the
        # frontend does not re-render every 5s while we wait for candles/prev_close.
        prev = SCAN_RESULTS.get(self.algo_id) or {}
        SCAN_RESULTS[self.algo_id] = result
        if scan_status == "incomplete" and prev.get("scan_status") == "incomplete":
            def _funnel_key(r: dict) -> tuple:
                return (
                    r.get("scan_status"),
                    r.get("buy_candidates"),
                    r.get("sell_candidates"),
                    r.get("buy_selected"),
                    r.get("sell_selected"),
                    sum(1 for row in (r.get("passed_opening_range") or []) if row.get("candle_received")),
                    sum(1 for row in (r.get("passed_opening_range") or []) if row.get("prev_close")),
                )
            if _funnel_key(prev) == _funnel_key(result):
                return
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

