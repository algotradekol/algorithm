import datetime
import threading
from collections import deque

from app import backtest as bt
from app.charges import get_charges_config
from app.strategies.algo6_silver_v_micro import Algo6SilverVMicro, _bucket_start_9m


class DummyBroker:
    def __init__(self):
        self.positions = []
        self.closed = []

    def open_positions(self):
        return list(self.positions)

    def open_trade(self, symbol, side, qty, entry_price, sl_price, target_price, trigger, snapshot, **kwargs):
        self.positions.append({
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "entry_trigger": trigger,
            "signal_snapshot": snapshot,
        })

    def close_trade(self, position, exit_price, exit_reason):
        self.closed.append((position, exit_price, exit_reason))


def make_strategy():
    strategy = object.__new__(Algo6SilverVMicro)
    strategy.symbol = "MCX:SILVERMIC26NOVFUT"
    strategy.watchlist = [strategy.symbol]
    strategy.settings = {
        "scan_enabled": True,
        "trading_enabled": True,
        "silver_breakout_points": 200,
        "silver_lots": 1,
        "sl_points": 200,
        "target_points": 2000,
        "tsl_activate_points": 500,
        "exit_mode": "fixed_target_sl",
        "overnight_carry_enabled": False,
    }
    strategy.broker = DummyBroker()
    strategy._history_loading = False
    strategy._history_ready = True
    strategy._history_error = None
    strategy._last_manual_history_refresh_at = None
    strategy._warmup_minute_candles = 0
    strategy._minute_buffer = []
    strategy._current_bucket = None
    strategy._last_ingested_minute_at = None
    strategy._bars = deque(maxlen=500)
    strategy._ema20 = 100.0
    strategy._volume_ema20 = 100.0
    strategy._buy_setup_close = None
    strategy._sell_setup_close = None
    strategy._buy_setup_bar_at = None
    strategy._sell_setup_bar_at = None
    strategy._buy_setup_volume = None
    strategy._buy_setup_volume_ema20 = None
    strategy._sell_setup_volume = None
    strategy._sell_setup_volume_ema20 = None
    strategy._prev_ltp = None
    strategy._last_fired_buy_bar_at = None
    strategy._last_fired_sell_bar_at = None
    strategy._last_attempted_buy_bar_at = None
    strategy._last_attempted_sell_bar_at = None
    strategy._sell_reentry_after_exit = None
    strategy._buy_reentry_after_exit = None
    strategy._entry_attempt_in_flight = False
    strategy._entry_guard_lock = threading.Lock()
    strategy._entry_cooldown_until_monotonic = 0.0
    strategy._sl_cooldown_until_monotonic = 0.0
    strategy._last_tick_at = None
    strategy._last_tick_ltp = None
    strategy._last_minute_candle_at = None
    strategy._last_bar_at = None
    return strategy


def test_bucket_is_anchored_from_0900():
    assert _bucket_start_9m(datetime.datetime(2026, 9, 8, 9, 8)) == datetime.datetime(2026, 9, 8, 9, 0)
    assert _bucket_start_9m(datetime.datetime(2026, 9, 8, 9, 9)) == datetime.datetime(2026, 9, 8, 9, 9)
    assert _bucket_start_9m(datetime.datetime(2026, 9, 8, 10, 2)) == datetime.datetime(2026, 9, 8, 9, 54)
    assert _bucket_start_9m(datetime.datetime(2026, 9, 8, 10, 3)) == datetime.datetime(2026, 9, 8, 10, 3)


def test_buy_requires_green_above_ema_and_strict_volume_ema():
    strategy = make_strategy()
    bar = {"time": datetime.datetime(2026, 9, 8, 9, 0), "open": 100, "high": 125, "low": 99, "close": 120, "volume": 101}
    assert strategy._qualifies_as_buy_setup(bar)
    strategy._update_setups(bar)
    assert strategy._buy_setup_close == 120

    strategy = make_strategy()
    equal_volume = dict(bar, volume=100)
    assert not strategy._qualifies_as_buy_setup(equal_volume)
    strategy._update_setups(equal_volume)
    assert strategy._buy_setup_close is None

    strategy = make_strategy()
    below_ema = dict(bar, close=99)
    assert not strategy._qualifies_as_buy_setup(below_ema)


