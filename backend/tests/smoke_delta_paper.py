"""Offline Delta EMA-volume, 24/7, persistence and protection regressions."""
import copy
import datetime
import os
import time
from collections import deque
from unittest.mock import patch

from app.delta_client import DeltaClient
from app.delta_engine import DeltaService
from app.delta_paper import DeltaPaperBroker
from app.strategies.algo3_silver_micro import Algo3SilverMicro
from app.strategies.delta_gold import (
    DELTA_DEFAULTS,
    DELTA_EXIT_MODE_THREE_CANDLE,
    DELTA_STRATEGY_VERSION,
    DELTA_TIMEFRAMES,
    DeltaGold,
    normalize_stored_settings,
    validate_settings,
)


PRODUCT = {"symbol": "PAXGUSD", "contract_value": "0.001", "tick_size": "0.01", "taker_commission_rate": "0.0002"}


class MemoryStore:
    def __init__(self):
        self.state = None
        self.closed = []
        self.fail = False

    def load(self):
        return copy.deepcopy(self.state)

    def trades(self, offset=0, limit=100, before=None):
        return copy.deepcopy(list(reversed(self.closed))[offset:offset + limit])

    def save(self, state, trade=None):
        if self.fail:
            raise RuntimeError("mock storage failure")
        self.state = copy.deepcopy(state)
        if trade:
            self.closed.append(copy.deepcopy(trade))


def strategy(minutes=15, settings=None, store=None):
    defaults = {
        **DELTA_DEFAULTS,
        "trading_enabled": True,
        "silver_breakout_points": 10,
        "sl_points": 20,
        "target_points": 100,
        "tsl_activate_points": 30,
        **(settings or {}),
    }
    broker = DeltaPaperBroker(store or MemoryStore(), defaults, PRODUCT)
    return DeltaGold(minutes, "PAXGUSD", broker)


def history(minutes, end, close=1000, volume=100):
    interval = minutes * 60
    return [
        {
            "time": end - (30 - index) * interval,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": volume,
        }
        for index in range(30)
    ]


def bar(at, open_price, high, low, close, volume=100, ema=100, volume_ema=100):
    return {
        "time": at,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "ema20": ema,
        "volume_ema20": volume_ema,
    }


