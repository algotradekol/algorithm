"""Offline smoke checks for Delta timeframes, visibility and overview."""
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.delta_candles import aggregate_seven_minute, delta_resolution
from app.delta_config import delta_capabilities
from app.delta_engine import DeltaService
from app.delta_routes import require_delta
from app.strategies.delta_gold import DELTA_TIMEFRAMES
from tests.smoke_delta_paper import MemoryStore, PRODUCT, strategy


def minute(stamp, price, volume=1):
    return {"time": stamp, "open": price, "high": price + 2, "low": price - 1, "close": price + 1, "volume": volume}


def run():
    assert DELTA_TIMEFRAMES == (5, 7, 15, 30, 60, 240)
    assert {minutes: delta_resolution(minutes) for minutes in DELTA_TIMEFRAMES} == {
        5: "5m", 7: "1m", 15: "15m", 30: "30m", 60: "1h", 240: "4h",
    }

    rows = [minute(index * 60, 100 + index, index + 1) for index in range(14)]
    bars = aggregate_seven_minute(rows)
    assert len(bars) == 2
    assert bars[0] == {"time": 0, "open": 100.0, "high": 108.0, "low": 99.0,
                       "close": 107.0, "volume": 28.0, "source_minutes": 7}
    assert bars[1]["time"] == 420 and bars[1]["volume"] == 77
    # A continuous 7m bucket is allowed to span UTC midnight without shortening.
    midnight = 24 * 60 * 60
    crossing_bucket = midnight // 420 * 420
    crossing = [minute(crossing_bucket + index * 60, 200 + index) for index in range(7)]
    assert len(aggregate_seven_minute(crossing)) == 1
    assert crossing_bucket < midnight < crossing_bucket + 420
    assert aggregate_seven_minute(crossing[:-1]) == []
    missing = crossing[:3] + crossing[4:]
    assert aggregate_seven_minute(missing) == []
    partial = aggregate_seven_minute(crossing[:3], current_time=crossing_bucket + 180)
    assert len(partial) == 1 and partial[0]["source_minutes"] == 3

    with patch.dict(os.environ, {"DELTA_HIDDEN_SECTIONS": "gold5m,gold7m,gold4h,activity"}, clear=False):
        capabilities = delta_capabilities()
        assert capabilities["enabled_timeframes"] == [15, 30, 60]
        assert not capabilities["sections"]["activity"] and capabilities["sections"]["overview"]
        try:
            require_delta(minutes=7)
            raise AssertionError("hidden Delta timeframe accepted")
        except HTTPException as exc:
            assert exc.status_code == 404
        fake_client = SimpleNamespace(
            region="india", symbol="PAXGUSD", configuration_error=lambda: None,
            product=lambda: {**PRODUCT, "quoting_asset": {"symbol": "USD"}},
        )
        with patch("app.delta_engine.DeltaClient", return_value=fake_client), \
             patch("app.delta_engine.DeltaStore", side_effect=lambda key: MemoryStore()), \
             patch("app.delta_engine.threading.Thread"):
            gated = DeltaService()
            gated._initialize()
        assert sorted(gated.strategies) == [15, 30, 60]
    with patch.dict(os.environ, {"DELTA_HIDDEN_SECTIONS": "typo"}, clear=False):
        capabilities = delta_capabilities()
        assert not capabilities["delta_enabled"] and capabilities["config_error"]
        assert capabilities["enabled_timeframes"] == []
    with patch.dict(os.environ, {"DELTA_HIDDEN_SECTIONS": "delta"}, clear=False):
        assert not delta_capabilities()["delta_enabled"]

    service = DeltaService()
    service.client = SimpleNamespace(region="india")
    service.product = {**PRODUCT, "quoting_asset": {"symbol": "USD"}}
    service.last_price = 1010
    service.last_event_at = time.time()
    service.strategies = {minutes: strategy(minutes) for minutes in DELTA_TIMEFRAMES}
    ids = set()
    for minutes, item in service.strategies.items():
        assert item._enter("BUY", 1000, 1000)
        ids.add(item._open_position()["id"])
        item.broker.close_trade(item._open_position(), 1010, "TARGET")
    assert len(ids) == len(DELTA_TIMEFRAMES)
    # Leave one open position so aggregate realized and unrealized outcomes coexist.
    assert service.strategies[5]._enter("BUY", 1000, 1000)
    with patch.dict(os.environ, {"DELTA_HIDDEN_SECTIONS": ""}, clear=False):
        overview = service.overview()
    assert [row["minutes"] for row in overview["timeframes"]] == list(DELTA_TIMEFRAMES)
    assert overview["totals"]["all_time"]["trades"] == 6
    assert overview["totals"]["today"]["trades"] == 6
    assert overview["totals"]["unrealized"] > 0
    assert overview["totals"]["all_time_with_unrealized"] == overview["totals"]["all_time"]["net"] + overview["totals"]["unrealized"]
    assert overview["inr_rate"] == 85
    print("smoke_delta_multiframe: all checks passed")


if __name__ == "__main__":
    run()
