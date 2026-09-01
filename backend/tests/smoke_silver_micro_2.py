"""Focused offline smoke checks for Silver Micro 2.0 EMA-wick references.

Run with: python -m tests.smoke_silver_micro_2
"""
from __future__ import annotations

import datetime
import threading
from collections import deque
from types import SimpleNamespace

import app.live_broker as live_broker_module
import app.paper_broker as paper_broker_module
from app.backtest import _simulate_silver_micro_range
from app.charges import get_charges_config
from app.live_broker import LiveBroker
from app.strategies.algo5_silver_micro_2 import Algo5SilverMicro2
from app.trailing_stop import calculate_candle_pair_trailing


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
    strategy._setup_references = strategy._empty_setup_references()
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


def test_each_reference_family_is_stored_independently() -> None:
    strategy = make_strategy()
    at = datetime.datetime(2026, 9, 1, 10, 0)
    strategy._update_setups({"open": 900, "high": 1250, "low": 850, "close": 1200, "time": at})
    strategy._update_setups({"open": 1250, "high": 1270, "low": 980, "close": 1100, "time": at})
    strategy._update_setups({"open": 1100, "high": 1150, "low": 700, "close": 800, "time": at})
    strategy._update_setups({"open": 750, "high": 1010, "low": 700, "close": 900, "time": at})
    refs = strategy._setup_references
    check("current BUY remains separately stored", refs["BUY"]["current"]["close"] == 1200)
    check("fallback BUY remains separately stored", refs["BUY"]["fallback_ema_wick"]["close"] == 1100)
    check("current SELL remains separately stored", refs["SELL"]["current"]["close"] == 800)
    check("fallback SELL remains separately stored", refs["SELL"]["fallback_ema_wick"]["close"] == 900)


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


def test_candle_pair_tsl_candidates_support_latest_pair_replacement() -> None:
    red = _replay_bar(datetime.datetime(2026, 9, 1, 10, 0), 1100, 1120, 900, 1000, ema20=950)
    green = _replay_bar(datetime.datetime(2026, 9, 1, 10, 15), 1000, 1080, 940, 1050, ema20=1000)
    buy = calculate_candle_pair_trailing(
        side="BUY", first_bar=red, second_bar=green, buffer_points=100
    )
    check("BUY red-green pair uses lower low minus buffer", buy is not None and buy["candidate_sl"] == 800, str(buy))
    weaker_buy = calculate_candle_pair_trailing(
        side="BUY",
        first_bar=_replay_bar(datetime.datetime(2026, 9, 1, 10, 30), 1100, 1110, 760, 900, ema20=850),
        second_bar=_replay_bar(datetime.datetime(2026, 9, 1, 10, 45), 900, 940, 780, 930, ema20=900),
        buffer_points=100,
    )
    check("BUY later pair can replace SL lower", weaker_buy is not None and weaker_buy["candidate_sl"] < buy["candidate_sl"], str(weaker_buy))

    sell_green = _replay_bar(datetime.datetime(2026, 9, 1, 11, 0), 900, 1100, 850, 1000, ema20=1050)
    sell_red = _replay_bar(datetime.datetime(2026, 9, 1, 11, 15), 1000, 1080, 820, 900, ema20=950)
    sell = calculate_candle_pair_trailing(
        side="SELL", first_bar=sell_green, second_bar=sell_red, buffer_points=100
    )
    check("SELL green-red pair uses higher high plus buffer", sell is not None and sell["candidate_sl"] == 1200, str(sell))
    weaker_sell = calculate_candle_pair_trailing(
        side="SELL",
        first_bar=_replay_bar(datetime.datetime(2026, 9, 1, 11, 30), 900, 1150, 850, 1000, ema20=1050),
        second_bar=_replay_bar(datetime.datetime(2026, 9, 1, 11, 45), 1000, 1130, 820, 900, ema20=950),
        buffer_points=100,
    )
    check("SELL later pair can replace SL higher", weaker_sell is not None and weaker_sell["candidate_sl"] > sell["candidate_sl"], str(weaker_sell))

    doji = _replay_bar(datetime.datetime(2026, 9, 1, 12, 0), 1000, 1050, 950, 1000, ema20=900)
    check("doji pair is ignored", calculate_candle_pair_trailing(side="BUY", first_bar=doji, second_bar=green, buffer_points=100) is None)
    buy_ema_reject = _replay_bar(datetime.datetime(2026, 9, 1, 12, 15), 1100, 1120, 900, 1000, ema20=1000)
    check(
        "BUY pair rejects a candle touching EMA20",
        calculate_candle_pair_trailing(side="BUY", first_bar=buy_ema_reject, second_bar=green, buffer_points=100) is None,
    )
    buy_below_ema = _replay_bar(datetime.datetime(2026, 9, 1, 12, 30), 1100, 1120, 900, 1000, ema20=1001)
    check(
        "BUY pair rejects a candle below EMA20",
        calculate_candle_pair_trailing(side="BUY", first_bar=buy_below_ema, second_bar=green, buffer_points=100) is None,
    )
    sell_ema_reject = _replay_bar(datetime.datetime(2026, 9, 1, 12, 30), 900, 1100, 850, 1000, ema20=1000)
    check(
        "SELL pair rejects a candle touching EMA20",
        calculate_candle_pair_trailing(side="SELL", first_bar=sell_ema_reject, second_bar=sell_red, buffer_points=100) is None,
    )
    sell_above_ema = _replay_bar(datetime.datetime(2026, 9, 1, 12, 45), 900, 1100, 850, 1000, ema20=999)
    check(
        "SELL pair rejects a candle above EMA20",
        calculate_candle_pair_trailing(side="SELL", first_bar=sell_above_ema, second_bar=sell_red, buffer_points=100) is None,
    )


