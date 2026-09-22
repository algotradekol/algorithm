"""Isolated Delta OHLC replay. Synthetic intraminute paths are assumptions, not ticks."""
import copy
import datetime as dt
import math
import threading
import time

from .delta_client import DeltaClient
from .delta_candles import aggregate_custom_minutes, delta_resolution
from .delta_paper import DeltaPaperBroker, POST_EXIT_REST_REASONS
from .delta_reporting import paper_row, inr_rate
from .strategies.delta_gold import DeltaGold, DELTA_DEFAULTS, DELTA_TIMEFRAMES, validate_settings, defaults_for
from .strategies.delta_silver import DeltaSilver, SILVER_TIMEFRAMES
from .timezone import IST
from .strategies.algo3_silver_micro import _ema_step

BACKTEST_LOCK = threading.Lock()
BACKTEST_CANCEL = threading.Event()
MAX_DAYS = 31


class DeltaBacktestCancelled(Exception):
    """Raised when the user cancels the active Delta backtest."""


def clear_cancel_request():
    BACKTEST_CANCEL.clear()


def cancel_active_backtest():
    if not BACKTEST_LOCK.locked():
        return False
    BACKTEST_CANCEL.set()
    return True


def raise_if_cancelled():
    if BACKTEST_CANCEL.is_set():
        raise DeltaBacktestCancelled()


def iso(stamp):
    return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()


