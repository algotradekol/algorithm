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

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
SUPPORTED_ALGOS = {"algo1", "algo2", "algo3"}
MAX_WORKERS = 2
MAX_BACKTEST_DAYS = 31
EMA_PERIOD = 20
WARMUP_LOOKBACK_DAYS = 10
SILVER_MICRO_BUCKET_MINUTES = 15
SILVER_MICRO_MCX_CLOSE_HHMM = "23:30"  # MCX evening session close
OPENING_WINDOW_START = "09:15"
OPENING_WINDOW_END = "09:16"
ENTRY_TIME = "09:16"
EXIT_SCAN_START = "09:17"

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

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "algo_id": algo_id,
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
    threading.Thread(target=_run_job, args=(job_id, algo_id, first_date, last_date, list(watchlist)), daemon=True).start()
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
):
    history_cache = BacktestHistoryCache()
    try:
        _raise_if_cancelled(job_id)
        settings = get_settings(algo_id)
        if algo_id == "algo3":
            if not watchlist:
                raise ValueError("The Silver Micro contract could not be resolved for backtesting.")
            _run_silver_micro_job(job_id, algo_id, first_date, last_date, watchlist[0], settings, history_cache)
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
        })
    best_day = max(daily_rows, key=lambda day: float(day["summary"]["net_pnl"]), default=None)
    worst_day = min(daily_rows, key=lambda day: float(day["summary"]["net_pnl"]), default=None)
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
            f"No Silver Micro history was returned for the chosen range after trying 1-minute and 5-minute candles."
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
        execution_assumption=_silver_micro_execution_assumption(history_resolution, settings),
    )
    _raise_if_cancelled(job_id)
    _update(job_id, status="complete", phase="complete", message="Silver Micro backtest complete.", result=result)


def _silver_micro_execution_assumption(history_resolution: str, settings: dict) -> str:
    n = int(settings.get("silver_breakout_points", 150))
    sl_pts = int(settings.get("sl_points", 100))
    target_pts = int(settings.get("target_points", 300))
    tsl_enabled = bool(settings.get("trailing_sl_enabled"))
    tsl_trigger = int(settings.get("tsl_trigger_points", 0) or 0)
    tsl_distance = int(settings.get("tsl_distance_points", 0) or 0)
    exit_mode = str(settings.get("exit_mode") or "fixed_target_trailing_sl")
    trailing_clause = (
        f" Trailing SL is ON: activates after {tsl_trigger} points profit and trails by "
        f"{tsl_distance} points."
        if tsl_enabled and exit_mode in {"trailing_sl_only", "fixed_target_trailing_sl"}
        else " Trailing SL is OFF for this replay."
    )
    return (
        f"Silver Micro replays 15-minute bars aggregated from 1-minute history "
        f"({history_resolution}). Each closed 15m bar updates EMA20; a green candle "
        f"closing above EMA20 stores its close as the BUY level, a red candle "
        f"closing below EMA20 stores the SELL red-reference close. BUY entry fires "
        f"when a subsequent 1-minute bar's high crosses (setup_close + n={n} points). "
        f"SELL entry fires only when a later qualifying red 15m candle closes at "
        f"least n points below the previous stored red reference close; green candles "
        f"in between do not reset that stored red reference. BUY entries use 1-minute "
        f"bar extremes and enter at the trigger level itself. SELL red-chain entries "
        f"enter at the qualifying red candle's close. SL={sl_pts} points, target={target_pts} "
        f"points, both fixed rupee distances from entry. If a 1-minute bar touches "
        f"both SL and target, SL is assumed first (conservative). Reversal on "
        f"contra trigger closes the current position and flips at the same bar."
        f"{trailing_clause}"
    )


