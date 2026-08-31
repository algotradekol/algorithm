"""Focused offline smoke checks for Silver Micro 2.0 EMA-wick references.

Run with: python -m tests.smoke_silver_micro_2
"""
from __future__ import annotations

import datetime
import threading
from collections import deque

from app.strategies.algo5_silver_micro_2 import Algo5SilverMicro2


def check(name: str, passed: bool, detail: str = "") -> None:
    if not passed:
        raise AssertionError(f"{name}: {detail}")
    print(f"  [PASS] {name}")


def make_strategy() -> Algo5SilverMicro2:
    strategy = object.__new__(Algo5SilverMicro2)
    strategy.algo_id = "algo5"
    strategy.symbol = "MCX:SILVERMIC26AUGFUT"
    strategy.settings = {
        "silver_breakout_points": 200,
        "ema_wick_distance_points": 300,
        "trading_enabled": True,
    }
    strategy._ema20 = 1000.0
    strategy._bars = deque(maxlen=500)
    strategy._buy_setup_close = None
    strategy._sell_setup_close = None
    strategy._buy_setup_bar_at = None
    strategy._sell_setup_bar_at = None
    strategy._buy_setup_family = None
    strategy._sell_setup_family = None
    strategy._buy_reentry_after_exit = None
    strategy._sell_reentry_after_exit = None
    strategy._last_fired_buy_bar_at = None
    strategy._last_fired_sell_bar_at = None
    strategy._last_attempted_buy_bar_at = None
    strategy._last_attempted_sell_bar_at = None
    strategy._entry_guard_lock = threading.Lock()
    strategy._entry_attempt_in_flight = False
    strategy._entry_cooldown_until_monotonic = 0.0
    strategy._sl_cooldown_until_monotonic = 0.0
    strategy._prev_ltp = 0.0
    return strategy


def test_current_rules_remain_first_choice() -> None:
    strategy = make_strategy()
    buy = {"open": 1050, "high": 1400, "low": 900, "close": 1200}
    sell = {"open": 950, "high": 1100, "low": 600, "close": 800}
    check("current BUY remains current", strategy._buy_setup_family_for(buy) == "current")
    check("current SELL remains current", strategy._sell_setup_family_for(sell) == "current")


def test_fallback_references_and_distance_gate() -> None:
    strategy = make_strategy()
    fallback_buy = {"open": 1250, "high": 1270, "low": 980, "close": 1100}
    fallback_sell = {"open": 750, "high": 1010, "low": 700, "close": 900}
    too_far_buy = {"open": 1450, "high": 1460, "low": 1301, "close": 1200}
    too_far_sell = {"open": 700, "high": 699, "low": 500, "close": 800}
    check("red-above EMA wick BUY qualifies", strategy._buy_setup_family_for(fallback_buy) == "fallback_ema_wick")
    check("green-below EMA wick SELL qualifies", strategy._sell_setup_family_for(fallback_sell) == "fallback_ema_wick")
    check("BUY wick beyond configured distance is rejected", strategy._buy_setup_family_for(too_far_buy) is None)
    check("SELL wick beyond configured distance is rejected", strategy._sell_setup_family_for(too_far_sell) is None)


def test_selected_reference_uses_unchanged_trigger_formula() -> None:
    strategy = make_strategy()
    at = datetime.datetime(2026, 9, 1, 10, 0)
    strategy._update_setups({"open": 1250, "high": 1270, "low": 980, "close": 1100, "time": at})
    check("fallback BUY is stored", strategy._buy_setup_close == 1100)
    check("fallback BUY trigger is close plus n", strategy._buy_setup_close + 200 == 1300)

    strategy._update_setups({"open": 750, "high": 1010, "low": 700, "close": 900, "time": at})
    check("fallback SELL is stored", strategy._sell_setup_close == 900)
    check("fallback SELL trigger is close minus n", strategy._sell_setup_close - 200 == 700)


def test_candle_close_trigger_uses_fallback_qualification() -> None:
    strategy = make_strategy()
    strategy._buy_setup_close = 1100
    strategy._buy_setup_bar_at = datetime.datetime(2026, 9, 1, 9, 45)
    strategy._buy_setup_family = "current"
    calls: list[tuple[str, float, float]] = []
    strategy._fire_entry = lambda side, ltp, trigger_level, **_: calls.append((side, ltp, trigger_level)) or True
    strategy._mark_fired = lambda *_, **__: None

    bar = {
        "open": 1350,
        "high": 1400,
        "low": 990,
        "close": 1300,
        "time": datetime.datetime(2026, 9, 1, 10, 0),
    }
    strategy._check_candle_close_trigger(bar)
    check("fallback BUY fires at close plus n", calls == [("BUY", 1300.0, 1300.0)], str(calls))


def test_fallback_sell_tick_cross_fires_from_green_candle() -> None:
    strategy = make_strategy()
    strategy._sell_setup_close = 900
    strategy._sell_setup_bar_at = datetime.datetime(2026, 9, 1, 9, 45)
    strategy._sell_setup_family = "fallback_ema_wick"
    strategy._current_bucket = datetime.datetime(2026, 9, 1, 10, 0)
    strategy._minute_buffer = [{"open": 650}]
    strategy._prev_ltp = 701
    calls: list[tuple[str, float, float]] = []
    strategy._fire_entry = lambda side, ltp, trigger_level, **_: calls.append((side, ltp, trigger_level)) or True
    strategy._mark_fired = lambda *_, **__: None

    strategy._check_triggers(700, event_time=datetime.datetime(2026, 9, 1, 10, 1))
    check("fallback SELL tick-cross fires from green candle", calls == [("SELL", 700, 700.0)], str(calls))


def test_audit_identifies_fallback_reference() -> None:
    strategy = make_strategy()
    strategy._buy_setup_close = 1100
    strategy._buy_setup_family = "fallback_ema_wick"
    audit = strategy._entry_trigger("BUY", 1300, 1300)
    check("audit names fallback family", "EMA-wick fallback" in audit, audit)


def main() -> None:
    print("\nSILVER MICRO 2.0 EMA-WICK SMOKE TEST")
    test_current_rules_remain_first_choice()
    test_fallback_references_and_distance_gate()
    test_selected_reference_uses_unchanged_trigger_formula()
    test_candle_close_trigger_uses_fallback_qualification()
    test_fallback_sell_tick_cross_fires_from_green_candle()
    test_audit_identifies_fallback_reference()
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
