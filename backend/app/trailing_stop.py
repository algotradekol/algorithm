"""Shared point-based trailing-stop calculations.

Silver Micro uses a profit-lock staircase rather than a fixed distance behind
the best price. Keeping the arithmetic here makes paper, live, and backtest
execution agree on the same stop levels.
"""

from __future__ import annotations

import math


SILVER_EXIT_MODE_FIXED_TARGET_SL = "fixed_target_sl"
SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN = "target_to_breakeven_sl"
SILVER_CANDLE_PAIR_TSL = "candle_pair_tsl_v1"


def normalize_silver_exit_mode(value: object) -> str:
    """Return one of the two supported Silver exit policies.

    Silver used to expose several point-lock trailing variants. Those values
    deliberately fall back to the safer fixed target/SL policy for new trades.
    Existing positions keep their entry-time policy in ``signal_snapshot``.
    """
    if str(value or "").strip() == SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN:
        return SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN
    return SILVER_EXIT_MODE_FIXED_TARGET_SL


def silver_position_exit_mode(position: dict | None, settings: dict) -> str:
    """Read the immutable Silver policy captured when a position was opened."""
    snapshot = (position or {}).get("signal_snapshot") or {}
    saved = snapshot.get("silver_exit_policy") if isinstance(snapshot, dict) else None
    if saved:
        return normalize_silver_exit_mode(saved)
    return normalize_silver_exit_mode(settings.get("exit_mode"))


def uses_silver_breakeven_stop(position: dict | None, settings: dict) -> bool:
    return silver_position_exit_mode(position, settings) == SILVER_EXIT_MODE_TARGET_TO_BREAKEVEN


def uses_silver_candle_pair_tsl(position: dict | None) -> bool:
    """Whether a newly-opened Silver Micro 2.0 position uses pair trailing."""
    snapshot = (position or {}).get("signal_snapshot") or {}
    pair_tsl = snapshot.get("silver_candle_pair_tsl") if isinstance(snapshot, dict) else None
    return isinstance(pair_tsl, dict) and pair_tsl.get("policy") == SILVER_CANDLE_PAIR_TSL


def calculate_candle_pair_trailing(
    *,
    side: str,
    first_bar: dict,
    second_bar: dict,
    buffer_points: float,
) -> dict | None:
    """Return the 2.0 candle-pair trailing candidate, or ``None``.

    BUY accepts a consecutive red then green pair only when both closes are
    above their own finalized EMA20, then follows the lower low. SELL mirrors
    that with green then red below their own EMA20. Dojis and EMA-touching
    candles are intentionally excluded from both patterns.
    """
    side = str(side or "").upper()
    first_open = float(first_bar["open"])
    first_close = float(first_bar["close"])
    second_open = float(second_bar["open"])
    second_close = float(second_bar["close"])
    first_ema20 = first_bar.get("ema20")
    second_ema20 = second_bar.get("ema20")
    buffer_points = float(buffer_points)
    first_time = first_bar.get("time")
    second_time = second_bar.get("time")
    if hasattr(first_time, "__sub__") and hasattr(second_time, "__sub__"):
        try:
            if (second_time - first_time).total_seconds() != 15 * 60:
                return None
        except (TypeError, ValueError):
            return None

    # Pair trailing is deliberately stricter than the color-only pattern:
    # both completed bars must confirm the open position's EMA direction.
    try:
        first_ema20 = float(first_ema20)
        second_ema20 = float(second_ema20)
    except (TypeError, ValueError):
        return None

    if side == "BUY":
        if not (
            first_open > first_close
            and second_open < second_close
            and first_close > first_ema20
            and second_close > second_ema20
        ):
            return None
        reference_price = min(float(first_bar["low"]), float(second_bar["low"]))
        candidate_sl = reference_price - buffer_points
        pattern = "red_green"
    elif side == "SELL":
        if not (
            first_open < first_close
            and second_open > second_close
            and first_close < first_ema20
            and second_close < second_ema20
        ):
            return None
        reference_price = max(float(first_bar["high"]), float(second_bar["high"]))
        candidate_sl = reference_price + buffer_points
        pattern = "green_red"
    else:
        return None

    return {
        "pattern": pattern,
        "reference_price": reference_price,
        "buffer_points": buffer_points,
        "candidate_sl": candidate_sl,
        "first_ema20": first_ema20,
        "second_ema20": second_ema20,
        "first_bar": {
            key: (
                first_bar.get(key).isoformat()
                if key == "time" and hasattr(first_bar.get(key), "isoformat")
                else first_bar.get(key)
            )
            for key in ("time", "open", "high", "low", "close")
        },
        "second_bar": {
            key: (
                second_bar.get(key).isoformat()
                if key == "time" and hasattr(second_bar.get(key), "isoformat")
                else second_bar.get(key)
            )
            for key in ("time", "open", "high", "low", "close")
        },
    }


