import datetime


DEFAULT_SETTINGS = {
    "starting_capital": 500000,
    "capital_per_trade": 50000,
    "margin_multiplier": 5,
    "scan_enabled": True,
    "target_pct": 2.0,
    "sl_pct": 1.0,
    "exit_mode": "fixed_target_sl",
    "trailing_sl_enabled": False,
    "trailing_sl_trigger_pct": 1.0,
    "trailing_sl_distance_pct": 0.5,
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
    "filter_vwap": True,
    "filter_rsi": True,
    "filter_adx": True,
    "filter_supertrend": True,
    "filter_ema20": False,
    "filter_ema50": False,
    "filter_volume": True,
    "filter_liquidity": True,
    "filter_price_range": True,
    "test_schedule_enabled": False,
    "test_candle_time": "11:10",
    "order_type": "LIMIT",
    # When True AND active mode is LIVE, every algo entry ALSO simulates a
    # paper trade with fake money in the paper tables. The paper trade uses
    # the same signal + qty math but never hits Fyers — pure DB simulation.
    # Provides a live-vs-paper side-by-side without disrupting live trading.
    # Ignored when active mode is PAPER (paper is already the primary).
    "parallel_paper_enabled": True,
}

ORDER_TYPES = {"LIMIT", "MARKET"}

STRATEGY_DEFAULT_OVERRIDES = {
    "algo1": {
        "exit_mode": "fixed_target_sl",
        "trailing_sl_enabled": False,
        "max_trades_per_day": 10,
        "max_buy_trades": 5,
        "max_sell_trades": 5,
    },
    "algo2": {
        "scan_enabled": False,
        "exit_mode": "fixed_target_sl",
        "trailing_sl_enabled": False,
        "max_trades_per_day": 10,
        "max_buy_trades": 5,
        "max_sell_trades": 5,
        "rsi_buy_threshold": 50,
        "rsi_sell_threshold": 50,
        "adx_threshold": 20,
        # The imported UN1 v14 rules only use liquidity, volume, and price.
        # Keep the advanced controls available, but start them disabled so a
        # reset produces the documented Tradetron-compatible filter profile.
        "filter_vwap": False,
        "filter_rsi": False,
        "filter_adx": False,
        "filter_supertrend": False,
        "filter_ema20": False,
        "filter_ema50": False,
        "filter_volume": True,
        "filter_liquidity": True,
        "filter_price_range": True,
        "min_volume": 100000,
        "min_total_value": 100000000,
        "ltp_min": 200,
        "ltp_max": 4000,
        "supertrend_period": 10,
        "supertrend_multiplier": 3,
    },
    "algo3": {
        "scan_enabled": False,
        # Points-based per spec doc (2026-08-19 rewrite):
        # - n: breakout offset above/below the stored setup close. Entry
        #   fires when live LTP crosses (setup_close +/- n) in the setup
        #   direction. Default 150 = doc's example value; user can tune.
        # - sl_points / target_points: fixed rupee distance from entry.
        # - tsl_trigger_points: how many points in profit before TSL
        #   activates. tsl_distance_points: how far behind the current
        #   high/low the TSL sits once active.
        "silver_breakout_points": 150,
        "sl_points": 100,
        "target_points": 300,
        "trailing_sl_enabled": False,
        "tsl_trigger_points": 100,
        "tsl_distance_points": 50,
        "exit_mode": "fixed_target_trailing_sl",
    },
}

INT_FIELDS = {
    "max_trades_per_day",
    "max_buy_trades",
    "max_sell_trades",
    "supertrend_period",
    "silver_breakout_points",
    "sl_points",
    "target_points",
    "tsl_trigger_points",
    "tsl_distance_points",
}

BOOL_FIELDS = {
    "scan_enabled",
    "parallel_paper_enabled",
    "filter_vwap",
    "filter_rsi",
    "filter_adx",
    "filter_supertrend",
    "filter_ema20",
    "filter_ema50",
    "filter_volume",
    "filter_liquidity",
    "filter_price_range",
    "trailing_sl_enabled",
    "test_schedule_enabled",
}

TEXT_FIELDS = {
    "exit_mode",
    "test_candle_time",
    "order_type",
}

