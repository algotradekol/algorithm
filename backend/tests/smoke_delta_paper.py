"""Offline Delta reference/parity, 24/7, persistence and protection regressions."""
import copy
import datetime
import os
import time
from unittest.mock import patch

from app.delta_client import DeltaClient
from app.delta_engine import DeltaService
from app.delta_paper import DeltaPaperBroker
from app.strategies.algo3_silver_micro import Algo3SilverMicro
from app.strategies.delta_gold import DeltaGold, DELTA_DEFAULTS, validate_settings


PRODUCT = {"symbol": "PAXGUSD", "contract_value": "0.001", "tick_size": "0.01", "taker_commission_rate": "0.0002"}


class MemoryStore:
    def __init__(self):
        self.state = None
        self.closed = []
        self.fail = False

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state, trade=None):
        if self.fail:
            raise RuntimeError("mock storage failure")
        self.state = copy.deepcopy(state)
        if trade:
            self.closed.append(copy.deepcopy(trade))


def strategy(minutes=15, settings=None, store=None):
    defaults = {**DELTA_DEFAULTS, "trading_enabled": True, "silver_breakout_points": 10,
                "sl_points": 20, "target_points": 100, "tsl_activate_points": 30, **(settings or {})}
    broker = DeltaPaperBroker(store or MemoryStore(), defaults, PRODUCT)
    return DeltaGold(minutes, "PAXGUSD", broker)


def history(minutes, end, close=1000):
    interval = minutes * 60
    return [{"time": end - (30 - i) * interval, "open": close, "high": close + 1,
             "low": close - 1, "close": close, "volume": 100} for i in range(30)]


