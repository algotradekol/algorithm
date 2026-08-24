"""Shared point-based trailing-stop calculations.

Silver Micro uses a profit-lock staircase rather than a fixed distance behind
the best price. Keeping the arithmetic here makes paper, live, and backtest
execution agree on the same stop levels.
"""

from __future__ import annotations

import math


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
