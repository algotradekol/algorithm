"""Historical, read-only replay for the two live opening-window strategies."""
import datetime
import gzip
import hashlib
import pickle
import shutil
import tempfile
import threading
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .charges import calculate_charges, get_charges_config
from .fyers_client import get_intraday_candles_for_range
from .strategy_settings import get_settings
from .strategies.algo4_opening_range_indicators import Algo4OpeningRangeIndicators
from .candidate_ranking import build_sector_breakdown
from .candidate_selection import select_candidates_first_come
from .supabase_client import run_with_supabase
from .symbols import get_nse500_sector_map
from .trailing_stop import calculate_point_trailing, silver_tsl_points

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
SUPPORTED_ALGOS = {"algo1", "algo2", "algo3"}
MAX_WORKERS = 2
MAX_BACKTEST_DAYS = 31
EMA_PERIOD = 20
WARMUP_LOOKBACK_DAYS = 10
SILVER_MICRO_BUCKET_MINUTES = 15
SILVER_LEGACY_BUY_BUCKET_MINUTES = 5
SILVER_LEGACY_BUY_CONFIRMATION_MINUTES = 15
SILVER_MICRO_MCX_CLOSE_HHMM = "23:30"  # MCX evening session close
OPENING_WINDOW_START = "09:15"
OPENING_WINDOW_END = "09:16"
ENTRY_TIME = "09:16"
EXIT_SCAN_START = "09:17"
SILVER_SELL_PLAN_RED_CHAIN = "red_chain"
SILVER_SELL_PLAN_LATEST_REFERENCE = "latest_reference"
SILVER_SELL_PLAN_LABELS = {
    SILVER_SELL_PLAN_RED_CHAIN: "Red-chain comparison (current)",
    SILVER_SELL_PLAN_LATEST_REFERENCE: "Latest red reference (legacy)",
}
SILVER_BUY_PLAN_REFERENCE_BREAKOUT = "reference_breakout"
# Kept as an input compatibility alias for old saved jobs/settings.
SILVER_BUY_PLAN_LEGACY_CONFIRMATION = "legacy_confirmation"
SILVER_BUY_PLAN_LABELS = {
    SILVER_BUY_PLAN_REFERENCE_BREAKOUT: "15m EMA reference breakout",
}

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


class BacktestCancelled(Exception):
    """Raised inside a worker after the user requests cancellation."""


def _raise_if_cancelled(job_id: str) -> None:
    with _lock:
        cancelled = bool((_jobs.get(job_id) or {}).get("cancel_requested"))
    if cancelled:
        raise BacktestCancelled()


def _ema_step(previous: float | None, value: float, period: int = EMA_PERIOD) -> float:
    k = 2 / (period + 1)
    return float(value) if previous is None else float(value) * k + previous * (1 - k)


def normalize_silver_sell_plan(value: str | None) -> str:
    plan = str(value or SILVER_SELL_PLAN_RED_CHAIN).strip().lower()
    if plan not in SILVER_SELL_PLAN_LABELS:
        raise ValueError("Unknown Silver sell plan. Choose red_chain or latest_reference.")
    return plan


def normalize_silver_buy_plan(value: str | None) -> str:
    plan = str(value or SILVER_BUY_PLAN_REFERENCE_BREAKOUT).strip().lower()
    if plan in {SILVER_BUY_PLAN_REFERENCE_BREAKOUT, "live_breakout", SILVER_BUY_PLAN_LEGACY_CONFIRMATION}:
        return SILVER_BUY_PLAN_REFERENCE_BREAKOUT
    raise ValueError("Silver BUY logic is fixed to the 15m reference breakout.")


class BacktestHistoryCache:
    """Compressed, job-local candle cache used to avoid replay re-downloads."""

    def __init__(self):
        self.directory = Path(tempfile.mkdtemp(prefix="algo-backtest-"))

    def _path(self, symbol: str) -> Path:
        digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.pkl.gz"

    def store(self, symbol: str, history: list[dict]) -> bool:
        if not history:
            return False
        with gzip.open(self._path(symbol), "wb") as handle:
            pickle.dump(history, handle, protocol=pickle.HIGHEST_PROTOCOL)
        return True

    def load(self, symbol: str) -> list[dict]:
        with gzip.open(self._path(symbol), "rb") as handle:
            return pickle.load(handle)

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def start_backtest(
    algo_id: str,
    start_date: str,
    end_date: str,
    watchlist: list[str],
    silver_buy_plan: str | None = None,
    silver_sell_plan: str | None = None,
) -> dict:
    if algo_id not in SUPPORTED_ALGOS:
        raise ValueError("Backtesting is currently available for Simple, Filter, and Silver Micro only.")
    first_date = datetime.date.fromisoformat(start_date)
    last_date = datetime.date.fromisoformat(end_date)
    today = datetime.datetime.now(IST).date()
    if first_date > last_date:
        raise ValueError("Start date must be on or before end date.")
    if last_date > today:
        raise ValueError("Choose today or an earlier trading date.")
    if (last_date - first_date).days + 1 > MAX_BACKTEST_DAYS:
        raise ValueError(f"Choose a range of {MAX_BACKTEST_DAYS} calendar days or fewer.")
    if not watchlist:
        raise ValueError("The NSE 500 watchlist is not ready yet.")
    normalized_buy_plan = normalize_silver_buy_plan(silver_buy_plan) if algo_id == "algo3" else None
    normalized_sell_plan = normalize_silver_sell_plan(silver_sell_plan) if algo_id == "algo3" else None

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "algo_id": algo_id,
        "silver_buy_plan": normalized_buy_plan,
        "silver_sell_plan": normalized_sell_plan,
        "start_date": first_date.isoformat(),
        "end_date": last_date.isoformat(),
        "total_symbols": len(watchlist),
        "completed_symbols": 0,
        "failed_symbols": 0,
        "phase": "queued",
        "replay_total": 0,
        "replay_completed": 0,
        "replay_failed": 0,
        "replay_activity": [],
        "cached_history_symbols": 0,
        "message": "Queued historical candle download.",
        "result": None,
        "error": None,
        "cancel_requested": False,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with _lock:
        active = next(
            (existing for existing in _jobs.values() if existing.get("status") in {"queued", "running", "cancelling"}),
            None,
        )
        if active:
            raise ValueError("A backtest is already running. Wait for it to finish before starting another one.")
        _jobs[job_id] = job
    _persist_job(job)
    threading.Thread(
        target=_run_job,
        args=(job_id, algo_id, first_date, last_date, list(watchlist), normalized_buy_plan, normalized_sell_plan),
        daemon=True,
    ).start()
    return _public_job(job)


def get_backtest_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
    if job:
        return _public_job(job)
    return _load_persisted_job(job_id)


def cancel_backtest_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        if job.get("status") in {"queued", "running", "cancelling"}:
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            job["phase"] = "cancelling"
            job["message"] = "Cancelling backtest after the current operation finishes."
        snapshot = dict(job)
    _persist_job(snapshot)
    return _public_job(snapshot)


def _public_job(job: dict | None) -> dict | None:
    if not job:
        return None
    return {key: value for key, value in job.items() if key != "_internal"}


def _update(job_id: str, **values):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(values)
            job = dict(_jobs[job_id])
        else:
            job = None
    # Progress changes many times per run; persist only lifecycle transitions.
    if job and any(key in values for key in ("status", "error", "result")):
        _persist_job(job)


def _persist_job(job: dict) -> None:
    """Persist a job when the optional Supabase table has been installed."""
    row = {
        "job_id": job["id"],
        "status": job.get("status"),
        "algo_id": job.get("algo_id"),
        "start_date": job.get("start_date"),
        "end_date": job.get("end_date"),
        "payload": _public_job(job),
        "updated_at": "now()",
    }
    try:
        run_with_supabase(
            lambda supabase: supabase.table("backtest_jobs").upsert(
                row, on_conflict="job_id"
            ).execute()
        )
    except Exception:
        # The feature remains usable before the migration is run. The API still
        # serves the in-process job, but will clearly report a restart loss.
        return


def _load_persisted_job(job_id: str) -> dict | None:
    try:
        result = run_with_supabase(
            lambda supabase: supabase.table("backtest_jobs")
            .select("payload")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        job = rows[0].get("payload") or None
        if job and job.get("status") in {"queued", "running", "cancelling"}:
            job.update({
                "status": "failed",
                "error": "Backtest interrupted by a backend restart. Start a new run.",
                "message": "Backtest interrupted by a backend restart.",
            })
            _persist_job(job)
        return job
    except Exception:
        return None


def _run_job(
    job_id: str,
    algo_id: str,
    first_date: datetime.date,
    last_date: datetime.date,
    watchlist: list[str],
    silver_buy_plan: str | None = None,
    silver_sell_plan: str | None = None,
):
    history_cache = BacktestHistoryCache()
    try:
        _raise_if_cancelled(job_id)
        settings = get_settings(algo_id)
        if algo_id == "algo3":
            if not watchlist:
                raise ValueError("The Silver Micro contract could not be resolved for backtesting.")
            _run_silver_micro_job(
                job_id, algo_id, first_date, last_date, watchlist[0], settings, history_cache,
                silver_buy_plan or SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
                silver_sell_plan or SILVER_SELL_PLAN_RED_CHAIN,
            )
            return
        _update(
            job_id,
            status="running",
            phase="screening",
            message="Screening NSE 500 symbols with two bounded workers.",
        )
        lookback_start = first_date - datetime.timedelta(days=7)
        trading_days = [
            first_date + datetime.timedelta(days=offset)
            for offset in range((last_date - first_date).days + 1)
            if (first_date + datetime.timedelta(days=offset)).weekday() < 5
        ]
        sector_map = get_nse500_sector_map()
        rows_by_day: dict[datetime.date, list[dict]] = {day: [] for day in trading_days}
        symbols_with_history = 0

        def screen_symbol(symbol: str):
            # Cache each response on disk. Keeping all 500 histories in RAM
            # exhausted Railway, while discarding them forced duplicate Fyers
            # requests for every selected replay signal.
            _raise_if_cancelled(job_id)
            history = get_intraday_candles_for_range(symbol, lookback_start, last_date)
            _raise_if_cancelled(job_id)
            rows = [_evaluate_symbol(algo_id, symbol, day, history, settings, sector_map) for day in trading_days]
            return rows, bool(history), history_cache.store(symbol, history)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(screen_symbol, symbol) for symbol in watchlist]
            for future in as_completed(futures):
                _raise_if_cancelled(job_id)
                try:
                    rows, has_history, cached = future.result()
                    if has_history:
                        symbols_with_history += 1
                    if cached:
                        _increment(job_id, "cached_history_symbols")
                    else:
                        _increment(job_id, "failed_symbols")
                    for day, row in zip(trading_days, rows):
                        rows_by_day[day].append(row)
                except Exception:
                    _increment(job_id, "failed_symbols")
                finally:
                    _increment(job_id, "completed_symbols")

        # Selection is complete before replay begins. This lets the UI report
        # the real remaining work instead of showing 500/500 while Fyers calls
        # are still being made for every selected signal.
        prepared_days = []
        for target_date in trading_days:
            _raise_if_cancelled(job_id)
            daily_result, selected = _prepare_daily_result(
                algo_id, target_date, rows_by_day[target_date], len(watchlist), settings
            )
            prepared_days.append((target_date, daily_result, selected))

        replay_total = sum(len(selected) for _, _, selected in prepared_days)
        _update(
            job_id,
            phase="replaying",
            replay_total=replay_total,
            replay_completed=0,
            replay_failed=0,
            message=(
                f"Replaying 0 / {replay_total} selected signals from the local candle cache "
                f"across {len(trading_days)} trading days."
            ),
        )
        charges_config = get_charges_config()
        trades_by_date: dict[datetime.date, list[dict]] = {
            target_date: [] for target_date, _, _ in prepared_days
        }

        replay_by_symbol: dict[str, list[tuple[datetime.date, dict]]] = defaultdict(list)
        for target_date, _, selected in prepared_days:
            for row in selected:
                replay_by_symbol[row["symbol"]].append((target_date, row))

        # Replay is now local CPU/disk work. One cached history is used for
        # every selected date of that symbol, with no second Fyers API call.
        def replay_symbol(symbol: str, selected_rows: list[tuple[datetime.date, dict]]):
            _raise_if_cancelled(job_id)
            result = _replay_cached_symbol(
                history_cache,
                symbol,
                selected_rows,
                settings,
                charges_config,
            )
            _raise_if_cancelled(job_id)
            return result

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for symbol, selected_rows in replay_by_symbol.items():
                future = pool.submit(
                    replay_symbol,
                    symbol,
                    selected_rows,
                )
                futures[future] = selected_rows
            for future in as_completed(futures):
                _raise_if_cancelled(job_id)
                try:
                    replayed_rows = future.result()
                except Exception:
                    replayed_rows = [
                        (target_date, row, None, True)
                        for target_date, row in futures[future]
                    ]
                for target_date, row, trade, failed in replayed_rows:
                    if failed:
                        _increment(job_id, "replay_failed")
                    if trade:
                        trades_by_date[target_date].append(trade)
                        row["selected_for_trade"] = True
                        row["rejection_reason"] = None
                        _append_replay_activity(job_id, {
                            "date": target_date.isoformat(),
                            "symbol": row["symbol"],
                            "side": row.get("side"),
                            "status": trade.get("exit_reason", "SIMULATED"),
                            "entry_price": trade.get("entry_price"),
                            "exit_price": trade.get("exit_price"),
                            "net_pnl": trade.get("net_pnl"),
                        })
                    else:
                        row["selected_for_trade"] = False
                        if failed:
                            row["rejection_reason"] = "replay_cache_unavailable"
                            activity_status = "CACHE_UNAVAILABLE"
                        else:
                            # _simulate_trade sets a specific failure reason
                            # (no_09_16_entry_candle vs capital_per_trade_below_share_price).
                            row["rejection_reason"] = row.pop("_simulation_failure_reason", "no_09_16_entry_candle")
                            activity_status = (
                                "NO_ENTRY_CANDLE"
                                if row["rejection_reason"] == "no_09_16_entry_candle"
                                else "CAPITAL_TOO_LOW"
                            )
                        _append_replay_activity(job_id, {
                            "date": target_date.isoformat(),
                            "symbol": row["symbol"],
                            "side": row.get("side"),
                            "status": activity_status,
                        })
                    _increment(job_id, "replay_completed")
                    progress = _job_progress(job_id, "replay_completed")
                    _update(
                        job_id,
                        message=(
                            f"Replaying {progress} / {replay_total} selected signals from the local candle cache "
                            f"across {len(trading_days)} trading days."
                        ),
                    )

        daily_results = []
        for target_date, daily_result, _ in prepared_days:
            _raise_if_cancelled(job_id)
            trades = trades_by_date[target_date]
            daily_result["trades"] = trades
            daily_result["summary"] = {
                **_performance_summary(trades),
                "buy_count": len([trade for trade in trades if trade["side"] == "BUY"]),
                "sell_count": len([trade for trade in trades if trade["side"] == "SELL"]),
            }
            daily_result["condition_breakdown"][-1]["passed"] = len(trades)
            daily_results.append(daily_result)
        coverage = {
            "requested_symbols": len(watchlist),
            "symbols_with_history": symbols_with_history,
            "symbols_without_history": len(watchlist) - symbols_with_history,
            "lookback_start": lookback_start.isoformat(),
        }
        result = _range_result(algo_id, first_date, last_date, daily_results, coverage)
        _raise_if_cancelled(job_id)
        _update(job_id, status="complete", phase="complete", message="Backtest complete.", result=result)
    except BacktestCancelled:
        _update(
            job_id,
            status="cancelled",
            phase="cancelled",
            message="Backtest cancelled.",
            error=None,
            result=None,
        )
    except Exception as exc:
        _update(job_id, status="failed", error=str(exc), message="Backtest failed.")
    finally:
        history_cache.cleanup()