def run():
    assert DeltaGold._check_triggers is not Algo3SilverMicro._check_triggers
    assert DeltaGold._update_setups is not Algo3SilverMicro._update_setups
    assert DeltaGold.check_exits is Algo3SilverMicro.check_exits
    assert DELTA_TIMEFRAMES == (5, 7, 15, 30, 60, 240)
    assert DELTA_DEFAULTS["silver_breakout_points"] == 3
    assert DELTA_DEFAULTS["sl_points"] == DELTA_DEFAULTS["tsl_activate_points"] == 15
    assert DELTA_DEFAULTS["target_points"] == 50 and DELTA_DEFAULTS["tsl_buffer_points"] == 3
    assert DELTA_DEFAULTS["post_exit_cooldown_minutes"] == 5

    legacy = normalize_stored_settings({"scan_enabled": False, "trading_enabled": True, "silver_breakout_points": 200})
    assert legacy["strategy_version"] == DELTA_STRATEGY_VERSION
    assert legacy["silver_breakout_points"] == 3 and legacy["sl_points"] == 15
    assert not legacy["scan_enabled"] and legacy["trading_enabled"]

    for minutes in DELTA_TIMEFRAMES:
        now = int(datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc).timestamp())
        now = now // (minutes * 60) * minutes * 60

        buy = strategy(minutes)
        rows = history(minutes, now)
        rows[-1].update(open=1000, high=1031, low=999, close=1030, volume=300)
        rows.append({"time": now, "open": 1031, "high": 1042, "low": 1030, "close": 1041, "volume": 10})
        buy.ingest_history(rows, now + 5)
        assert buy._buy_setup_close == 1030 and buy._volume_ema20 is not None
        assert buy.reference_history[0]["volume"] == 300
        old_price_ema, old_volume_ema = buy._ema20, buy._volume_ema20
        buy.ingest_history(rows, now + 5)
        assert (buy._ema20, buy._volume_ema20, len(buy._bars)) == (old_price_ema, old_volume_ema, 30)
        buy.process_price(1039, now + 6)
        buy.process_price(1041, now + 7)
        position = buy._open_position()
        assert position and position["side"] == "BUY" and position["entry_price"] == 1040
        assert position["signal_snapshot"]["timeframe"] == f"{minutes}m"
        assert position["signal_snapshot"]["entry_candle_open"] == 1031
        buy.square_off_all()
        assert buy._open_position()
        buy.process_price(1020, now + 8)
        assert buy.broker.store.closed[-1]["exit_reason"] == "SL"
        buy.process_price(1041, now + 9)
        assert not buy._open_position()

        sell = strategy(minutes)
        rows = history(minutes, now)
        rows[-1].update(open=1000, high=1001, low=969, close=970, volume=300)
        rows.append({"time": now, "open": 975, "high": 975, "low": 955, "close": 955, "volume": 10})
        sell.ingest_history(rows, now + 5)
        sell.process_price(961, now + 6)
        sell.process_price(959, now + 7)
        assert sell._open_position()["side"] == "SELL" and sell._open_position()["entry_price"] == 960

        gap = strategy(minutes)
        gap.ingest_history([
            *history(minutes, now)[:-1],
            {**history(minutes, now)[-1], "open": 1000, "high": 1031, "low": 999, "close": 1030, "volume": 300},
            {"time": now, "open": 1041, "high": 1050, "low": 1040, "close": 1045, "volume": 10},
        ], now + 5)
        gap.process_price(1050, now + 6)
        assert not gap._open_position()

        strict = strategy(minutes)
        strict._ema20 = strict._volume_ema20 = 100
        stamp = datetime.datetime(2026, 9, 12, 9)
        strict._update_setups(bar(stamp, 101, 103, 100, 102, volume=100))
        assert strict._buy_setup_close is None
        strict._update_setups(bar(stamp, 101, 103, 100, 102, volume=101))
        assert strict._buy_setup_close == 102
        strict._update_setups(bar(stamp, 99, 100, 97, 98, volume=100))
        assert strict._sell_setup_close is None
        strict._update_setups(bar(stamp, 99, 100, 97, 98, volume=101))
        assert strict._sell_setup_close == 98

        for side in ("BUY", "SELL"):
            for outcome in ("TARGET", "TRAILING_SL"):
                tsl = strategy(minutes, {"exit_mode": "target_to_breakeven_sl"})
                assert tsl._enter(side, 1000, 1000)
                tsl._last_tick_ltp = 1030 if side == "BUY" else 970
                tsl.check_exits()
                pos = tsl._open_position()
                assert pos["sl_price"] == 1000 and pos["trailing_sl_active"]
                tsl.settings["exit_mode"] = "fixed_target_sl"
                tsl._last_tick_ltp = (1100 if side == "BUY" else 900) if outcome == "TARGET" else 1000
                tsl.check_exits()
                assert tsl.broker.store.closed[-1]["exit_reason"] == outcome

        broken = strategy(minutes)
        missing = history(minutes, now)
        del missing[-3]
        try:
            broken.ingest_history(missing, now + 5)
            raise AssertionError("history gap accepted")
        except ValueError:
            assert not broken._history_ready and broken._buy_setup_close is None

    base = datetime.datetime(2026, 9, 12, 9)
    for side in ("BUY", "SELL"):
        three = strategy(settings={"exit_mode": DELTA_EXIT_MODE_THREE_CANDLE, "tsl_buffer_points": 3, "target_points": 50})
        lows = (90, 95, 94)
        highs = (110, 105, 108)
        three._bars = deque([
            bar(base + datetime.timedelta(minutes=15 * index), 100, highs[index], lows[index], 100)
            for index in range(3)
        ], maxlen=500)
        three._current_bucket = base + datetime.timedelta(minutes=45)
        three._current_candle_open = 99 if side == "BUY" else 101
        event = (base + datetime.timedelta(minutes=46)).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        assert three._enter(side, 100, 100, event_time=event)
        pos = three._open_position()
        assert pos["initial_sl"] == (80 if side == "BUY" else 120)
        assert pos["sl_price"] == (87 if side == "BUY" else 113)
        assert pos["trailing_sl_active"]
        assert len(pos["signal_snapshot"]["delta_three_candle_tsl"]["events"]) == 1

        entry_bar = bar(base + datetime.timedelta(minutes=45), 100, 104, 96, 102)
        three._bars.append(entry_bar)
        three._apply_three_candle_tsl(entry_bar)
        assert three._open_position()["sl_price"] == pos["sl_price"]
        next_bar = bar(base + datetime.timedelta(minutes=60), 102, 106, 96, 104)
        three._bars.append(next_bar)
        three._apply_three_candle_tsl(next_bar)
        moved = three._open_position()
        assert moved["sl_price"] == (91 if side == "BUY" else 111)
        latest = moved["signal_snapshot"]["delta_three_candle_tsl"]["events"][-1]
        assert all(candle["time"] != entry_bar["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat() for candle in latest["candles"])

    persisted = strategy()
    persisted._enter("BUY", 1000, 1000)
    recovered = strategy(store=persisted.broker.store)
    assert recovered._open_position()["id"] == persisted._open_position()["id"]
    persisted.broker.store.fail = True
    try:
        persisted.broker.close_trade(persisted._open_position(), 1010, "MANUAL_EXIT")
        raise AssertionError("failed close reported success")
    except RuntimeError:
        assert persisted._open_position()

    service = DeltaService()
    service.strategies = {minutes: strategy(minutes) for minutes in DELTA_TIMEFRAMES}
    service.save_settings(30, {"silver_lots": 2})
    assert all(
        item.settings["silver_lots"] == (2 if minutes == 30 else 1)
        for minutes, item in service.strategies.items()
    )

    for invalid in (0, -1, float("nan"), float("inf")):
        try:
            validate_settings({**DELTA_DEFAULTS, "sl_points": invalid})
            raise AssertionError("invalid setting accepted")
        except ValueError:
            pass
    for invalid in (0.5, 10_081):
        try:
            validate_settings({**DELTA_DEFAULTS, "post_exit_cooldown_minutes": invalid})
            raise AssertionError("invalid custom cooldown accepted")
        except ValueError:
            pass
    assert validate_settings({**DELTA_DEFAULTS, "post_exit_cooldown_minutes": 37})["post_exit_cooldown_minutes"] == 37

    for reason in ("MANUAL_EXIT", "SL", "TRAILING_SL", "TARGET"):
        cooldown = strategy(settings={"post_exit_cooldown_minutes": 15})
        assert cooldown._enter("BUY", 1000, 1000)
        with patch("app.delta_paper.time.time", return_value=10_000):
            cooldown.broker.close_trade(cooldown._open_position(), 1000, reason)
        assert cooldown.broker.state["cooldown_until"] == 10_900
        assert cooldown.broker.state["cooldown_reason"] == reason
    reversal = strategy(settings={"post_exit_cooldown_minutes": 15})
    assert reversal._enter("BUY", 1000, 1000)
    reversal.broker.close_trade(reversal._open_position(), 1000, "REVERSAL_CONTRA_SIGNAL")
    assert not reversal.broker.state.get("cooldown_until")

    blocked = strategy()
    blocked.broker.state.update(cooldown_until=time.time() + 300, cooldown_reason="TARGET")
    assert not blocked._fire_entry("BUY", 1000, 1000)
    blocked.broker.state["cooldown_until"] = time.time() - 1
    assert blocked._fire_entry("BUY", 1000, 1000)

    with patch.dict(os.environ, {}, clear=True):
        client = DeltaClient()
        assert client.region == "india" and client.symbol == "PAXGUSD"
    with patch.dict(os.environ, {"DELTA_EXCHANGE": "global"}, clear=True):
        assert DeltaClient().symbol == "PAXGUSDT"
    with patch.dict(os.environ, {"DELTA_EXCHANGE": "india", "DELTA_PROXY_URL": "http://name:pass@proxy.example:3128"}):
        client = DeltaClient()
        assert client.websocket_options()["http_proxy_host"] == "proxy.example"
    print("smoke_delta_paper: all checks passed")


if __name__ == "__main__":
    run()