def test_candle_pair_move_is_audited_and_sent_to_live_broker() -> None:
    first = _replay_bar(datetime.datetime(2026, 9, 1, 10, 0), 1100, 1120, 900, 1000, ema20=950)
    second = _replay_bar(datetime.datetime(2026, 9, 1, 10, 15), 1000, 1080, 940, 1050, ema20=1000)
    position = {
        "id": "pair-1",
        "symbol": "MCX:SILVERMIC26AUGFUT",
        "side": "BUY",
        "qty": 1,
        "sl_price": 750.0,
        "trailing_sl_active": True,
        "signal_snapshot": {
            "fyers_sl_order_id": "SL-PAIR-1",
            "silver_candle_pair_tsl": {"policy": "candle_pair_tsl_v1", "buffer_points": 100, "armed": True},
            "trailing": {"activated": True, "events": [], "update_count": 1},
        },
    }
    broker = object.__new__(LiveBroker)
    broker.positions_table_name = lambda: "positions"
    amendments: list[tuple] = []
    broker._modify_slm_order = lambda *args, **kwargs: amendments.append((args, kwargs)) or {"s": "ok"}
    original_paper_run = paper_broker_module.run_with_supabase
    original_live_run = live_broker_module.run_with_supabase
    paper_broker_module.run_with_supabase = lambda _fn: None
    live_broker_module.run_with_supabase = lambda _fn: None
    try:
        updated = broker.apply_candle_pair_trailing_stop(position, 1100, first, second, 100)
        first_amendments = list(amendments)
        weaker_first = _replay_bar(datetime.datetime(2026, 9, 1, 10, 30), 1100, 1110, 760, 900, ema20=850)
        weaker_second = _replay_bar(datetime.datetime(2026, 9, 1, 10, 45), 900, 940, 780, 930, ema20=900)
        replaced = broker.apply_candle_pair_trailing_stop(updated, 1100, weaker_first, weaker_second, 100)
        unchanged = broker.apply_candle_pair_trailing_stop(replaced, 1100, weaker_first, weaker_second, 100)
    finally:
        paper_broker_module.run_with_supabase = original_paper_run
        live_broker_module.run_with_supabase = original_live_run
    event = updated["signal_snapshot"]["trailing"]["events"][-1]
    check("pair trail raises BUY SL", updated["sl_price"] == 800.0, str(updated))
    check("pair trail records both source candles", event["first_bar"]["time"] and event["second_bar"]["time"], str(event))
    check("pair trail sends one FYERS SL amendment", len(first_amendments) == 1 and first_amendments[0][0][1] == 800.0, str(first_amendments))
    check("newer BUY pair replaces SL even when lower", replaced["sl_price"] == 660.0, str(replaced))
    check("newer pair sends one additional FYERS SL amendment", len(amendments) == 2 and amendments[-1][0][1] == 660.0, str(amendments))
    check("same pair is not amended repeatedly", unchanged["sl_price"] == 660.0 and len(amendments) == 2, str(amendments))


