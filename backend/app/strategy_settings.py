DEFAULT_SETTINGS = {
    "starting_capital": 500000,
    "capital_per_trade": 50000,
    "margin_multiplier": 5,
    "target_pct": 2.0,
    "sl_pct": 1.0,
    "max_trades_per_day": 10,
    "max_buy_trades": 5,
    "max_sell_trades": 5,
    "rsi_buy_threshold": 55,
    "rsi_sell_threshold": 45,
    "adx_threshold": 25,
    "min_volume": 100000,
    "min_total_value": 100000000,
    "ltp_min": 200,
    "ltp_max": 4000,
    "supertrend_period": 10,
    "supertrend_multiplier": 3,
}

INT_FIELDS = {
    "max_trades_per_day",
    "max_buy_trades",
    "max_sell_trades",
    "supertrend_period",
}


def _normalize(settings: dict) -> dict:
    normalized = {**DEFAULT_SETTINGS, **settings}
    for key in DEFAULT_SETTINGS:
        value = normalized.get(key)
        if key in INT_FIELDS:
            normalized[key] = int(value)
        else:
            normalized[key] = float(value)
    return normalized


def get_settings(algo_id: str) -> dict:
    """Read settings for this algo from Supabase. Fall back to hardcoded defaults if missing."""
    from app.supabase_client import supabase

    result = supabase.table("strategy_settings").select("*").eq("algo_id", algo_id).execute()
    if result.data:
        return _normalize(result.data[0])
    return _normalize({})


def update_settings(algo_id: str, settings: dict):
    """Write updated settings back to Supabase."""
    from app.supabase_client import supabase

    supabase.table("strategy_settings").upsert({
        "algo_id": algo_id,
        **settings,
        "updated_at": "now()",
    }).execute()