def silver_tsl_points(settings: dict) -> tuple[float, float, float]:
    """Return (activation, profit_step, lock_step) in price points.

    The old two-field settings remain accepted for old backtest payloads and
    rows that have not been normalized yet. The new model's profit step falls
    back to activation when only the old trigger exists.
    """
    activate = settings.get("tsl_activate_points")
    profit_step = settings.get("tsl_profit_step_points")
    lock_step = settings.get("tsl_lock_step_points")

    if activate is None:
        activate = settings.get("tsl_trigger_points", 0)
    if profit_step is None:
        profit_step = activate
    if lock_step is None:
        lock_step = settings.get("tsl_distance_points", 0)

    return float(activate or 0), float(profit_step or 0), float(lock_step or 0)


def calculate_point_trailing(
    *,
    entry: float,
    side: str,
    current_sl: float,
    highest: float,
    lowest: float,
    activate_points: float,
    profit_step_points: float,
    lock_step_points: float,
) -> dict:
    """Calculate one monotonic point-lock update.

    At activation the stop is breakeven. Each complete profit interval after
    activation locks one additional ``lock_step_points``. A retracement never
    loosens the already protected stop.
    """
    entry = float(entry)
    current_sl = float(current_sl)
    highest = max(float(highest), entry)
    lowest = min(float(lowest), entry)
    side = str(side or "").upper()
    if side not in {"BUY", "SELL"}:
        return {
            "highest": highest,
            "lowest": lowest,
            "gain_points": 0.0,
            "trailing_active": False,
            "sl_price": current_sl,
            "sl_moved": False,
            "previous_sl": current_sl,
            "protected_points": 0.0,
            "step_index": 0,
        }

    if side == "BUY":
        gain = max(0.0, highest - entry)
    else:
        gain = max(0.0, entry - lowest)

    active = activate_points > 0 and gain + 1e-9 >= activate_points
    if not active or profit_step_points <= 0:
        return {
            "highest": highest,
            "lowest": lowest,
            "gain_points": gain,
            "trailing_active": False,
            "sl_price": current_sl,
            "sl_moved": False,
            "previous_sl": current_sl,
            "protected_points": 0.0,
            "step_index": 0,
        }

    step_index = int(math.floor(max(0.0, gain - activate_points) / profit_step_points + 1e-9))
    protected_points = max(0.0, step_index * max(0.0, lock_step_points))
    candidate = entry + protected_points if side == "BUY" else entry - protected_points
    sl_moved = candidate > current_sl + 1e-9 if side == "BUY" else candidate < current_sl - 1e-9
    new_sl = candidate if sl_moved else current_sl
    return {
        "highest": highest,
        "lowest": lowest,
        "gain_points": gain,
        "trailing_active": True,
        "sl_price": new_sl,
        "sl_moved": sl_moved,
        "previous_sl": current_sl,
        "protected_points": protected_points,
        "step_index": step_index,
    }