def _increment(job_id: str, field: str):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job[field] = int(job.get(field) or 0) + 1


def _job_progress(job_id: str, field: str) -> int:
    with _lock:
        return int((_jobs.get(job_id) or {}).get(field) or 0)


def _append_replay_activity(job_id: str, activity: dict) -> None:
    """Keep a small real-time audit trail without bloating the job payload."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        events = list(job.get("replay_activity") or [])
        events.append(activity)
        job["replay_activity"] = events[-8:]


def _replay_cached_symbol(
    cache: BacktestHistoryCache,
    symbol: str,
    selected_rows: list[tuple[datetime.date, dict]],
    settings: dict,
    charges_config: dict,
) -> list[tuple[datetime.date, dict, dict | None, bool]]:
    """Replay all selected dates for one symbol from one cached response."""
    history = cache.load(symbol)
    return [
        (target_date, row, _simulate_trade(row, history, target_date, settings, charges_config), False)
        for target_date, row in selected_rows
    ]


def _simulate(algo_id: str, target_date: datetime.date, watchlist: list[str], histories: dict[str, list[dict]], settings: dict, sector_map: dict[str, str] | None = None) -> dict:
    rows: list[dict] = []
    condition = {"candle": 0, "shape": 0, "gap": 0, "filters": 0}
    for symbol in watchlist:
        history = histories.get(symbol) or []
        row = _evaluate_symbol(algo_id, symbol, target_date, history, settings, sector_map)
        if row["has_opening_candle"]:
            condition["candle"] += 1
        if row["shape_passed"]:
            condition["shape"] += 1
        if row["gap_passed"]:
            condition["gap"] += 1
        if row["filters_passed"]:
            condition["filters"] += 1
        rows.append(row)

    candidates = [row for row in rows if row.get("side") and row.get("filters_passed")]
    selected = _select_candidates(candidates, settings)
    selected_symbols = {row["symbol"] for row in selected}
    charges_config = get_charges_config()
    trades = []
    for row in selected:
        history = histories[row["symbol"]]
        trade = _simulate_trade(row, history, target_date, settings, charges_config)
        if trade:
            trades.append(trade)
        row["selected_for_trade"] = bool(trade)
        if trade:
            row["rejection_reason"] = None
        else:
            # _simulate_trade attaches a specific reason; fall back to the
            # legacy label if it did not (defensive).
            row["rejection_reason"] = row.pop("_simulation_failure_reason", "no_09_16_entry_candle")

    # A candidate that passed shape+gap+filters but was NOT selected either
    # (a) lost to earlier candidates within its side cap, or
    # (b) lost to the total daily cap being exhausted by the other side.
    # Report distinctly so audit rows don't all show generic "slots_full".
    total_cap = max(0, int(settings.get("max_trades_per_day", 10) or 10))
    buy_cap = max(0, int(settings.get("max_buy_trades", total_cap) or total_cap))
    sell_cap = max(0, int(settings.get("max_sell_trades", total_cap) or total_cap))
    selected_buys = sum(1 for r in selected if str(r.get("side", "")).upper() == "BUY")
    selected_sells = sum(1 for r in selected if str(r.get("side", "")).upper() == "SELL")
    for row in rows:
        if not (row.get("side") and row.get("filters_passed") and row["symbol"] not in selected_symbols):
            continue
        side = str(row.get("side") or "").upper()
        if side == "BUY" and selected_buys >= buy_cap:
            row["rejection_reason"] = "buy_side_cap_reached"
        elif side == "SELL" and selected_sells >= sell_cap:
            row["rejection_reason"] = "sell_side_cap_reached"
        elif len(selected) >= total_cap:
            row["rejection_reason"] = "daily_cap_reached"
        else:
            # Cap was NOT hit — this candidate really should have been selected.
            # This exposes a bug in _select_candidates ordering / dedup rather
            # than being a real "slots full" event.
            row["rejection_reason"] = "not_selected_despite_open_slot"

    summary = _performance_summary(trades)
    buys = len([trade for trade in trades if trade["side"] == "BUY"])
    sells = len([trade for trade in trades if trade["side"] == "SELL"])
    return {
        "algo_id": algo_id,
        "date": target_date.isoformat(),
        "mode": "historical_candle_replay",
        "execution_assumption": "Signal uses the 09:15 candle only; entry uses the 09:16 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
        "summary": {**summary, "buy_count": buys, "sell_count": sells},
        "sector_breakdown": build_sector_breakdown(rows),
        "condition_breakdown": [
            {"label": "Scanned universe", "passed": len(watchlist), "total": len(watchlist)},
            {"label": "Condition 1: 09:15 signal candle received", "passed": condition["candle"], "total": len(watchlist)},
            {"label": "Condition 2: open equals low/high", "passed": condition["shape"], "total": condition["candle"]},
            {"label": "Condition 3: gap rule", "passed": condition["gap"], "total": condition["shape"]},
            {"label": "Condition 4: enabled filters", "passed": condition["filters"], "total": condition["gap"]},
            {"label": "Final: selected for trade", "passed": len(trades), "total": len(candidates)},
        ],
        "candidates": rows,
        "trades": trades,
    }


def _prepare_daily_result(
    algo_id: str,
    target_date: datetime.date,
    rows: list[dict],
    watchlist_size: int,
    settings: dict,
) -> tuple[dict, list[dict]]:
    """Select a day's candidates without retaining historical candle arrays."""
    condition = {
        "candle": sum(bool(row.get("has_opening_candle")) for row in rows),
        "shape": sum(bool(row.get("shape_passed")) for row in rows),
        "gap": sum(bool(row.get("gap_passed")) for row in rows),
        "filters": sum(bool(row.get("filters_passed")) for row in rows),
    }
    candidates = [row for row in rows if row.get("side") and row.get("filters_passed")]
    selected = _select_candidates(candidates, settings)
    selected_symbols = {row["symbol"] for row in selected}
    total_cap = max(0, int(settings.get("max_trades_per_day", 10) or 10))
    buy_cap = max(0, int(settings.get("max_buy_trades", total_cap) or total_cap))
    sell_cap = max(0, int(settings.get("max_sell_trades", total_cap) or total_cap))
    selected_buys = sum(1 for r in selected if str(r.get("side", "")).upper() == "BUY")
    selected_sells = sum(1 for r in selected if str(r.get("side", "")).upper() == "SELL")
    for row in rows:
        if not (row.get("side") and row.get("filters_passed") and row["symbol"] not in selected_symbols):
            continue
        side = str(row.get("side") or "").upper()
        if side == "BUY" and selected_buys >= buy_cap:
            row["rejection_reason"] = "buy_side_cap_reached"
        elif side == "SELL" and selected_sells >= sell_cap:
            row["rejection_reason"] = "sell_side_cap_reached"
        elif len(selected) >= total_cap:
            row["rejection_reason"] = "daily_cap_reached"
        else:
            row["rejection_reason"] = "not_selected_despite_open_slot"
    return {
        "algo_id": algo_id,
        "date": target_date.isoformat(),
        "mode": "historical_candle_replay",
        "execution_assumption": "Signal uses the 09:15 candle only; entry uses the 09:16 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
        "summary": {},
        "sector_breakdown": build_sector_breakdown(rows),
        "condition_breakdown": [
            {"label": "Scanned universe", "passed": watchlist_size, "total": watchlist_size},
            {"label": "Condition 1: 09:15 signal candle received", "passed": condition["candle"], "total": watchlist_size},
            {"label": "Condition 2: open equals low/high", "passed": condition["shape"], "total": condition["candle"]},
            {"label": "Condition 3: gap rule", "passed": condition["gap"], "total": condition["shape"]},
            {"label": "Condition 4: enabled filters", "passed": condition["filters"], "total": condition["gap"]},
            {"label": "Final: selected for trade", "passed": 0, "total": len(candidates)},
        ],
        "candidates": rows,
        "trades": [],
    }, selected


def _performance_summary(trades: list[dict]) -> dict:
    net_values = [float(trade["net_pnl"]) for trade in trades]
    gross_values = [float(trade["gross_pnl"]) for trade in trades]
    charge_values = [float(trade["total_charges"]) for trade in trades]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    gross_profit = round(sum(wins), 2)
    gross_loss = round(abs(sum(losses)), 2)
    deployed = round(sum(float(trade["entry_price"]) * int(trade["qty"]) for trade in trades), 2)
    exit_counts = {reason: 0 for reason in ("TARGET", "SL", "EOD_SQUAREOFF")}
    for trade in trades:
        reason = trade.get("exit_reason")
        if reason in exit_counts:
            exit_counts[reason] += 1
    return {
        "trade_count": len(trades),
        "gross_pnl": round(sum(gross_values), 2),
        "total_charges": round(sum(charge_values), 2),
        "net_pnl": round(sum(net_values), 2),
        "win_count": len(wins),
        "loss_count": len(losses),
        "breakeven_count": len(net_values) - len(wins) - len(losses),
        "win_rate_pct": round((len(wins) / len(net_values) * 100) if net_values else 0, 2),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (None if not gross_profit else "Infinity"),
        "average_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "average_net_per_trade": round(sum(net_values) / len(net_values), 2) if net_values else 0,
        "capital_deployed": deployed,
        "net_return_on_deployed_pct": round((sum(net_values) / deployed * 100) if deployed else 0, 3),
        "exit_counts": exit_counts,
    }


