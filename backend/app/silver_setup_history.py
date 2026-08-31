from __future__ import annotations

import datetime

from .storage_namespace import current_storage_values, namespaced_value
from .supabase_client import run_with_supabase
from .timezone import IST


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_iso(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # Algo3 builds candle buckets in market-time IST using naive
        # datetimes. Treat those values as IST before converting to UTC;
        # interpreting them as UTC shifts every saved setup forward by
        # +5:30 and makes today's 23:15 candle look like tomorrow 04:45.
        value = value.replace(tzinfo=IST)
    else:
        value = value.astimezone(IST)
    value = value.astimezone(datetime.timezone.utc)
    return value.isoformat()


def _normalize_legacy_row(row: dict) -> dict:
    """Repair early setup-history rows that were saved with naive IST
    timestamps wrongly tagged as UTC.

    Symptom: a real 20 Aug 23:15 IST candle shows in the UI as
    21 Aug 04:45 IST. We detect rows that land implausibly in the future
    and shift them back by 5h30 so the history reflects the true candle
    the algo used.
    """
    normalized = dict(row)
    candle_time = normalized.get("candle_time")
    if not candle_time:
        return normalized
    try:
        parsed = datetime.datetime.fromisoformat(str(candle_time).replace("Z", "+00:00"))
    except Exception:
        return normalized
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    now_utc = _now_utc()
    future_skew = parsed - now_utc
    ist_offset = datetime.timedelta(hours=5, minutes=30)
    if datetime.timedelta(minutes=1) < future_skew <= datetime.timedelta(hours=6):
        normalized["candle_time"] = (parsed - ist_offset).astimezone(datetime.timezone.utc).isoformat()
    return normalized


def _is_qualifying_setup_row(row: dict) -> bool:
    """Return whether a persisted setup still satisfies Silver's rule.

    This is deliberately shared by the writer and reader.  The strategy is
    the primary gate, but a second gate here prevents malformed callers or
    legacy rows from ever becoming a visible BUY/SELL reference.
    """
    try:
        side = str(row.get("setup_side") or "").upper()
        open_price = float(row["candle_open"])
        close = float(row["candle_close"])
        ema20 = float(row["ema20"])
    except (KeyError, TypeError, ValueError):
        return False
    source = str(row.get("source") or "")
    is_algo5 = str(row.get("algo_id") or "").split("__", 1)[0] == "algo5"
    if is_algo5 and source.startswith("live:fallback_ema_wick:"):
        try:
            wick_distance = float(source.rsplit(":", 1)[1])
            high = float(row["candle_high"])
            low = float(row["candle_low"])
        except (KeyError, TypeError, ValueError):
            return False
        if side == "BUY":
            return open_price > close and close > ema20 and low <= ema20 + wick_distance
        if side == "SELL":
            return open_price < close and close < ema20 and high >= ema20 - wick_distance
        return False
    if side == "BUY":
        return close > open_price and close > ema20
    if side == "SELL":
        return close < open_price and close < ema20
    return False


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
        "algo_id": namespaced_value(algo_id),
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
        "captured_at": _now_utc().isoformat(),
    }
    if not _is_qualifying_setup_row(payload):
        print(
            "[silver_setup_history] record skipped: non-qualifying "
            f"{side} setup O={payload['candle_open']:.2f} "
            f"C={payload['candle_close']:.2f} EMA20={payload['ema20']}"
        )
        return
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
    current_session_only: bool = False,
    live_only: bool = False,
) -> dict:
    now_utc = _now_utc()
    if current_session_only:
        now_ist = now_utc.astimezone(IST)
        session_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = session_start_ist.astimezone(datetime.timezone.utc).isoformat()
    else:
        start_date = (
            now_utc - datetime.timedelta(days=max(1, int(days)))
        ).isoformat()

    def query(supabase):
        rows: list[dict] = []
        for candidate in current_storage_values(algo_id):
            request = (
                supabase.table("silver_setup_events")
                .select("*")
                .eq("algo_id", candidate)
                .gte("candle_time", start_date)
                .order("candle_time", desc=True)
                .limit(max(1, min(int(limit), 500)))
            )
            if side in {"BUY", "SELL"}:
                request = request.eq("setup_side", side)
            if live_only:
                request = request.eq("source", "live")
            result = request.execute()
            rows.extend(result.data or [])
            if rows:
                break
        return rows

    try:
        raw_rows = [_normalize_legacy_row(row) for row in run_with_supabase(query)]
        rows = [row for row in raw_rows if _is_qualifying_setup_row(row)]
        invalid_rows = [row for row in raw_rows if not _is_qualifying_setup_row(row)]
        excluded_count = len(invalid_rows)
        if excluded_count:
            print(
                "[silver_setup_history] excluded "
                f"{excluded_count} invalid persisted setup row(s) from history"
            )
            for row in invalid_rows:
                print(
                    "[silver_setup_history] invalid row "
                    f"side={row.get('setup_side')} time={row.get('candle_time')} "
                    f"O={row.get('candle_open')} C={row.get('candle_close')} "
                    f"EMA20={row.get('ema20')}"
                )
        for row in rows:
            row["algo_id"] = algo_id
        return {
            "algo_id": algo_id,
            "side": side,
            "rows": rows,
            "warning": (
                f"Excluded {excluded_count} older setup row(s) that do not satisfy "
                "the stored candle/EMA qualification rule."
                if excluded_count
                else ""
            ),
            "current_session_only": current_session_only,
            "live_only": live_only,
        }
    except Exception as exc:
        return {
            "algo_id": algo_id,
            "side": side,
            "rows": [],
            "warning": str(exc),
            "current_session_only": current_session_only,
            "live_only": live_only,
        }


def get_latest_setup_reference(
    algo_id: str,
    *,
    side: str,
    live_only: bool = False,
) -> dict | None:
    normalized_side = str(side or "").upper()
    if normalized_side not in {"BUY", "SELL"}:
        return None

    def query(supabase):
        rows: list[dict] = []
        for candidate in current_storage_values(algo_id):
            request = (
                supabase.table("silver_setup_events")
                .select("*")
                .eq("algo_id", candidate)
                .eq("setup_side", normalized_side)
                .order("candle_time", desc=True)
                .limit(25)
            )
            if live_only:
                request = request.eq("source", "live")
            result = request.execute()
            rows.extend(result.data or [])
            if rows:
                break
        return rows

    try:
        raw_rows = [_normalize_legacy_row(row) for row in run_with_supabase(query)]
    except Exception as exc:
        print(
            "[silver_setup_history] latest reference lookup failed "
            f"for {normalized_side}: {exc}"
        )
        return None

    for row in raw_rows:
        if not _is_qualifying_setup_row(row):
            continue
        normalized = dict(row)
        normalized["algo_id"] = algo_id
        return normalized
    return None