def date_range(start_date, end_date, now=None):
    start = dt.datetime.combine(start_date, dt.time(), IST).timestamp()
    requested_end = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time(), IST).timestamp()
    now = time.time() if now is None else now
    if end_date < start_date or (end_date - start_date).days >= MAX_DAYS or start >= now:
        raise ValueError('Choose a past/current IST date range of at most 31 days')
    end = min(requested_end, int(now // 60) * 60)
    if end <= start:
        raise ValueError('No completed minutes in the selected range')
    return int(start), int(end)


def fetch_candles(client, resolution, start, end, interval, anchor_lookback=0):
    rows = {}
    query_start = start - max(0, int(anchor_lookback)) * interval
    # Stay below Delta's 2000-candle response limit, including inclusive end points.
    for cursor in range(query_start, end, interval * 1500):
        raise_if_cancelled()
        stop = min(end, cursor + interval * 1500)
        batch = client.candles(resolution, cursor, stop)
        raise_if_cancelled()
        if not isinstance(batch, list):
            raise ValueError('Delta returned invalid candle data')
        for raw in batch:
            stamp = int(raw['time'])
            if not query_start <= stamp < end:
                continue
            prices = {key: float(raw[key]) for key in ('open', 'high', 'low', 'close')}
            if stamp % interval or any(not math.isfinite(v) or v <= 0 for v in prices.values()):
                raise ValueError('Delta returned invalid candle timestamps/prices')
            if not prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high']:
                raise ValueError('Delta returned inconsistent OHLC')
            volume = float(raw.get('volume', 0))
            if not math.isfinite(volume) or volume < 0:
                raise ValueError('Delta returned invalid candle volume')
            row = {'time': stamp, **prices, 'volume': volume}
            if stamp in rows and rows[stamp] != row:
                raise ValueError('Conflicting duplicate Delta candles; retry history later')
            rows[stamp] = row
    expected = list(range(query_start, end, interval))
    output = []
    last_close = None
    filled = 0
    for stamp in expected:
        raise_if_cancelled()
        row = rows.get(stamp)
        if row is None:
            if last_close is None:
                raise ValueError(f'Delta {resolution} history missing the first candle at {iso(stamp)}; no prior close is available to anchor replay.')
            row = {'time': stamp, 'open': last_close, 'high': last_close, 'low': last_close,
                   'close': last_close, 'volume': 0.0, 'synthetic_flat': True}
            filled += 1
        else:
            last_close = row['close']
        output.append(row)
    output = [row for row in output if row['time'] >= start]
    if filled:
        print(f"[delta_backtest] filled {filled} missing {resolution} candles as flat zero-volume bars")
    return output


class MemoryStore:
    def load(self):
        return None

    def save(self, state, trade=None):
        pass


class ReplayBroker(DeltaPaperBroker):
    def __init__(self, settings, product):
        super().__init__(MemoryStore(), settings, product)
        self.now = 0
        self.closed = []
        self.serial = 0

    def commit(self, state, trade=None):
        position = state.get('position')
        if position:
            if not self.state.get('position'):
                self.serial += 1
                position['id'] = f'delta-bt-{self.serial}'
                position['entry_time'] = iso(self.now)
            protection = position['signal_snapshot'].get('silver_breakeven')
            if protection and protection.get('armed') and not (self.state.get('position') or {}).get('trailing_sl_active'):
                protection['armed_at'] = iso(self.now)
        self.state = state

    def now_iso(self):
        return iso(self.now)

    def close_trade(self, position, exit_price, exit_reason):
        current = self.state.get('position')
        if not current or current['id'] != position['id']:
            return
        gross = self.pnl(current, exit_price)
        fees = (current['entry_price'] + exit_price) * current['qty'] * current['contract_value'] * current['fee_rate']
        self.closed.append({**copy.deepcopy(current), 'exit_price': exit_price, 'exit_time': iso(self.now),
                            'exit_reason': exit_reason, 'gross_pnl': gross, 'fees': fees, 'net_pnl': gross - fees})
        self.state.update(position=None, gross_pnl=self.state['gross_pnl'] + gross,
                          fees=self.state['fees'] + fees, closed_count=len(self.closed))
        if exit_reason in POST_EXIT_REST_REASONS:
            raw_minutes = self.state['settings'].get('post_exit_cooldown_minutes')
            minutes = 5.0 if raw_minutes is None else float(raw_minutes)
            self.state['cooldown_until'] = self.now + minutes * 60 if minutes > 0 else 0.0
            self.state['cooldown_reason'] = exit_reason if minutes > 0 else None
        if len(self.closed) > 5000:
            raise ValueError('Replay exceeded 5000 trades; shorten the range or review risk distances')


class ReplayGold(DeltaGold):
    """Reuse canonical reference/exit handling without global clocks or production writes."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entry_diagnostics = {'eligible_events': 0, 'already_positioned': 0, 'cooldown_blocked': 0}

    def _arm_post_sl_cooldown(self, exit_reason):
        return

    def _post_sl_cooldown_remaining(self):
        return max(0, float(self.broker.state.get('cooldown_until') or 0) - self.broker.now)

    def _fire_entry(self, side, ltp, trigger_level, setup_bar_at_override=None, event_time=None):
        self.entry_diagnostics['eligible_events'] += 1
        current = self._open_position()
        if current and current['side'] == side:
            self.entry_diagnostics['already_positioned'] += 1
            return False
        if self._post_sl_cooldown_remaining() and not current:
            self.entry_diagnostics['cooldown_blocked'] += 1
            return False
        if current:
            self.broker.close_trade(current, ltp, 'REVERSAL_CONTRA_SIGNAL')
        event = dt.datetime.fromtimestamp(self.broker.now, dt.timezone.utc)
        return self._enter(side, ltp, trigger_level, event_time=event)

    def _signal_snapshot(self, side, entry_price, trigger_level):
        snapshot = super()._signal_snapshot(side, entry_price, trigger_level)
        snapshot['execution'] = 'backtest'
        return snapshot

    def tick(self, price, stamp):
        self.broker.now = stamp
        self.process_price(price, stamp)
        position = self.broker.state.get('position')
        unrealized = self.broker.pnl(position, price) if position else 0
        equity = self.broker.state['gross_pnl'] - self.broker.state['fees'] + unrealized
        self.replay_peak = max(getattr(self, 'replay_peak', 0), equity)
        self.replay_drawdown = max(getattr(self, 'replay_drawdown', 0), self.replay_peak - equity)

    def segment(self, start_price, end_price, start_time, end_time, tick_size):
        if start_price == end_price:
            self.tick(end_price, end_time)
            return
        direction = 1 if end_price > start_price else -1
        price = start_price
        for _ in range(10000):
            levels = [end_price]
            if self._buy_setup_close is not None:
                levels.append(self._buy_setup_close + self.settings['silver_breakout_points'])
            if self._sell_setup_close is not None:
                levels.append(self._sell_setup_close - self.settings['silver_breakout_points'])
            position = self._open_position()
            if position:
                levels.extend([position['sl_price'], position['target_price']])
                protection = position['signal_snapshot'].get('silver_breakeven')
                if protection and not protection.get('armed'):
                    levels.append(protection['activation_price'])
                ladder = position['signal_snapshot'].get('delta_ladder_tsl')
                if isinstance(ladder, dict):
                    if not ladder.get('armed'):
                        levels.append(ladder.get('activation_price'))
                    else:
                        entry = float(position['entry_price'])
                        step = int(ladder.get('step_index', 0)) + 1
                        gain = float(ladder.get('activation_points') or 0) + step * float(ladder.get('profit_step_points') or 0)
                        if gain > 0:
                            levels.append(entry + gain if position['side'] == 'BUY' else entry - gain)
            else:
                cooldown = float(self.broker.state.get('cooldown_until') or 0)
                if start_time < cooldown < end_time:
                    levels.append(start_price + (end_price - start_price) * (cooldown - start_time) / (end_time - start_time))
            candidates = [p for p in levels if direction * (p - price) > 1e-9 and direction * (end_price - p) >= 0]
            if not candidates:
                self.tick(end_price, end_time)
                return
            next_price = min(candidates) if direction > 0 else max(candidates)
            stamp = start_time + (end_time - start_time) * abs((next_price - start_price) / (end_price - start_price))
            self.tick(next_price, stamp)
            price = next_price
            if price == end_price:
                return
        raise ValueError('Intraminute replay exceeded safety limit; review settings/range')


class ReplaySilver(ReplayGold, DeltaSilver):
    pass


def chart_candles(references, minute_rows, minutes, start, end):
    """Chart the replay inputs, truncating the final forming bar at the replay end."""
    interval = minutes * 60
    output = []
    price_ema = volume_ema = None
    for raw in sorted(references, key=lambda r: r['time']):
        row = dict(raw)
        stamp = row['time']
        if stamp >= end:
            break
        if stamp + interval > end:
            parts = [r for r in minute_rows if stamp <= r['time'] < end]
            if not parts:
                continue
            row.update(open=parts[0]['open'], high=max(r['high'] for r in parts),
                       low=min(r['low'] for r in parts), close=parts[-1]['close'],
                       volume=sum(r.get('volume', 0) for r in parts), partial=True)
        price_ema = _ema_step(price_ema, row['close'])
        volume_ema = _ema_step(volume_ema, row.get('volume', 0))
        if stamp >= start // interval * interval - 20 * interval:
            output.append({**row, 'time': iso(stamp), 'ema20': price_ema, 'volume_ema20': volume_ema})
    return output


def replay(product, region, minutes, settings, references, minute_rows, start, end, path, asset='gold'):
    raise_if_cancelled()
    settings = validate_settings({**defaults_for(asset), **settings, 'scan_enabled': True, 'trading_enabled': True}, asset)
    broker = ReplayBroker(settings, product)
    strategy = (ReplaySilver if asset == 'silver' else ReplayGold)(minutes, product['symbol'], broker)
    interval = minutes * 60
    ref_map = {r['time']: r for r in references}
    bucket = start // interval * interval
    warmup = [r for r in references if r['time'] < bucket]
    strategy.ingest_history(warmup, bucket)
    previous_bucket = None
    equity = []
    diagnostics = {'buy_reference_updates': 0, 'sell_reference_updates': 0,
                   'buy_threshold_minutes': 0, 'sell_threshold_minutes': 0,
                   'warmup_buy_reference': strategy._buy_setup_close, 'warmup_sell_reference': strategy._sell_setup_close,
                   'closest_buy': None, 'closest_sell': None,
                   'price_low': min(r['low'] for r in minute_rows), 'price_high': max(r['high'] for r in minute_rows)}
    deadline = time.monotonic() + 60
    for row in minute_rows:
        raise_if_cancelled()
        stamp = row['time']
        if not start <= stamp < end:
            continue
        if time.monotonic() > deadline:
            raise ValueError('Replay time limit reached; select a shorter range')
        bucket = stamp // interval * interval
        if bucket != previous_bucket:
            # Only reference bars completed BEFORE this minute can affect entries.
            old_buy, old_sell = strategy._buy_setup_bar_at, strategy._sell_setup_bar_at
            broker.now = stamp
            strategy.ingest_history([ref_map[bucket - interval], ref_map[bucket]], stamp)
            diagnostics['buy_reference_updates'] += strategy._buy_setup_bar_at != old_buy
            diagnostics['sell_reference_updates'] += strategy._sell_setup_bar_at != old_sell
            previous_bucket = bucket
        for side in ('buy', 'sell'):
            reference = getattr(strategy, f'_{side}_setup_close')
            reference_at = getattr(strategy, f'_{side}_setup_bar_at')
            if reference is None:
                continue
            trigger = reference + (1 if side == 'buy' else -1) * settings['silver_breakout_points']
            observed = row['high'] if side == 'buy' else row['low']
            gap = trigger - observed if side == 'buy' else observed - trigger
            diagnostics[f'{side}_threshold_minutes'] += gap <= 0
            previous = diagnostics[f'closest_{side}']
            if previous is None or gap < previous['remaining_points']:
                diagnostics[f'closest_{side}'] = {'reference_close': reference, 'reference_time': reference_at.replace(tzinfo=IST).isoformat(),
                                                 'trigger': trigger, 'observed_price': observed, 'minute': iso(stamp), 'remaining_points': gap}
        strategy.tick(row['open'], stamp)
        points = [row['open'], row['high'], row['low'], row['close']] if path == 'high_first' else [row['open'], row['low'], row['high'], row['close']]
        for i, (a, b) in enumerate(zip(points, points[1:])):
            strategy.segment(a, b, stamp + i * 19, stamp + (i + 1) * 19, float(product['tick_size']))
        position = broker.state.get('position')
        unrealized = broker.pnl(position, row['close']) if position else 0
        equity.append({'time': stamp + 59, 'net': broker.state['gross_pnl'] - broker.state['fees'], 'equity': broker.state['gross_pnl'] - broker.state['fees'] + unrealized})
    position = copy.deepcopy(broker.state.get('position'))
    if position:
        position['unrealized_pnl'] = broker.pnl(position, minute_rows[-1]['close'])
    net = broker.state['gross_pnl'] - broker.state['fees']
    rate = inr_rate(region, product.get('quoting_asset', {}).get('symbol'))
    diagnostics.update(strategy.entry_diagnostics, entries=broker.serial)
    if not broker.serial:
        if not diagnostics['buy_threshold_minutes'] and not diagnostics['sell_threshold_minutes']:
            diagnostics['explanation'] = 'No active reference +/- offset trigger was reached. This run produced no entry signals; the loaded candles were replayed.'
        else:
            diagnostics['explanation'] = 'Some price thresholds were touched, but no entry passed the candle-direction/cooldown rules. Threshold touches alone are not entry confirmations.'
    else:
        diagnostics['explanation'] = f'{broker.serial} entries were simulated; {len(broker.closed)} closed and {int(position is not None)} remains open.'
    return {'asset': asset, 'symbol': product['symbol'], 'minutes': minutes, 'settings': settings, 'path': path, 'start': start, 'end': end,
            'diagnostics': diagnostics,
            'candles': chart_candles(references, minute_rows, minutes, start, end),
            'currency': product.get('quoting_asset', {}).get('symbol'), 'inr_rate': rate,
            'trades': [paper_row(t, region) for t in broker.closed], 'open_position': paper_row(position, region) if position else None,
            'summary': {'trades': len(broker.closed), 'wins': sum(t['net_pnl'] > 0 for t in broker.closed),
                        'gross': broker.state['gross_pnl'], 'fees': broker.state['fees'], 'net': net,
                        'net_inr': net * rate if rate else None, 'max_drawdown': getattr(strategy, 'replay_drawdown', 0)},
            'equity': equity[::max(1, len(equity) // 1000)] + (equity[-1:] if equity else []),
            'coverage': {'minutes': len(minute_rows), 'reference_bars': len(references)},
            'warnings': ['1-minute OHLC replay uses an assumed price path, not historical ticks. Intraminute times and fills are simulated.',
                         'The configured post-exit cooldown uses the assumed timeline. Compare both paths; neither is a guaranteed best/worst bound.',
                         'No daily square-off. Any remaining position stays open at range end and is excluded from realized net.',
                         'Current product size, base margin and taker fee metadata are used for the whole range. Funding, taxes, slippage and liquidation are excluded.']}


def run_backtest(minutes, start_date, end_date, settings, path, asset='gold'):
    raise_if_cancelled()
    if asset not in {'gold', 'silver'} or minutes not in (SILVER_TIMEFRAMES if asset == 'silver' else DELTA_TIMEFRAMES) or path not in ('high_first', 'low_first'):
        raise ValueError('Invalid Delta timeframe or intraminute path')
    start, end = date_range(start_date, end_date)
    settings = validate_settings({**defaults_for(asset), **settings}, asset)
    client = DeltaClient() if asset == 'gold' else DeltaClient(asset=asset)
    try:
        raise_if_cancelled()
        product = client.product()
        raise_if_cancelled()
        interval = minutes * 60
        first_bucket, final_bucket = start // interval * interval, (end - 60) // interval * interval
        history_start, history_end = first_bucket - 300 * interval, final_bucket + interval
        if minutes in {7, 120}:
            custom_warmup = 300 if minutes == 7 else 40
            history_start = first_bucket - custom_warmup * interval
            source_minutes = fetch_candles(client, '1m', history_start, history_end, 60, anchor_lookback=120)
            references = aggregate_custom_minutes(source_minutes, minutes)
            minute_rows = [row for row in source_minutes if start <= row['time'] < end]
        else:
            references = fetch_candles(client, delta_resolution(minutes), history_start, history_end, interval, anchor_lookback=20)
            minute_rows = fetch_candles(client, '1m', start, end, 60, anchor_lookback=120)
        return replay(product, client.region, minutes, settings, references, minute_rows, start, end, path, asset)
    finally:
        client.session.close()