def _range_result(
    algo_id: str,
    first_date: datetime.date,
    last_date: datetime.date,
    daily_results: list[dict],
    data_coverage: dict,
    *,
    mode: str = "historical_candle_replay",
    execution_assumption: str = "Signal uses the 09:15 candle only; entry uses the 09:16 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
) -> dict:
    all_trades = [trade for day in daily_results for trade in day["trades"]]
    summary = _performance_summary(all_trades)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    daily_rows = []
    for day in daily_results:
        day_summary = day["summary"]
        equity += float(day_summary["net_pnl"])
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
        daily_rows.append({
            "date": day["date"],
            "summary": day_summary,
            "condition_breakdown": day["condition_breakdown"],
            "sector_breakdown": day.get("sector_breakdown") or [],
            "data_available_symbols": day.get("data_available_symbols", next(
                (step["passed"] for step in day["condition_breakdown"] if step["label"] == "Condition 1: 09:15 signal candle received"), 0,
            )),
            "trades": day["trades"],
            "candidates": day["candidates"],
            "chart": day.get("chart"),
        })
    ranked_daily_rows = [day for day in daily_rows if int(day["summary"].get("trade_count") or 0) > 0] or daily_rows
    best_day = max(ranked_daily_rows, key=lambda day: float(day["summary"]["net_pnl"]), default=None)
    worst_day = min(ranked_daily_rows, key=lambda day: float(day["summary"]["net_pnl"]), default=None)
    return {
        "algo_id": algo_id,
        "start_date": first_date.isoformat(),
        "end_date": last_date.isoformat(),
        "mode": mode,
        "execution_assumption": execution_assumption,
        "summary": {**summary, "trading_days_replayed": len(daily_results), "max_drawdown": round(max_drawdown, 2)},
        "best_day": {"date": best_day["date"], "net_pnl": best_day["summary"]["net_pnl"]} if best_day else None,
        "worst_day": {"date": worst_day["date"], "net_pnl": worst_day["summary"]["net_pnl"]} if worst_day else None,
        "data_coverage": data_coverage,
        "sector_breakdown": build_sector_breakdown([row for day in daily_results for row in day.get("candidates", [])]),
        "daily_results": daily_rows,
    }


def _evaluate_symbol(algo_id: str, symbol: str, target_date: datetime.date, history: list[dict], settings: dict, sector_map: dict[str, str] | None = None) -> dict:
    opening_candles = [
        candle for candle in history
        if candle["time"].date() == target_date and OPENING_WINDOW_START <= candle["time"].strftime("%H:%M") < OPENING_WINDOW_END
    ]
    base = {"symbol": symbol, "sector": (sector_map or {}).get(symbol), "has_opening_candle": bool(opening_candles), "shape_passed": False, "gap_passed": False, "filters_passed": False, "selected_for_trade": False, "rejection_reason": "missing_09_15_signal_candle", "indicator_results": {}}
    if not opening_candles:
        return base
    opening = {
        "time": opening_candles[0]["time"],
        "open": opening_candles[0]["open"],
        "high": max(candle["high"] for candle in opening_candles),
        "low": min(candle["low"] for candle in opening_candles),
        "close": opening_candles[-1]["close"],
        "volume": sum(float(candle.get("volume") or 0) for candle in opening_candles),
    }
    prior = [candle for candle in history if candle["time"].date() < target_date]
    prev_close = float(prior[-1]["close"]) if prior else None
    base.update({
        "open": opening["open"],
        "high": opening["high"],
        "low": opening["low"],
        "close": opening["close"],
        "volume": opening.get("volume") or 0,
        "prev_close": prev_close,
    })
    if not prev_close:
        base["rejection_reason"] = "missing_previous_close"
        return base

    is_buy_shape = abs(opening["open"] - opening["low"]) <= 0.05
    is_sell_shape = abs(opening["open"] - opening["high"]) <= 0.05
    base["shape_passed"] = is_buy_shape or is_sell_shape
    if not base["shape_passed"]:
        base["rejection_reason"] = "open_not_at_candle_extreme"
        return base
    buy_gap = (opening["open"] - prev_close) / prev_close * 100
    sell_gap = (prev_close - opening["open"]) / prev_close * 100
    base["gap_pct"] = round(buy_gap, 4)
    if algo_id == "algo1":
        buy_ok = is_buy_shape and abs(buy_gap) <= 2
        sell_ok = is_sell_shape and abs(sell_gap) <= 2
    else:
        buy_ok = is_buy_shape and 0.5 <= buy_gap <= 2
        sell_ok = is_sell_shape and 0.5 <= sell_gap <= 2
    side = "BUY" if buy_ok else "SELL" if sell_ok else None
    base.update({"side": side or "WATCH", "gap_pct": buy_gap if buy_ok else sell_gap if sell_ok else buy_gap, "gap_passed": bool(side)})
    if not side:
        base["rejection_reason"] = "gap_rule_failed"
        return base
    if algo_id == "algo1":
        base["filters_passed"] = True
        base["rejection_reason"] = "slots_full"
        return base

    prior_and_opening = [candle for candle in history if candle["time"].date() < target_date or candle["time"].strftime("%H:%M") < OPENING_WINDOW_END]
    day_candles = [candle for candle in prior_and_opening if candle["time"].date() == target_date]
    volume = float(opening.get("volume") or 0)
    total_value = sum(float(candle["close"]) * float(candle.get("volume") or 0) for candle in day_candles)
    total_volume = sum(float(candle.get("volume") or 0) for candle in day_candles)
    vwap = total_value / total_volume if total_volume else None
    helper = object.__new__(Algo4OpeningRangeIndicators)
    ema20 = helper._ema(prior_and_opening, 20)
    ema50 = helper._ema(prior_and_opening, 50)
    rsi = helper._rsi(prior_and_opening, 14)
    adx = helper._adx(prior_and_opening, 14)
    supertrend = helper._supertrend(prior_and_opening, int(settings["supertrend_period"]), float(settings["supertrend_multiplier"]))
    ltp = float(opening["close"])
    buy = side == "BUY"
    # Keep replay behavior identical to the active v14 Filter: Rs 3,000
    # maximum for BUY and the configurable Rs 4,000 sell-side ceiling.
    price_max = min(float(settings["ltp_max"]), 3000.0) if buy else float(settings["ltp_max"])

    def check(key, value, passed, enabled):
        return {"value": value, "passed": bool(passed), "enabled": bool(enabled)}

    results = {
        "vwap": check("vwap", vwap, vwap is not None and (ltp > vwap if buy else ltp < vwap), settings.get("filter_vwap", True)),
        "rsi": check("rsi", rsi, rsi is not None and (rsi > settings["rsi_buy_threshold"] if buy else rsi < settings["rsi_sell_threshold"]), settings.get("filter_rsi", True)),
        "adx": check("adx", adx, adx is not None and adx > settings["adx_threshold"], settings.get("filter_adx", True)),
        "supertrend": check("supertrend", supertrend, supertrend is not None and (ltp > supertrend if buy else ltp < supertrend), settings.get("filter_supertrend", True)),
        "ema20": check("ema20", ema20, ema20 is not None and (ltp > ema20 if buy else ltp < ema20), settings.get("filter_ema20", False)),
        "ema50": check("ema50", ema50, ema20 is not None and ema50 is not None and (ema20 > ema50 if buy else ema20 < ema50), settings.get("filter_ema50", False)),
        "volume": check("volume", volume, volume > settings["min_volume"], settings.get("filter_volume", True)),
        "liquidity": check("liquidity", total_value, total_value > settings["min_total_value"], settings.get("filter_liquidity", True)),
        "price_range": check("price_range", ltp, settings["ltp_min"] < ltp < price_max, settings.get("filter_price_range", True)),
    }
    base["indicator_results"] = results
    base["filters_passed"] = all(item["passed"] for item in results.values() if item["enabled"])
    base["rejection_reason"] = "slots_full" if base["filters_passed"] else "failed_indicator_filter"
    return base


def _select_candidates(candidates: list[dict], settings: dict) -> list[dict]:
    return select_candidates_first_come(candidates, settings)


