"""Offline metal isolation, normal Silver rules, endpoint gates and replay parity."""
import copy
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.delta_backtest import ReplayBroker, ReplaySilver, replay
from app.delta_config import delta_capabilities
from app.delta_engine import DeltaService
from app.delta_paper import DeltaPaperBroker
from app.delta_routes import router
from app.strategies.algo3_silver_micro import Algo3SilverMicro
from app.strategies.delta_gold import SILVER_DEFAULTS, validate_settings
from app.strategies.delta_silver import DeltaSilver, SILVER_TIMEFRAMES
from tests.smoke_delta_paper import MemoryStore, history, strategy as gold_strategy

NOW = 1789200000
PRODUCT = {'symbol': 'SLVONUSD', 'id': 124058, 'contract_value': '0.1', 'tick_size': '0.01',
           'taker_commission_rate': '0.0002', 'initial_margin': '1', 'quoting_asset': {'symbol': 'USD'}}
SETTINGS = {**SILVER_DEFAULTS, 'trading_enabled': True, 'silver_breakout_points': 10,
            'sl_points': 20, 'target_points': 100, 'tsl_activate_points': 30}


def silver(minutes, settings=None):
    return DeltaSilver(minutes, PRODUCT['symbol'], DeltaPaperBroker(MemoryStore(), {**SETTINGS, **(settings or {})}, PRODUCT))


