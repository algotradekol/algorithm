"""Offline reporting/account regression checks. Never contacts an exchange or DB."""
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
        assert position['qty'] == 1 and position['estimated_entry_margin'] == .01
        s.broker.product['initial_margin'] = '50'
        position['unrealized_pnl'] = -2
        display = paper_row(position, 'india')
        assert display['margin_inr'] == .85 and display['pnl_inr'] == -170
        s.broker.close_trade(position, 1010 if side == 'BUY' else 990, 'TARGET')
        closed = s.broker.store.trades()[0]
        display = paper_row(closed, 'india')
        assert abs(display['pnl_inr'] - closed['net_pnl'] * 85) < 1e-9
        assert display['estimated_entry_margin'] == .01

    rows = account_rows([{'product_id': 1, 'product_symbol': 'PAXGUSD', 'size': '-3', 'margin': '2', 'user_id': 'private'},
                         {'size': 0}], 'positions', 'india')
    assert len(rows) == 1 and rows[0]['side'] == 'SELL' and rows[0]['lots'] == 3
    assert rows[0]['margin_inr'] == 170 and 'user_id' not in rows[0]
    assert account_rows([{'size': 1}], 'history', 'india')[0]['margin'] is None

    with patch.dict(os.environ, {'DELTA_API_KEY': 'test-key', 'DELTA_API_SECRET': 'test-secret', 'DELTA_EXCHANGE': 'india', 'DELTA_PROXY_URL': ''}):
        client = DeltaClient()
        response = MagicMock(status_code=200)
        response.json.return_value = {'success': True, 'result': [], 'meta': {'after': 'next'}}
        with patch.object(client.session, 'get', return_value=response) as get, patch('app.delta_client.time.time', return_value=1234):
            for path in ('/v2/orders', '/v2/orders/history', '/v2/positions/margined', '/v2/wallet/balances'):
                params = {'states': 'open,pending', 'after': 'a+b/=='} if path == '/v2/orders' else {}
                payload = client.get(path, params, private=True, envelope=True)
                request_path = requests.Request('GET', client.base_url + path, params=params).prepare().path_url
                expected = hmac.new(b'test-secret', f'GET1234{request_path}'.encode(), hashlib.sha256).hexdigest()
                assert get.call_args.kwargs['headers']['signature'] == expected
                assert payload['meta']['after'] == 'next'
            before = get.call_count
            try:
                client.get('/v2/positions/close_all', private=True)
                raise AssertionError('non-allowlisted endpoint accepted')
            except ValueError:
                assert get.call_count == before
        assert not hasattr(client, 'post') and not hasattr(client, 'delete')

    fake = MagicMock(region='india')
    fake.get.return_value = {'result': [{'id': 1, 'size': 1, 'side': 'buy', 'stop_order_type': None},
                                      {'id': 2, 'size': 1, 'side': 'sell', 'stop_order_type': 'stop_loss_order'}],
                             'meta': {'after': 'page2'}}
    s = strategy()
    s._enter('BUY', 1000, 1000)
    service = SimpleNamespace(client=fake, strategy=lambda minutes: s)
    with patch.object(delta_routes, 'delta_service', service):
        for kind, expected in [('open_orders', '1'), ('stop_orders', '2')]:
            result = delta_routes.account(kind, None, 'live', 15, 0)
            assert len(result['rows']) == 1 and result['rows'][0]['id'] == expected
            assert result['next_cursor'] == 'page2'
        fake.get.reset_mock()
        for kind, count in [('positions', 1), ('open_orders', 0), ('stop_orders', 2), ('history', 1)]:
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