def run():
    assert DeltaGold._check_triggers is Algo3SilverMicro._check_triggers
    assert DeltaGold._update_setups is Algo3SilverMicro._update_setups
    assert DeltaGold.check_exits is Algo3SilverMicro.check_exits
    for minutes in (15, 60):
        # Midnight UTC on Saturday: no NSE/MCX session guard or daily reset.
        now = int(datetime.datetime(2026, 9, 12, tzinfo=datetime.timezone.utc).timestamp())
        s = strategy(minutes)
        rows = history(minutes, now)
        rows[-1].update(open=1000, high=1031, low=999, close=1030)
        rows.append({"time": now, "open": 1031, "high": 1042, "low": 1030, "close": 1041, "volume": 100})
        s.ingest_history(rows, now + 5)
        assert s._buy_setup_close == 1030
        assert s.last_candle_epoch == now - minutes * 60
        old_ema = s._ema20
        s.ingest_history(rows, now + 5)
        assert s._ema20 == old_ema and len(s._bars) == 30
        s.process_price(1039, now + 6)
        s.process_price(1041, now + 7)
        position = s._open_position()
        assert position and position["side"] == "BUY" and position["entry_price"] == 1041
        assert position["signal_snapshot"]["timeframe"] == f"{minutes}m"
        s.square_off_all()
        assert s._open_position()
        s.process_price(1021, now + 8)
        assert not s._open_position()
        assert s.broker.store.closed[-1]["exit_reason"] == "SL"
        assert abs(s.broker.store.closed[-1]["gross_pnl"] + 0.02) < 1e-8
        s.process_price(1042, now + 9)
        assert not s._open_position()  # 30-second SL cooldown
        recovered = strategy(minutes, store=s.broker.store)
        assert recovered._post_sl_cooldown_remaining() > 0

        # SELL retains original forming-red, below-EMA condition.
        sell = strategy(minutes)
        rows = history(minutes, now)
        rows[-1].update(open=1000, high=1001, low=969, close=970)
        rows.append({"time": now, "open": 975, "high": 975, "low": 955, "close": 955})
        sell.ingest_history(rows, now + 5)
        sell.process_price(961, now + 6)
        sell.process_price(959, now + 7)
        assert sell._open_position()["side"] == "SELL"

        # Both sides: activation to entry, then final target or breakeven exit.
        for side in ("BUY", "SELL"):
            for outcome in ("TARGET", "TRAILING_SL"):
                tsl = strategy(minutes, {"exit_mode": "target_to_breakeven_sl"})
                assert tsl._enter(side, 1000, 1000)
                tsl._last_tick_ltp = 1030 if side == "BUY" else 970
                tsl.check_exits()
                pos = tsl._open_position()
                assert pos["sl_price"] == 1000 and pos["trailing_sl_active"]
                # New settings must not alter the open trade's captured policy.
                tsl.settings["exit_mode"] = "fixed_target_sl"
                tsl._last_tick_ltp = (1100 if side == "BUY" else 900) if outcome == "TARGET" else 1000
                tsl.check_exits()
                assert tsl.broker.store.closed[-1]["exit_reason"] == outcome

        # History gaps reject the whole update without changing references.
        gap = strategy(minutes)
        broken = history(minutes, now)
        del broken[-3]
        try:
            gap.ingest_history(broken, now + 5)
            raise AssertionError("history gap accepted")
        except ValueError:
            assert not gap._history_ready and gap._buy_setup_close is None

    s = strategy()
    s._enter("BUY", 1000, 1000)
    recovered = strategy(store=s.broker.store)
    assert recovered._open_position()["id"] == s._open_position()["id"]
    s.broker.store.fail = True
    try:
        s.broker.close_trade(s._open_position(), 1010, "MANUAL_EXIT")
        raise AssertionError("failed close reported success")
    except RuntimeError:
        assert s._open_position()

    reversal = strategy()
    reversal._enter("BUY", 1000, 1000)
    assert reversal._fire_entry("SELL", 990, 990)
    assert reversal._open_position()["side"] == "SELL"
    assert reversal._open_position()["sl_price"] == 1010
    assert reversal.broker.store.closed[-1]["exit_reason"] == "REVERSAL_CONTRA_SIGNAL"
    # A fixed target really exits (it must not arm breakeven).
    reversal._last_tick_ltp = 890
    reversal.check_exits()
    assert reversal.broker.store.closed[-1]["exit_reason"] == "TARGET"

    manual = strategy()
    manual._buy_setup_bar_at = datetime.datetime(2026, 9, 9, 9)
    manual._buy_setup_close = 990
    manual._prev_ltp = 1010
    manual._enter("BUY", 1010, 1000)
    manual.broker.close_trade(manual._open_position(), 1010, "MANUAL_EXIT")
    assert not manual._fire_entry("BUY", 1011, 1000)
    manual._prev_ltp = 999
    assert manual._fire_entry("BUY", 1001, 1000)

    service = DeltaService()
    service.strategies = {15: strategy(), 60: strategy(60)}
    service.save_settings(15, {"silver_lots": 2})
    assert service.strategies[60].settings["silver_lots"] == 1
    with patch.object(service.strategies[15], "process_price") as process:
        service.accept_price(1000, time.time() - 60, "REST")
        process.assert_not_called()
        fresh = time.time()
        service.accept_price(1000, fresh, "WS")
        service.accept_price(1000, fresh, "REST")
        assert process.call_count == 1  # Cross-source duplicates are ignored.

    protection = strategy()
    protection._enter("BUY", 1000, 1000)
    protection.data_error = "missing history"
    protection.process_price(980, time.time())
    assert not protection._open_position()  # Bad history cannot disable exits.

    isolated = strategy()
    assert not isolated._open_position() and isolated.broker.state["closed_count"] == 0
    for invalid in (0, -1, float("nan"), float("inf")):
        try:
            validate_settings({**DELTA_DEFAULTS, "sl_points": invalid})
            raise AssertionError("invalid setting accepted")
        except ValueError:
            pass

    with patch.dict(os.environ, {}, clear=True):
        client = DeltaClient()
        assert client.region == "india" and client.symbol == "PAXGUSD"
        assert client.base_url == "https://api.india.delta.exchange"
    with patch.dict(os.environ, {"DELTA_EXCHANGE": "global"}, clear=True):
        assert DeltaClient().symbol == "PAXGUSDT"
    with patch.dict(os.environ, {"DELTA_EXCHANGE": "india", "DELTA_PROXY_URL": "http://name:pass@proxy.example:3128"}):
        client = DeltaClient()
        assert client.session.proxies["https"] == client.proxy
        assert client.websocket_options()["http_proxy_host"] == "proxy.example"
        with patch.object(client, "get", return_value=[{"price": "1000", "timestamp": time.time() * 1e6}]):
            assert client.recent_trade()[0] == 1000
    with patch.dict(os.environ, {"DELTA_EXCHANGE": "india", "DELTA_GOLD_SYMBOL": "PAXGUSD", "DELTA_PROXY_URL": ""}):
        client = DeltaClient()
        assert client.base_url == "https://api.india.delta.exchange"
        assert client.symbol == "PAXGUSD"
        with patch.object(client.session, "get") as request:
            try:
                client.get("/v2/orders", private=True)
                raise AssertionError("private order path should be blocked")
            except ValueError:
                request.assert_not_called()
    print("smoke_delta_paper: all checks passed")


if __name__ == "__main__":
    run()
