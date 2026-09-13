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
SILVER_TIMEFRAME_KEYS = {5: 'silver5m', 15: 'silver15m', 30: 'silver30m', 60: 'silver1h', 240: 'silver4h'}
DELTA_SECTION_KEYS = {"delta", "gold", "silver", "overview", "activity", "backtest",
                      "silveroverview", "silveractivity", "silverbacktest",
                      *DELTA_TIMEFRAME_KEYS.values(), *SILVER_TIMEFRAME_KEYS.values()}


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
        if delta_enabled and 'gold' not in requested and key not in requested
    ]
    gold_sections = {key: delta_enabled and 'gold' not in requested and key not in requested for key in ('overview', 'activity', 'backtest')}
    silver_sections = {key: delta_enabled and 'silver' not in requested and f'silver{key}' not in requested for key in ('overview', 'activity', 'backtest')}
    silver_timeframes = [minutes for minutes, key in SILVER_TIMEFRAME_KEYS.items() if delta_enabled and 'silver' not in requested and key not in requested]
    return {
        "delta_enabled": delta_enabled,
        "enabled_timeframes": enabled_timeframes,
        "sections": gold_sections,
        "assets": {
            "gold": {"enabled": delta_enabled and 'gold' not in requested, "enabled_timeframes": enabled_timeframes, "sections": gold_sections},
            "silver": {"enabled": delta_enabled and 'silver' not in requested, "enabled_timeframes": silver_timeframes, "sections": silver_sections},
        },
        "hidden": sorted(requested),
        "config_error": config_error,
    }


def asset_capabilities(asset='gold', capabilities=None):
    if asset not in {'gold', 'silver'}:
        raise ValueError('Unknown Delta asset')
    capabilities = capabilities or delta_capabilities()
    group = capabilities['assets'][asset]
    return {**capabilities, 'delta_enabled': group['enabled'],
            'enabled_timeframes': group['enabled_timeframes'], 'sections': group['sections']}


def timeframe_enabled(minutes: int, capabilities: dict | None = None, asset='gold') -> bool:
    capabilities = capabilities or delta_capabilities()
    return minutes in asset_capabilities(asset, capabilities)["enabled_timeframes"]
