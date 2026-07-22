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
from .candidate_ranking import rank_candidates, select_ranked_candidates
from .supabase_client import run_with_supabase

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
SUPPORTED_ALGOS = {"algo1", "algo2"}
MAX_WORKERS = 2
MAX_BACKTEST_DAYS = 31
OPENING_WINDOW_START = "09:15"
OPENING_WINDOW_END = "09:18"
ENTRY_TIME = "09:18"
EXIT_SCAN_START = "09:19"

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


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
        raise ValueError("Backtesting is currently available for Simple and Filter only.")
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
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with _lock:
        active = next(
            (existing for existing in _jobs.values() if existing.get("status") in {"queued", "running"}),
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
        if job and job.get("status") in {"queued", "running"}:
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
        settings = get_settings(algo_id)
        rows_by_day: dict[datetime.date, list[dict]] = {day: [] for day in trading_days}
        symbols_with_history = 0

        def screen_symbol(symbol: str):
            # Cache each response on disk. Keeping all 500 histories in RAM
            # exhausted Railway, while discarding them forced duplicate Fyers
            # requests for every selected replay signal.
            history = get_intraday_candles_for_range(symbol, lookback_start, last_date)
            rows = [_evaluate_symbol(algo_id, symbol, day, history, settings) for day in trading_days]
            return rows, bool(history), history_cache.store(symbol, history)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(screen_symbol, symbol) for symbol in watchlist]
            for future in as_completed(futures):
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
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for symbol, selected_rows in replay_by_symbol.items():
                future = pool.submit(
                    _replay_cached_symbol,
                    history_cache,
                    symbol,
                    selected_rows,
                    settings,
                    charges_config,
                )
                futures[future] = selected_rows
            for future in as_completed(futures):
                try:
                    replayed_rows = future.result()
                except Exception:
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
                        row["rejection_reason"] = "replay_cache_unavailable" if failed else "no_09_18_entry_candle"
                        _append_replay_activity(job_id, {
                            "date": target_date.isoformat(),
                            "symbol": row["symbol"],
                            "side": row.get("side"),
                            "status": "CACHE_UNAVAILABLE" if failed else "NO_ENTRY_CANDLE",
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
        _update(job_id, status="complete", phase="complete", message="Backtest complete.", result=result)
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


def _simulate(algo_id: str, target_date: datetime.date, watchlist: list[str], histories: dict[str, list[dict]], settings: dict) -> dict:
    rows: list[dict] = []
    condition = {"candle": 0, "shape": 0, "gap": 0, "filters": 0}
    for symbol in watchlist:
        history = histories.get(symbol) or []
        row = _evaluate_symbol(algo_id, symbol, target_date, history, settings)
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
        row["rejection_reason"] = None if trade else "no_09_18_entry_candle"

    for row in rows:
        if row.get("side") and row.get("filters_passed") and row["symbol"] not in selected_symbols:
            row["rejection_reason"] = "slots_full"

    summary = _performance_summary(trades)
    buys = len([trade for trade in trades if trade["side"] == "BUY"])
    sells = len([trade for trade in trades if trade["side"] == "SELL"])
    return {
        "algo_id": algo_id,
        "date": target_date.isoformat(),
        "mode": "historical_candle_replay",
        "execution_assumption": "Signal uses the combined 09:15-09:17 window; entry uses the 09:18 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
        "summary": {**summary, "buy_count": buys, "sell_count": sells},
        "condition_breakdown": [
            {"label": "Scanned universe", "passed": len(watchlist), "total": len(watchlist)},
            {"label": "Condition 1: 09:15-09:17 candles received", "passed": condition["candle"], "total": len(watchlist)},
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
    for row in rows:
        if row.get("side") and row.get("filters_passed") and row["symbol"] not in selected_symbols:
            row["rejection_reason"] = "slots_full"
    return {
        "algo_id": algo_id,
        "date": target_date.isoformat(),
        "mode": "historical_candle_replay",
        "execution_assumption": "Signal uses the combined 09:15-09:17 window; entry uses the 09:18 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
        "summary": {},
        "condition_breakdown": [
            {"label": "Scanned universe", "passed": watchlist_size, "total": watchlist_size},
            {"label": "Condition 1: 09:15-09:17 candles received", "passed": condition["candle"], "total": watchlist_size},
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
            "data_available_symbols": next(
                (step["passed"] for step in day["condition_breakdown"] if step["label"] == "Condition 1: 09:15-09:17 candles received"), 0,
            ),
            "trades": day["trades"],
            "candidates": day["candidates"],
        })
    best_day = max(daily_rows, key=lambda day: float(day["summary"]["net_pnl"]), default=None)
    worst_day = min(daily_rows, key=lambda day: float(day["summary"]["net_pnl"]), default=None)
    return {
        "algo_id": algo_id,
        "start_date": first_date.isoformat(),
        "end_date": last_date.isoformat(),
        "mode": "historical_candle_replay",
        "execution_assumption": "Signal uses the combined 09:15-09:17 window; entry uses the 09:18 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
        "summary": {**summary, "trading_days_replayed": len(daily_results), "max_drawdown": round(max_drawdown, 2)},
        "best_day": {"date": best_day["date"], "net_pnl": best_day["summary"]["net_pnl"]} if best_day else None,
        "worst_day": {"date": worst_day["date"], "net_pnl": worst_day["summary"]["net_pnl"]} if worst_day else None,
        "data_coverage": data_coverage,
        "daily_results": daily_rows,
    }


def _evaluate_symbol(algo_id: str, symbol: str, target_date: datetime.date, history: list[dict], settings: dict) -> dict:
    opening_candles = [
        candle for candle in history
        if candle["time"].date() == target_date and OPENING_WINDOW_START <= candle["time"].strftime("%H:%M") < OPENING_WINDOW_END
    ]
    base = {"symbol": symbol, "has_opening_candle": bool(opening_candles), "shape_passed": False, "gap_passed": False, "filters_passed": False, "selected_for_trade": False, "rejection_reason": "missing_09_15_09_17_window", "indicator_results": {}}
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
        "price_range": check("price_range", ltp, settings["ltp_min"] < ltp < settings["ltp_max"], settings.get("filter_price_range", True)),
    }
    base["indicator_results"] = results
    base["filters_passed"] = all(item["passed"] for item in results.values() if item["enabled"])
    base["rejection_reason"] = "slots_full" if base["filters_passed"] else "failed_indicator_filter"
    return base


def _select_candidates(candidates: list[dict], settings: dict) -> list[dict]:
    profile = "simple" if candidates and candidates[0].get("algo_id") == "algo1" else "filter"
    # Candidate rows do not carry algo_id in older stored data. The simple
    # strategy has no indicator data, which is the reliable fallback signal.
    if candidates and not candidates[0].get("indicator_results"):
        profile = "simple"
    return select_ranked_candidates(rank_candidates(candidates, settings, profile), settings)


def _simulate_trade(row: dict, history: list[dict], target_date: datetime.date, settings: dict, charges_config: dict) -> dict | None:
    entry_candle = next((candle for candle in history if candle["time"].date() == target_date and candle["time"].strftime("%H:%M") == ENTRY_TIME), None)
    if not entry_candle:
        return None
    side = row["side"]
    entry = float(entry_candle["open"])
    qty = int(float(settings["capital_per_trade"]) // entry)
    if qty < 1:
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
        "entry_trigger": f"Historical {target_date.isoformat()} 09:15-09:17 opening-window replay.",
        **charges,
    }
