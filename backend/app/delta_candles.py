"""Candle resolution helpers shared by Delta paper and backtest paths."""
from __future__ import annotations

import math


NATIVE_DELTA_RESOLUTIONS = {
    5: "5m",
    15: "15m",
    30: "30m",
    60: "1h",
    240: "4h",
}


def delta_resolution(minutes: int) -> str:
    return "1m" if minutes == 7 else NATIVE_DELTA_RESOLUTIONS[minutes]


def aggregate_seven_minute(rows, current_time: int | float | None = None):
    """Aggregate continuous epoch-anchored 7m bars from validated 1m OHLCV."""
    by_bucket: dict[int, dict[int, dict]] = {}
    for raw in rows:
        stamp = int(raw["time"])
        if stamp % 60:
            raise ValueError("Delta 7m source contains a non-minute timestamp")
        prices = {key: float(raw[key]) for key in ("open", "high", "low", "close")}
        volume = float(raw.get("volume", 0))
        if (
            any(not math.isfinite(value) or value <= 0 for value in prices.values())
            or not math.isfinite(volume)
            or volume < 0
            or not prices["low"] <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"]) <= prices["high"]
        ):
            raise ValueError("Invalid Delta 1m OHLCV history")
        bucket = stamp // 420 * 420
        row = {"time": stamp, **prices, "volume": volume}
        existing = by_bucket.setdefault(bucket, {}).get(stamp)
        if existing is not None and existing != row:
            raise ValueError("Conflicting duplicate Delta 1m candles")
        by_bucket[bucket][stamp] = row

    current_bucket = int(current_time // 420 * 420) if current_time is not None else None
    output = []
    for bucket in sorted(by_bucket):
        minute_rows = by_bucket[bucket]
        expected = list(range(bucket, bucket + 420, 60))
        complete = all(stamp in minute_rows for stamp in expected)
        is_forming = current_bucket == bucket
        if not complete:
            if not is_forming:
                continue
            available = sorted(minute_rows)
            expected_partial = list(range(bucket, available[-1] + 60, 60)) if available else []
            if not available or available != expected_partial:
                continue
            selected = [minute_rows[stamp] for stamp in available]
        else:
            selected = [minute_rows[stamp] for stamp in expected]
        output.append({
            "time": bucket,
            "open": selected[0]["open"],
            "high": max(row["high"] for row in selected),
            "low": min(row["low"] for row in selected),
            "close": selected[-1]["close"],
            "volume": sum(row["volume"] for row in selected),
            "source_minutes": len(selected),
        })
    return output
