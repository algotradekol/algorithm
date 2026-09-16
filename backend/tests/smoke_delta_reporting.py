"""Offline reporting/account regression checks. Never contacts an exchange or DB."""
import datetime
import hashlib
import hmac
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from fastapi import HTTPException

from app.delta_client import DeltaClient
from app.delta_reporting import account_rows, account_time, inr_rate, paper_margin, paper_row
from app import delta_routes
from tests.smoke_delta_paper import strategy


def run():
    assert paper_margin(4000, 10, .001, 1) == .4
    assert paper_margin(4000, 10, .001, 1, leverage=50) == .8
    assert paper_margin(4000, 10, .001, None) is None
    assert paper_margin(4000, 10, .001, float('nan')) is None
    assert inr_rate('global', 'USDT') is None
    old = {'qty': 10, 'entry_price': 4000}
    assert paper_row(old, 'india')['pnl_inr'] is None
    assert paper_row({'symbol': 'PAXGUSD', 'unrealized_pnl': 2}, 'india')['pnl_inr'] == 170
    assert account_time('1704067200000000') == '2024-01-01T00:00:00+00:00'
    assert account_time('invalid') is None and account_time(None) is None
    for side in ('BUY', 'SELL'):
        s = strategy()
        s.broker.product = {**s.broker.product, 'initial_margin': '1', 'quoting_asset': {'symbol': 'USD'}}
        s._enter(side, 1000, 1000)
        position = s._open_position()
        assert position['qty'] == 1 and position['estimated_entry_margin'] == .02
        s.broker.product['initial_margin'] = '50'
        position['unrealized_pnl'] = -2
        display = paper_row(position, 'india')
        assert display['margin_inr'] == 1.7 and display['pnl_inr'] == -170
        s.broker.close_trade(position, 1010 if side == 'BUY' else 990, 'TARGET')
        closed = s.broker.store.trades()[0]
        display = paper_row(closed, 'india')
        assert abs(display['pnl_inr'] - closed['net_pnl'] * 85) < 1e-9
        assert display['estimated_entry_margin'] == .02

    rows = account_rows([{'product_id': 1, 'product_symbol': 'PAXGUSD', 'size': '-3', 'margin': '2', 'user_id': 'private'},
                         {'size': 0}], 'positions', 'india')
    assert len(rows) == 1 and rows[0]['side'] == 'SELL' and rows[0]['lots'] == 3
    assert rows[0]['margin_inr'] == 170 and 'user_id' not in rows[0]
    assert account_rows([{'size': 1}], 'history', 'india')[0]['margin'] is None

    # 2026-09-16 regression: Delta history rows carry realised cash / fees per
    # order in `cashflow`, `realized_pnl` and `paid_commission`. The reporting
    # normalizer must surface them (INR-converted for India accounts) so the
    # Realized net (INR) column stops showing -- for every filled order.
    filled = account_rows([{
        'id': 1540983422, 'product_symbol': 'PAXGUSD', 'side': 'buy', 'state': 'closed',
        'size': 10, 'unfilled_size': 0, 'average_fill_price': 4333.5,
        'limit_price': 4765.84, 'stop_price': 4333.3, 'realized_pnl': -0.05,
        'cashflow': -0.05, 'paid_commission': 0.02, 'order_type': 'stop_market',
    }], 'history', 'india')[0]
    assert filled['entry_price'] == 4333.5
    assert filled['limit_price'] == 4765.84 and filled['stop_price'] == 4333.3
    assert filled['pnl_inr'] == -0.05 * 85  # realized_pnl * INR rate
    assert filled['fee_inr'] == 0.02 * 85
    assert filled['cashflow'] == -0.05 and filled['filled_size'] == 10

    # Cashflow fallback when realized_pnl is missing — Delta uses either field.
    fallback = account_rows([{
        'id': 1, 'product_symbol': 'PAXGUSD', 'side': 'sell', 'state': 'closed',
        'size': 12, 'unfilled_size': 0, 'average_fill_price': 4298.5,
        'cashflow': 0.06,
    }], 'history', 'india')[0]
    assert fallback['pnl_inr'] == 0.06 * 85 and fallback['cashflow'] == 0.06

    # execution_price fills entry_price when average_fill_price is absent.
    exec_only = account_rows([{
        'id': 2, 'product_symbol': 'PAXGUSD', 'side': 'buy', 'state': 'closed',
        'size': 1, 'unfilled_size': 0, 'execution_price': 4307.6,
    }], 'history', 'india')[0]
    assert exec_only['entry_price'] == 4307.6 and exec_only['pnl_inr'] is None

    # Cancelled order — no fill, no realised cash. Must NOT fabricate any of it.
    cancelled = account_rows([{
        'id': 3, 'product_symbol': 'PAXGUSD', 'side': 'buy', 'state': 'cancelled',
        'size': 10, 'unfilled_size': 10, 'stop_price': 4278.3, 'order_type': 'take_profit_order',
    }], 'history', 'india')[0]
    assert cancelled['entry_price'] is None and cancelled['pnl_inr'] is None
    assert cancelled['fee_inr'] is None and cancelled['filled_size'] == 0
    assert cancelled['stop_price'] == 4278.3  # Delta still reports the trigger

    # Global (non-India) account: no INR rate, so pnl_inr stays None even if
    # cashflow is populated — the display column is INR only.
    global_row = account_rows([{
        'id': 4, 'product_symbol': 'BTCUSD', 'side': 'buy', 'state': 'closed',
        'size': 1, 'unfilled_size': 0, 'average_fill_price': 60000.0, 'cashflow': 10.0,
    }], 'history', 'global')[0]
    assert global_row['pnl_inr'] is None and global_row['cashflow'] == 10.0
    today = datetime.datetime.now(datetime.timezone.utc)
    yesterday = today - datetime.timedelta(days=1)
    older = today - datetime.timedelta(days=2)
    store = SimpleNamespace(trades=lambda offset=0, limit=100, before=None: [
        {'id': 'today-2', 'exit_time': today.isoformat()},
        {'id': 'today-1', 'exit_time': today.replace(hour=0, minute=1).isoformat()},
        {'id': 'yesterday-context', 'exit_time': yesterday.isoformat()},
        {'id': 'older-hidden', 'exit_time': older.isoformat()},
    ][offset:offset + limit])
    daily = delta_routes.daily_trades_with_previous_context(store, 0, 100)
    assert [row['id'] for row in daily] == ['today-2', 'today-1', 'yesterday-context']
    assert [row['id'] for row in delta_routes.daily_trades_with_previous_context(store, 1, 1)] == ['today-1']

    with patch.dict(os.environ, {'DELTA_API_KEY': 'test-key', 'DELTA_API_SECRET': 'test-secret', 'DELTA_EXCHANGE': 'india', 'DELTA_PROXY_URL': ''}):
        client = DeltaClient()
        response = MagicMock(status_code=200)
        response.json.return_value = {'success': True, 'result': [], 'meta': {'after': 'next'}}
        with patch.object(client.session, 'request', return_value=response) as request, patch('app.delta_client.time.time', return_value=1234):
            for path in ('/v2/orders', '/v2/orders/history', '/v2/positions/margined', '/v2/wallet/balances'):
                params = {'states': 'open,pending', 'after': 'a+b/=='} if path == '/v2/orders' else {}
                payload = client.get(path, params, private=True, envelope=True)
                request_path = requests.Request('GET', client.base_url + path, params=params).prepare().path_url
                expected = hmac.new(b'test-secret', f'GET1234{request_path}'.encode(), hashlib.sha256).hexdigest()
                assert request.call_args.kwargs['headers']['signature'] == expected
                assert payload['meta']['after'] == 'next'
            before = request.call_count
            try:
                client.get('/v2/positions/close_all', private=True)
                raise AssertionError('non-allowlisted endpoint accepted')
            except ValueError:
                assert request.call_count == before
    with patch.dict(os.environ, {
        'DELTA_API_KEY': 'read-key',
        'DELTA_API_SECRET': 'read-secret',
        'DELTA_LIVE_API_KEY': 'live-key',
        'DELTA_LIVE_API_SECRET': 'live-secret',
        'DELTA_EXCHANGE': 'india',
        'DELTA_PROXY_URL': '',
    }):
        live_client = DeltaClient(credential_scope='live')
        assert live_client.key == 'live-key'
        response = MagicMock(status_code=200)
        response.json.return_value = {'success': True, 'result': {'id': 1}}
        with patch.object(live_client.session, 'request', return_value=response) as request, patch('app.delta_client.time.time', return_value=1234):
            live_client.post('/v2/orders', {'product_id': 1, 'size': 1}, private=True)
            assert request.call_args.args[0] == 'POST'
            assert request.call_args.kwargs['data'] == '{"product_id":1,"size":1}'
        bad_response = MagicMock(status_code=400)
        bad_response.json.return_value = {'success': False, 'error': {'code': 'invalid_order', 'message': 'bad size'}}
        with patch.object(live_client.session, 'request', return_value=bad_response), patch('app.delta_client.time.time', return_value=1234):
            try:
                live_client.post('/v2/orders', {'product_id': 1, 'size': 0}, private=True)
                assert False, 'Delta HTTP errors must include exchange detail'
            except RuntimeError as exc:
                assert 'invalid_order' in str(exc) and 'bad size' in str(exc)

    fake = MagicMock(region='india', symbol='PAXGUSD')
    def fake_delta_get(path, params=None, private=True, envelope=True):
        if path == '/v2/positions/margined':
            return {'result': [{'id': 'pos', 'product_id': 123006, 'product_symbol': 'PAXGUSD', 'size': 1, 'entry_price': 1000}],
                    'meta': {'after': 'ignored'}}
        if path == '/v2/orders':
            return {'result': [{'id': 1, 'product_id': 123006, 'product_symbol': 'PAXGUSD', 'size': 1, 'side': 'sell', 'stop_order_type': None, 'limit_price': 1050},
                               {'id': 2, 'product_id': 123006, 'product_symbol': 'PAXGUSD', 'size': 1, 'side': 'sell', 'stop_order_type': 'stop_loss_order', 'stop_price': 985}],
                    'meta': {'after': 'page2'}}
        return {'result': [], 'meta': {'after': 'page2'}}
    fake.get.side_effect = fake_delta_get
    s = strategy()
    s._enter('BUY', 1000, 1000)
    service = SimpleNamespace(client=fake, product={'id': 123006}, strategy=lambda minutes: s)
    with patch.object(delta_routes, 'service_for', lambda asset='gold', mode='paper': service):
        result = delta_routes.account('positions', None, 'live', 15, 0)
        assert [row['id'] for row in result['rows']] == ['pos', '1', '2']
        assert 'waiting' in result['note']
        for kind, expected in [('open_orders', '1'), ('stop_orders', '2')]:
            result = delta_routes.account(kind, None, 'live', 15, 0)
            assert len(result['rows']) == 1 and result['rows'][0]['id'] == expected
            assert result['next_cursor'] == 'page2'
        fake.get.reset_mock()
        fake.get.side_effect = fake_delta_get
        for kind, count in [('positions', 3), ('open_orders', 1), ('stop_orders', 1), ('history', 1)]:
            result = delta_routes.account(kind, None, 'paper', 15, 0)
            assert len(result['rows']) == count and result['mode'] == 'paper'
        fake.get.assert_not_called()
        # Paper pagination must not omit or repeat either event of a closed trade.
        s.broker.store.closed = [{**s._open_position(), 'id': f'closed-{i}', 'exit_time': '2026-09-10T01:00:00+00:00',
                                 'exit_price': 1010, 'exit_reason': 'TARGET', 'net_pnl': .01} for i in range(101)]
        first = delta_routes.account('history', None, 'paper', 15, 0)
        second = delta_routes.account('history', None, 'paper', 15, 100)
        assert first['has_more'] and not second['has_more']
        assert len(first['rows']) == 201 and len(second['rows']) == 2
        assert not ({r['id'] for r in first['rows']} & {r['id'] for r in second['rows']})
        delta_routes.account('history', 'abc+==', 'live', 15, 0)
        assert fake.get.call_args.args[1]['after'] == 'abc+=='
        fake.get.side_effect = RuntimeError('Delta HTTP 401')
        try:
            delta_routes.account('positions', None, 'live', 15, 0)
            raise AssertionError('auth failure shown as empty success')
        except HTTPException as exc:
            assert exc.status_code == 400
    print('smoke_delta_reporting: all checks passed')


if __name__ == '__main__':
    run()