def test_pair_already_crossed_exits_immediately() -> None:
    strategy = make_strategy()
    red = _replay_bar(datetime.datetime(2026, 9, 1, 10, 0), 1100, 1120, 1000, 1050, ema20=1000)
    green = _replay_bar(datetime.datetime(2026, 9, 1, 10, 15), 1050, 1080, 1020, 1070, ema20=1020)
    strategy._bars = deque([red, green], maxlen=500)
    closed: list[tuple] = []
    strategy.broker = SimpleNamespace(close_trade=lambda *args: closed.append(args))
    strategy._arm_buy_reentry_after_exit = lambda reason: None
    position = {
        "side": "BUY",
        "sl_price": 1000.0,
        "trailing_sl_active": True,
        "signal_snapshot": {
            "silver_candle_pair_tsl": {"policy": "candle_pair_tsl_v1", "armed": True},
        },
    }
    result = strategy._apply_candle_pair_trailing(position, 850.0)
    check("already-crossed pair SL closes BUY immediately", result is None and len(closed) == 1 and closed[0][2] == "TRAILING_SL", str(closed))


def _replay_bar(
    at: datetime.datetime,
    open_price: float,
    high: float,
    low: float,
    close: float,
    *,
    ema20: float | None = None,
) -> dict:
    return {
        "time": at,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1,
        "ema20": ema20,
    }


def _micro_2_replay_history(target_day: datetime.date, setup: dict, trigger: dict) -> list[dict]:
    warmup_day = target_day - datetime.timedelta(days=1)
    history = []
    for index in range(20):
        at = datetime.datetime.combine(warmup_day, datetime.time(9, 0)) + datetime.timedelta(minutes=15 * index)
        history.append(_replay_bar(at, 1000, 1000, 1000, 1000))
    history.append(_replay_bar(datetime.datetime.combine(target_day, datetime.time(9, 0)), **setup))
    history.append(_replay_bar(datetime.datetime.combine(target_day, datetime.time(9, 15)), **trigger))
    history.append(_replay_bar(datetime.datetime.combine(target_day, datetime.time(9, 30)), 1000, 1000, 1000, 1000))
    return history


def _replay_micro_2(target_day: datetime.date, setup: dict, trigger: dict) -> dict:
    settings = {
        "silver_breakout_points": 200,
        "ema_wick_distance_points": 300,
        "sl_points": 200,
        "tsl_activate_points": 500,
        "target_points": 2000,
        "exit_mode": "fixed_target_sl",
        "silver_lots": 1,
    }
    results = _simulate_silver_micro_range(
        "smoke-algo5",
        "algo5",
        target_day,
        target_day,
        "MCX:SILVERMIC26AUGFUT",
        _micro_2_replay_history(target_day, setup, trigger),
        [target_day],
        settings,
        get_charges_config(),
    )
    return results[0]


def test_backtest_replays_ema_wick_fallback_references() -> None:
    target_day = datetime.date(2026, 8, 25)
    buy = _replay_micro_2(
        target_day,
        {"open_price": 1200, "high": 1220, "low": 980, "close": 1100},
        {"open_price": 1200, "high": 1300, "low": 1200, "close": 1300},
    )
    buy_candidate = buy["candidates"][0]
    check("backtest records fallback BUY family", buy_candidate["setup_family"] == "fallback_ema_wick")
    check("backtest BUY uses close plus n", buy_candidate["trigger_level"] == 1300)
    check("backtest BUY enters from fallback reference", buy_candidate["entry_price"] == 1300)

    sell = _replay_micro_2(
        target_day,
        {"open_price": 800, "high": 1010, "low": 700, "close": 900},
        {"open_price": 800, "high": 850, "low": 700, "close": 700},
    )
    sell_candidate = sell["candidates"][0]
    check("backtest records fallback SELL family", sell_candidate["setup_family"] == "fallback_ema_wick")
    check("backtest SELL uses close minus n", sell_candidate["trigger_level"] == 700)
    check("backtest SELL enters from later-candle trigger", sell_candidate["entry_price"] == 700)


def main() -> None:
    print("\nSILVER MICRO 2.0 EMA-WICK SMOKE TEST")
    test_current_rules_remain_first_choice()
    test_fallback_references_and_distance_gate()
    test_selected_reference_uses_unchanged_trigger_formula()
    test_each_reference_family_is_stored_independently()
    test_candle_close_trigger_uses_fallback_qualification()
    test_fallback_sell_tick_cross_fires_from_green_candle()
    test_audit_identifies_fallback_reference()
    test_candle_pair_tsl_candidates_support_latest_pair_replacement()
    test_candle_pair_move_is_audited_and_sent_to_live_broker()
    test_pair_already_crossed_exits_immediately()
    test_backtest_replays_ema_wick_fallback_references()
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
