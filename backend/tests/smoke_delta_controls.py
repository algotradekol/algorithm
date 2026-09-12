"""Paper protection edits/exit and full CSV regressions without any network."""
import copy
import csv
import io
import json
import time
from types import SimpleNamespace

from app.delta_engine import DeltaService
from app.delta_export import export_paper, csv_cell
from tests.smoke_delta_paper import strategy


def run():
    for side in ('BUY', 'SELL'):
        s = strategy(settings={'exit_mode': 'target_to_breakeven_sl'})
        s._enter(side, 1000, 1000)
        service = DeltaService()
        service.client = SimpleNamespace(region='india')
        service.strategies = {15: s}
        service.last_price = 1020 if side == 'BUY' else 980
        service.last_event_at = time.time()
        position = s._open_position()
        initial = position['sl_price']
        sl, target = (1010, 1150) if side == 'BUY' else (990, 850)
        snapshot_settings = copy.deepcopy(s.settings)
        result = service.edit_protection(15, position['id'], sl, target, initial, position['target_price'])
        assert result['sl_price'] == sl and result['target_price'] == target
        assert result['initial_sl'] == initial and len(result['protection_edits']) == 1
        assert s.settings == snapshot_settings
        # Later activation must preserve a manual stop better than breakeven.
        updated = s.broker.apply_trailing_stop(result, 1030 if side == 'BUY' else 970, {})
        assert updated['sl_price'] == sl and updated['trailing_sl_active']
        for args in [(position['id'], sl, target, initial, position['target_price']),
                     ('old-id', sl, target, sl, target),
                     (position['id'], 900 if side == 'BUY' else 1100, target, sl, target),
                     (position['id'], float('nan'), target, sl, target)]:
            try:
                service.edit_protection(15, *args)
                raise AssertionError('invalid/stale edit accepted')
            except ValueError:
                pass
        service.last_event_at = time.time() - 60
        try:
            service.edit_protection(15, position['id'], sl + 1, target, sl, target)
            raise AssertionError('stale feed accepted')
        except ValueError:
            pass
        service.last_event_at = time.time()
        before = copy.deepcopy(s.broker.state)
        s.broker.store.fail = True
        try:
            service.edit_protection(15, position['id'], sl + 1, target, sl, target)
            raise AssertionError('failed write accepted')
        except RuntimeError:
            assert s.broker.state == before
        s.broker.store.fail = False

        opened = export_paper(service, 15, 'open')
        rows = list(csv.DictReader(io.StringIO(opened['csv'])))
        assert opened['count'] == 1 and rows[0]['lots'] == '1'
        assert json.loads(rows[0]['settings_at_export']) == snapshot_settings
        assert json.loads(rows[0]['protection_edits'])[0]['new_sl'] == sl
        service.close(15, position['id'])
        assert not s._open_position() and s.broker.store.closed[-1]['exit_reason'] == 'MANUAL_EXIT'
        assert s.cooldown_status()['active'] and s.cooldown_status()['reason'] == 'MANUAL_EXIT'
        service.resume(15)
        assert not s.cooldown_status()['active'] and not s.broker.state.get('cooldown_until')
        try:
            service.close(15, position['id'])
            raise AssertionError('duplicate exit accepted')
        except ValueError:
            pass
        assert len(s.broker.store.closed) == 1
        assert export_paper(service, 15, 'open')['count'] == 0
        sample = copy.deepcopy(s.broker.store.closed[0])
        s.broker.store.closed = [{**sample, 'id': f'row-{i}'} for i in range(1001)]
        closed = export_paper(service, 15, 'closed')
        exported = list(csv.DictReader(io.StringIO(closed['csv'])))
        assert len(exported) == closed['count'] == 1001
        assert len({row['id'] for row in exported}) == 1001

    isolated = DeltaService()
    isolated.strategies = {5: strategy(5), 15: strategy(15)}
    assert isolated.strategies[5]._enter('BUY', 1000, 1000)
    isolated.pause(5, 37)
    assert isolated.strategies[5].cooldown_status()['active']
    assert isolated.strategies[5].cooldown_status()['reason'] == 'MANUAL_PAUSE'
    assert isolated.strategies[5]._open_position(), 'pausing entries must not close an open position'
    assert not isolated.strategies[15].cooldown_status()['active']
    isolated.strategies[5].broker.state.update(cooldown_until=time.time() + 900, cooldown_reason='TARGET')
    isolated.resume(5)
    assert not isolated.strategies[5].cooldown_status()['active']
    assert not isolated.strategies[15].broker.state.get('cooldown_until')
    assert csv_cell('=HYPERLINK("bad")').startswith("'")
    assert csv_cell(-12.5) == -12.5
    print('smoke_delta_controls: all checks passed')


if __name__ == '__main__':
    run()