# Rupee amounts are stored to paise precision. Percentages and multipliers are
# intentionally retained to four decimals for strategy tuning.
RUPEE_FIELDS = {
    "starting_capital",
    "capital_per_trade",
    "min_total_value",
    "ltp_min",
    "ltp_max",
}

EXIT_MODES = {
    "fixed_target_sl",
    "trailing_sl_only",
    "fixed_target_trailing_sl",
}


def default_settings_for(algo_id: str) -> dict:
    return {**DEFAULT_SETTINGS, **STRATEGY_DEFAULT_OVERRIDES.get(algo_id, {})}


def _normalize(settings: dict, algo_id: str) -> dict:
    defaults = default_settings_for(algo_id)
    normalized = {**defaults, **settings}
    for key in defaults:
        value = normalized.get(key)
        if key in BOOL_FIELDS:
            normalized[key] = bool(value)
        elif key in TEXT_FIELDS:
            normalized[key] = str(value or defaults[key])
            if key == "exit_mode" and normalized[key] not in EXIT_MODES:
                normalized[key] = defaults[key]
            if key == "test_candle_time":
                try:
                    datetime.datetime.strptime(normalized[key], "%H:%M")
                except ValueError:
                    normalized[key] = defaults[key]
            if key == "order_type":
                normalized[key] = normalized[key].upper()
                if normalized[key] not in ORDER_TYPES:
                    normalized[key] = defaults[key]
        elif key in INT_FIELDS:
            normalized[key] = int(value)
        else:
            number = float(value)
            normalized[key] = round(number, 2) if key in RUPEE_FIELDS else round(number, 4)
    return normalized


def get_settings(algo_id: str) -> dict:
    """Read settings for this algo from Supabase. Fall back to hardcoded defaults if missing."""
    from .supabase_client import supabase

    result = supabase.table("strategy_settings").select("*").eq("algo_id", algo_id).execute()
    if result.data:
        return _normalize(result.data[0], algo_id)
    return _normalize({}, algo_id)


# Fields added AFTER the initial Supabase schema. If the DB hasn't been
# migrated to include one of these columns, we drop it from the upsert
# payload and continue — no SQL migration required, at the cost of the
# setting being ephemeral (falls back to the DEFAULT_SETTINGS value on
# next read). Add to this set when introducing a new column that clients
# might not have migrated yet.
_NEW_COLUMNS_TOLERATE_MISSING = {
    "order_type",             # added 2026-08-10
    "parallel_paper_enabled", # added 2026-08-13
    # algo3 spec-doc rewrite (2026-08-19). Points-based risk fields
    # replace percent fields for Silver Micro only.
    "silver_breakout_points",
    "sl_points",
    "target_points",
    "tsl_trigger_points",
    "tsl_distance_points",
}


def _upsert_settings_with_fallback(algo_id: str, settings: dict) -> None:
    """Try to write every setting; if Supabase rejects an unknown column,
    strip that column from the payload and retry once. Prevents the whole
    save from silently failing when a new field hasn't been migrated yet."""
    from .supabase_client import supabase

    payload = {"algo_id": algo_id, **settings, "updated_at": "now()"}
    try:
        supabase.table("strategy_settings").upsert(payload).execute()
        return
    except Exception as exc:
        text = str(exc).lower()
        dropped = []
        for col in list(_NEW_COLUMNS_TOLERATE_MISSING):
            if col in text and col in payload:
                payload.pop(col, None)
                dropped.append(col)
        if not dropped:
            raise
        print(f"[strategy_settings] Supabase rejected column(s) {dropped}; retrying without them. To make these persist, run: ALTER TABLE public.strategy_settings ADD COLUMN IF NOT EXISTS {dropped[0]} ...")
        supabase.table("strategy_settings").upsert(payload).execute()


def update_settings(algo_id: str, settings: dict):
    """Write updated settings back to Supabase."""
    settings = _normalize(settings, algo_id)
    _upsert_settings_with_fallback(algo_id, settings)


def reset_settings(algo_id: str) -> dict:
    """Restore the strategy to its default Tradetron-style settings."""
    settings = _normalize({}, algo_id)
    _upsert_settings_with_fallback(algo_id, settings)
    return settings