def test_sell_requires_red_below_ema_and_strict_volume_ema():
    strategy = make_strategy()
    bar = {"time": datetime.datetime(2026, 9, 8, 9, 0), "open": 120, "high": 121, "low": 80, "close": 90, "volume": 101}
    assert strategy._qualifies_as_sell_setup(bar)
    strategy._update_setups(bar)
    assert strategy._sell_setup_close == 90

    strategy = make_strategy()
    equal_volume = dict(bar, volume=100)
    assert not strategy._qualifies_as_sell_setup(equal_volume)
    strategy._update_setups(equal_volume)
    assert strategy._sell_setup_close is None

    strategy = make_strategy()
    above_ema = dict(bar, close=101)
    assert not strategy._qualifies_as_sell_setup(above_ema)


def test_triggers_require_fresh_crossing():
    strategy = make_strategy()
    setup_time = datetime.datetime(2026, 9, 8, 9, 0)
    strategy._buy_setup_close = 1000
    strategy._buy_setup_bar_at = setup_time
    strategy._current_bucket = datetime.datetime(2026, 9, 8, 9, 9)
    strategy._prev_ltp = 1199
    strategy._check_triggers(1200, datetime.datetime(2026, 9, 8, 9, 10))
    assert len(strategy.broker.positions) == 1
    assert strategy.broker.positions[0]["side"] == "BUY"
    assert strategy.broker.positions[0]["entry_price"] == 1200

    strategy = make_strategy()
    strategy._buy_setup_close = 1000
    strategy._buy_setup_bar_at = setup_time
    strategy._current_bucket = datetime.datetime(2026, 9, 8, 9, 9)
    strategy._prev_ltp = 1200
    strategy._check_triggers(1205, datetime.datetime(2026, 9, 8, 9, 10))
    assert strategy.broker.positions == []

    strategy = make_strategy()
    strategy._buy_setup_close = 1000
    strategy._buy_setup_bar_at = setup_time
    strategy._current_bucket = setup_time
    strategy._prev_ltp = 1199
    strategy._check_triggers(1200, datetime.datetime(2026, 9, 8, 9, 5))
    assert strategy.broker.positions == []

    strategy = make_strategy()
    strategy._sell_setup_close = 1000
    strategy._sell_setup_bar_at = setup_time
    strategy._current_bucket = datetime.datetime(2026, 9, 8, 9, 9)
    strategy._prev_ltp = 801
    strategy._check_triggers(800, datetime.datetime(2026, 9, 8, 9, 10))
    assert len(strategy.broker.positions) == 1
    assert strategy.broker.positions[0]["side"] == "SELL"
    assert strategy.broker.positions[0]["entry_price"] == 800

    strategy = make_strategy()
    strategy._sell_setup_close = 1000
    strategy._sell_setup_bar_at = setup_time
    strategy._current_bucket = datetime.datetime(2026, 9, 8, 9, 9)
    strategy._prev_ltp = 800
    strategy._check_triggers(795, datetime.datetime(2026, 9, 8, 9, 10))
    assert strategy.broker.positions == []

    strategy = make_strategy()
    strategy._sell_setup_close = 1000
    strategy._sell_setup_bar_at = setup_time
    strategy._current_bucket = setup_time
    strategy._prev_ltp = 801
    strategy._check_triggers(800, datetime.datetime(2026, 9, 8, 9, 5))
    assert strategy.broker.positions == []

    strategy = make_strategy()
    strategy._sell_setup_close = 1000
    strategy._sell_setup_bar_at = setup_time
    strategy._current_bucket = datetime.datetime(2026, 9, 8, 9, 9)
    strategy._last_fired_sell_bar_at = setup_time
    strategy._prev_ltp = 801
    strategy._check_triggers(800, datetime.datetime(2026, 9, 8, 9, 10))
    assert strategy.broker.positions == []


def test_opening_gap_can_use_prior_day_reference():
    strategy = make_strategy()
    setup_time = datetime.datetime(2026, 9, 8, 23, 21)
    strategy._buy_setup_close = 1000
    strategy._buy_setup_bar_at = setup_time
    strategy._prev_ltp = None
    strategy._check_triggers(1205, datetime.datetime(2026, 9, 9, 9, 0, 22))
    assert len(strategy.broker.positions) == 1
    assert strategy.broker.positions[0]["side"] == "BUY"
    assert strategy.broker.positions[0]["entry_price"] == 1200


def test_overnight_carry_controls_squareoff():
    strategy = make_strategy()
    carried = {"symbol": strategy.symbol, "side": "BUY", "entry_price": 1000, "signal_snapshot": {"overnight_carry_enabled": True}}
    normal = {"symbol": strategy.symbol, "side": "SELL", "entry_price": 900, "signal_snapshot": {"overnight_carry_enabled": False}}
    strategy.broker.positions = [carried, normal]

    strategy.square_off_all()

    assert strategy.broker.closed == [(normal, 900, "EOD_SQUAREOFF")]


