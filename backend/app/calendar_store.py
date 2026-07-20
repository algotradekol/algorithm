import datetime
import json
from decimal import Decimal
from typing import Any

from app.supabase_client import run_with_supabase


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _today_ist() -> str:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).date().isoformat()


def save_dashboard_snapshot(algo_id: str | None = None, note: str = "manual") -> list[dict]:
    from app.engine import SCAN_RESULTS, STRATEGIES, attach_entry_triggers, enrich_positions_with_ltp, get_engine_status
    from app.fyers_client import get_connection_status

    strategy_items = STRATEGIES.items()
    if algo_id:
        strategy = STRATEGIES.get(algo_id)
        strategy_items = [(algo_id, strategy)] if strategy else []

    rows = []
    snapshot_date = _today_ist()
    engine_status = _jsonable(get_engine_status())
    fyers_status = _jsonable(get_connection_status())

    for current_algo_id, strategy in strategy_items:
        if not strategy:
            continue
        settings = getattr(strategy, "settings", {}) or {}
        row = {
            "snapshot_date": snapshot_date,
            "algo_id": current_algo_id,
            "display_name": getattr(strategy, "display_name", current_algo_id),
            "summary": _jsonable(strategy.broker.summary()),
            "positions": _jsonable(attach_entry_triggers(current_algo_id, enrich_positions_with_ltp(strategy.broker.open_positions()))),
            "trades": _jsonable(attach_entry_triggers(current_algo_id, strategy.broker.recent_trades(limit=10000))),
            "scan_results": _jsonable(SCAN_RESULTS.get(current_algo_id)),
            "settings": _jsonable(settings),
            "engine_status": engine_status,
            "fyers_status": fyers_status,
            "note": note,
            "updated_at": "now()",
        }
        run_with_supabase(
            lambda supabase, payload=row: supabase.table("calendar_snapshots").upsert(
                payload,
                on_conflict="snapshot_date,algo_id",
            ).execute()
        )
        rows.append(row)
    return rows


def list_calendar_days(days: int = 60) -> list[dict]:
    start_date = datetime.date.today() - datetime.timedelta(days=max(days - 1, 0))
    result = run_with_supabase(
        lambda supabase: supabase.table("calendar_snapshots")
        .select("snapshot_date,algo_id,display_name,summary,note,updated_at")
        .gte("snapshot_date", start_date.isoformat())
        .order("snapshot_date", desc=True)
        .execute()
    )
    return result.data


def get_calendar_day(snapshot_date: str) -> list[dict]:
    result = run_with_supabase(
        lambda supabase: supabase.table("calendar_snapshots")
        .select("*")
        .eq("snapshot_date", snapshot_date)
        .order("algo_id")
        .execute()
    )
    return result.data


def delete_calendar_day(snapshot_date: str) -> dict:
    result = run_with_supabase(
        lambda supabase: supabase.table("calendar_snapshots")
        .delete()
        .eq("snapshot_date", snapshot_date)
        .execute()
    )
    return {"status": "deleted", "snapshot_date": snapshot_date, "deleted": len(result.data or [])}


def delete_calendar_snapshot(snapshot_date: str, algo_id: str) -> dict:
    result = run_with_supabase(
        lambda supabase: supabase.table("calendar_snapshots")
        .delete()
        .eq("snapshot_date", snapshot_date)
        .eq("algo_id", algo_id)
        .execute()
    )
    return {"status": "deleted", "snapshot_date": snapshot_date, "algo_id": algo_id, "deleted": len(result.data or [])}


def store_market_candles(symbol: str, resolution: str, candles: list[dict], source: str = "fyers_history") -> int:
    if not candles:
        return 0

    rows = []
    for candle in candles:
        candle_time = candle.get("time") or candle.get("timestamp") or candle.get("date")
        if not candle_time:
            continue
        rows.append({
            "symbol": symbol,
            "resolution": str(resolution),
            "candle_time": candle_time,
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume"),
            "source": source,
            "raw": json.loads(json.dumps(_jsonable(candle))),
        })

    if not rows:
        return 0

    run_with_supabase(
        lambda supabase: supabase.table("market_candles").upsert(
            rows,
            on_conflict="symbol,resolution,candle_time",
        ).execute()
    )
    return len(rows)
