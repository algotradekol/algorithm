"""Offline Delta replay correctness and paper-parity checks."""
import copy
import datetime as dt
from unittest.mock import MagicMock, patch

from app.delta_backtest import ReplayBroker, ReplayGold, date_range, fetch_candles, replay
from app.strategies.delta_gold import DELTA_DEFAULTS
from tests.smoke_delta_paper import strategy, history, PRODUCT

NOW = 1789171200  # UTC midnight, aligned to both tested timeframes.
SETTINGS = {**DELTA_DEFAULTS, 'trading_enabled': True, 'silver_breakout_points': 10,
            'sl_points': 20, 'target_points': 100, 'tsl_activate_points': 30}
META = {**PRODUCT, 'initial_margin': '1', 'quoting_asset': {'symbol': 'USD'}}


def run():
    for minutes in (15, 60):
        for side in ('BUY', 'SELL'):
            for mode in ('fixed_target_sl', 'target_to_breakeven_sl'):
                for outcome in ('target', 'stop', 'breakeven'):
                    settings = {**SETTINGS, 'exit_mode': mode}
                    live = strategy(minutes, settings)
                    broker = ReplayBroker(settings, META)
                    test = ReplayGold(minutes, META['symbol'], broker)
                    broker.now = NOW
                    live._enter(side, 1000, 1000)
                    test._fire_entry(side, 1000, 1000)
                    sign = 1 if side == 'BUY' else -1
                    prices = [1000 + sign * 100] if outcome == 'target' else [1000 - sign * 20] if outcome == 'stop' else [1000 + sign * 30, 1000]
                    for i, price in enumerate(prices):
                        broker.now = NOW + i + 1
                        for s in (live, test):
                            s._last_tick_ltp = price
                            s.check_exits()
                    a, b = live._open_position(), test._open_position()
                    assert bool(a) == bool(b), (minutes, side, mode, outcome)
                    if a:
                        assert a['sl_price'] == b['sl_price'] and a['target_price'] == b['target_price']
                    else:
                        x, y = live.broker.store.closed[-1], broker.closed[-1]
                        assert (x['exit_reason'], x['exit_price'], x['net_pnl']) == (y['exit_reason'], y['exit_price'], y['net_pnl'])
                        assert y['exit_time'].startswith('2026-09-12')
                        if outcome == 'stop':
                            assert test._post_sl_cooldown_remaining() == 30
                            broker.now += 30
                            assert test._post_sl_cooldown_remaining() == 0

        # Trigger parity on both sides, including reversal and same-reference reentry.
        actual = strategy(minutes, SETTINGS)
        test = ReplayGold(minutes, META['symbol'], ReplayBroker(SETTINGS, META))
        refs = history(minutes, NOW)
        refs[-2].update(open=1000, high=1001, low=969, close=970)
        refs[-1].update(open=1000, high=1031, low=999, close=1030)
        refs.append({'time': NOW, 'open': 1000, 'high': 1200, 'low': 800, 'close': 1000})
        for s in (actual, test):
            s.ingest_history(refs, NOW)
        for i, price in enumerate((1039, 1041, 1050, 959, 950, 859, 850)):
            actual.process_price(price, NOW + i * 31)
            test.tick(price, NOW + i * 31)
            a, b = actual._open_position(), test._open_position()
            assert bool(a) == bool(b)
            if a:
                assert (a['side'], a['entry_price'], a['sl_price'], a['target_price']) == (b['side'], b['entry_price'], b['sl_price'], b['target_price'])

        refs = history(minutes, NOW)
        refs[-1].update(open=1000, high=1031, low=999, close=1030)
        refs.append({'time': NOW, 'open': 1039, 'high': 2000, 'low': 900, 'close': 1999})
        minute = [{'time': NOW, 'open': 1039, 'high': 1145, 'low': 1038, 'close': 1140}]
        first = replay(META, 'india', minutes, SETTINGS, refs, minute, NOW, NOW + 60, 'high_first')
        assert first['trades'] and first['trades'][0]['entry_price'] == 1040
        assert first['trades'][0]['exit_price'] == 1140 and first['trades'][0]['exit_reason'] == 'TARGET'
        assert first['trades'][0]['signal_snapshot']['setup_close'] == 1030
        assert first['open_position']  # No artificial range-end/EOD liquidation.
        changed = copy.deepcopy(refs)
        changed[-1].update(high=3000, close=2999)
        second = replay(META, 'india', minutes, SETTINGS, changed, minute, NOW, NOW + 60, 'high_first')
        assert first['trades'] == second['trades']  # Forming reference close cannot leak.

        # SELL reference + downward path fills at the threshold, not the minute low.
        sell_refs = history(minutes, NOW)
        sell_refs[-1].update(open=1000, high=1001, low=969, close=970)
        sell_refs.append({'time': NOW, 'open': 961, 'high': 962, 'low': 850, 'close': 855})
        sell_minute = [{'time': NOW, 'open': 961, 'high': 962, 'low': 855, 'close': 860}]
        result = replay(META, 'india', minutes, SETTINGS, sell_refs, sell_minute, NOW, NOW + 60, 'low_first')
        assert result['trades'][0]['side'] == 'SELL' and result['trades'][0]['entry_price'] == 960
        assert result['trades'][0]['exit_price'] == 860

        # A contra signal reverses before either broad protective level is reached.
        wide = {**SETTINGS, 'sl_points': 500, 'target_points': 500}
        actual = strategy(minutes, wide)
        test = ReplayGold(minutes, META['symbol'], ReplayBroker(wide, META))
        for s in (actual, test):
            s.ingest_history(refs, NOW)
            s._sell_setup_close = 1000
            s._sell_setup_bar_at = s._buy_setup_bar_at
        for i, price in enumerate((1039, 1041, 989)):
            actual.process_price(price, NOW + i)
            test.tick(price, NOW + i)
        assert actual._open_position()['side'] == test._open_position()['side'] == 'SELL'
        assert test.broker.closed[-1]['exit_reason'] == 'REVERSAL_CONTRA_SIGNAL'

        # The explicit path assumption can change the outcome of a conflicting bar.
        conflict = [{'time': NOW, 'open': 1039, 'high': 1145, 'low': 900, 'close': 1040}]
        a = replay(META, 'india', minutes, SETTINGS, refs, conflict, NOW, NOW + 60, 'high_first')
        b = replay(META, 'india', minutes, SETTINGS, refs, conflict, NOW, NOW + 60, 'low_first')
        assert a['trades'] != b['trades']

        # A valid reference with an offset too wide for the price path should
        # explain the flat result instead of looking like a silent engine bug.
        wide_settings = {**SETTINGS, 'silver_breakout_points': 200}
        quiet_minute = [{'time': NOW, 'open': 1030, 'high': 1040, 'low': 1020, 'close': 1035}]
        flat = replay(META, 'india', minutes, wide_settings, refs, quiet_minute, NOW, NOW + 60, 'high_first')
        diag = flat['diagnostics']
        assert flat['summary']['trades'] == 0 and not flat['open_position']
        assert diag['warmup_buy_reference'] == 1030
        assert diag['buy_threshold_minutes'] == 0
        assert diag['closest_buy']['trigger'] == 1230
        assert diag['closest_buy']['observed_price'] == 1040
        assert diag['closest_buy']['remaining_points'] == 190
        assert 'No active reference' in diag['explanation']

        # With the narrower test offset, the same replay records entry events.
        assert first['diagnostics']['entries'] >= 1
        assert first['diagnostics']['buy_threshold_minutes'] >= 1

    fake = MagicMock()
    candles = [{'time': t, 'open': 100, 'high': 102, 'low': 99, 'close': 101} for t in range(0, 1600 * 60, 60)]
    fake.candles.side_effect = lambda res, start, end: list(reversed([r for r in candles if start <= r['time'] <= end]))
    assert len(fetch_candles(fake, '1m', 0, 1600 * 60, 60)) == 1600
    assert fake.candles.call_count == 2
    fake.candles.side_effect = None
    fake.candles.return_value = candles[:1]
    try:
        fetch_candles(fake, '1m', 0, 120, 60)
        raise AssertionError('history gaps accepted')
    except ValueError as exc:
        assert 'missing' in str(exc)
    for start, end in ((dt.date(2026, 1, 2), dt.date(2026, 1, 1)), (dt.date(2026, 1, 1), dt.date(2026, 2, 1))):
        try:
            date_range(start, end, NOW)
            raise AssertionError('invalid range accepted')
        except ValueError:
            pass
    print('smoke_delta_backtest: all checks passed')


if __name__ == '__main__':
    run()