def _simulate_trade(row: dict, history: list[dict], target_date: datetime.date, settings: dict, charges_config: dict) -> dict | None:
    entry_candle = next((candle for candle in history if candle["time"].date() == target_date and candle["time"].strftime("%H:%M") == ENTRY_TIME), None)
    if not entry_candle:
        # Distinct reason so the caller can display it accurately instead of
        # mis-attributing every simulation failure to "no_09_16_entry_candle".
        row["_simulation_failure_reason"] = "no_09_16_entry_candle"
        return None
    side = row["side"]
    entry = float(entry_candle["open"])
    qty = int(float(settings["capital_per_trade"]) // entry)
    if qty < 1:
        row["_simulation_failure_reason"] = "capital_per_trade_below_share_price"
        return None
    sl = entry * (1 - settings["sl_pct"] / 100) if side == "BUY" else entry * (1 + settings["sl_pct"] / 100)
    target = entry * (1 + settings["target_pct"] / 100) if side == "BUY" else entry * (1 - settings["target_pct"] / 100)
    highest = lowest = entry
    exit_price = None
    exit_reason = None
    exit_time = None
    candles = [candle for candle in history if candle["time"].date() == target_date and candle["time"].strftime("%H:%M") >= EXIT_SCAN_START and candle["time"].strftime("%H:%M") < "15:15"]
    for candle in candles:
        # Conservative order: an existing stop is checked before target when
        # both are touched inside the same OHLC candle.
        stop_hit = candle["low"] <= sl if side == "BUY" else candle["high"] >= sl
        target_hit = candle["high"] >= target if side == "BUY" else candle["low"] <= target
        if stop_hit:
            exit_price, exit_reason = sl, "SL"
            exit_time = candle["time"]
            break
        if target_hit and settings.get("exit_mode") != "trailing_sl_only":
            exit_price, exit_reason = target, "TARGET"
            exit_time = candle["time"]
            break
        highest = max(highest, float(candle["high"]))
        lowest = min(lowest, float(candle["low"]))
        if settings.get("trailing_sl_enabled") or settings.get("exit_mode") in {"trailing_sl_only", "fixed_target_trailing_sl"}:
            trigger = float(settings.get("trailing_sl_trigger_pct") or 0)
            distance = float(settings.get("trailing_sl_distance_pct") or 0)
            if trigger > 0 and distance > 0:
                if side == "BUY" and (highest - entry) / entry * 100 >= trigger:
                    sl = max(sl, highest * (1 - distance / 100))
                elif side == "SELL" and (entry - lowest) / entry * 100 >= trigger:
                    sl = min(sl, lowest * (1 + distance / 100))
    if exit_price is None:
        final_candle = candles[-1] if candles else entry_candle
        exit_price, exit_reason = float(final_candle["close"]), "EOD_SQUAREOFF"
        exit_time = final_candle["time"]
    buy_value = entry * qty if side == "BUY" else exit_price * qty
    sell_value = exit_price * qty if side == "BUY" else entry * qty
    charges = calculate_charges(buy_value, sell_value, charges_config)
    return {
        "symbol": row["symbol"], "side": side, "qty": qty,
        "entry_price": round(entry, 2), "entry_time": entry_candle["time"].isoformat(),
        "exit_price": round(exit_price, 2), "exit_time": exit_time.isoformat(), "exit_reason": exit_reason,
        "target_price": round(target, 2), "sl_price": round(sl, 2),
        "entry_trigger": f"Historical {target_date.isoformat()} 09:15 signal candle replay.",
        **charges,
    }


def _run_silver_micro_job(
    job_id: str,
    algo_id: str,
    first_date: datetime.date,
    last_date: datetime.date,
    symbol: str,
    settings: dict,
    history_cache: BacktestHistoryCache,
    silver_buy_plan: str,
    silver_sell_plan: str,
) -> None:
    _raise_if_cancelled(job_id)
    lookback_start = first_date - datetime.timedelta(days=WARMUP_LOOKBACK_DAYS)
    charges_config = get_charges_config()
    _update(
        job_id,
        status="running",
        phase="screening",
        message=f"Loading Silver Micro history for {symbol}.",
        replay_total=0,
        replay_completed=0,
        replay_failed=0,
    )
    history, history_resolution = _load_silver_micro_history(symbol, lookback_start, last_date)
    _raise_if_cancelled(job_id)
    if history_cache.store(symbol, history):
        _increment(job_id, "cached_history_symbols")
    if not history:
        raise ValueError(
            "No Silver Micro history was returned for the chosen range after trying 1-minute and 5-minute candles. "
            "The selected contract may have no data for this range, or FYERS returned an empty history response."
        )

    trading_days = [
        first_date + datetime.timedelta(days=offset)
        for offset in range((last_date - first_date).days + 1)
        if (first_date + datetime.timedelta(days=offset)).weekday() < 5
    ]
    _update(
        job_id,
        completed_symbols=1,
        failed_symbols=0,
        phase="replaying",
        replay_total=len(trading_days),
        replay_completed=0,
        replay_failed=0,
        message=f"Replaying 0 / {len(trading_days)} trading days for Silver Micro from the local candle cache.",
    )

    daily_results = _simulate_silver_micro_range(
        job_id,
        algo_id,
        first_date,
        last_date,
        symbol,
        history,
        trading_days,
        settings,
        charges_config,
        silver_buy_plan,
        silver_sell_plan,
    )
    _raise_if_cancelled(job_id)
    data_coverage = {
        "requested_symbols": 1,
        "symbols_with_history": 1,
        "symbols_without_history": 0,
        "lookback_start": lookback_start.isoformat(),
        "history_resolution": history_resolution,
    }
    result = _range_result(
        algo_id,
        first_date,
        last_date,
        daily_results,
        data_coverage,
        mode="historical_mcx_replay",
        execution_assumption=_silver_micro_execution_assumption(history_resolution, settings, silver_buy_plan, silver_sell_plan),
    )
    result["silver_buy_plan"] = silver_buy_plan
    result["silver_buy_plan_label"] = SILVER_BUY_PLAN_LABELS[silver_buy_plan]
    result["silver_sell_plan"] = silver_sell_plan
    result["silver_sell_plan_label"] = SILVER_SELL_PLAN_LABELS[silver_sell_plan]
    _raise_if_cancelled(job_id)
    _update(job_id, status="complete", phase="complete", message="Silver Micro backtest complete.", result=result)


def _silver_micro_execution_assumption(
    history_resolution: str,
    settings: dict,
    silver_buy_plan: str = SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
    silver_sell_plan: str = SILVER_SELL_PLAN_RED_CHAIN,
) -> str:
    n = int(settings.get("silver_breakout_points", 150))
    sl_pts = int(settings.get("sl_points", 100))
    target_pts = int(settings.get("target_points", 300))
    tsl_enabled = bool(settings.get("trailing_sl_enabled"))
    tsl_activate, tsl_profit_step, tsl_lock_step = silver_tsl_points(settings)
    exit_mode = str(settings.get("exit_mode") or "fixed_target_trailing_sl")
    trailing_clause = (
        f" Trailing SL is ON: activates at {tsl_activate:g} points profit, then every "
        f"{tsl_profit_step:g} additional points locks {tsl_lock_step:g} more points."
        if tsl_enabled and exit_mode in {"trailing_sl_only", "fixed_target_trailing_sl"}
        else " Trailing SL is OFF for this replay."
    )
    buy_plan_text = "BUY stores each finalized green 15m close above EMA20 as the reference and enters when price crosses reference + n; after a BUY target/SL, renewed upward movement can re-enter against the same reference until a newer green 15m close replaces it."
    sell_plan_text = (
        "SELL compares each later qualifying red 15m close with the previous red reference; "
        "green candles do not reset that reference."
        if silver_sell_plan == SILVER_SELL_PLAN_RED_CHAIN
        else "SELL replaces the reference on every qualifying red 15m candle, then waits for a later 1-minute break below that latest reference."
    )
    sell_entry_text = (
        "SELL red-chain entries enter at the trigger level when the next qualifying red candle's intrabar low crosses it."
        if silver_sell_plan == SILVER_SELL_PLAN_RED_CHAIN
        else "SELL latest-reference entries enter at the 1-minute trigger level."
    )
    return (
        f"Silver Micro replays 15-minute bars aggregated from 1-minute history "
        f"({history_resolution}). Each closed 15m bar updates the SELL EMA20; a red candle "
        f"closing below EMA20 stores the SELL red-reference close. {buy_plan_text} "
        f"{sell_plan_text} BUY entries use 1-minute "
        f"bar extremes and enter at the trigger level itself. {sell_entry_text} SL={sl_pts} points, target={target_pts} "
        f"points, both fixed rupee distances from entry. If a 1-minute bar touches "
        f"both SL and target, SL is assumed first (conservative). Reversal on "
        f"contra trigger closes the current position and flips at the same bar."
        f"{trailing_clause}"
    )


def _new_silver_micro_day_result(
    symbol: str,
    day: datetime.date,
    bar_count: int,
    silver_buy_plan: str = SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
    silver_sell_plan: str = SILVER_SELL_PLAN_RED_CHAIN,
) -> dict:
    buy_plan = normalize_silver_buy_plan(silver_buy_plan)
    sell_plan = normalize_silver_sell_plan(silver_sell_plan)
    buy_plan_text = "BUY uses a finalized green 15m close above EMA20 as the reference and enters at reference + n, with same-reference re-entry after BUY target/SL on renewed upward movement."
    sell_plan_text = (
        "SELL compares the forming price of a later qualifying red candle with the previous red reference and enters at the intrabar trigger; green candles do not reset it."
        if sell_plan == SILVER_SELL_PLAN_RED_CHAIN
        else "SELL replaces the reference on each qualifying red candle, then waits for a later 1-minute break below that latest reference."
    )
    return {
        "algo_id": "algo3",
        "date": day.isoformat(),
        "mode": "historical_mcx_replay",
        "silver_buy_plan": buy_plan,
        "silver_buy_plan_label": SILVER_BUY_PLAN_LABELS[buy_plan],
        "silver_sell_plan": sell_plan,
        "silver_sell_plan_label": SILVER_SELL_PLAN_LABELS[sell_plan],
        "execution_assumption": (
            f"Silver Micro: {buy_plan_text} "
            + "Each new finalized green 15m close above EMA20 replaces the BUY reference. "
            + f"A red 15m candle below EMA20 stores a SELL red reference. {sell_plan_text}"
        ),
        "data_available_symbols": 1 if bar_count else 0,
        "summary": {},
        "sector_breakdown": [],
        "condition_breakdown": [
            {"label": "Scanned universe", "passed": 1 if bar_count else 0, "total": 1},
            {"label": "15m bars processed", "passed": 0, "total": bar_count},
            {"label": "Setups captured (green above / red below EMA20)", "passed": 0, "total": 0},
            {"label": "Final: entries executed", "passed": 0, "total": 0},
        ],
        "candidates": [],
        "trades": [],
        "chart": {
            "symbol": symbol,
            "resolution": "15",
            "candles": [],
            "setups": [],
            "trades": [],
            "viewport_hint": {
                "mode": "full_day",
                "start_time": None,
                "end_time": None,
                "trade_id": None,
            },
        },
    }


def _round_or_none(value, digits: int = 2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits)


def _silver_backtest_trade_id(symbol: str, side: str, entry_time: datetime.datetime) -> str:
    return f"{symbol}|{side}|{entry_time.isoformat()}"


def _iso_to_naive_ist(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(IST).replace(tzinfo=None)
    return parsed


def _minutes_between(start_value, end_value) -> int | None:
    start_dt = _iso_to_naive_ist(start_value)
    if not start_dt:
        return None
    if isinstance(end_value, datetime.datetime):
        end_dt = end_value.astimezone(IST).replace(tzinfo=None) if end_value.tzinfo is not None else end_value
    else:
        end_dt = _iso_to_naive_ist(end_value)
    if not end_dt:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() // 60))


def _silver_warning_message(code: str, context: dict) -> str:
    delay = context.get("delay_minutes")
    same_candle_time = context.get("same_candle_sl_priority_time")
    if code == "never_reached_trailing_trigger":
        return "Trailing SL never armed before the trade exited."
    if code == "profit_gave_back_before_exit":
        return (
            f"Trade reached a peak unrealized profit of Rs {context.get('peak_unrealized_profit', 0):,.2f} "
            f"but gave back Rs {context.get('profit_giveback', 0):,.2f} before exit."
        )
    if code == "same_candle_sl_priority":
        return (
            f"The same 1-minute candle touched both target and stop; the simulator conservatively honored SL first"
            + (f" at {same_candle_time}." if same_candle_time else ".")
        )
    if code == "late_breakout_entry":
        return f"Entry happened {delay} minutes after the setup candle, so the breakout was late."
    if code == "whipsaw_after_entry":
        return "Price moved both strongly in favor and against the trade within the first few minutes after entry."
    if code == "high_volatility_near_entry":
        return "The first few 1-minute candles after entry were unusually volatile."
    if code == "setup_stale_before_entry":
        return f"The setup sat for {delay} minutes before entry, so it was no longer a fresh breakout."
    if code == "entry_near_local_extreme":
        return "Entry happened near a short-term local extreme, which raised immediate reversal risk."
    if code == "sharp_reversal_after_breakout":
        return "The breakout initially worked, then sharply reversed before the trade could exit well."
    if code == "charges_deepened_loss":
        return "Brokerage and taxes materially worsened the final net loss."
    return code.replace("_", " ")


def _silver_primary_cause_label(code: str) -> str:
    labels = {
        "stop_loss_hit": "Stop loss hit",
        "trailing_stop_hit": "Trailing stop hit",
        "target_not_reached_eod": "Ended red before close",
        "reversal_contra_signal": "Reversal on contra signal",
        "target_hit": "Target hit",
        "session_close_exit": "Session close exit",
        "simulated_exit": "Simulated exit",
    }
    return labels.get(code, code.replace("_", " ").title())


def _build_silver_trade_diagnostics(position: dict, trade: dict, exit_time: datetime.datetime) -> dict:
    setup_context = dict(position.get("setup_context") or {})
    side = str(position.get("side") or "")
    qty = int(position.get("qty") or 0)
    entry = float(position.get("entry_price") or 0)
    exit_price = float(trade.get("exit_price") or 0)
    initial_sl = float(position.get("initial_sl_price") or position.get("sl_price") or 0)
    final_sl = float(position.get("sl_price") or 0)
    target = float(position.get("target_price") or 0)
    trailing_enabled = bool(position.get("trailing_sl_enabled"))
    trailing_active = bool(position.get("trailing_sl_active"))
    trailing_moves = list(position.get("trailing_moves") or [])
    max_favorable_points = float(position.get("max_favorable_points") or 0)
    max_adverse_points = float(position.get("max_adverse_points") or 0)
    peak_unrealized_profit = round(max_favorable_points * qty, 2)
    worst_unrealized_drawdown = round(max_adverse_points * qty, 2)
    exit_points = round((exit_price - entry) if side == "BUY" else (entry - exit_price), 2)
    profit_giveback_points = round(max(0.0, max_favorable_points - exit_points), 2)
    profit_giveback = round(max(0.0, peak_unrealized_profit - float(trade.get("gross_pnl") or 0)), 2)
    delay_minutes = _minutes_between(setup_context.get("setup_time"), position.get("entry_time"))
    same_candle_sl_priority = bool(position.get("same_candle_sl_priority"))
    same_candle_time = position.get("same_candle_sl_priority_time")
    first_window_high = float(position.get("entry_window_high") or entry)
    first_window_low = float(position.get("entry_window_low") or entry)
    first_window_range = max(0.0, first_window_high - first_window_low)
    first_window_favorable = float(position.get("entry_window_favorable_points") or 0)
    first_window_adverse = float(position.get("entry_window_adverse_points") or 0)
    gross_pnl = float(trade.get("gross_pnl") or 0)
    net_pnl = float(trade.get("net_pnl") or 0)
    total_charges = float(trade.get("total_charges") or 0)
    exit_reason = str(trade.get("exit_reason") or "")
    tsl_activate_pts = float(
        position.get("trailing_activate_points")
        or position.get("trailing_trigger_points")
        or 0
    )
    sl_points = abs(entry - initial_sl)
    n_points = float(setup_context.get("n_points") or 0)
    entry_mode = str(position.get("entry_mode") or "THRESHOLD_TRIGGER")
    entry_mode_labels = {
        "THRESHOLD_TRIGGER": "Initial threshold trigger",
        "SAME_REFERENCE_REENTRY": "Same-reference re-entry after exit",
        "LEGACY_CONFIRMATION": "Legacy confirmation entry",
    }
    entry_mode_label = entry_mode_labels.get(entry_mode, entry_mode.replace("_", " ").title())
    active_reference_close = _round_or_none(position.get("active_reference_close"))
    prior_reference_close = _round_or_none(position.get("prior_reference_close"))
    trigger_level_used = _round_or_none(position.get("trigger_level_used"))
    reentry_exit_reason = position.get("reentry_exit_reason")

    if exit_reason == "SL":
        primary_cause_code = "trailing_stop_hit" if trailing_active and abs(final_sl - initial_sl) > 1e-9 else "stop_loss_hit"
    elif exit_reason == "EOD_SQUAREOFF" and net_pnl < 0:
        primary_cause_code = "target_not_reached_eod"
    elif exit_reason == "REVERSAL_CONTRA_SIGNAL":
        primary_cause_code = "reversal_contra_signal"
    elif exit_reason == "TARGET":
        primary_cause_code = "target_hit"
    elif exit_reason == "EOD_SQUAREOFF":
        primary_cause_code = "session_close_exit"
    else:
        primary_cause_code = "simulated_exit"

    warning_codes: list[str] = []

    if trailing_enabled and tsl_activate_pts > 0 and max_favorable_points + 1e-9 < tsl_activate_pts:
        warning_codes.append("never_reached_trailing_trigger")
    meaningful_profit_threshold = max(100.0, tsl_activate_pts or 0.0, n_points * 0.5 if n_points > 0 else 0.0)
    if max_favorable_points >= meaningful_profit_threshold and profit_giveback_points >= max(100.0, meaningful_profit_threshold * 0.5):
        warning_codes.append("profit_gave_back_before_exit")
    if same_candle_sl_priority and exit_reason == "SL":
        warning_codes.append("same_candle_sl_priority")
    if delay_minutes is not None and delay_minutes > 15:
        warning_codes.append("late_breakout_entry")
    if first_window_favorable >= max(100.0, sl_points * 0.5) and first_window_adverse >= max(100.0, sl_points * 0.5):
        warning_codes.append("whipsaw_after_entry")
    if first_window_range >= max(250.0, sl_points * 1.2):
        warning_codes.append("high_volatility_near_entry")
    if delay_minutes is not None and delay_minutes > 15:
        warning_codes.append("setup_stale_before_entry")
    if side == "BUY":
        near_extreme = (first_window_high - entry) <= max(40.0, n_points * 0.15) and (entry - first_window_low) >= max(120.0, sl_points * 0.75)
    else:
        near_extreme = (entry - first_window_low) <= max(40.0, n_points * 0.15) and (first_window_high - entry) >= max(120.0, sl_points * 0.75)
    if near_extreme:
        warning_codes.append("entry_near_local_extreme")
    if max_favorable_points >= max(150.0, n_points * 0.75 if n_points > 0 else 150.0) and profit_giveback_points >= max(150.0, sl_points):
        warning_codes.append("sharp_reversal_after_breakout")
    if gross_pnl < 0 and total_charges >= max(50.0, abs(gross_pnl) * 0.15):
        warning_codes.append("charges_deepened_loss")

    deduped_codes: list[str] = []
    for code in warning_codes:
        if code not in deduped_codes:
            deduped_codes.append(code)

    warning_context = {
        "delay_minutes": delay_minutes,
        "peak_unrealized_profit": peak_unrealized_profit,
        "profit_giveback": profit_giveback,
        "same_candle_sl_priority_time": same_candle_time,
    }
    warning_messages = [_silver_warning_message(code, warning_context) for code in deduped_codes]

    trailing_phrase = (
        f"Trailing activated {len(trailing_moves)} time(s)"
        if trailing_active
        else "Trailing never armed"
    ) if trailing_enabled else "Trailing was off"
    delay_phrase = (
        f"entry triggered {delay_minutes} minutes after setup"
        if delay_minutes is not None
        else "entry timing could not be measured"
    )
    entry_phrase = entry_mode_label.lower()
    if active_reference_close is not None and trigger_level_used is not None:
        entry_phrase = f"{entry_phrase} using reference {active_reference_close:,.2f} and trigger {trigger_level_used:,.2f}"
    summary = (
        f"{side} setup was valid, {entry_phrase}, {delay_phrase}, {trailing_phrase}, and the trade exited via "
        f"{_silver_primary_cause_label(primary_cause_code).lower()} at {round(exit_price, 2):,.2f}. "
        f"Max favorable excursion was {round(max_favorable_points, 2):,.2f} points and max adverse excursion was "
        f"{round(max_adverse_points, 2):,.2f} points."
    )

    return {
        "primary_cause_code": primary_cause_code,
        "primary_cause_label": _silver_primary_cause_label(primary_cause_code),
        "summary": summary,
        "entry_context": {
            "setup_side": setup_context.get("side") or side,
            "setup_time": setup_context.get("setup_time"),
            "setup_close": _round_or_none(setup_context.get("setup_close")),
            "trigger_level": _round_or_none(setup_context.get("trigger_level")),
            "ema20": _round_or_none(setup_context.get("ema20")),
            "entry_time": position["entry_time"].isoformat(),
            "entry_price": round(entry, 2),
            "entry_mode": entry_mode,
            "entry_mode_label": entry_mode_label,
            "delay_from_setup_minutes": delay_minutes,
            "active_reference_close": active_reference_close,
            "prior_reference_close": prior_reference_close,
            "trigger_level_used": trigger_level_used,
            "reentry_exit_reason": reentry_exit_reason,
            "previous_red_reference_close": _round_or_none(setup_context.get("previous_red_reference_close")),
            "current_qualifying_red_close": _round_or_none(setup_context.get("current_qualifying_red_close")),
        },
        "exit_context": {
            "exit_reason": exit_reason,
            "exit_time": exit_time.isoformat(),
            "exit_price": round(exit_price, 2),
            "initial_sl": round(initial_sl, 2),
            "final_sl": round(final_sl, 2),
            "target": round(target, 2),
            "trailing_enabled": trailing_enabled,
            "trailing_active": trailing_active,
            "trailing_move_count": len(trailing_moves),
        },
        "path_metrics": {
            "max_favorable_excursion_points": round(max_favorable_points, 2),
            "max_adverse_excursion_points": round(max_adverse_points, 2),
            "peak_unrealized_profit": peak_unrealized_profit,
            "worst_unrealized_drawdown": worst_unrealized_drawdown,
            "profit_giveback_from_peak": profit_giveback,
            "profit_giveback_from_peak_points": profit_giveback_points,
        },
        "warning_codes": deduped_codes,
        "warning_messages": warning_messages,
    }


def _load_silver_micro_history(
    symbol: str,
    start_date: datetime.date,
    end_date: datetime.date,
) -> tuple[list[dict], str]:
    """Fetch MCX history day-by-day so a single bad range does not zero out replay data."""
    history: list[dict] = []
    one_minute_days = 0
    five_minute_days = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            try:
                day_history = get_intraday_candles_for_range(
                    symbol,
                    current,
                    current,
                    resolution="1",
                    raise_on_error=True,
                )
            except Exception as exc:
                raise ValueError(
                    f"Silver Micro history request failed for {current.isoformat()} at 1-minute: {exc}. "
                    "The FYERS session may be expired, rate-limited, or not logged in; re-authenticate FYERS and retry."
                ) from exc
            resolution = "1"
            if not day_history:
                try:
                    day_history = get_intraday_candles_for_range(
                        symbol,
                        current,
                        current,
                        resolution="5",
                        raise_on_error=True,
                    )
                except Exception as exc:
                    raise ValueError(
                        f"Silver Micro history request failed for {current.isoformat()} at 5-minute fallback: {exc}. "
                        "The FYERS session may be expired, rate-limited, or not logged in; re-authenticate FYERS and retry."
                    ) from exc
                resolution = "5"
            if day_history:
                if resolution == "1":
                    one_minute_days += 1
                else:
                    five_minute_days += 1
                history.extend(day_history)
            else:
                print(f"[algo3] no intraday history returned for {symbol} on {current.isoformat()}")
        current += datetime.timedelta(days=1)
    history.sort(key=lambda item: item["time"])
    resolution_label = f"{one_minute_days}d@1m/{five_minute_days}d@5m"
    return history, resolution_label


def _simulate_silver_micro_range(
    job_id: str,
    algo_id: str,
    first_date: datetime.date,
    last_date: datetime.date,
    symbol: str,
    history: list[dict],
    trading_days: list[datetime.date],
    settings: dict,
    charges_config: dict,
    silver_buy_plan: str = SILVER_BUY_PLAN_REFERENCE_BREAKOUT,
    silver_sell_plan: str = SILVER_SELL_PLAN_RED_CHAIN,
) -> list[dict]:
    """Replay the 15m reference BUY and selected 15m SELL plan.

    The raw 1-minute history is the execution source of truth. Each finalized
    green 15m close above EMA20 becomes the BUY reference; a later 1m move
    through reference+n enters immediately. A BUY target/SL preserves that
    reference and permits re-entry on renewed upward movement until a newer
    qualifying 15m close replaces it.
    """
    silver_buy_plan = normalize_silver_buy_plan(silver_buy_plan)
    silver_sell_plan = normalize_silver_sell_plan(silver_sell_plan)
    n = float(settings.get("silver_breakout_points", 150))
    sl_pts = float(settings.get("sl_points", 100))
    target_pts = float(settings.get("target_points", 300))
    tsl_activate_pts, tsl_profit_step_pts, tsl_lock_step_pts = silver_tsl_points(settings)
    exit_mode = str(settings.get("exit_mode") or "fixed_target_trailing_sl")
    tsl_enabled = bool(settings.get("trailing_sl_enabled")) and exit_mode in {"trailing_sl_only", "fixed_target_trailing_sl"}

    # Pre-count 1m bars per day so the UI can show how much data existed.
    minute_bars_by_day: dict[datetime.date, int] = defaultdict(int)
    sorted_history = sorted(history, key=lambda item: item["time"])
    normalized_history: list[dict] = []
    for candle in sorted_history:
        ts = candle["time"]
        if ts.tzinfo is not None:
            ts = ts.astimezone(IST).replace(tzinfo=None)
        entry = {
            "time": ts,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume") or 0),
        }
        normalized_history.append(entry)
        if first_date <= ts.date() <= last_date:
            minute_bars_by_day[ts.date()] += 1

    daily_results = {
        day: _new_silver_micro_day_result(symbol, day, minute_bars_by_day.get(day, 0), silver_buy_plan, silver_sell_plan)
        for day in trading_days
    }

    # Live-state variables — mirror algo3_silver_micro.py exactly.
    ema20: float | None = None
    buy_setup_close: float | None = None
    sell_setup_close: float | None = None
    buy_setup_context: dict | None = None
    sell_setup_context: dict | None = None
    buy_setup_bar_at: datetime.datetime | None = None
    sell_setup_bar_at: datetime.datetime | None = None
    last_fired_buy_setup_at: datetime.datetime | None = None
    last_fired_sell_setup_at: datetime.datetime | None = None
    minute_buffer: list[dict] = []
    current_bucket: datetime.datetime | None = None
    legacy_buy_5m_buffer: list[dict] = []
    legacy_buy_5m_bucket: datetime.datetime | None = None
    legacy_buy_price_ema20: float | None = None
    legacy_buy_volume_ema20: float | None = None
    legacy_buy_bars_finalized = 0
    legacy_buy_pending_setup: dict | None = None
    legacy_buy_pending_entry: dict | None = None
    prev_ltp: float | None = None
    bars_finalized = 0
    current_day: datetime.date | None = None
    last_bar_processed: dict | None = None
    position: dict | None = None
    position_candidate: dict | None = None
    # Set only after a real protective exit. The next qualifying minute may
    # continue the same reference move without inventing a fresh signal.
    sell_reentry_after_exit: dict | None = None
    buy_reentry_after_exit: dict | None = None

    def finalize_15m_bar(allow_signals: bool):
        """Aggregate the minute_buffer into one 15m bar, update EMA20
        and the BUY/SELL setup levels."""
        nonlocal minute_buffer, ema20, buy_setup_close, sell_setup_close
        nonlocal buy_setup_context, sell_setup_context, buy_setup_bar_at, sell_setup_bar_at
        nonlocal last_fired_buy_setup_at, last_fired_sell_setup_at, bars_finalized
        nonlocal sell_reentry_after_exit, buy_reentry_after_exit
        if not minute_buffer or current_bucket is None:
            return
        bar = {
            "time": current_bucket,
            "open": minute_buffer[0]["open"],
            "high": max(c["high"] for c in minute_buffer),
            "low": min(c["low"] for c in minute_buffer),
            "close": minute_buffer[-1]["close"],
            "volume": sum(c["volume"] for c in minute_buffer),
        }
        ema20 = _ema_step(ema20, bar["close"])
        bars_finalized += 1

        is_green = bar["close"] > bar["open"]
        is_red = bar["close"] < bar["open"]
        # Setup update: overwrite on every new qualifier (per spec doc).
        # A qualifier can also be recorded in-scope so the UI candidates
        # table shows what triggered.
        setup_event: dict | None = None
        previous_buy_reference_close = buy_setup_close
        previous_sell_reference_close = sell_setup_close
        if is_green and ema20 is not None and bar["close"] > ema20:
            buy_setup_context = {
                "side": "BUY",
                "setup_time": bar["time"].isoformat(),
                "setup_close": round(bar["close"], 2),
                "trigger_level": round(bar["close"] + n, 2),
                "ema20": _round_or_none(ema20),
                "n_points": n,
                "previous_reference_close": _round_or_none(previous_buy_reference_close),
            }
            setup_event = {
                "side": "BUY",
                "close": bar["close"],
                "bar": bar,
                "previous_reference_close": previous_buy_reference_close,
            }
        if is_red and ema20 is not None and bar["close"] < ema20:
            sell_setup_context = {
                "side": "SELL",
                "setup_time": bar["time"].isoformat(),
                "setup_close": round(bar["close"], 2),
                "trigger_level": round(bar["close"] - n, 2),
                "ema20": _round_or_none(ema20),
                "n_points": n,
                "previous_red_reference_close": _round_or_none(previous_sell_reference_close),
                "current_qualifying_red_close": round(bar["close"], 2),
            }
            setup_event = {
                "side": "SELL",
                "close": bar["close"],
                "bar": bar,
                "previous_red_reference_close": previous_sell_reference_close,
                "current_qualifying_red_close": bar["close"],
            }
            # A finalized red bar becomes the new reference. Any handoff
            # tied to the older reference must not leak into this candle.
            sell_reentry_after_exit = None
        if allow_signals and first_date <= bar["time"].date() <= last_date:
            chart_day = daily_results.get(bar["time"].date())
            if chart_day:
                chart_day["chart"]["candles"].append({
                    "time": bar["time"].isoformat(),
                    "open": round(bar["open"], 2),
                    "high": round(bar["high"], 2),
                    "low": round(bar["low"], 2),
                    "close": round(bar["close"], 2),
                    "volume": round(float(bar["volume"]), 2),
                    "ema20": _round_or_none(ema20),
                })
        if setup_event and allow_signals and first_date <= bar["time"].date() <= last_date:
            day_result = daily_results.get(bar["time"].date())
            if day_result:
                candidate = {
                    "symbol": symbol,
                    "sector": "MCX",
                    "side": setup_event["side"],
                    "open": round(bar["open"], 2),
                    "high": round(bar["high"], 2),
                    "low": round(bar["low"], 2),
                    "close": round(bar["close"], 2),
                    "setup_time": bar["time"].isoformat(),
                    "setup_close": round(setup_event["close"], 2),
                    "trigger_level": round(setup_event["close"] + n, 2) if setup_event["side"] == "BUY" else round(setup_event["close"] - n, 2),
                    "ema20": round(ema20, 2) if ema20 is not None else None,
                    "n_points": n,
                     "previous_red_reference_close": _round_or_none(setup_event.get("previous_red_reference_close")),
                     "current_qualifying_red_close": _round_or_none(setup_event.get("current_qualifying_red_close")),
                     "previous_reference_close": _round_or_none(setup_event.get("previous_reference_close")),
                    "signal_stage": "setup",
                    "selected_for_trade": False,
                    "rejection_reason": None,
                    "entry_time": None,
                    "entry_price": None,
                    "exit_time": None,
                    "exit_price": None,
                    "exit_reason": None,
                    "net_pnl": None,
                    "gross_pnl": None,
                    "total_charges": None,
                }
                day_result["candidates"].append(candidate)
                day_result["chart"]["setups"].append({
                    "side": setup_event["side"],
                    "time": bar["time"].isoformat(),
                    "setup_close": round(setup_event["close"], 2),
                    "trigger_level": round(setup_event["close"] + n, 2) if setup_event["side"] == "BUY" else round(setup_event["close"] - n, 2),
                    "ema20": _round_or_none(ema20),
                    "previous_red_reference_close": _round_or_none(setup_event.get("previous_red_reference_close")),
                    "current_qualifying_red_close": _round_or_none(setup_event.get("current_qualifying_red_close")),
                    "previous_reference_close": _round_or_none(setup_event.get("previous_reference_close")),
                })
                day_result["condition_breakdown"][2]["passed"] += 1
        # A completed bar can be the first observable evidence of a sparse or
        # gap-through BUY. Compare it with the prior reference before replacing
        # that reference with the current green close.
        if (
            silver_buy_plan == SILVER_BUY_PLAN_REFERENCE_BREAKOUT
            and allow_signals
            and first_date <= bar["time"].date() <= last_date
            and bars_finalized > EMA_PERIOD
            and previous_buy_reference_close is not None
            and is_green
            and ema20 is not None
            and bar["close"] >= float(previous_buy_reference_close) + n
            and (position is None or position.get("side") != "BUY")
        ):
            buy_level = float(previous_buy_reference_close) + n
            if position and position.get("side") != "BUY":
                close_position(buy_level, bar["time"], "REVERSAL_CONTRA_SIGNAL", bar["time"].date())
            if position is None:
                open_position(
                    "BUY",
                    float(bar["close"]),
                    bar["time"],
                    bar["time"].date(),
                    entry_metadata={
                        "entry_mode": "THRESHOLD_TRIGGER_CANDLE_CLOSE",
                        "active_reference_close": previous_buy_reference_close,
                        "trigger_level_used": buy_level,
                    },
                )
                last_fired_buy_setup_at = buy_setup_bar_at
        if (
            silver_sell_plan == SILVER_SELL_PLAN_RED_CHAIN
            and allow_signals
            and first_date <= bar["time"].date() <= last_date
            and bars_finalized >= EMA_PERIOD
            and setup_event
            and setup_event["side"] == "SELL"
            and sell_setup_close is not None
        ):
            sell_level = sell_setup_close - n
            if (
                float(bar["low"]) <= float(sell_level)
                and sell_setup_bar_at is not None
                and last_fired_sell_setup_at != sell_setup_bar_at
            ):
                current_position = position
                if current_position and current_position["side"] == "SELL":
                    pass
                else:
                    if current_position and current_position["side"] != "SELL":
                        close_position(float(sell_level), bar["time"], "REVERSAL_CONTRA_SIGNAL", bar["time"].date())
                    open_position(
                        "SELL",
                        float(sell_level),
                        bar["time"],
                        bar["time"].date(),
                        entry_metadata={
                            "entry_mode": "THRESHOLD_TRIGGER",
                            "active_reference_close": sell_setup_close,
                            "prior_reference_close": (sell_setup_context or {}).get("previous_red_reference_close"),
                            "trigger_level_used": sell_level,
                        },
                    )
                    last_fired_sell_setup_at = sell_setup_bar_at
        if is_green and ema20 is not None and bar["close"] > ema20:
            buy_setup_close = bar["close"]
            buy_setup_bar_at = bar["time"]
            buy_reentry_after_exit = None
        if is_red and ema20 is not None and bar["close"] < ema20:
            sell_setup_close = bar["close"]
            sell_setup_bar_at = bar["time"]

    def finalize_legacy_buy_5m_bar(allow_signals: bool):
        """Replay the pre-15m BUY model without changing the live-parity path.

        The historical model used a 5m price EMA20, a 5m volume EMA20, a
        green/above-EMA setup, a same-direction confirmation within 15
        minutes, and entry at the next 5m candle open. Its candidate is still
        sent through the shared Silver position/exit engine so the selected
        BUY plan can be compared fairly with either SELL plan.
        """
        nonlocal legacy_buy_5m_buffer, legacy_buy_price_ema20, legacy_buy_volume_ema20
        nonlocal legacy_buy_5m_bucket, legacy_buy_bars_finalized
        nonlocal legacy_buy_pending_setup, legacy_buy_pending_entry, buy_setup_context
        if not legacy_buy_5m_buffer or legacy_buy_5m_bucket is None:
            return
        bar = {
            "time": legacy_buy_5m_bucket,
            "open": legacy_buy_5m_buffer[0]["open"],
            "high": max(c["high"] for c in legacy_buy_5m_buffer),
            "low": min(c["low"] for c in legacy_buy_5m_buffer),
            "close": legacy_buy_5m_buffer[-1]["close"],
            "volume": sum(c["volume"] for c in legacy_buy_5m_buffer),
        }
        legacy_buy_5m_buffer = []
        legacy_buy_price_ema20 = _ema_step(legacy_buy_price_ema20, bar["close"])
        legacy_buy_volume_ema20 = _ema_step(legacy_buy_volume_ema20, bar["volume"])
        legacy_buy_bars_finalized += 1
        day = bar["time"].date()
        in_scope = first_date <= day <= last_date
        day_result = daily_results.get(day)
        if not allow_signals or not in_scope or not day_result or legacy_buy_bars_finalized < EMA_PERIOD:
            return

        # A confirmation is evaluated on the closed 5m bar. The actual entry
        # is deliberately deferred to the first minute of the next 5m bar.
        if legacy_buy_pending_setup:
            setup = legacy_buy_pending_setup
            if bar["time"] > setup["setup_bucket"] + datetime.timedelta(minutes=SILVER_LEGACY_BUY_CONFIRMATION_MINUTES):
                setup["candidate"]["signal_stage"] = "rejected"
                setup["candidate"]["rejection_reason"] = "confirmation_timeout"
                legacy_buy_pending_setup = None
            elif (
                bar["time"] > setup["setup_bucket"]
                and bar["close"] > bar["open"]
                and bar["close"] > setup["setup_close"]
            ):
                candidate = setup["candidate"]
                candidate["signal_stage"] = "confirmed"
                candidate["confirmation_time"] = bar["time"].isoformat()
                candidate["confirmation_close"] = round(float(bar["close"]), 2)
                legacy_buy_pending_entry = {
                    "side": "BUY",
                    "entry_bucket": bar["time"] + datetime.timedelta(minutes=SILVER_LEGACY_BUY_BUCKET_MINUTES),
                    "candidate": candidate,
                }
                day_result["condition_breakdown"][2]["passed"] += 1
                legacy_buy_pending_setup = None

        is_green = bar["close"] > bar["open"]
        qualifies = (
            is_green
            and legacy_buy_price_ema20 is not None
            and legacy_buy_volume_ema20 is not None
            and bar["close"] > legacy_buy_price_ema20
            and bar["volume"] > legacy_buy_volume_ema20
        )
        if not qualifies:
            return

        if legacy_buy_pending_setup:
            legacy_buy_pending_setup["candidate"]["signal_stage"] = "rejected"
            legacy_buy_pending_setup["candidate"]["rejection_reason"] = "superseded_by_new_setup"

        candidate = {
            "symbol": symbol,
            "sector": "MCX",
            "side": "BUY",
            "open": round(float(bar["open"]), 2),
            "high": round(float(bar["high"]), 2),
            "low": round(float(bar["low"]), 2),
            "close": round(float(bar["close"]), 2),
            "volume": round(float(bar["volume"]), 2),
            "setup_time": bar["time"].isoformat(),
            "setup_close": round(float(bar["close"]), 2),
            "trigger_level": None,
            "ema20": round(float(legacy_buy_price_ema20), 2),
            "ema20_price": round(float(legacy_buy_price_ema20), 2),
            "ema20_volume": round(float(legacy_buy_volume_ema20), 2),
            "n_points": None,
            "signal_stage": "setup",
            "selected_for_trade": False,
            "rejection_reason": None,
            "entry_time": None,
            "entry_price": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "net_pnl": None,
            "gross_pnl": None,
            "total_charges": None,
        }
        day_result["candidates"].append(candidate)
        day_result["condition_breakdown"][1]["passed"] += 1
        day_result["condition_breakdown"][2]["total"] += 1
        day_result["chart"]["setups"].append({
            "side": "BUY",
            "time": bar["time"].isoformat(),
            "timeframe": "5",
            "setup_close": round(float(bar["close"]), 2),
            "trigger_level": None,
            "ema20": round(float(legacy_buy_price_ema20), 2),
            "ema20_volume": round(float(legacy_buy_volume_ema20), 2),
        })
        buy_setup_context = {
            "side": "BUY",
            "setup_time": bar["time"].isoformat(),
            "setup_close": round(float(bar["close"]), 2),
            "trigger_level": None,
            "ema20": round(float(legacy_buy_price_ema20), 2),
            "n_points": None,
            "previous_red_reference_close": None,
            "current_qualifying_red_close": None,
        }
        legacy_buy_pending_setup = {
            "side": "BUY",
            "setup_close": float(bar["close"]),
            "setup_bucket": bar["time"],
            "candidate": candidate,
        }

    def close_position(exit_price: float, exit_time: datetime.datetime, exit_reason: str, day: datetime.date):
        nonlocal position, position_candidate, sell_reentry_after_exit, buy_reentry_after_exit
        if not position:
            return
        trade = _close_silver_micro_position(position, exit_price, exit_time, exit_reason, settings, charges_config)
        if position.get("side") == "SELL" and exit_reason in {"SL", "TARGET"} and sell_setup_close is not None and sell_setup_bar_at is not None:
            sell_reentry_after_exit = {
                "setup_bar_at": sell_setup_bar_at,
                "trigger_level": float(sell_setup_close) - n,
                "exit_reason": exit_reason,
            }
        elif position.get("side") == "SELL":
            sell_reentry_after_exit = None
        if position.get("side") == "BUY" and exit_reason in {"SL", "TARGET"} and buy_setup_close is not None and buy_setup_bar_at is not None:
            buy_reentry_after_exit = {
                "setup_bar_at": buy_setup_bar_at,
                "trigger_level": float(buy_setup_close) + n,
                "exit_reason": exit_reason,
            }
        elif position.get("side") == "BUY":
            buy_reentry_after_exit = None
        daily_results[day]["trades"].append(trade)
        daily_results[day]["chart"]["trades"].append({
            "trade_id": trade["trade_id"],
            "side": trade["side"],
            "entry_time": trade["entry_time"],
            "entry_price": trade["entry_price"],
            "entry_mode": trade.get("entry_mode"),
            "entry_mode_label": (trade.get("diagnostics") or {}).get("entry_context", {}).get("entry_mode_label"),
            "active_reference_close": (trade.get("diagnostics") or {}).get("entry_context", {}).get("active_reference_close"),
            "prior_reference_close": (trade.get("diagnostics") or {}).get("entry_context", {}).get("prior_reference_close"),
            "trigger_level_used": (trade.get("diagnostics") or {}).get("entry_context", {}).get("trigger_level_used"),
            "reentry_exit_reason": (trade.get("diagnostics") or {}).get("entry_context", {}).get("reentry_exit_reason"),
            "exit_time": trade["exit_time"],
            "exit_price": trade["exit_price"],
            "initial_sl_price": trade["initial_sl_price"],
            "final_sl_price": trade["sl_price"],
            "target_price": trade["target_price"],
            "trailing_sl_enabled": trade["trailing_sl_enabled"],
            "trailing_sl_active": trade["trailing_sl_active"],
            "trailing_moves": list(trade.get("trailing_moves") or []),
            "exit_reason": trade["exit_reason"],
            "net_pnl": trade["net_pnl"],
        })
        if position_candidate:
            position_candidate["exit_time"] = trade["exit_time"]
            position_candidate["exit_price"] = trade["exit_price"]
            position_candidate["exit_reason"] = trade["exit_reason"]
            position_candidate["net_pnl"] = trade["net_pnl"]
            position_candidate["gross_pnl"] = trade["gross_pnl"]
            position_candidate["total_charges"] = trade["total_charges"]
            position_candidate["selected_for_trade"] = True
            position_candidate["signal_stage"] = "exited"
        position = None
        position_candidate = None

    def open_position(
        side: str,
        entry_price: float,
        entry_time: datetime.datetime,
        day: datetime.date,
        provided_candidate: dict | None = None,
        entry_metadata: dict | None = None,
    ):
        nonlocal position, position_candidate
        # Silver Micro is sized in LOTS (1 lot = 1 kg = 1 unit). Mirrors
        # the live algo3 sizing so backtest ↔ live parity is preserved.
        lots = int(settings.get("silver_lots", 1) or 1)
        qty = max(1, lots)
        if qty < 1:
            return
        if side == "BUY":
            sl_price = float(entry_price) - sl_pts
            target_price = float(entry_price) + target_pts
        else:
            sl_price = float(entry_price) + sl_pts
            target_price = float(entry_price) - target_pts
        entry_metadata = dict(entry_metadata or {})
        position = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "trade_id": _silver_backtest_trade_id(symbol, side, entry_time),
            "entry_price": float(entry_price),
            "entry_time": entry_time,
            "initial_sl_price": float(sl_price),
            "sl_price": sl_price,
            "target_price": target_price,
            "highest": float(entry_price),
            "lowest": float(entry_price),
            "trailing_sl_enabled": bool(tsl_enabled),
            "trailing_sl_active": False,
            "trailing_activate_points": float(tsl_activate_pts),
            "trailing_profit_step_points": float(tsl_profit_step_pts),
            "trailing_lock_step_points": float(tsl_lock_step_pts),
            # Compatibility aliases retained in the result/diagnostics shape.
            "trailing_trigger_points": float(tsl_activate_pts),
            "trailing_distance_points": float(tsl_lock_step_pts),
            "trailing_moves": [],
            "entry_mode": entry_metadata.get("entry_mode") or (
                "THRESHOLD_TRIGGER"
            ),
            "active_reference_close": entry_metadata.get("active_reference_close"),
            "prior_reference_close": entry_metadata.get("prior_reference_close"),
            "trigger_level_used": entry_metadata.get("trigger_level_used"),
            "reentry_exit_reason": entry_metadata.get("reentry_exit_reason"),
            "entry_trigger": (
                f"Historical {entry_time.date().isoformat()} Silver Micro 15m reference BUY breakout replay."
                if side == "BUY"
                else (
                    f"Historical {entry_time.date().isoformat()} Silver Micro "
                    f"15m red-chain trigger replay."
                    if side == "SELL" and silver_sell_plan == SILVER_SELL_PLAN_RED_CHAIN
                    else f"Historical {entry_time.date().isoformat()} Silver Micro trigger replay."
                )
            ),
        }
        # Latest matching-side setup in this day's candidates is the source
        # candidate we mark as selected_for_trade.
        source_candidate = provided_candidate
        day_result = daily_results.get(day)
        if source_candidate is None and day_result:
            for cand in reversed(day_result["candidates"]):
                if cand["side"] == side and not cand.get("selected_for_trade"):
                    source_candidate = cand
                    break
        if source_candidate:
            source_candidate["selected_for_trade"] = True
            source_candidate["signal_stage"] = "entered"
            source_candidate["entry_time"] = entry_time.isoformat()
            source_candidate["entry_price"] = round(float(entry_price), 2)
            source_candidate["sl_price"] = round(float(sl_price), 2)
            source_candidate["target_price"] = round(float(target_price), 2)
        active_setup_context = buy_setup_context if side == "BUY" else sell_setup_context
        setup_source = source_candidate or active_setup_context or {}
        setup_context = {
            "side": setup_source.get("side") or side,
            "setup_time": setup_source.get("setup_time"),
            "setup_close": setup_source.get("setup_close"),
            "trigger_level": setup_source.get("trigger_level"),
            "ema20": setup_source.get("ema20"),
            "n_points": setup_source.get("n_points"),
            "previous_red_reference_close": setup_source.get("previous_red_reference_close"),
            "current_qualifying_red_close": setup_source.get("current_qualifying_red_close"),
            "previous_reference_close": setup_source.get("previous_reference_close"),
        }
        position["setup_context"] = setup_context
        position["max_favorable_points"] = 0.0
        position["max_adverse_points"] = 0.0
        position["peak_price"] = float(entry_price)
        position["peak_time"] = entry_time.isoformat()
        position["trough_price"] = float(entry_price)
        position["trough_time"] = entry_time.isoformat()
        position["same_candle_sl_priority"] = False
        position["same_candle_sl_priority_time"] = None
        position["entry_window_end"] = entry_time + datetime.timedelta(minutes=5)
        position["entry_window_high"] = float(entry_price)
        position["entry_window_low"] = float(entry_price)
        position["entry_window_favorable_points"] = 0.0
        position["entry_window_adverse_points"] = 0.0
        position_candidate = source_candidate

    def maybe_apply_trailing(entry: float, side: str):
        """Apply the shared X/Y/Z profit-lock staircase."""
        nonlocal position
        if not position or not tsl_enabled:
            return
        result = calculate_point_trailing(
            entry=entry,
            side=side,
            current_sl=float(position["sl_price"]),
            highest=float(position["highest"]),
            lowest=float(position["lowest"]),
            activate_points=tsl_activate_pts,
            profit_step_points=tsl_profit_step_pts,
            lock_step_points=tsl_lock_step_pts,
        )
        position["highest"] = result["highest"]
        position["lowest"] = result["lowest"]
        if result["trailing_active"]:
            position["trailing_sl_active"] = True
        if result["sl_moved"]:
            position["sl_price"] = result["sl_price"]
            position.setdefault("trailing_moves", []).append({
                "time": position.get("_last_trail_time"),
                "side": side,
                "gain_points": round(result["gain_points"], 2),
                "reference_price": round(float(position["highest"] if side == "BUY" else position["lowest"]), 2),
                "previous_sl": round(result["previous_sl"], 2),
                "new_sl": round(result["sl_price"], 2),
                "protected_points": round(result["protected_points"], 2),
                "step_index": result["step_index"],
            })

    def check_buy_reference_intrabar(candle: dict, day: datetime.date, in_scope: bool):
        """Enter BUY at the live 1m crossing of the carried 15m reference."""
        nonlocal last_fired_buy_setup_at, buy_reentry_after_exit
        if not in_scope or silver_buy_plan != SILVER_BUY_PLAN_REFERENCE_BREAKOUT:
            return
        if bars_finalized < EMA_PERIOD or buy_setup_close is None or buy_setup_bar_at is None:
            return
        if current_bucket is None or current_bucket <= buy_setup_bar_at or not minute_buffer:
            return
        if ema20 is None or (position is not None and position.get("side") == "BUY"):
            return

        buy_level = float(buy_setup_close) + n
        current_bucket_open = float(minute_buffer[0]["open"])
        current_price = float(candle["close"])
        current_candle_is_green = current_price > current_bucket_open and current_price > float(ema20)
        crossed = (
            float(candle["high"]) >= buy_level
            and (
                (prev_ltp is not None and float(prev_ltp) < buy_level)
                or float(candle["open"]) >= buy_level
                or prev_ltp is None
            )
        )
        same_reference_reentry = bool(
            buy_reentry_after_exit
            and buy_reentry_after_exit.get("setup_bar_at") == buy_setup_bar_at
            and abs(float(buy_reentry_after_exit.get("trigger_level") or 0) - buy_level) < 1e-9
            and prev_ltp is not None
            and current_price > float(prev_ltp)
            and current_price >= buy_level
        )
        # The initial entry is driven by the executable price crossing, just
        # like live ticks. A same-reference re-entry is more conservative:
        # it must resume upward in a green, above-EMA minute.
        if not (crossed or same_reference_reentry):
            return
        if same_reference_reentry and not current_candle_is_green:
            return

        if position is not None and position.get("side") != "BUY":
            close_position(buy_level, candle["time"], "REVERSAL_CONTRA_SIGNAL", day)
        if position is None:
            entry_price = current_price if same_reference_reentry else buy_level
            open_position(
                "BUY",
                entry_price,
                candle["time"],
                day,
                entry_metadata={
                    "entry_mode": "SAME_REFERENCE_REENTRY" if same_reference_reentry else "THRESHOLD_TRIGGER",
                    "active_reference_close": buy_setup_close,
                    "trigger_level_used": buy_level,
                    "reentry_exit_reason": (buy_reentry_after_exit or {}).get("exit_reason") if same_reference_reentry else None,
                },
            )
            last_fired_buy_setup_at = buy_setup_bar_at
            buy_reentry_after_exit = None

    def check_red_chain_intrabar(candle: dict, day: datetime.date, in_scope: bool):
        """Enter current red-chain SELLs from the 1-minute crossing.

        The stored reference belongs to the previous finalized red 15m bar.
        The current 15m bar must already be red and below the carried EMA20;
        its first 1m low crossing is the simulator's best available entry
        point and is deliberately not replaced by the eventual 15m close.
        """
        nonlocal last_fired_sell_setup_at, sell_reentry_after_exit
        if not in_scope or silver_sell_plan != SILVER_SELL_PLAN_RED_CHAIN:
            return
        if bars_finalized < EMA_PERIOD or sell_setup_close is None:
            return
        if sell_setup_bar_at is None or current_bucket is None or current_bucket <= sell_setup_bar_at:
            return
        if ema20 is None or not minute_buffer:
            return
        if position is not None and position.get("side") == "SELL":
            return

        sell_level = float(sell_setup_close) - n
        current_bucket_open = float(minute_buffer[0]["open"])
        current_price = float(candle["close"])
        current_candle_is_red = current_price < current_bucket_open and current_price < float(ema20)
        crossed = (
            float(candle["low"]) <= sell_level
            and (
                (prev_ltp is not None and float(prev_ltp) > sell_level)
                or float(candle["open"]) <= sell_level
                or prev_ltp is None
            )
        )
        same_reference_reentry = bool(
            sell_reentry_after_exit
            and sell_reentry_after_exit.get("setup_bar_at") == sell_setup_bar_at
            and abs(float(sell_reentry_after_exit.get("trigger_level") or 0) - sell_level) < 1e-9
            and prev_ltp is not None
            and current_price < float(prev_ltp)
        )
        if not current_candle_is_red or not (crossed or same_reference_reentry):
            return

        if position is not None and position.get("side") != "SELL":
            close_position(sell_level, candle["time"], "REVERSAL_CONTRA_SIGNAL", day)
        if position is None:
            entry_price = current_price if same_reference_reentry else sell_level
            reentry_metadata = {
                "entry_mode": "SAME_REFERENCE_REENTRY" if same_reference_reentry else "THRESHOLD_TRIGGER",
                "active_reference_close": sell_setup_close,
                "prior_reference_close": (sell_setup_context or {}).get("previous_red_reference_close"),
                "trigger_level_used": sell_level,
                "reentry_exit_reason": (sell_reentry_after_exit or {}).get("exit_reason") if same_reference_reentry else None,
            }
            open_position("SELL", entry_price, candle["time"], day, entry_metadata=reentry_metadata)
            last_fired_sell_setup_at = sell_setup_bar_at
            sell_reentry_after_exit = None

    for candle in normalized_history:
        _raise_if_cancelled(job_id)
        ts = candle["time"]
        day = ts.date()
        if day > last_date:
            break

        in_scope = first_date <= day <= last_date

        # Day boundary — square off any open position at the previous day's
        # last bar close (kept the same convention as before the rewrite).
        if in_scope:
            if current_day is None:
                current_day = day
            if day != current_day:
                if position and last_bar_processed:
                    close_position(float(last_bar_processed["close"]), last_bar_processed["time"], "EOD_SQUAREOFF", current_day)
                current_day = day

        day_result = daily_results.get(day) if in_scope else None
        if day_result:
            day_result["condition_breakdown"][0]["passed"] = 1

        # 15-min bucket rollover: finalize before ingesting this new candle.
        bucket = ts.replace(minute=(ts.minute // SILVER_MICRO_BUCKET_MINUTES) * SILVER_MICRO_BUCKET_MINUTES, second=0, microsecond=0)
        if current_bucket is None:
            current_bucket = bucket
            minute_buffer = [candle]
        elif bucket != current_bucket:
            finalize_15m_bar(allow_signals=True)
            if day_result:
                day_result["condition_breakdown"][1]["passed"] += 1
            current_bucket = bucket
            minute_buffer = [candle]
        else:
            minute_buffer.append(candle)

        # In-scope logic: trigger detection + exit management.
        if in_scope and bars_finalized >= EMA_PERIOD:
            # Both sides consume finalized 15m references and the current
            # 1m candle supplies the earliest executable crossing.
            sell_level = sell_setup_close - n if sell_setup_close is not None else None

            check_buy_reference_intrabar(candle, day, in_scope)
            check_red_chain_intrabar(candle, day, in_scope)

            # Legacy comparison plan: every qualifying red candle replaces the
            # reference, then a later 1-minute low crossing reference - n enters
            # at the trigger level. The current red-chain plan above keeps the
            # previous reference and enters on the forming candle's crossing.
            if (
                silver_sell_plan == SILVER_SELL_PLAN_LATEST_REFERENCE
                and bars_finalized >= EMA_PERIOD
                and (position is None or position.get("side") != "SELL")
                and sell_level is not None
                and (
                    (
                        candle["low"] <= sell_level
                        and (
                            (prev_ltp is not None and prev_ltp > sell_level)
                            or float(candle["open"]) <= sell_level
                        )
                    )
                    or (
                        sell_reentry_after_exit
                        and sell_reentry_after_exit.get("setup_bar_at") == sell_setup_bar_at
                        and abs(float(sell_reentry_after_exit.get("trigger_level") or 0) - float(sell_level)) < 1e-9
                        and prev_ltp is not None
                        and float(candle["close"]) < float(prev_ltp)
                        and float(candle["close"]) < float(candle["open"])
                        and ema20 is not None
                        and float(candle["close"]) < float(ema20)
                    )
                )
                and sell_setup_bar_at is not None
                and (
                    last_fired_sell_setup_at != sell_setup_bar_at
                    or (
                        sell_reentry_after_exit
                        and sell_reentry_after_exit.get("setup_bar_at") == sell_setup_bar_at
                        and abs(float(sell_reentry_after_exit.get("trigger_level") or 0) - float(sell_level)) < 1e-9
                    )
                )
            ):
                if position and position["side"] != "SELL":
                    close_position(sell_level, ts, "REVERSAL_CONTRA_SIGNAL", day)
                same_reference_reentry = bool(
                    sell_reentry_after_exit
                    and sell_reentry_after_exit.get("setup_bar_at") == sell_setup_bar_at
                    and abs(float(sell_reentry_after_exit.get("trigger_level") or 0) - float(sell_level)) < 1e-9
                    and prev_ltp is not None
                    and float(candle["close"]) < float(prev_ltp)
                )
                open_position(
                    "SELL",
                    float(candle["close"]) if same_reference_reentry else sell_level,
                    ts,
                    day,
                    entry_metadata={
                        "entry_mode": "SAME_REFERENCE_REENTRY" if same_reference_reentry else "THRESHOLD_TRIGGER",
                        "active_reference_close": sell_setup_close,
                        "prior_reference_close": (sell_setup_context or {}).get("previous_red_reference_close"),
                        "trigger_level_used": sell_level,
                        "reentry_exit_reason": (sell_reentry_after_exit or {}).get("exit_reason") if same_reference_reentry else None,
                    },
                )
                last_fired_sell_setup_at = sell_setup_bar_at
                sell_reentry_after_exit = None

            # Exit management for whatever's currently open.
            if position:
                previous_highest = float(position["highest"])
                previous_lowest = float(position["lowest"])
                position["highest"] = max(previous_highest, candle["high"])
                position["lowest"] = min(previous_lowest, candle["low"])
                if float(position["highest"]) > previous_highest:
                    position["peak_price"] = float(position["highest"])
                    position["peak_time"] = ts.isoformat()
                if float(position["lowest"]) < previous_lowest:
                    position["trough_price"] = float(position["lowest"])
                    position["trough_time"] = ts.isoformat()
                position["_last_trail_time"] = ts.isoformat()
                side = position["side"]
                sl = float(position["sl_price"])
                target = float(position["target_price"])
                entry = float(position["entry_price"])
                if side == "BUY":
                    favorable_points = max(0.0, float(position["highest"]) - entry)
                    adverse_points = max(0.0, entry - float(position["lowest"]))
                else:
                    favorable_points = max(0.0, entry - float(position["lowest"]))
                    adverse_points = max(0.0, float(position["highest"]) - entry)
                position["max_favorable_points"] = max(float(position.get("max_favorable_points") or 0.0), favorable_points)
                position["max_adverse_points"] = max(float(position.get("max_adverse_points") or 0.0), adverse_points)
                entry_window_end = position.get("entry_window_end")
                if isinstance(entry_window_end, datetime.datetime) and ts <= entry_window_end:
                    position["entry_window_high"] = max(float(position.get("entry_window_high") or entry), candle["high"])
                    position["entry_window_low"] = min(float(position.get("entry_window_low") or entry), candle["low"])
                    if side == "BUY":
                        position["entry_window_favorable_points"] = max(float(position.get("entry_window_favorable_points") or 0.0), max(0.0, candle["high"] - entry))
                        position["entry_window_adverse_points"] = max(float(position.get("entry_window_adverse_points") or 0.0), max(0.0, entry - candle["low"]))
                    else:
                        position["entry_window_favorable_points"] = max(float(position.get("entry_window_favorable_points") or 0.0), max(0.0, entry - candle["low"]))
                        position["entry_window_adverse_points"] = max(float(position.get("entry_window_adverse_points") or 0.0), max(0.0, candle["high"] - entry))
                # Mirror the live broker: update the trailing stop from this
                # bar's favorable extreme before checking whether its
                # reversal hits the newly tightened stop.
                maybe_apply_trailing(entry, side)
                sl = float(position["sl_price"])
                stop_hit = candle["low"] <= sl if side == "BUY" else candle["high"] >= sl
                target_hit = candle["high"] >= target if side == "BUY" else candle["low"] <= target
                use_target = exit_mode != "trailing_sl_only"
                if stop_hit and target_hit and use_target and not position.get("same_candle_sl_priority"):
                    position["same_candle_sl_priority"] = True
                    position["same_candle_sl_priority_time"] = ts.isoformat()
                if stop_hit:
                    close_position(sl, ts, "SL", day)
                elif target_hit and use_target:
                    close_position(target, ts, "TARGET", day)

        prev_ltp = candle["close"]
        last_bar_processed = candle

    # Final flush — finalize the 15m aggregation before closing any open
    # position. The 5m BUY compatibility code is intentionally not called.
    finalize_15m_bar(allow_signals=True)
    if current_day and position and last_bar_processed:
        close_position(float(last_bar_processed["close"]), last_bar_processed["time"], "EOD_SQUAREOFF", current_day)

    for day in trading_days:
        _raise_if_cancelled(job_id)
        day_result = daily_results[day]
        trades = day_result["trades"]
        day_result["summary"] = {
            **_performance_summary(trades),
            "buy_count": len([trade for trade in trades if trade["side"] == "BUY"]),
            "sell_count": len([trade for trade in trades if trade["side"] == "SELL"]),
        }
        day_result["condition_breakdown"][3]["passed"] = len(trades)
        # totals: setups (row 2 total) = 15m bars processed (row 1 passed);
        # entries (row 3 total) = setups captured (row 2 passed).
        day_result["condition_breakdown"][2]["total"] = day_result["condition_breakdown"][1]["passed"]
        day_result["condition_breakdown"][3]["total"] = day_result["condition_breakdown"][2]["passed"]
        day_result["sector_breakdown"] = build_sector_breakdown(day_result["candidates"])
        chart = day_result.get("chart") or {}
        chart_candles = list(chart.get("candles") or [])
        chart_trades = list(chart.get("trades") or [])
        if chart_candles:
            viewport = chart.get("viewport_hint") or {}
            if chart_trades:
                focus_trade = chart_trades[-1]
                entry_dt = _iso_to_naive_ist(focus_trade.get("entry_time"))
                exit_dt = _iso_to_naive_ist(focus_trade.get("exit_time"))
                viewport.update({
                    "mode": "trade_window",
                    "start_time": (entry_dt - datetime.timedelta(minutes=8 * SILVER_MICRO_BUCKET_MINUTES)).isoformat() if entry_dt else chart_candles[0]["time"],
                    "end_time": (exit_dt + datetime.timedelta(minutes=8 * SILVER_MICRO_BUCKET_MINUTES)).isoformat() if exit_dt else chart_candles[-1]["time"],
                    "trade_id": focus_trade.get("trade_id"),
                })
            else:
                viewport.update({
                    "mode": "full_day",
                    "start_time": chart_candles[0]["time"],
                    "end_time": chart_candles[-1]["time"],
                    "trade_id": None,
                })
            chart["viewport_hint"] = viewport

    return [daily_results[day] for day in trading_days]


def _close_silver_micro_position(
    position: dict,
    exit_price: float,
    exit_time: datetime.datetime,
    exit_reason: str,
    settings: dict,
    charges_config: dict,
) -> dict:
    side = position["side"]
    qty = int(position["qty"])
    entry = float(position["entry_price"])
    initial_sl = float(position.get("initial_sl_price") or position["sl_price"])
    sl = float(position["sl_price"])
    target = float(position["target_price"])
    trailing_moves = list(position.get("trailing_moves") or [])
    trailing_active = bool(position.get("trailing_sl_active"))
    trailing_enabled = bool(position.get("trailing_sl_enabled"))
    max_protected_points = 0.0
    if side == "BUY":
        max_protected_points = max(0.0, sl - entry)
    else:
        max_protected_points = max(0.0, entry - sl)
    buy_value = entry * qty if side == "BUY" else exit_price * qty
    sell_value = exit_price * qty if side == "BUY" else entry * qty
    charges = calculate_charges(buy_value, sell_value, charges_config)
    gross_pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
    trade = {
        "trade_id": position.get("trade_id") or _silver_backtest_trade_id(position["symbol"], side, position["entry_time"]),
        "symbol": position["symbol"],
        "side": side,
        "qty": qty,
        "entry_price": round(entry, 2),
        "entry_time": position["entry_time"].isoformat(),
        "entry_mode": position.get("entry_mode") or "THRESHOLD_TRIGGER",
        "active_reference_close": _round_or_none(position.get("active_reference_close")),
        "prior_reference_close": _round_or_none(position.get("prior_reference_close")),
        "trigger_level_used": _round_or_none(position.get("trigger_level_used")),
        "reentry_exit_reason": position.get("reentry_exit_reason"),
        "exit_price": round(float(exit_price), 2),
        "exit_time": exit_time.isoformat(),
        "exit_reason": exit_reason,
        "target_price": round(target, 2),
        "sl_price": round(sl, 2),
        "initial_sl_price": round(initial_sl, 2),
        "trailing_sl_enabled": trailing_enabled,
        "trailing_sl_active": trailing_active,
        "trailing_activate_points": round(float(position.get("trailing_activate_points") or 0), 2),
        "trailing_profit_step_points": round(float(position.get("trailing_profit_step_points") or 0), 2),
        "trailing_lock_step_points": round(float(position.get("trailing_lock_step_points") or 0), 2),
        "trailing_trigger_points": round(float(position.get("trailing_trigger_points") or 0), 2),
        "trailing_distance_points": round(float(position.get("trailing_distance_points") or 0), 2),
        "trailing_move_count": len(trailing_moves),
        "trailing_moves": trailing_moves,
        "max_protected_points": round(max_protected_points, 2),
        "entry_trigger": position.get("entry_trigger") or f"Historical {position['entry_time'].date().isoformat()} Silver Micro trigger replay.",
        **charges,
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(float(charges["net_pnl"]), 2),
    }
    trade["diagnostics"] = _build_silver_trade_diagnostics(position, trade, exit_time)
    return trade
