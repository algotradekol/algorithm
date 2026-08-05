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
from ..fyers_client import get_previous_close, get_intraday_candles_for_range, get_single_minute_candle, get_live_ltp_batch
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
        # Load previous closes in background to avoid blocking startup
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
            prev_close = self.prev_close.get(symbol)
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
            # A flat opening window satisfies both shapes. It has no
            # directional information, so never let BUY win merely because it
            # is evaluated first.
            flat_shape = buy_shape and sell_shape
            gap_pct = abs(open_price - prev_close) / prev_close * 100
            gap_up = open_price >= prev_close

            # A flat opening candle (open=high=low, single print at open) is
            # resolved by the gap direction: gap-up → treat as BUY (open=low),
            # gap-down → treat as SELL (open=high).
            if flat_shape:
                buy_shape = gap_up
                sell_shape = not gap_up

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
                    "failed_opening_shape"
                    if not (buy_shape or sell_shape)
                    else "failed_gap_filter"
                ),
            }

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
                    filled += 1
        if filled:
            print(f"[algo1] verified opening candles for {filled}/{len(symbols_to_verify)} symbols from intraday history")
        return filled

    def _prefetch_missing_ltps(self, candidates: list[dict], get_ltp_fn) -> dict[str, float]:
        """Batch-fetch LTPs via the Fyers quotes API for symbols not yet in the live feed."""
        missing = [row["symbol"] for row in candidates if not get_ltp_fn(row["symbol"])]
        if not missing:
            return {}
        try:
            result = get_live_ltp_batch(missing)
            if result:
                print(
                    f"[algo1] fetched fallback LTP for "
                    f"{len(result)}/{len(missing)} symbols via quotes API"
                )
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
        self.entry_failures = {}
        self._build_candidates_from_collection()
        phase1_qualified = [
            d for d in self.candidate_details.values() if d.get("gap_passed")
        ]
        all_attempted: set[str] = set()

        if phase1_qualified:
            fallback_ltp1 = self._prefetch_missing_ltps(phase1_qualified, get_ltp_fn)

            def enter_phase1(row: dict) -> bool:
                ltp = get_ltp_fn(row["symbol"]) or fallback_ltp1.get(row["symbol"])
                return self._enter(row["symbol"], row["side"], ltp)

            _, attempted1 = execute_candidates_first_come(
                phase1_qualified, self.settings, enter_phase1, self.broker.summary()
            )
            all_attempted |= attempted1
            print(
                f"[algo1] phase-1 done: {len(phase1_qualified)} live-feed candidates evaluated, "
                f"{len(self.selected_symbols)} trade(s) placed so far"
            )

        # ── Phase 2: backfill + remaining slots ────────────────────────────
        summary = self.broker.summary()
        total_cap = max(0, int(self.settings.get("max_trades_per_day", 10)))
        trades_so_far = max(0, int(summary.get("trade_count_today") or 0))
        slots_left = total_cap - trades_so_far

        if slots_left > 0:
            if is_test_schedule:
                # TEST MODE: fill missing symbols using the current live LTP
                # from the websocket — no history API calls needed.
                ltp_filled = 0
                for symbol in self.watchlist:
                    if symbol in self.opening_candles:
                        continue
                    ltp = get_ltp_fn(symbol)
                    if not ltp:
                        continue
                    # Create a flat candle from current LTP (open=high=low=close=ltp)
                    self.opening_candles[symbol] = [{
                        "time": datetime.datetime.now().replace(second=0, microsecond=0),
                        "open": ltp, "high": ltp, "low": ltp,
                        "close": ltp, "volume": 0.0,
                    }]
                    self.scan_seen_symbols.add(symbol)
                    ltp_filled += 1
                print(f"[algo1] test mode: filled {ltp_filled} missing symbols from live LTP")
                filled = ltp_filled
            else:
                # PRODUCTION MODE: fetch actual 9:15 OHLC from Fyers history API.
                filled = self._backfill_opening_window_from_history(
                    include_missing=True
                )

            if filled or not phase1_qualified:
                # Rebuild candidates now that history data is in.
                self._build_candidates_from_collection()
                phase2_qualified = [
                    d for d in self.candidate_details.values() if d.get("gap_passed")
                ]
                if phase2_qualified:
                    fallback_ltp2 = self._prefetch_missing_ltps(phase2_qualified, get_ltp_fn)

                    def enter_phase2(row: dict) -> bool:
                        ltp = get_ltp_fn(row["symbol"]) or fallback_ltp2.get(row["symbol"])
                        return self._enter(row["symbol"], row["side"], ltp)

                    _, attempted2 = execute_candidates_first_come(
                        phase2_qualified, self.settings, enter_phase2, self.broker.summary()
                    )
                    all_attempted |= attempted2
                    print(
                        f"[algo1] phase-2 done: {len(phase2_qualified)} backfilled candidates evaluated, "
                        f"{len(self.selected_symbols)} total trade(s) placed"
                    )

        if not self._opening_data_ready():
            self._record_scan_results([], [], scan_status="incomplete", scan_message=self._opening_data_message())
            return False

        self.entries_evaluated_today = today
        buys_selected = [s for s, side in self.selected_sides.items() if side == "BUY"]
        sells_selected = [s for s, side in self.selected_sides.items() if side == "SELL"]
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