def run():
    assert DeltaSilver._check_triggers is Algo3SilverMicro._check_triggers
    assert not SILVER_DEFAULTS['trading_enabled']
    assert SILVER_DEFAULTS['exit_mode'] == 'target_to_breakeven_sl'
    assert SILVER_DEFAULTS['silver_breakout_points'] == 0.10
    assert SILVER_DEFAULTS['sl_points'] == 0.30
    assert SILVER_DEFAULTS['tsl_activate_points'] == 0.30
    assert SILVER_DEFAULTS['target_points'] == 1.0
    for minutes in SILVER_TIMEFRAMES:
        refs = history(minutes, NOW)
        refs[-2].update(open=1000, high=1001, low=969, close=970, volume=1)
        refs[-1].update(open=1000, high=1031, low=999, close=1030, volume=1)
        refs.append({'time': NOW, 'open': 1000, 'high': 1200, 'low': 800, 'close': 1000, 'volume': 1})
        s = silver(minutes)
        g = gold_strategy(minutes)
        for item in (s, g):
            item.ingest_history(refs, NOW)
        assert s._buy_setup_close == 1030 and s._sell_setup_close == 970
        assert g._buy_setup_close is None and g._sell_setup_close is None  # Volume rule stays Gold-only.

        fallback = silver(minutes)
        fallback.ingest_history(refs, NOW)
        assert fallback._open_position() is None
        next_rows = list(refs[:-1]) + [
            {'time': NOW, 'open': 1000, 'high': 1045, 'low': 999, 'close': 1042, 'volume': 1},
            {'time': NOW + minutes * 60, 'open': 1042, 'high': 1043, 'low': 1041, 'close': 1042, 'volume': 1},
        ]
        fallback.ingest_history(next_rows, NOW + minutes * 60 + 1)
        assert fallback._open_position()['side'] == 'BUY'
        assert fallback._open_position()['entry_price'] == 1042

        # The actual Silver Micro trigger function executes on every Delta timeframe.
        s.process_price(1039, NOW + 1)
        s.process_price(1041, NOW + 2)
        assert s._open_position()['side'] == 'BUY' and s._open_position()['entry_price'] == 1041
        assert s._open_position()['signal_snapshot']['strategy_version'] == 'silver_micro_delta_usd_v1'

        for side in ('BUY', 'SELL'):
            for mode in ('fixed_target_sl', 'target_to_breakeven_sl'):
                for outcome in ('SL', 'TARGET', 'TRAILING_SL'):
                    settings = {**SETTINGS, 'exit_mode': mode}
                    actual = silver(minutes, settings)
                    sim = ReplaySilver(minutes, PRODUCT['symbol'], ReplayBroker(settings, PRODUCT))
                    sim.broker.now = NOW
                    for item in (actual, sim):
                        item._enter(side, 1000, 1000)
                    sign = 1 if side == 'BUY' else -1
                    prices = [1000 - sign * 20] if outcome == 'SL' else [1000 + sign * 100] if outcome == 'TARGET' else [1000 + sign * 30, 1000]
                    for i, price in enumerate(prices):
                        sim.broker.now = NOW + i + 1
                        for item in (actual, sim):
                            item._last_tick_ltp = price
                            item.check_exits()
                    assert bool(actual._open_position()) == bool(sim._open_position())
                    if not actual._open_position():
                        a, b = actual.broker.store.closed[-1], sim.broker.closed[-1]
                        assert (a['exit_price'], a['exit_reason'], a['net_pnl']) == (b['exit_price'], b['exit_reason'], b['net_pnl'])

        # Reversal leaves no rest timer; ordinary exits do.
        wide = silver(minutes, {'sl_points': 500, 'target_points': 500})
        wide.ingest_history(refs, NOW)
        for i, price in enumerate((1039, 1041, 959)):
            wide.process_price(price, NOW + i)
        assert wide._open_position()['side'] == 'SELL'
        assert wide.broker.store.closed[-1]['exit_reason'] == 'REVERSAL_CONTRA_SIGNAL'
        assert not wide.cooldown_status()['active']

        minutes_data = [{'time': NOW, 'open': 1039, 'high': 1145, 'low': 1038, 'close': 1140}]
        result = replay(PRODUCT, 'india', minutes, SETTINGS, refs, minutes_data, NOW, NOW + 60, 'high_first', 'silver')
        assert result['trades'] and result['trades'][0]['exit_reason'] == 'TARGET'
        assert result['symbol'] == 'SLVONUSD' and result['asset'] == 'silver'

    try:
        validate_settings({**SETTINGS, 'exit_mode': 'three_candle_tsl'}, 'silver')
        raise AssertionError('Silver accepted Gold three-candle policy')
    except ValueError:
        pass

    with patch.dict(os.environ, {'DELTA_HIDDEN_SECTIONS': ''}):
        keys = []
        def client(asset='gold'):
            product = PRODUCT if asset == 'silver' else {**PRODUCT, 'symbol': 'PAXGUSD'}
            return SimpleNamespace(region='india', symbol=product['symbol'], configuration_error=lambda: None, product=lambda: product)
        with patch('app.delta_engine.DeltaClient', side_effect=client), patch('app.delta_engine.DeltaStore', side_effect=lambda key: keys.append(key) or MemoryStore()), patch('app.delta_engine.threading.Thread'):
            gold, sil = DeltaService(), DeltaService('silver')
            gold._initialize()
            sil._initialize()
        assert len(keys) == len(set(keys)) == 12
        assert 'delta:india:PAXGUSD:15:paper' in keys  # Existing Gold keys survive.
        assert 'delta:silver:india:SLVONUSD:15:paper' in keys
        for service in (gold, sil):
            service.last_event_at = time.time()
            service.last_price = 1000
        sil.strategies[15]._enter('BUY', 1000, 1000)
        sil.strategies[15].broker.close_trade(sil.strategies[15]._open_position(), 1010, 'TARGET')
        assert sil.overview()['totals']['all_time']['trades'] == 1
        assert gold.overview()['totals']['all_time']['trades'] == 0
        assert not gold.strategies[15].cooldown_status()['active']
        assert sil.strategies[15].cooldown_status()['active']
        saved = copy.deepcopy(gold.strategies[15].settings)
        sil.save_settings(15, {'silver_breakout_points': 2})
        assert gold.strategies[15].settings == saved

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: {'sub': 'test'}
    http = TestClient(app)
    routes = [('GET', '/15/status', None), ('GET', '/15/trades', None), ('PUT', '/15/settings', {}),
              ('GET', '/15/export?kind=closed', None), ('POST', '/15/close', {'position_id': 'test'}),
              ('POST', '/15/resume', None), ('POST', '/15/pause', {'duration_minutes': 5}),
              ('PUT', '/15/protection', {'position_id': 'test', 'sl_price': 1, 'target_price': 2, 'expected_sl': 1, 'expected_target': 2}),
              ('GET', '/account/positions?source=paper&minutes=15', None),
              ('POST', '/backtest/run', {'asset': 'silver', 'minutes': 15, 'start_date': '2026-09-01', 'end_date': '2026-09-01'})]
    with patch.dict(os.environ, {'DELTA_HIDDEN_SECTIONS': 'silver15m'}):
        caps = delta_capabilities()
        assert 15 in caps['enabled_timeframes'] and 15 not in caps['assets']['silver']['enabled_timeframes']
        for method, path, body in routes:
            join = '&' if '?' in path else '?'
            response = http.request(method, '/api/delta' + path + join + 'asset=silver', json=body)
            assert response.status_code == 404, (path, response.status_code, response.text)
    for hidden in ('silver', 'delta', 'misspelled'):
        with patch.dict(os.environ, {'DELTA_HIDDEN_SECTIONS': hidden}), patch('app.delta_engine.DeltaClient') as factory:
            service = DeltaService('silver')
            service._initialize()
            factory.assert_not_called()
            assert not service.strategies
            assert http.get('/api/delta/overview?asset=silver').status_code == 404
    print('smoke_delta_silver: all checks passed')


if __name__ == '__main__':
    run()
