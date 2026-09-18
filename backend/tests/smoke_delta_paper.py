"""Offline Delta EMA-volume, 24/7, persistence and protection regressions."""
import copy
import datetime
import os
import time
from collections import deque
from unittest.mock import patch

from app.delta_client import DeltaClient
from app.delta_engine import DeltaService
from app.delta_live import DeltaLiveBroker
from app.delta_paper import DeltaPaperBroker
from app.strategies.algo3_silver_micro import Algo3SilverMicro
from app.strategies.delta_gold import (
    DELTA_DEFAULTS,
    DELTA_EXIT_MODE_THREE_CANDLE,
    DELTA_STRATEGY_VERSION,
    DELTA_TIMEFRAMES,
    DeltaGold,
    effective_delta_lots,
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


class FakeLiveClient:
    def __init__(self):
        self.orders = []
        self.brackets = []
        self.edits = []
        self.deletes = []
        self.active_orders = []
        self.live_size = 0
        self.region = "india"
        self.symbol = "PAXGUSD"

    def post(self, path, payload=None, private=True, envelope=False):
        assert path in {"/v2/orders", "/v2/orders/bracket"} and private
        if path == "/v2/orders/bracket":
            self.brackets.append(copy.deepcopy(payload))
            self.active_orders = [
                {
                    "id": 2,
                    "product_id": payload["product_id"],
                    "product_symbol": self.symbol,
                    "size": 1,
                    "side": "sell",
                    "order_type": payload["stop_loss_order"]["order_type"],
                    "stop_order_type": "stop_loss_order",
                    "stop_price": payload["stop_loss_order"]["stop_price"],
                    "state": "pending",
                    "reduce_only": True,
                    "bracket_order": True,
                },
                {
                    "id": 3,
                    "product_id": payload["product_id"],
                    "product_symbol": self.symbol,
                    "size": 1,
                    "side": "sell",
                    "order_type": payload["take_profit_order"]["order_type"],
                    "stop_order_type": "take_profit_order",
                    "stop_price": payload["take_profit_order"]["stop_price"],
                    "state": "pending",
                    "reduce_only": True,
                    "bracket_order": True,
                },
            ]
            return {"success": True, "result": None} if envelope else None
        self.orders.append(copy.deepcopy(payload))
        if payload.get("stop_order_type"):
            row = {
                "id": len(self.orders),
                "product_id": payload["product_id"],
                "product_symbol": self.symbol,
                "size": payload.get("size"),
                "side": payload.get("side"),
                "order_type": payload.get("order_type"),
                "stop_order_type": payload.get("stop_order_type"),
                "stop_price": payload.get("stop_price"),
                "state": "pending",
                "reduce_only": bool(payload.get("reduce_only")),
            }
            self.active_orders.append(row)
            return copy.deepcopy(row)
        if payload.get("reduce_only"):
            self.live_size = 0
        else:
            self.live_size = payload.get("size") or 0
        return {"id": len(self.orders), "average_fill_price": payload.get("stop_price") or "1000", "unfilled_size": 0, "state": "closed"}

    def put(self, path, payload=None, private=True, envelope=False):
        assert path in {"/v2/orders", "/v2/orders/bracket"} and private
        self.edits.append(copy.deepcopy(payload))
        if path == "/v2/orders/bracket":
            raise RuntimeError("Delta HTTP 400: code=open_order_not_found")
        row = next(row for row in self.active_orders if row["id"] == payload["id"])
        row.update(payload)
        return copy.deepcopy(row)

    def delete(self, path, payload=None, private=True):
        assert path == "/v2/orders" and private
        self.deletes.append(copy.deepcopy(payload))
        self.active_orders = [row for row in self.active_orders if str(row.get("id")) != str(payload.get("id"))]
        return {"id": payload["id"], "state": "cancelled"}

    def get(self, path, params=None, private=True, envelope=False):
        assert path in {"/v2/orders", "/v2/positions/margined"} and private
        if path == "/v2/positions/margined":
            rows = []
            if self.live_size:
                rows.append({"product_id": 123006, "product_symbol": self.symbol, "size": self.live_size})
            return {"result": rows, "meta": {}} if envelope else rows
        return {"result": copy.deepcopy(self.active_orders), "meta": {"after": None}} if envelope else copy.deepcopy(self.active_orders)


class FakeCloseRejectClient(FakeLiveClient):
    def post(self, path, payload=None, private=True, envelope=False):
        if path == "/v2/orders/bracket":
            return super().post(path, payload, private=private, envelope=envelope)
        if payload and payload.get("stop_order_type"):
            return super().post(path, payload, private=private, envelope=envelope)
        assert path == "/v2/orders" and private
        self.orders.append(copy.deepcopy(payload))
        if payload.get("reduce_only") and not payload.get("stop_order_type"):
            self.live_size = payload.get("size") or self.live_size
            raise RuntimeError("Delta HTTP 400: code=position_not_found; message=no matching live position")
        if payload.get("reduce_only"):
            self.live_size = 0
        else:
            self.live_size = payload.get("size") or 0
        return {"id": len(self.orders), "average_fill_price": payload.get("stop_price") or "1000", "unfilled_size": 0, "state": "closed"}


class FakeUnfilledCloseClient(FakeLiveClient):
    def post(self, path, payload=None, private=True, envelope=False):
        if path == "/v2/orders/bracket":
            return super().post(path, payload, private=private, envelope=envelope)
        if payload and payload.get("stop_order_type"):
            return super().post(path, payload, private=private, envelope=envelope)
        assert path == "/v2/orders" and private
        self.orders.append(copy.deepcopy(payload))
        if payload.get("reduce_only") and not payload.get("stop_order_type"):
            self.live_size = payload.get("size") or self.live_size
            return {"id": len(self.orders), "unfilled_size": payload.get("size"), "state": "cancelled"}
        if payload.get("reduce_only"):
            self.live_size = 0
        else:
            self.live_size = payload.get("size") or 0
        return {"id": len(self.orders), "average_fill_price": payload.get("stop_price") or "1000", "unfilled_size": 0, "state": "closed"}


class FakeBracketMissingClient(FakeLiveClient):
    def post(self, path, payload=None, private=True, envelope=False):
        if payload and payload.get("stop_order_type"):
            self.active_orders = []
            raise RuntimeError("Delta protection order rejected")
        if path == "/v2/orders/bracket":
            self.brackets.append(copy.deepcopy(payload))
            self.active_orders = []
            return {"success": True, "result": None} if envelope else None
        return super().post(path, payload, private=private, envelope=envelope)


class FakeExistingBracketClient(FakeLiveClient):
    def __init__(self):
        super().__init__()
        self.active_orders = [
            {
                "id": 21,
                "product_id": 123006,
                "product_symbol": self.symbol,
                "size": 1,
                "side": "sell",
                "order_type": "market_order",
                "stop_order_type": "stop_loss_order",
                "stop_price": "900",
                "state": "pending",
                "reduce_only": True,
                "bracket_order": True,
            },
            {
                "id": 22,
                "product_id": 123006,
                "product_symbol": self.symbol,
                "size": 1,
                "side": "sell",
                "order_type": "market_order",
                "stop_order_type": "take_profit_order",
                "stop_price": "1100",
                "state": "pending",
                "reduce_only": True,
                "bracket_order": True,
            },
        ]

    def post(self, path, payload=None, private=True, envelope=False):
        if path == "/v2/orders/bracket":
            self.brackets.append(copy.deepcopy(payload))
            raise RuntimeError("Delta HTTP 400: code=bracket_order_exists")
        return super().post(path, payload, private=private, envelope=envelope)

    def delete(self, path, payload=None, private=True):
        self.deletes.append(copy.deepcopy(payload))
        raise RuntimeError("Delta HTTP 400: code=open_order_not_found")


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
    assert DELTA_TIMEFRAMES == (5, 7, 15, 30, 60, 120, 240)
    assert DELTA_DEFAULTS["silver_breakout_points"] == 3
    assert DELTA_DEFAULTS["sl_points"] == DELTA_DEFAULTS["tsl_activate_points"] == 15
    assert DELTA_DEFAULTS["target_points"] == 50 and DELTA_DEFAULTS["tsl_buffer_points"] == 3
    assert DELTA_DEFAULTS["post_exit_cooldown_minutes"] == 5
    assert DELTA_DEFAULTS["size_mode"] == "lots" and DELTA_DEFAULTS["pax_size"] == 0.001
    assert DELTA_DEFAULTS["leverage"] == 50

    legacy = normalize_stored_settings({"scan_enabled": False, "trading_enabled": True, "silver_breakout_points": 200})
    assert legacy["strategy_version"] == DELTA_STRATEGY_VERSION
    assert legacy["silver_breakout_points"] == 3 and legacy["sl_points"] == 15
    assert not legacy["scan_enabled"] and legacy["trading_enabled"]
    assert effective_delta_lots({**DELTA_DEFAULTS, "size_mode": "pax", "pax_size": 0.02}, PRODUCT) == 20
    pax = strategy(settings={"size_mode": "pax", "pax_size": 0.0201, "leverage": 50})
    assert pax._enter("BUY", 1000, 1000)
    pax_position = pax._open_position()
    assert pax_position["qty"] == 21
    assert pax_position["size_mode"] == "pax"
    assert pax_position["configured_pax_size"] == 0.0201
    assert abs(pax_position["estimated_entry_margin"] - 0.42) < 1e-9

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

        sell_gap = strategy(minutes)
        sell_gap.ingest_history([
            *history(minutes, now)[:-1],
            {**history(minutes, now)[-1], "open": 1000, "high": 1001, "low": 969, "close": 970, "volume": 300},
            {"time": now, "open": 959, "high": 965, "low": 950, "close": 955, "volume": 10},
        ], now + 5)
        sell_gap.process_price(961, now + 6)
        sell_gap.process_price(955, now + 7)
        assert not sell_gap._open_position(), "SELL needs a candle open above its trigger, even on a later recross"

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
        lows = (10, 20, 30)
        highs = (190, 180, 170)
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
        assert pos["sl_price"] == pos["initial_sl"]
        assert not pos["trailing_sl_active"]
        assert pos["signal_snapshot"]["delta_three_candle_tsl"]["events"] == []
        assert pos["signal_snapshot"]["delta_three_candle_tsl"]["status"] == "waiting_for_three_post_entry_candles"

        entry_bar = bar(base + datetime.timedelta(minutes=45), 100, 104, 96, 102)
        three._bars.append(entry_bar)
        three._apply_three_candle_tsl(entry_bar)
        assert three._open_position()["sl_price"] == pos["sl_price"]
        post1 = bar(base + datetime.timedelta(minutes=60), 102, 104, 96, 102)
        post2 = bar(base + datetime.timedelta(minutes=75), 102, 106, 95, 104)
        post3 = bar(base + datetime.timedelta(minutes=90), 104, 108, 94, 106)
        for wait_bar in (post1, post2):
            three._bars.append(wait_bar)
            three._apply_three_candle_tsl(wait_bar)
            assert three._open_position()["sl_price"] == pos["sl_price"]
        three._bars.append(post3)
        three._apply_three_candle_tsl(post3)
        moved = three._open_position()
        assert moved["sl_price"] == (91 if side == "BUY" else 111)
        latest = moved["signal_snapshot"]["delta_three_candle_tsl"]["events"][-1]
        assert [candle["time"] for candle in latest["candles"]] == [
            post1["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
            post2["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
            post3["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
        ]
        post4 = bar(base + datetime.timedelta(minutes=105), 106, 106, 97, 105)
        three._bars.append(post4)
        three._apply_three_candle_tsl(post4)
        assert three._open_position()["sl_price"] == moved["sl_price"]
        post5 = bar(base + datetime.timedelta(minutes=120), 105, 105, 98, 104)
        three._bars.append(post5)
        three._apply_three_candle_tsl(post5)
        checked = three._open_position()["signal_snapshot"]["delta_three_candle_tsl"]["evaluations"]
        assert [[candle["time"] for candle in item["candles"]] for item in checked[-3:]] == [
            [post1["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
             post2["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
             post3["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat()],
            [post2["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
             post3["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
             post4["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat()],
            [post3["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
             post4["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat(),
             post5["time"].replace(tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))).isoformat()],
        ]
        assert checked[-2]["status"] == "not_tighter" and checked[-1]["status"] == "not_tighter"
        post6 = bar(base + datetime.timedelta(minutes=135), 104, 104, 99, 103)
        three._bars.append(post6)
        three._apply_three_candle_tsl(post6)
        rolled = three._open_position()
        assert rolled["sl_price"] == (94 if side == "BUY" else 109)

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
    service.client = type("Client", (), {"region": "india", "symbol": "PAXGUSD", "proxy": "", "key": "", "secret": ""})()
    service.product = {"contract_value": "0.001", "quoting_asset": {"symbol": "USD"}, "settling_asset": {"symbol": "USD"}}
    service.save_settings(30, {"silver_lots": 2})
    assert all(
        item.settings["silver_lots"] == (2 if minutes == 30 else 1)
        for minutes, item in service.strategies.items()
    )
    recheck_now = int(datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc).timestamp())
    recheck_now = recheck_now // (5 * 60) * 5 * 60
    recheck_strategy = service.strategies[5]
    recheck_rows = history(5, recheck_now)
    recheck_rows[-1].update(open=1000, high=1031, low=999, close=1030, volume=300)
    recheck_rows.append({"time": recheck_now, "open": 1031, "high": 1036, "low": 1030, "close": 1036, "volume": 10})
    recheck_strategy.ingest_history(recheck_rows, recheck_now + 5)
    recheck_strategy.process_price(1036, recheck_now + 6)
    assert not recheck_strategy._open_position()
    service.last_price = 1036
    service.last_event_at = time.time()
    service.save_settings(5, {"silver_breakout_points": 5})
    assert recheck_strategy._open_position()["entry_price"] == 1035

    today_strategy = service.strategies[15]
    today_strategy.broker.store.closed = [
        {"side": "BUY", "exit_time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "gross_pnl": 5, "fees": 1, "net_pnl": 4},
        {"side": "SELL", "exit_time": "2020-01-01T00:00:00+00:00", "gross_pnl": 100, "fees": 10, "net_pnl": 90},
    ]
    today_strategy.broker.state.update(closed_count=2, gross_pnl=105, fees=11)
    service._overview_trade_cache.clear()
    snapshot = service.snapshot(15)
    assert snapshot["summary"]["closed_count"] == 2
    assert snapshot["today_summary"]["closed_count"] == 1
    assert snapshot["today_summary"]["gross_pnl"] == 5
    assert snapshot["today_summary"]["fees"] == 1

    for invalid in (0, -1, float("nan"), float("inf")):
        try:
            validate_settings({**DELTA_DEFAULTS, "sl_points": invalid})
            raise AssertionError("invalid setting accepted")
        except ValueError:
            pass
    for invalid in (0.5, -1, 10_081):
        try:
            validate_settings({**DELTA_DEFAULTS, "post_exit_cooldown_minutes": invalid})
            raise AssertionError("invalid custom cooldown accepted")
        except ValueError:
            pass
    assert validate_settings({**DELTA_DEFAULTS, "post_exit_cooldown_minutes": 0})["post_exit_cooldown_minutes"] == 0
    assert validate_settings({**DELTA_DEFAULTS, "post_exit_cooldown_minutes": 37})["post_exit_cooldown_minutes"] == 37

    for reason in ("MANUAL_EXIT", "SL", "TRAILING_SL", "TARGET"):
        cooldown = strategy(settings={"post_exit_cooldown_minutes": 15})
        assert cooldown._enter("BUY", 1000, 1000)
        with patch("app.delta_paper.time.time", return_value=10_000):
            cooldown.broker.close_trade(cooldown._open_position(), 1000, reason)
        assert cooldown.broker.state["cooldown_until"] == 10_900
        assert cooldown.broker.state["cooldown_reason"] == reason
        no_rest = strategy(settings={"post_exit_cooldown_minutes": 0})
        assert no_rest._enter("BUY", 1000, 1000)
        with patch("app.delta_paper.time.time", return_value=10_000):
            no_rest.broker.close_trade(no_rest._open_position(), 1000, reason)
        assert not no_rest.broker.state.get("cooldown_until")
        assert no_rest.broker.state.get("cooldown_reason") is None
    reversal = strategy(settings={"post_exit_cooldown_minutes": 15})
    assert reversal._enter("BUY", 1000, 1000)
    reversal.broker.close_trade(reversal._open_position(), 1000, "REVERSAL_CONTRA_SIGNAL")
    assert not reversal.broker.state.get("cooldown_until")

    live_store = MemoryStore()
    live_product = {**PRODUCT, "id": 123006, "quoting_asset": {"symbol": "USD"}, "initial_margin": "1"}
    live_client = FakeLiveClient()
    live_broker = DeltaLiveBroker(live_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, live_client)
    live = DeltaGold(15, "PAXGUSD", live_broker)
    assert live._enter("BUY", 1000, 1000)
    live_pos = live._open_position()
    assert live_pos["execution"] == "live" and live_pos["signal_snapshot"]["execution"] == "live"
    assert live_client.orders[0]["order_type"] == "market_order" and not live_client.orders[0]["reduce_only"]
    assert len(live_client.orders) == 3, "entry plus per-timeframe SL/target protection should be submitted"
    assert not live_client.brackets, "live timeframe protection must not use Delta's shared product bracket"
    assert live_client.orders[1]["stop_order_type"] == "stop_loss_order"
    assert live_client.orders[1]["stop_price"] == "985"
    assert live_client.orders[2]["stop_order_type"] == "take_profit_order"
    assert live_client.orders[2]["stop_price"] == "1050"
    assert live_pos["live_orders"]["bracket_order"] is False
    assert live_pos["live_orders"]["stop_order_id"] == 2
    assert live_pos["live_orders"]["target_order_id"] == 3
    live_broker.update_protection(live_pos, 990, 1050, 1005)
    assert live_client.edits[-1]["id"] == 2
    assert live_client.edits[-1]["stop_price"] == "990"
    live_broker.update_protection(live_broker.state["position"], 990, 1060, 1005)
    assert live_client.edits[-1]["id"] == 3, "target-only edits must edit only the target leg"
    assert live_client.edits[-1]["stop_price"] == "1060"
    live_broker.close_trade(live_broker.state["position"], 1005, "MANUAL_EXIT")
    assert live_client.orders[-1]["reduce_only"] and live_client.orders[-1]["side"] == "sell"
    assert live_store.closed[-1]["execution"] == "live"

    sync_client = FakeLiveClient()
    sync_store = MemoryStore()
    sync_broker = DeltaLiveBroker(sync_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, sync_client)
    sync_strategy = DeltaGold(5, "PAXGUSD", sync_broker)
    assert sync_strategy._enter("BUY", 1000, 1000)
    sync_client.active_orders[0]["stop_price"] = "992"
    sync_client.active_orders[1]["stop_price"] = "1075"
    service_sync = DeltaService(mode="live")
    service_sync.client = sync_client
    service_sync.product = live_product
    service_sync.strategies = {5: sync_strategy}
    service_sync._reconcile_live_positions()
    assert sync_broker.state["position"]["sl_price"] == 992
    assert sync_broker.state["position"]["target_price"] == 1075
    assert sync_store.state["position"]["target_price"] == 1075
    assert not sync_client.edits, "Importing external edits must not place or modify orders"

    before_external = copy.deepcopy(sync_broker.state["position"])
    sync_client.active_orders[1]["stop_price"] = "1080"
    try:
        sync_broker.update_protection(before_external, 992, 1090, 1005)
        raise AssertionError("stale edit overwrote an external target")
    except ValueError as exc:
        assert "changed on Delta" in str(exc)
    assert sync_broker.state["position"]["target_price"] == 1080
    assert not sync_client.edits

    real_put = sync_client.put
    def fail_target(path, payload=None, **kwargs):
        if payload["id"] == 3:
            raise RuntimeError("target edit rejected")
        return real_put(path, payload, **kwargs)
    with patch.object(sync_client, "put", side_effect=fail_target):
        try:
            sync_broker.update_protection(sync_broker.state["position"], 995, 1090, 1005)
            raise AssertionError("partial failure reported as success")
        except RuntimeError as exc:
            assert "target edit rejected" in str(exc)
    assert sync_broker.state["position"]["sl_price"] == 995
    assert sync_broker.state["position"]["target_price"] == 1080
    # Automatic TSL follows the same leg endpoint and preserves Delta's size.
    sync_client.active_orders[0]["size"] = 3
    sync_broker._amend_stop(sync_broker.state["position"], 996)
    assert sync_client.edits[-1]["id"] == 2
    assert sync_client.edits[-1]["stop_price"] == "996"
    assert sync_client.edits[-1]["size"] == 3
    sync_broker.sync_protection()
    assert sync_broker.state["position"]["sl_price"] == 996
    sync_client.active_orders[0]["id"] = 99
    sync_client.active_orders[0]["stop_price"] = "999"
    sync_broker.sync_protection()
    assert sync_broker.state["position"]["sl_price"] == 996, "Never adopt another timeframe's untracked order"
    before_missing = len(sync_client.edits)
    try:
        sync_broker.update_protection(sync_broker.state["position"], 996, 1090, 1005)
        raise AssertionError("missing protection was silently replaced")
    except ValueError as exc:
        assert "no longer active" in str(exc)
    assert len(sync_client.edits) == before_missing

    orphan_store = MemoryStore()
    orphan_client = FakeLiveClient()
    orphan_client.active_orders = [
        {"id": 91, "product_id": 123006, "product_symbol": "PAXGUSD", "stop_order_type": "stop_loss_order", "reduce_only": True},
        {"id": 92, "product_id": 123006, "product_symbol": "PAXGUSD", "stop_order_type": "take_profit_order", "reduce_only": True},
        {"id": 93, "product_id": 123006, "product_symbol": "PAXGUSD", "order_type": "limit_order", "reduce_only": False},
    ]
    orphan_broker = DeltaLiveBroker(orphan_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, orphan_client)
    orphan = DeltaGold(15, "PAXGUSD", orphan_broker)
    assert orphan._enter("BUY", 1000, 1000)
    assert [row["id"] for row in orphan_client.deletes[:2]] == [91, 92]
    assert 93 not in [row["id"] for row in orphan_client.deletes]

    existing_store = MemoryStore()
    existing_client = FakeExistingBracketClient()
    existing_broker = DeltaLiveBroker(existing_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, existing_client)
    existing = DeltaGold(15, "PAXGUSD", existing_broker)
    assert existing._enter("BUY", 1000, 1000)
    existing_pos = existing_broker.state["position"]
    assert existing_pos and existing_pos["execution"] == "live"
    assert existing_pos["live_orders"]["stop_order_id"] == 2
    assert existing_pos["live_orders"]["target_order_id"] == 3
    assert not existing_client.brackets
    assert existing_client.orders[1]["stop_order_type"] == "stop_loss_order"
    assert existing_client.orders[2]["stop_order_type"] == "take_profit_order"
    assert not [order for order in existing_client.orders if order.get("reduce_only") and not order.get("stop_order_type")]

    missing_store = MemoryStore()
    missing_client = FakeBracketMissingClient()
    missing_broker = DeltaLiveBroker(missing_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, missing_client)
    missing = DeltaGold(15, "PAXGUSD", missing_broker)
    assert not missing._enter("BUY", 1000, 1000)
    assert missing.data_error and "bracket protection failed" in missing.data_error
    assert missing_broker.state["position"] is None
    assert missing_client.orders[-1]["reduce_only"] and missing_client.orders[-1]["side"] == "sell"

    guarded_store = MemoryStore()
    guarded_client = FakeLiveClient()
    guarded_broker = DeltaLiveBroker(guarded_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, guarded_client)
    guarded_broker.entry_guard = lambda side: (_ for _ in ()).throw(ValueError("live cap reached"))
    guarded = DeltaGold(15, "PAXGUSD", guarded_broker)
    assert not guarded._enter("BUY", 1000, 1000)
    assert guarded.data_error == "live cap reached"
    assert not guarded_client.orders

    live_service = DeltaService(mode="live")
    live_service.strategies = {
        minutes: DeltaGold(minutes, "PAXGUSD", DeltaLiveBroker(MemoryStore(), {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, FakeLiveClient()))
        for minutes in (5, 7, 15, 30)
    }
    live_service.strategies[5].broker.state["position"] = {"id": "p5", "side": "BUY"}
    try:
        live_service._validate_live_entry(7, "BUY")
        assert False, "second live timeframe entry should be blocked"
    except ValueError as exc:
        assert "one active live timeframe trade" in str(exc)
    try:
        live_service._validate_live_entry(30, "SELL")
        assert False, "opposite-side live entry should be blocked"
    except ValueError as exc:
        assert "one active live timeframe trade" in str(exc)
    live_service.strategies[5].broker.state["position"] = None
    live_service._validate_live_entry(30, "BUY")

    reject_store = MemoryStore()
    reject_client = FakeCloseRejectClient()
    reject_broker = DeltaLiveBroker(reject_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, reject_client)
    reject = DeltaGold(15, "PAXGUSD", reject_broker)
    assert reject._enter("BUY", 1000, 1000)
    try:
        reject_broker.close_trade(reject_broker.state["position"], 1005, "SL")
        assert False, "live close rejection should bubble once with exact Delta detail"
    except RuntimeError as exc:
        assert "position_not_found" in str(exc)
    first_order_count = len(reject_client.orders)
    assert reject_broker.state["position"]["live_close_retry_after"] > time.time()
    try:
        reject_broker.close_trade(reject_broker.state["position"], 1005, "SL")
        assert False, "retry-paused live close should not report success"
    except RuntimeError as exc:
        assert "retry paused" in str(exc)
    assert len(reject_client.orders) == first_order_count

    unfilled_store = MemoryStore()
    unfilled_client = FakeUnfilledCloseClient()
    unfilled_broker = DeltaLiveBroker(unfilled_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, unfilled_client)
    unfilled = DeltaGold(15, "PAXGUSD", unfilled_broker)
    assert unfilled._enter("BUY", 1000, 1000)
    try:
        unfilled_broker.close_trade(unfilled_broker.state["position"], 1005, "TARGET")
        assert False, "unfilled live close must not create a fake closed trade"
    except RuntimeError as exc:
        assert "not filled" in str(exc) or "partially filled" in str(exc)
    assert unfilled_broker.state["position"] is not None
    assert not unfilled_store.closed

    external_store = MemoryStore()
    external_client = FakeLiveClient()
    external_broker = DeltaLiveBroker(external_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, external_client)
    external = DeltaGold(15, "PAXGUSD", external_broker)
    assert external._enter("BUY", 1000, 1000)
    orders_before_external_close = len(external_client.orders)
    assert external_broker.record_external_close(1007, "MANUAL_EXTERNAL_EXIT")
    assert len(external_client.orders) == orders_before_external_close
    assert external_broker.state["position"] is None
    assert external_store.closed[-1]["exit_reason"] == "MANUAL_EXTERNAL_EXIT"
    assert external_store.closed[-1]["external_close"] is True

    # 2026-09-17: manual SL / target edits mark sl_source / target_source and
    # yield SL_EDITED / TARGET_EDITED exit reasons instead of the generic
    # SL / TARGET / MANUAL_EXTERNAL_EXIT. Audit rows read honestly for both
    # paper (engine fires close_trade) and live (Delta stop fires natively,
    # reconciler picks up via record_external_close with no explicit reason).
    edited_paper = strategy()
    edited_paper._enter("BUY", 1000, 1000)
    pos = edited_paper._open_position()
    pos["sl_price"] = 1004  # simulate engine edit_protection outcome
    pos["sl_source"] = "manual"
    reason = Algo3SilverMicro._stop_exit_reason(pos)
    assert reason == "SL_EDITED", f"got {reason!r}"
    pos["target_source"] = "manual"
    assert Algo3SilverMicro._target_exit_reason(pos) == "TARGET_EDITED"
    # Manual override wins over trailing marker.
    pos["trailing_sl_active"] = True
    assert Algo3SilverMicro._stop_exit_reason(pos) == "SL_EDITED"

    # Live reconciler: record_external_close infers SL from the recent
    # close-failure and upgrades to SL_EDITED when the SL was manually moved.
    infer_store = MemoryStore()
    infer_client = FakeLiveClient()
    infer_broker = DeltaLiveBroker(infer_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, infer_client)
    infer = DeltaGold(15, "PAXGUSD", infer_broker)
    assert infer._enter("BUY", 1000, 1000)
    infer_broker.state["position"]["sl_source"] = "manual"
    infer_broker.state["live_close_error"] = {"reason": "SL", "message": "delta beat us"}
    assert infer_broker.record_external_close(998)
    assert infer_store.closed[-1]["exit_reason"] == "SL_EDITED"

    # And plain SL (no manual edit) when sl_source is not manual.
    plain_store = MemoryStore()
    plain_client = FakeLiveClient()
    plain_broker = DeltaLiveBroker(plain_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, plain_client)
    plain = DeltaGold(15, "PAXGUSD", plain_broker)
    assert plain._enter("BUY", 1000, 1000)
    plain_broker.state["live_close_error"] = {"reason": "TARGET"}
    assert plain_broker.record_external_close(1020)
    assert plain_store.closed[-1]["exit_reason"] == "TARGET"

    # Truly external exit (no close attempt recorded) stays as MANUAL_EXTERNAL_EXIT.
    ext2_store = MemoryStore()
    ext2_client = FakeLiveClient()
    ext2_broker = DeltaLiveBroker(ext2_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, ext2_client)
    ext2 = DeltaGold(15, "PAXGUSD", ext2_broker)
    assert ext2._enter("BUY", 1000, 1000)
    ext2_broker.state["live_close_error"] = None
    assert ext2_broker.record_external_close(1002)
    assert ext2_store.closed[-1]["exit_reason"] == "MANUAL_EXTERNAL_EXIT"

    # update_protection stamps sl_source / target_source for the LIVE broker.
    upd_store = MemoryStore()
    upd_client = FakeLiveClient()
    upd_broker = DeltaLiveBroker(upd_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, upd_client)
    upd = DeltaGold(15, "PAXGUSD", upd_broker)
    assert upd._enter("BUY", 1000, 1000)
    original = copy.deepcopy(upd_broker.state["position"])
    updated = upd_broker.update_protection(original, 995.0, original["target_price"], 1001.0)
    assert updated["sl_source"] == "manual"
    assert updated.get("target_source") is None  # target unchanged
    updated2 = upd_broker.update_protection(updated, 995.0, updated["target_price"] + 5, 1001.0)
    assert updated2["sl_source"] == "manual"
    assert updated2["target_source"] == "manual"

    # 2026-09-17 exit_mode-switching regression: the Edit dialog can flip the
    # position's TSL policy between fixed / breakeven / three-candle mid-trade.
    from app.strategies.delta_gold import exit_mode_snapshot_patch, apply_exit_mode_patch
    import datetime as _dt
    now_utc = _dt.datetime(2026, 9, 17, 12, 30, tzinfo=_dt.timezone.utc)

    # Same-mode -> None (no-op) so callers can skip the write.
    fixed_pos = {"side": "BUY", "entry_price": 1000, "sl_price": 985, "target_price": 1050,
                 "signal_snapshot": {"silver_exit_policy": "fixed_target_sl"}}
    assert exit_mode_snapshot_patch("fixed_target_sl", fixed_pos, DELTA_DEFAULTS, 15, "gold", now_utc) is None

    # Fixed -> breakeven builds a fresh silver_breakeven from the current sl/target
    # (activation_price derives from entry ± activation_points). Local var name
    # deliberately NOT "patch" to avoid shadowing unittest.mock.patch elsewhere
    # in run() — Python function-scoping compiles the whole body as one scope.
    be_patch = exit_mode_snapshot_patch("target_to_breakeven_sl", fixed_pos, DELTA_DEFAULTS, 15, "gold", now_utc)
    assert be_patch is not None
    assert be_patch["silver_exit_policy"] == "target_to_breakeven_sl"
    assert be_patch["silver_breakeven"]["armed"] is False
    assert be_patch["silver_breakeven"]["activation_price"] == 1000 + DELTA_DEFAULTS["tsl_activate_points"]
    assert be_patch["silver_breakeven"]["target_price"] == 1050
    assert be_patch["silver_breakeven"]["initial_sl_price"] == 985
    assert be_patch["delta_three_candle_tsl"] is None  # None means delete key

    # Fixed -> three_candle builds a delta_three_candle_tsl bucketed at NOW,
    # not at the original entry — the operator activates from this moment.
    tc_patch = exit_mode_snapshot_patch(DELTA_EXIT_MODE_THREE_CANDLE, fixed_pos, DELTA_DEFAULTS, 15, "gold", now_utc)
    assert tc_patch["silver_exit_policy"] == DELTA_EXIT_MODE_THREE_CANDLE
    tc = tc_patch["delta_three_candle_tsl"]
    assert tc["events"] == [] and tc["evaluations"] == []
    assert tc["status"] == "waiting_for_three_post_entry_candles"
    assert "after_edit" in tc["window_rule"]
    # entry_bucket is aligned to the 15m boundary containing now_utc IST.
    bucket_dt = _dt.datetime.fromisoformat(tc["entry_bucket"])
    from app.timezone import IST
    now_ist = now_utc.astimezone(IST)
    assert bucket_dt <= now_ist < bucket_dt + _dt.timedelta(minutes=15)

    # Silver never allows three-candle; strategy validator and this helper agree.
    silver_pos = dict(fixed_pos)
    try:
        exit_mode_snapshot_patch(DELTA_EXIT_MODE_THREE_CANDLE, silver_pos, DELTA_DEFAULTS, 15, "silver", now_utc)
        assert False, "silver + three_candle must raise"
    except ValueError as exc:
        assert "Silver" in str(exc)

    # apply_exit_mode_patch merges + deletes correctly and clears trailing flag.
    live_pos = {"side": "BUY", "entry_price": 1000, "sl_price": 985, "target_price": 1050,
                "trailing_sl_active": True,
                "signal_snapshot": {"silver_exit_policy": "target_to_breakeven_sl",
                                     "silver_breakeven": {"armed": True, "activation_price": 1010}}}
    tc_patch2 = exit_mode_snapshot_patch(DELTA_EXIT_MODE_THREE_CANDLE, live_pos, DELTA_DEFAULTS, 15, "gold", now_utc)
    apply_exit_mode_patch(live_pos, tc_patch2)
    assert live_pos["signal_snapshot"]["silver_exit_policy"] == DELTA_EXIT_MODE_THREE_CANDLE
    assert "silver_breakeven" not in live_pos["signal_snapshot"]  # stripped
    assert "delta_three_candle_tsl" in live_pos["signal_snapshot"]
    assert "trailing_sl_active" not in live_pos  # cleared

    # End-to-end via DeltaLiveBroker.update_protection: exit_mode_patch is
    # applied AND Delta orders still edit (verified by client.orders growth).
    switch_store = MemoryStore()
    switch_client = FakeLiveClient()
    switch_broker = DeltaLiveBroker(switch_store, {**DELTA_DEFAULTS, "trading_enabled": True}, live_product, switch_client)
    switch = DeltaGold(15, "PAXGUSD", switch_broker)
    assert switch._enter("BUY", 1000, 1000)
    before_edits = len(switch_client.edits)
    current_snapshot = copy.deepcopy(switch_broker.state["position"])
    tc_patch_live = exit_mode_snapshot_patch(
        DELTA_EXIT_MODE_THREE_CANDLE, current_snapshot, DELTA_DEFAULTS, 15, "gold", now_utc,
    )
    updated_live = switch_broker.update_protection(current_snapshot, 985.0, 1050.0, 1001.0, exit_mode_patch=tc_patch_live)
    assert updated_live["signal_snapshot"]["silver_exit_policy"] == DELTA_EXIT_MODE_THREE_CANDLE
    assert updated_live["signal_snapshot"]["delta_three_candle_tsl"]["events"] == []
    # A mode-only change does not rewrite unchanged exchange orders.
    assert len(switch_client.edits) == before_edits
    latest_edit = updated_live["protection_edits"][-1]
    assert latest_edit["exit_mode_from"] == "fixed_target_sl"
    assert latest_edit["exit_mode_to"] == DELTA_EXIT_MODE_THREE_CANDLE

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
    with patch.dict(os.environ, {
        "DELTA_API_KEY": "read-key",
        "DELTA_API_SECRET": "read-secret",
        "DELTA_LIVE_API_KEY": "live-key",
        "DELTA_LIVE_API_SECRET": "live-secret",
    }, clear=True):
        read_client = DeltaClient()
        live_client = DeltaClient(credential_scope="live")
        assert read_client.key == "read-key" and read_client.secret == "read-secret"
        assert live_client.key == "live-key" and live_client.secret == "live-secret"
        try:
            read_client.post("/v2/orders", {"product_id": 1})
            raise AssertionError("read credentials accepted for trading endpoint")
        except ValueError:
            pass
    print("smoke_delta_paper: all checks passed")


if __name__ == "__main__":
    run()
