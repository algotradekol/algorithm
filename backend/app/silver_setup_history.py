from __future__ import annotations

import datetime

from .supabase_client import run_with_supabase


def _utc_iso(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    else:
        value = value.astimezone(datetime.timezone.utc)
    return value.isoformat()


def record_setup_event(
    *,
    algo_id: str,
    symbol: str,
    side: str,
    bar: dict,
    ema20: float | None,
    breakout_points: float,
    source: str,
) -> None:
    candle_time = bar.get("time")
    if not isinstance(candle_time, datetime.datetime):
        return
    trigger_level = (
        float(bar["close"]) + float(breakout_points)
        if side == "BUY"
        else float(bar["close"]) - float(breakout_points)
    )
    payload = {
        "algo_id": algo_id,
        "symbol": symbol,
        "setup_side": side,
        "candle_time": _utc_iso(candle_time),
        "candle_open": float(bar["open"]),
        "candle_high": float(bar["high"]),
        "candle_low": float(bar["low"]),
        "candle_close": float(bar["close"]),
        "candle_volume": float(bar.get("volume") or 0),
        "minute_count": int(bar.get("minute_count") or 0),
        "ema20": float(ema20) if ema20 is not None else None,
        "breakout_points": float(breakout_points),
        "trigger_level": float(trigger_level),
        "source": str(source or "live"),
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        run_with_supabase(
            lambda supabase: supabase.table("silver_setup_events").upsert(
                payload,
                on_conflict="algo_id,symbol,setup_side,candle_time",
            ).execute()
        )
    except Exception as exc:
        print(f"[silver_setup_history] record skipped: {exc}")


def get_setup_history(
    algo_id: str,
    *,
    side: str | None = None,
    limit: int = 100,
    days: int = 30,
) -> dict:
    start_date = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max(1, int(days)))
    ).isoformat()

    def query(supabase):
        request = (
            supabase.table("silver_setup_events")
            .select("*")
            .eq("algo_id", algo_id)
            .gte("candle_time", start_date)
            .order("candle_time", desc=True)
            .limit(max(1, min(int(limit), 500)))
        )
        if side in {"BUY", "SELL"}:
            request = request.eq("setup_side", side)
        return request.execute()

    try:
        result = run_with_supabase(query)
        return {
            "algo_id": algo_id,
            "side": side,
            "rows": result.data or [],
            "warning": "",
        }
    except Exception as exc:
        return {
            "algo_id": algo_id,
            "side": side,
            "rows": [],
            "warning": str(exc),
        }
