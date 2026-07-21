"""Historical, read-only replay for the two live 09:15 strategies."""
import datetime
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from .charges import calculate_charges, get_charges_config
from .fyers_client import get_intraday_candles_for_range
from .strategy_settings import get_settings
from .strategies.algo4_opening_range_indicators import Algo4OpeningRangeIndicators

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")
SUPPORTED_ALGOS = {"algo1", "algo2"}
MAX_WORKERS = 4

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def start_backtest(algo_id: str, session_date: str, watchlist: list[str]) -> dict:
    if algo_id not in SUPPORTED_ALGOS:
        raise ValueError("Backtesting is currently available for Simple and Filter only.")
    target_date = datetime.date.fromisoformat(session_date)
    if target_date > datetime.datetime.now(IST).date():
        raise ValueError("Choose today or an earlier trading date.")
    if not watchlist:
        raise ValueError("The NSE 500 watchlist is not ready yet.")

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": "queued",
        "algo_id": algo_id,
        "date": target_date.isoformat(),
        "total_symbols": len(watchlist),
        "completed_symbols": 0,
        "failed_symbols": 0,
        "message": "Queued historical candle download.",
        "result": None,
        "error": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with _lock:
        _jobs[job_id] = job
    threading.Thread(target=_run_job, args=(job_id, algo_id, target_date, list(watchlist)), daemon=True).start()
    return _public_job(job)


def get_backtest_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _public_job(job) if job else None


def _public_job(job: dict | None) -> dict | None:
    if not job:
        return None
    return {key: value for key, value in job.items() if key != "_internal"}


def _update(job_id: str, **values):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(values)


def _run_job(job_id: str, algo_id: str, target_date: datetime.date, watchlist: list[str]):
    try:
        _update(job_id, status="running", message="Downloading 1-minute candles from Fyers.")
        start_date = target_date - datetime.timedelta(days=7)
        histories: dict[str, list[dict]] = {}

        def load(symbol: str):
            return symbol, get_intraday_candles_for_range(symbol, start_date, target_date)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(load, symbol) for symbol in watchlist]
            for future in as_completed(futures):
                try:
                    symbol, candles = future.result()
                    if candles:
                        histories[symbol] = candles
                    else:
                        _increment(job_id, "failed_symbols")
                except Exception:
                    _increment(job_id, "failed_symbols")
                finally:
                    _increment(job_id, "completed_symbols")

        _update(job_id, message="Replaying entries, exits, and charges.")
        settings = get_settings(algo_id)
        result = _simulate(algo_id, target_date, watchlist, histories, settings)
        result["data_coverage"] = {
            "requested_symbols": len(watchlist),
            "symbols_with_history": len(histories),
            "symbols_without_history": len(watchlist) - len(histories),
            "lookback_start": start_date.isoformat(),
        }
        _update(job_id, status="complete", message="Backtest complete.", result=result)
    except Exception as exc:
        _update(job_id, status="failed", error=str(exc), message="Backtest failed.")


def _increment(job_id: str, field: str):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job[field] = int(job.get(field) or 0) + 1


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
        row["rejection_reason"] = None if trade else "no_09_16_entry_candle"

    for row in rows:
        if row.get("side") and row.get("filters_passed") and row["symbol"] not in selected_symbols:
            row["rejection_reason"] = "slots_full"

    gross = round(sum(float(trade["gross_pnl"]) for trade in trades), 2)
    charges = round(sum(float(trade["total_charges"]) for trade in trades), 2)
    net = round(sum(float(trade["net_pnl"]) for trade in trades), 2)
    buys = len([trade for trade in trades if trade["side"] == "BUY"])
    sells = len([trade for trade in trades if trade["side"] == "SELL"])
    return {
        "algo_id": algo_id,
        "date": target_date.isoformat(),
        "mode": "historical_candle_replay",
        "execution_assumption": "Entry uses the 09:16 candle open. If a later candle touches both stop-loss and target, stop-loss is assumed first (conservative).",
        "summary": {
            "trade_count": len(trades), "buy_count": buys, "sell_count": sells,
            "gross_pnl": gross, "total_charges": charges, "net_pnl": net,
            "win_count": len([trade for trade in trades if trade["net_pnl"] > 0]),
            "loss_count": len([trade for trade in trades if trade["net_pnl"] <= 0]),
        },
        "condition_breakdown": [
            {"label": "Scanned universe", "passed": len(watchlist), "total": len(watchlist)},
            {"label": "Condition 1: 09:15 candle received", "passed": condition["candle"], "total": len(watchlist)},
            {"label": "Condition 2: open equals low/high", "passed": condition["shape"], "total": condition["candle"]},
            {"label": "Condition 3: gap rule", "passed": condition["gap"], "total": condition["shape"]},
            {"label": "Condition 4: enabled filters", "passed": condition["filters"], "total": condition["gap"]},
            {"label": "Final: selected for trade", "passed": len(trades), "total": len(candidates)},
        ],
        "candidates": rows,
        "trades": trades,
    }


