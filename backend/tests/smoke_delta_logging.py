"""Compact logs retain decisions and audit data without per-tick noise."""
import contextlib
import io
import json
import os
from unittest.mock import patch

from app.delta_log import _summaries, delta_log


def run():
    output = io.StringIO()
    fields = {"mode": "live", "asset": "gold", "minutes": 5,
              "settings": {"trading_enabled": False}, "ltp": 100,
              "sell_checks": {"crossed": False}, "reason": "tick"}
    _summaries.clear()
    with patch.dict(os.environ, {"DELTA_DEBUG_LOGS": "true", "DELTA_LOG_MODE": "compact"}), \
            contextlib.redirect_stdout(output), patch("app.delta_log.time.monotonic", return_value=100) as clock:
        delta_log("trigger_check", **fields)
        for price in range(101, 201):
            delta_log("trigger_check", **{**fields, "ltp": price})
        clock.return_value = 161
        delta_log("trigger_check", **fields)
        delta_log("trigger_check", **{**fields, "mode": "paper"})
        delta_log("trigger_check", **{**fields, "settings": {"trading_enabled": True}})
        for _ in range(2):
            delta_log("trigger_check", **{**fields, "sell_checks": {"crossed": True}})
            delta_log("entry_submit_failed", mode="live", error="exchange rejected")
        delta_log("trigger_check", **{**fields, "reason": "settings_change"})
        delta_log("settings_saved", settings={"api_secret": "never-print-this", "trading_enabled": True})
    rows = [json.loads(line.removeprefix("[delta-trace] ")) for line in output.getvalue().splitlines()]
    assert len(rows) == 10
    assert rows[1]["suppressed_repeats"] == 100
    assert rows[2]["mode"] == "paper"
    assert rows[3]["settings"]["trading_enabled"] is True
    assert sum(row["event"] == "entry_submit_failed" for row in rows) == 2
    assert "never-print-this" not in output.getvalue()
    assert rows[-1]["settings"]["api_secret"] == "<redacted>"
    output = io.StringIO()
    with patch.dict(os.environ, {"DELTA_DEBUG_LOGS": "true", "DELTA_LOG_MODE": "verbose"}), contextlib.redirect_stdout(output):
        for _ in range(10):
            delta_log("price_accepted", mode="live", price=100)
    assert len(output.getvalue().splitlines()) == 10
    print("smoke_delta_logging: all checks passed")


if __name__ == "__main__":
    run()