def _new_silver_micro_day_result(symbol: str, day: datetime.date, bar_count: int) -> dict:
    return {
        "algo_id": "algo3",
        "date": day.isoformat(),
        "mode": "historical_mcx_replay",
        "execution_assumption": (
            "Silver Micro (15m EMA breakout): green candle above EMA20 sets BUY level, "
            "red candle below EMA20 stores a SELL red reference. BUY uses setup close + n; "
            "SELL fires only when a later qualifying red closes at least n points below "
            "the previous stored red reference."
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
            day_history = get_intraday_candles_for_range(symbol, current, current, resolution="1")
            resolution = "1"
            if not day_history:
                day_history = get_intraday_candles_for_range(symbol, current, current, resolution="5")
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
) -> list[dict]:
    """15m EMA breakout replay (2026-08-19 rewrite).

    Iterates the raw 1-minute history. Each time a 15-min bucket closes
    we update EMA20 and (if it qualifies) the BUY/SELL setup level. On
    every 1-min bar we check whether its high/low crossed a trigger
    (setup_close +/- n) — the closest tick-level approximation the
    backtest can offer from 1m data. Enters at the trigger level itself
    (conservative). SL/target/TSL are in POINTS from entry, matching the
    live strategy exactly.
    """
    n = float(settings.get("silver_breakout_points", 150))
    sl_pts = float(settings.get("sl_points", 100))
    target_pts = float(settings.get("target_points", 300))
    tsl_trigger_pts = float(settings.get("tsl_trigger_points", 0))
    tsl_distance_pts = float(settings.get("tsl_distance_points", 0))
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
        day: _new_silver_micro_day_result(symbol, day, minute_bars_by_day.get(day, 0))
        for day in trading_days
    }

    # Live-state variables — mirror algo3_silver_micro.py exactly.
    ema20: float | None = None
    buy_setup_close: float | None = None
    sell_setup_close: float | None = None
    minute_buffer: list[dict] = []
    current_bucket: datetime.datetime | None = None
    prev_ltp: float | None = None
    bars_finalized = 0
    current_day: datetime.date | None = None
    last_bar_processed: dict | None = None
    position: dict | None = None
    position_candidate: dict | None = None

    def finalize_15m_bar(allow_signals: bool):
        """Aggregate the minute_buffer into one 15m bar, update EMA20
        and the BUY/SELL setup levels."""
        nonlocal minute_buffer, ema20, buy_setup_close, sell_setup_close, bars_finalized
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
        if is_green and ema20 is not None and bar["close"] > ema20:
            buy_setup_close = bar["close"]
            setup_event = {"side": "BUY", "close": bar["close"], "bar": bar}
        elif is_red and ema20 is not None and bar["close"] < ema20:
            setup_event = {"side": "SELL", "close": bar["close"], "bar": bar}
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
                day_result["condition_breakdown"][2]["passed"] += 1
        if (
            allow_signals
            and first_date <= bar["time"].date() <= last_date
            and bars_finalized >= EMA_PERIOD
            and setup_event
            and setup_event["side"] == "SELL"
            and sell_setup_close is not None
        ):
            sell_level = sell_setup_close - n
            if float(bar["close"]) <= float(sell_level):
                current_position = position
                if current_position and current_position["side"] == "SELL":
                    pass
                else:
                    if current_position and current_position["side"] != "SELL":
                        close_position(float(bar["close"]), bar["time"], "REVERSAL_CONTRA_SIGNAL", bar["time"].date())
                    open_position("SELL", float(bar["close"]), bar["time"], bar["time"].date())
        if is_green and ema20 is not None and bar["close"] > ema20:
            buy_setup_close = bar["close"]
        elif is_red and ema20 is not None and bar["close"] < ema20:
            sell_setup_close = bar["close"]

    def close_position(exit_price: float, exit_time: datetime.datetime, exit_reason: str, day: datetime.date):
        nonlocal position, position_candidate
        if not position:
            return
        trade = _close_silver_micro_position(position, exit_price, exit_time, exit_reason, settings, charges_config)
        daily_results[day]["trades"].append(trade)
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

    def open_position(side: str, entry_price: float, entry_time: datetime.datetime, day: datetime.date):
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
        position = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": float(entry_price),
            "entry_time": entry_time,
            "initial_sl_price": float(sl_price),
            "sl_price": sl_price,
            "target_price": target_price,
            "highest": float(entry_price),
            "lowest": float(entry_price),
            "trailing_sl_enabled": bool(tsl_enabled),
            "trailing_sl_active": False,
            "trailing_trigger_points": float(tsl_trigger_pts),
            "trailing_distance_points": float(tsl_distance_pts),
            "trailing_moves": [],
        }
        # Latest matching-side setup in this day's candidates is the source
        # candidate we mark as selected_for_trade.
        source_candidate = None
        day_result = daily_results.get(day)
        if day_result:
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
        position_candidate = source_candidate

    def maybe_apply_trailing(entry: float, side: str):
        """Points-based trailing: activate once favorable move >= trigger,
        then trail SL distance points behind the extremum."""
        nonlocal position
        if not position or not tsl_enabled or tsl_trigger_pts <= 0 or tsl_distance_pts <= 0:
            return
        if side == "BUY":
            gain = float(position["highest"]) - entry
            if gain >= tsl_trigger_pts:
                position["trailing_sl_active"] = True
                new_sl = float(position["highest"]) - tsl_distance_pts
                if new_sl > float(position["sl_price"]):
                    previous_sl = float(position["sl_price"])
                    position["sl_price"] = new_sl
                    position.setdefault("trailing_moves", []).append({
                        "time": position.get("_last_trail_time"),
                        "side": side,
                        "gain_points": round(gain, 2),
                        "reference_price": round(float(position["highest"]), 2),
                        "previous_sl": round(previous_sl, 2),
                        "new_sl": round(new_sl, 2),
                        "protected_points": round(new_sl - entry, 2),
                    })
        else:
            gain = entry - float(position["lowest"])
            if gain >= tsl_trigger_pts:
                position["trailing_sl_active"] = True
                new_sl = float(position["lowest"]) + tsl_distance_pts
                if new_sl < float(position["sl_price"]):
                    previous_sl = float(position["sl_price"])
                    position["sl_price"] = new_sl
                    position.setdefault("trailing_moves", []).append({
                        "time": position.get("_last_trail_time"),
                        "side": side,
                        "gain_points": round(gain, 2),
                        "reference_price": round(float(position["lowest"]), 2),
                        "previous_sl": round(previous_sl, 2),
                        "new_sl": round(new_sl, 2),
                        "protected_points": round(entry - new_sl, 2),
                    })

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
            # Trigger levels from the latest stored setups.
            buy_level = buy_setup_close + n if buy_setup_close is not None else None
            sell_level = sell_setup_close - n if sell_setup_close is not None else None

            # BUY cross: prev_ltp < level AND this minute's high >= level.
            if position is None or position.get("side") != "BUY":
                if buy_level is not None and prev_ltp is not None and prev_ltp < buy_level <= candle["high"]:
                    if position and position["side"] != "BUY":
                        close_position(buy_level, ts, "REVERSAL_CONTRA_SIGNAL", day)
                    open_position("BUY", buy_level, ts, day)

            # Exit management for whatever's currently open.
            if position:
                position["highest"] = max(float(position["highest"]), candle["high"])
                position["lowest"] = min(float(position["lowest"]), candle["low"])
                position["_last_trail_time"] = ts.isoformat()
                side = position["side"]
                sl = float(position["sl_price"])
                target = float(position["target_price"])
                entry = float(position["entry_price"])
                stop_hit = candle["low"] <= sl if side == "BUY" else candle["high"] >= sl
                target_hit = candle["high"] >= target if side == "BUY" else candle["low"] <= target
                use_target = exit_mode != "trailing_sl_only"
                if stop_hit:
                    close_position(sl, ts, "SL", day)
                elif target_hit and use_target:
                    close_position(target, ts, "TARGET", day)
                else:
                    maybe_apply_trailing(entry, side)

        prev_ltp = candle["close"]
        last_bar_processed = candle

    # Final flush — finalize the last 15m bucket + close any open position.
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
    return {
        "symbol": position["symbol"],
        "side": side,
        "qty": qty,
        "entry_price": round(entry, 2),
        "entry_time": position["entry_time"].isoformat(),
        "exit_price": round(float(exit_price), 2),
        "exit_time": exit_time.isoformat(),
        "exit_reason": exit_reason,
        "target_price": round(target, 2),
        "sl_price": round(sl, 2),
        "initial_sl_price": round(initial_sl, 2),
        "trailing_sl_enabled": trailing_enabled,
        "trailing_sl_active": trailing_active,
        "trailing_trigger_points": round(float(position.get("trailing_trigger_points") or 0), 2),
        "trailing_distance_points": round(float(position.get("trailing_distance_points") or 0), 2),
        "trailing_move_count": len(trailing_moves),
        "trailing_moves": trailing_moves,
        "max_protected_points": round(max_protected_points, 2),
        "entry_trigger": f"Historical {position['entry_time'].date().isoformat()} Silver Micro 15m breakout replay.",
        **charges,
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(float(charges["net_pnl"]), 2),
    }
