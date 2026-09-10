"""Full Delta paper exports with a fixed cutoff and settings/audit context."""
import copy
import csv
import datetime
import io
import json

from .delta_reporting import paper_row


def csv_cell(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(',', ':'), ensure_ascii=True)
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return '' if value is None else value


def export_paper(service, minutes, kind):
    if kind not in {'open', 'closed'}:
        raise ValueError('Invalid export table')
    with service.lock:
        strategy = service.strategy(minutes)
        state = copy.deepcopy(strategy.broker.state)
        region = service.client.region
        cutoff = datetime.datetime.now(datetime.timezone.utc).isoformat()
        last_price, last_tick_at = service.last_price, service.last_event_at
        position = state.get('position')
        if position and last_price is not None:
            position['unrealized_pnl'] = strategy.broker.pnl(position, last_price)
    rows = [position] if kind == 'open' and position else []
    if kind == 'closed':
        for offset in range(0, 50001, 500):
            batch = strategy.broker.store.trades(offset, 500, before=cutoff)
            rows.extend(batch)
            if len(rows) > 50000:
                raise ValueError('Export exceeds 50,000 trades; no partial CSV was created')
            if len(batch) < 500:
                break
    fields = ['id', 'symbol', 'side', 'lots', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'exit_reason',
              'initial_sl', 'sl_price', 'target_price', 'trailing_sl_active', 'gross_pnl', 'fees', 'net_pnl',
              'unrealized_pnl', 'pnl_inr', 'estimated_entry_margin', 'margin_inr', 'signal_snapshot', 'protection_edits',
              'exported_at', 'exchange', 'timeframe_minutes', 'mode', 'settings_at_export', 'last_price_at_export', 'last_tick_at_export']
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        data = {**paper_row(row, region), 'lots': row['qty'], 'exported_at': cutoff, 'exchange': region,
                'timeframe_minutes': minutes, 'mode': 'paper', 'settings_at_export': state['settings'],
                'last_price_at_export': last_price, 'last_tick_at_export': last_tick_at}
        writer.writerow([csv_cell(data.get(field)) for field in fields])
    return {'filename': f'delta-{minutes}m-paper-{kind}-{cutoff[:10]}.csv', 'csv': output.getvalue(), 'count': len(rows)}
