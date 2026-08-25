import datetime

from .runtime_mode import get_runtime_trading_mode, normalize_trading_mode
from .storage_namespace import namespaced_value

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
        # - target_points / sl_points are the only exit distances. The
        #   optional breakeven mode moves SL to entry once target is reached.
        "silver_breakout_points": 150,
        # Silver BUY uses the finalized 15m green-above-EMA reference.
        "silver_buy_plan": "reference_breakout",
        "sl_points": 100,
        "target_points": 300,
        "exit_mode": "fixed_target_sl",
        # Position sizing for MCX Silver Micro is done in LOTS, not by
        # dividing capital by price. 1 lot of SILVERMIC = 1 kg = 1 unit
        # on Fyers. Client trades in lots; default 1.
        "silver_lots": 1,
        # Client wants market orders by default so gap-through /
        # candle-close triggers actually fill instead of chasing LIMIT.
        "order_type": "MARKET",
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
    "tsl_activate_points",
    "tsl_profit_step_points",
    "tsl_lock_step_points",
    "tsl_trigger_points",
    "tsl_distance_points",
    "silver_lots",
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
    "silver_buy_plan",
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
    "target_to_breakeven_sl",
}


def default_settings_for(algo_id: str) -> dict:
    return {**DEFAULT_SETTINGS, **STRATEGY_DEFAULT_OVERRIDES.get(algo_id, {})}


def get_settings_storage_key(algo_id: str, mode: str | None = None) -> str:
    """Per-deployment, per-mode settings key.

    The frontend promises that paper/live settings save independently, so the
    storage key must include the active runtime mode as well as the deployment
    namespace. This keeps the paper scan toggle from re-enabling live trading
    after a mode switch, while still preserving historical shared rows as a
    read-only fallback until each mode saves once.
    """
    normalized_algo_id = str(algo_id or "").strip()
    effective_mode = normalize_trading_mode(mode or get_runtime_trading_mode())
    return namespaced_value(f"{normalized_algo_id}__{effective_mode}")


def _normalize(settings: dict, algo_id: str) -> dict:
    defaults = default_settings_for(algo_id)
    normalized = {**defaults, **settings}
    if algo_id == "algo3":
        legacy_exit_mode = str(settings.get("exit_mode") or "")
        # A pre-upgrade open position has no immutable policy snapshot. Keep
        # its old policy available only to the position runtime; new entries
        # are always stamped with one of the simplified policies below.
        if legacy_exit_mode in {"trailing_sl_only", "fixed_target_trailing_sl"}:
            normalized["_legacy_silver_open_position_exit_mode"] = legacy_exit_mode
            normalized["_legacy_silver_open_position_trailing_enabled"] = bool(
                settings.get("trailing_sl_enabled")
            )
        # New Silver entries use only fixed target/SL or target-to-breakeven.
        # Old point-lock modes are intentionally normalized to the safer
        # fixed policy; legacy open positions retain their snapshot policy.
        if str(normalized.get("exit_mode") or "") not in {"fixed_target_sl", "target_to_breakeven_sl"}:
            normalized["exit_mode"] = "fixed_target_sl"
        normalized["trailing_sl_enabled"] = False
    # Silver has one canonical BUY model. Old values are compatibility aliases
    # and must not reactivate the removed 5m confirmation path.
    if algo_id == "algo3":
        normalized["silver_buy_plan"] = "reference_breakout"
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


def get_settings(algo_id: str, mode: str | None = None) -> dict:
    """Read settings for this algo from Supabase. Fall back to hardcoded defaults if missing."""
    from .supabase_client import run_with_supabase

    effective_mode = normalize_trading_mode(mode or get_runtime_trading_mode())
    storage_key = get_settings_storage_key(algo_id, mode=effective_mode)
    result = run_with_supabase(
        lambda supabase: supabase.table("strategy_settings").select("*").eq("algo_id", storage_key).execute()
    )
    if result.data:
        return _normalize(result.data[0], algo_id)
    # Backward-compatible fallbacks:
    # 1. deployment-scoped legacy shared row: algo3__client
    # 2. historical global shared row: algo3
    # Only hydrate from these when the mode-specific row does not exist yet.
    legacy_candidates: list[str] = []
    deployment_legacy_key = namespaced_value(str(algo_id or "").strip())
    if deployment_legacy_key != storage_key:
        legacy_candidates.append(deployment_legacy_key)
    plain_legacy_key = str(algo_id or "").strip()
    if plain_legacy_key not in legacy_candidates and plain_legacy_key != storage_key:
        legacy_candidates.append(plain_legacy_key)
    for legacy_key in legacy_candidates:
        legacy_result = run_with_supabase(
            lambda supabase, key=legacy_key: supabase.table("strategy_settings").select("*").eq("algo_id", key).execute()
        )
        if legacy_result.data:
            return _normalize(legacy_result.data[0], algo_id)
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
    "tsl_activate_points",
    "tsl_profit_step_points",
    "tsl_lock_step_points",
    "tsl_trigger_points",
    "tsl_distance_points",
    "silver_lots",
    "silver_buy_plan",
}


def _upsert_settings_with_fallback(algo_id: str, settings: dict, mode: str | None = None) -> list[str]:
    """Try to write every setting; if Supabase rejects an unknown column,
    strip that column from the payload and retry. Prevents the whole
    save from silently failing when a new field hasn't been migrated yet."""
    from .supabase_client import run_with_supabase

    payload = {
        "algo_id": get_settings_storage_key(algo_id, mode=mode),
        **settings,
        # PostgREST treats "now()" as a literal string, not a SQL function.
        # Send a real RFC3339 value so a timestamptz column always accepts it.
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    dropped: list[str] = []
    while True:
        try:
            run_with_supabase(lambda supabase: supabase.table("strategy_settings").upsert(payload).execute())
            return dropped
        except Exception as exc:
            text = str(exc).lower()
            rejected = [
                col for col in _NEW_COLUMNS_TOLERATE_MISSING
                if col in text and col in payload
            ]
            if not rejected:
                raise
            for col in rejected:
                payload.pop(col, None)
                dropped.append(col)
            print(
                "[strategy_settings] Supabase rejected missing column(s) "
                f"{rejected}; retrying remaining fields. Run the Silver settings migration "
                "to persist these settings."
            )


def update_settings(algo_id: str, settings: dict, mode: str | None = None):
    """Write updated settings back to Supabase and report schema gaps."""
    settings = _normalize(settings, algo_id)
    missing_columns = _upsert_settings_with_fallback(algo_id, settings, mode=mode)
    # Read through the same mode/deployment namespace used by the runtime so
    # the UI never claims an ephemeral value has been durably persisted.
    return {
        "settings": get_settings(algo_id, mode=mode),
        "missing_columns": missing_columns,
    }


def reset_settings(algo_id: str, mode: str | None = None) -> dict:
    """Restore the strategy to its default Tradetron-style settings."""
    settings = _normalize({}, algo_id)
    _upsert_settings_with_fallback(algo_id, settings, mode=mode)
    return settings
