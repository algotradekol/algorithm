"""Deployment-level visibility and execution gates for Delta strategies."""
from __future__ import annotations

import os


DELTA_TIMEFRAME_KEYS = {
    5: "gold5m",
    7: "gold7m",
    15: "gold15m",
    30: "gold30m",
    60: "gold1h",
    240: "gold4h",
}
DELTA_SECTION_KEYS = {"delta", "overview", "activity", "backtest", *DELTA_TIMEFRAME_KEYS.values()}


def _normalize(value: str) -> str:
    return value.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def delta_capabilities() -> dict:
    requested = {
        _normalize(value)
        for value in os.environ.get("DELTA_HIDDEN_SECTIONS", "").split(",")
        if value.strip()
    }
    unknown = sorted(requested - DELTA_SECTION_KEYS)
    config_error = (
        f"Unknown DELTA_HIDDEN_SECTIONS keyword(s): {', '.join(unknown)}"
        if unknown
        else None
    )
    delta_enabled = not unknown and "delta" not in requested
    enabled_timeframes = [
        minutes
        for minutes, key in DELTA_TIMEFRAME_KEYS.items()
        if delta_enabled and key not in requested
    ]
    return {
        "delta_enabled": delta_enabled,
        "enabled_timeframes": enabled_timeframes,
        "sections": {
            "overview": delta_enabled and "overview" not in requested,
            "activity": delta_enabled and "activity" not in requested,
            "backtest": delta_enabled and "backtest" not in requested,
        },
        "hidden": sorted(requested),
        "config_error": config_error,
    }


def timeframe_enabled(minutes: int, capabilities: dict | None = None) -> bool:
    capabilities = capabilities or delta_capabilities()
    return minutes in capabilities["enabled_timeframes"]
