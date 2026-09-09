import datetime
from unittest.mock import patch

from app.candle_aggregator import CandleAggregator
from app.strategies.algo3_silver_micro import Algo3SilverMicro
from tests.smoke_silver_v_micro import make_strategy, _expand_9m_bar


def run():
    aggregator = CandleAggregator()
    aggregator.on_tick("TEST", 100, 10000)
    aggregator.on_tick("TEST", 101, 0)
    aggregator.on_tick("TEST", 102, 10020)
    assert aggregator.symbols["TEST"].volume == 20
    aggregator.on_tick("TEST", 102, 0)
    aggregator.on_tick("TEST", 103, 10030)
    assert aggregator.symbols["TEST"].volume == 30

    strategy = make_strategy()
    start = datetime.datetime(2026, 8, 1, 9)
    candles = _expand_9m_bar(start, 100, 121, 99, 120, 200)
    for candle in candles[:-1]:
        strategy._ingest_minute_candle(candle, False)
    strategy._finalize_bar(False)
    assert not strategy._bars
    assert strategy._ema20 == 100
    assert strategy._buy_setup_close is None

    strategy = make_strategy()
    with patch("app.strategies.algo6_silver_v_micro.record_setup_event"):
        for candle in candles:
            strategy._ingest_minute_candle(candle, False)
        strategy._finalize_bar(False)
    assert len(strategy._bars) == 1
    assert strategy._references_current(start + datetime.timedelta(minutes=9))
    assert not strategy._references_current(start + datetime.timedelta(minutes=18))

    strategy._last_fired_buy_bar_at = start
    strategy._sl_cooldown_until_monotonic = 12345
    def replay(instance):
        instance._reset_aggregation_state()
        instance._history_loading = False
    with patch.object(Algo3SilverMicro, "_load_history_background", replay):
        strategy._load_history_background()
    assert strategy._last_fired_buy_bar_at == start
    assert strategy._sl_cooldown_until_monotonic == 12345
    assert not strategy._reference_rebuilding

    strategy._is_paper_mode_active = lambda: True
    with patch.object(strategy, "_request_reference_repair") as repair:
        strategy.on_candle_close(strategy.symbol, candles[0], {})
        repair.assert_called_once()
    assert not strategy._bars
    strategy._history_loading = True
    with patch.object(strategy, "_fire_entry") as entry:
        strategy._check_triggers(1000)
        entry.assert_not_called()
    print("smoke_silver_v_feed passed")


if __name__ == "__main__":
    run()
