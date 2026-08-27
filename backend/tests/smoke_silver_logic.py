"""
smoke_silver_logic.py — focused Silver Micro strategy regression suite.

This is the "smoke test 2" runner for the core algo3 candle/reference logic.
It reuses the proven offline fixtures from ``tests.smoke_live_orders`` but
executes only the Silver-specific scenarios we care about when validating:

  * 15-minute setup capture rules
  * BUY reference persistence / rollover
  * SELL red-chain reference carry / replacement
  * immediate same-reference re-entry after exits
  * prior-day gap-through behavior
  * reversal and mode-switch behavior
  * backtest/live parity for the Silver logic

Run:
    python -m tests.smoke_silver_logic

Exit code 0 = all checks passed, 1 = at least one check failed.
"""
from __future__ import annotations

import sys
from collections.abc import Callable

from tests import smoke_live_orders as slo


SILVER_LOGIC_SUITE: list[Callable[[], None]] = [
    # 15m aggregation + verified setup formation.
    slo.test_algo3_bucket_start_15m,
    slo.test_algo3_ema_step_matches_python_reference,
    slo.test_algo3_current_minute_is_not_treated_as_closed,
    slo.test_algo3_partial_15m_bucket_is_not_finalized_on_warmup_tail,
    slo.test_algo3_closed_15m_bucket_finalizes_without_next_bucket_tick,
    slo.test_algo3_clock_finalization_waits_for_fyers_settle_window,
    slo.test_algo3_unverified_local_bar_never_becomes_a_setup_reference,
    slo.test_algo3_live_15m_setup_uses_fyers_verified_bar_close,
    slo.test_algo3_live_15m_sell_setup_uses_fyers_verified_bar_close,
    slo.test_algo3_setup_captures_and_overwrites,
    slo.test_algo3_no_setup_when_wrong_side_of_ema,
    slo.test_algo3_setup_persistence_rejects_wrong_candle_color,
    # SELL red-chain semantics.
    slo.test_algo3_sell_reference_survives_green_candles_and_rearms_on_new_red,
    slo.test_algo3_sell_reference_shifts_when_gap_is_under_n,
    slo.test_algo3_live_red_chain_enters_on_forming_candle_cross,
    slo.test_algo3_sell_does_not_fire_from_tick_cross,
    # BUY trigger semantics.
    slo.test_algo3_buy_trigger_only_on_upward_cross,
    slo.test_algo3_configurable_n_parameter,
    # Reversal / re-entry / mode-switch behavior.
    slo.test_algo3_reversal_on_contra_signal,
    slo.test_algo3_no_reentry_same_side,
    slo.test_algo3_unlimited_reentry_after_exit_same_setup,
    slo.test_algo3_manual_exit_safe_mode_clears_handoff_and_requires_fresh_trigger,
    slo.test_algo3_mode_switch_keeps_reference_but_clears_previous_mode_fired_state,
    slo.test_algo3_sell_target_reenters_when_reference_still_crossed,
    slo.test_algo3_sell_stop_does_not_reenter_above_old_trigger,
    slo.test_algo3_live_buy_reference_reentry_and_rollover,
    # Carry-forward / session-open behavior.
    slo.test_algo3_gap_through_fires_immediately,
    slo.test_algo3_previous_day_buy_setup_gap_open_fires_immediately,
    slo.test_algo3_previous_day_sell_setup_gap_open_fires_immediately,
    # Backtest/live contract checks for the same strategy rules.
    slo.test_algo3_black_box_end_to_end,
    slo.test_algo3_backtest_parity_with_live,
    slo.test_algo3_backtest_buy_reference_breakout_contract,
    slo.test_algo3_backtest_buy_plan_is_15m_reference_only,
    slo.test_algo3_backtest_buy_reenters_after_target_in_same_15m_candle,
    slo.test_algo3_backtest_sell_red_chain_survives_green_candles,
    slo.test_algo3_backtest_sell_reentry_requires_carried_trigger,
    slo.test_algo3_backtest_fixed_target_mode_keeps_fixed_stop,
    slo.test_algo3_backtest_sell_breakeven_exits_on_reversal,
]


def _run_test(test_func: Callable[[], None]) -> None:
    try:
        test_func()
    except Exception as exc:  # pragma: no cover - smoke runner safety net
        slo._failures += 1
        print(f"  [{slo.FAIL}] {test_func.__name__} crashed  - {exc}")


def main() -> None:
    print("\n==================================================================")
    print("  SILVER MICRO LOGIC SMOKE TEST 2")
    print("  Focused reference / trigger / re-entry / parity regression suite")
    print("==================================================================")

    slo._failures = 0
    for test_func in SILVER_LOGIC_SUITE:
        _run_test(test_func)

    print("\n==================================================================")
    if slo._failures:
        print(f"  RESULT: {slo._failures} CHECK(S) FAILED")
        raise SystemExit(1)
    print("  RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main())