def _expand_9m_bar(start, open_price, high, low, close, volume):
    candles = []
    per_minute_volume = volume / 9
    for offset in range(9):
        ts = start + datetime.timedelta(minutes=offset)
        candles.append({
            "time": ts,
            "open": open_price if offset == 0 else close,
            "high": high if offset == 8 else max(open_price, close),
            "low": low if offset == 8 else min(open_price, close),
            "close": close,
            "volume": per_minute_volume,
        })
    return candles


def test_backtest_uses_9m_ema_volume_reference():
    day = datetime.date(2026, 9, 8)
    start = datetime.datetime(2026, 9, 8, 9, 0)
    history = []
    for index in range(20):
        history.extend(_expand_9m_bar(start + datetime.timedelta(minutes=9 * index), 100, 101, 99, 100, 100))
    setup_start = start + datetime.timedelta(minutes=9 * 20)
    history.extend(_expand_9m_bar(setup_start, 100, 121, 99, 120, 200))
    trigger_start = setup_start + datetime.timedelta(minutes=9)
    history.extend([
        {"time": trigger_start, "open": 120, "high": 130, "low": 119, "close": 130, "volume": 10},
        {"time": trigger_start + datetime.timedelta(minutes=1), "open": 130, "high": 160, "low": 129, "close": 160, "volume": 10},
    ])

    results = bt._simulate_silver_micro_range(
        "silver-v-smoke",
        "algo6",
        day,
        day,
        "MCX:TEST",
        history,
        [day],
        {
            "silver_breakout_points": 10,
            "silver_lots": 1,
            "sl_points": 20,
            "target_points": 30,
            "tsl_activate_points": 15,
            "exit_mode": "fixed_target_sl",
            "overnight_carry_enabled": False,
        },
        get_charges_config(),
    )

    result = results[0]
    assert result["chart"]["resolution"] == "9"
    assert all(1 <= candle["minute_count"] <= 9 for candle in result["chart"]["candles"])
    assert result["chart"]["candles"][-1]["volume_ema20"] is not None
    assert result["candidates"][-1]["setup_family"] == "nine_minute_ema_volume"
    assert result["candidates"][-1]["volume_ema20"] is not None
    assert result["candidates"][-1]["minute_count"] == 9
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["side"] == "BUY"
    assert trade["entry_price"] == 130
    assert trade["exit_reason"] == "TARGET"


def test_backtest_does_not_reuse_same_9m_reference_after_sl():
    day = datetime.date(2026, 9, 8)
    start = datetime.datetime(2026, 9, 8, 9, 0)
    history = []
    for index in range(20):
        history.extend(_expand_9m_bar(start + datetime.timedelta(minutes=9 * index), 100, 101, 99, 100, 100))
    setup_start = start + datetime.timedelta(minutes=9 * 20)
    history.extend(_expand_9m_bar(setup_start, 100, 121, 99, 120, 200))
    trigger_start = setup_start + datetime.timedelta(minutes=9)
    history.extend([
        {"time": trigger_start, "open": 120, "high": 130, "low": 124, "close": 124, "volume": 10},
        {"time": trigger_start + datetime.timedelta(minutes=1), "open": 124, "high": 130, "low": 124, "close": 124, "volume": 10},
    ])

    results = bt._simulate_silver_micro_range(
        "silver-v-no-duplicate-reference",
        "algo6",
        day,
        day,
        "MCX:TEST",
        history,
        [day],
        {
            "silver_breakout_points": 10,
            "silver_lots": 1,
            "sl_points": 5,
            "target_points": 1000,
            "tsl_activate_points": 500,
            "exit_mode": "fixed_target_sl",
            "overnight_carry_enabled": False,
        },
        get_charges_config(),
    )

    trades = results[0]["trades"]
    assert len(trades) == 1
    assert trades[0]["side"] == "BUY"
    assert trades[0]["entry_price"] == 130
    assert trades[0]["exit_reason"] == "SL"


def run():
    test_bucket_is_anchored_from_0900()
    test_buy_requires_green_above_ema_and_strict_volume_ema()
    test_sell_requires_red_below_ema_and_strict_volume_ema()
    test_triggers_require_fresh_crossing()
    test_opening_gap_can_use_prior_day_reference()
    test_overnight_carry_controls_squareoff()
    test_backtest_uses_9m_ema_volume_reference()
    test_backtest_does_not_reuse_same_9m_reference_after_sl()
    print("smoke_silver_v_micro passed")


if __name__ == "__main__":
    run()