def _evaluate_symbol(algo_id: str, symbol: str, target_date: datetime.date, history: list[dict], settings: dict) -> dict:
    opening = next((candle for candle in history if candle["time"].date() == target_date and candle["time"].strftime("%H:%M") == "09:15"), None)
    base = {"symbol": symbol, "has_opening_candle": bool(opening), "shape_passed": False, "gap_passed": False, "filters_passed": False, "selected_for_trade": False, "rejection_reason": "missing_09_15_candle", "indicator_results": {}}
    if not opening:
        return base
    prior = [candle for candle in history if candle["time"].date() < target_date]
    prev_close = float(prior[-1]["close"]) if prior else None
    base.update({"open": opening["open"], "high": opening["high"], "low": opening["low"], "close": opening["close"], "prev_close": prev_close})
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
    if algo_id == "algo1":
        buy_ok = is_buy_shape and abs(buy_gap) <= 2
        sell_ok = is_sell_shape and abs(sell_gap) <= 2
    else:
        buy_ok = is_buy_shape and 0.5 <= buy_gap <= 2
        sell_ok = is_sell_shape and 0.5 <= sell_gap <= 2
    side = "BUY" if buy_ok else "SELL" if sell_ok else None
    base.update({"side": side or "WATCH", "gap_pct": buy_gap if buy_ok else sell_gap if sell_ok else abs(buy_gap), "gap_passed": bool(side)})
    if not side:
        base["rejection_reason"] = "gap_rule_failed"
        return base
    if algo_id == "algo1":
        base["filters_passed"] = True
        base["rejection_reason"] = "slots_full"
        return base

    prior_and_opening = [candle for candle in history if candle["time"] <= opening["time"]]
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
    max_total = int(settings["max_trades_per_day"])
    max_buy = int(settings["max_buy_trades"])
    max_sell = int(settings["max_sell_trades"])
    buys = [row for row in candidates if row["side"] == "BUY"]
    sells = [row for row in candidates if row["side"] == "SELL"]
    selected = buys[:max_buy] + sells[:max_sell]
    remaining = max(0, max_total - len(selected))
    selected += (buys[max_buy:] + sells[max_sell:])[:remaining]
    return selected


def _simulate_trade(row: dict, history: list[dict], target_date: datetime.date, settings: dict, charges_config: dict) -> dict | None:
    entry_candle = next((candle for candle in history if candle["time"].date() == target_date and candle["time"].strftime("%H:%M") == "09:16"), None)
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
    candles = [candle for candle in history if candle["time"].date() == target_date and candle["time"].strftime("%H:%M") >= "09:17" and candle["time"].strftime("%H:%M") < "15:15"]
    for candle in candles:
        # Conservative order: an existing stop is checked before target when
        # both are touched inside the same OHLC candle.
        stop_hit = candle["low"] <= sl if side == "BUY" else candle["high"] >= sl
        target_hit = candle["high"] >= target if side == "BUY" else candle["low"] <= target
        if stop_hit:
            exit_price, exit_reason = sl, "SL"
            break
        if target_hit and settings.get("exit_mode") != "trailing_sl_only":
            exit_price, exit_reason = target, "TARGET"
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
    buy_value = entry * qty if side == "BUY" else exit_price * qty
    sell_value = exit_price * qty if side == "BUY" else entry * qty
    charges = calculate_charges(buy_value, sell_value, charges_config)
    return {
        "symbol": row["symbol"], "side": side, "qty": qty,
        "entry_price": round(entry, 2), "entry_time": entry_candle["time"].isoformat(),
        "exit_price": round(exit_price, 2), "exit_reason": exit_reason,
        "target_price": round(target, 2), "sl_price": round(sl, 2),
        "entry_trigger": f"Historical {target_date.isoformat()} 09:15 opening-range replay.",
        **charges,
    }
