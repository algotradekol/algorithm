"""Isolated Delta OHLC replay. Synthetic intraminute paths are assumptions, not ticks."""
import copy
import datetime as dt
import math
import threading
import time

from .delta_client import DeltaClient
from .delta_paper import DeltaPaperBroker
from .delta_reporting import paper_row, inr_rate
from .strategies.delta_gold import DeltaGold, DELTA_DEFAULTS, validate_settings
from .timezone import IST

BACKTEST_LOCK = threading.Lock()
MAX_DAYS = 31


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


def fetch_candles(client, resolution, start, end, interval):
    rows = {}
    # Stay below Delta's 2000-candle response limit, including inclusive end points.
    for cursor in range(start, end, interval * 1500):
        stop = min(end, cursor + interval * 1500)
        batch = client.candles(resolution, cursor, stop)
        if not isinstance(batch, list):
            raise ValueError('Delta returned invalid candle data')
        for raw in batch:
            stamp = int(raw['time'])
            if not start <= stamp < end:
                continue
            prices = {key: float(raw[key]) for key in ('open', 'high', 'low', 'close')}
            if stamp % interval or any(not math.isfinite(v) or v <= 0 for v in prices.values()):
                raise ValueError('Delta returned invalid candle timestamps/prices')
            if not prices['low'] <= min(prices['open'], prices['close']) <= max(prices['open'], prices['close']) <= prices['high']:
                raise ValueError('Delta returned inconsistent OHLC')
            row = {'time': stamp, **prices}
            if stamp in rows and rows[stamp] != row:
                raise ValueError('Conflicting duplicate Delta candles; retry history later')
            rows[stamp] = row
    expected = list(range(start, end, interval))
    missing = [stamp for stamp in expected if stamp not in rows]
    if missing:
        raise ValueError(f'Delta {resolution} history missing {len(missing)} candles; first gap {iso(missing[0])}. Replay refused rather than invent prices.')
    return [rows[stamp] for stamp in expected]


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
        if len(self.closed) > 5000:
            raise ValueError('Replay exceeded 5000 trades; shorten the range or review risk distances')


class ReplayGold(DeltaGold):
    """Reuse canonical reference/exit handling without global clocks or production writes."""
    def _arm_post_sl_cooldown(self, exit_reason):
        self.replay_cooldown = self.broker.now + 30

    def _post_sl_cooldown_remaining(self):
        return max(0, getattr(self, 'replay_cooldown', 0) - self.broker.now)

    def _fire_entry(self, side, ltp, trigger_level, setup_bar_at_override=None, event_time=None):
        current = self._open_position()
        if current and current['side'] == side:
            return False
        if self._post_sl_cooldown_remaining() and not current:
            return False
        if current:
            self.broker.close_trade(current, ltp, 'REVERSAL_CONTRA_SIGNAL')
        direction = 1 if side == 'BUY' else -1
        sl = ltp - direction * self.settings['sl_points']
        target = ltp + direction * self.settings['target_points']
        if min(sl, target) <= 0:
            raise ValueError('SL/target becomes non-positive; reduce point distances for gold')
        snapshot = self._signal_snapshot(side, ltp, trigger_level)
        snapshot.update(execution='backtest', silver_exit_policy=self.settings['exit_mode'])
        if self.settings['exit_mode'] == 'target_to_breakeven_sl':
            snapshot['silver_breakeven'] = {'armed': False, 'activation_price': ltp + direction * self.settings['tsl_activate_points'],
                                          'activation_points': self.settings['tsl_activate_points'], 'target_price': target,
                                          'final_target_enabled': True, 'initial_sl_price': sl}
        self.broker.open_trade(self.symbol, side, int(self.settings['silver_lots']), ltp, sl, target,
                               self._entry_trigger(side, ltp, trigger_level), snapshot, entry_time=iso(self.broker.now))
        return True

    def _check_triggers(self, ltp, event_time=None):
        # Mirror DeltaGold's inherited original-Silver SELL-first trigger priority.
        n, prev = self.settings['silver_breakout_points'], self._prev_ltp
        sell = self._sell_setup_close - n if self._sell_setup_close is not None else None
        handoff = self._sell_reentry_after_exit
        same = bool(handoff and handoff['setup_bar_at'] == self._sell_setup_bar_at and handoff['trigger_level'] == sell)
        red = bool(sell is not None and self._ema20 is not None and self._minute_buffer and
                   self._current_bucket > self._sell_setup_bar_at and ltp < self._minute_buffer[0]['open'] and ltp < self._ema20)
        if red and ltp <= sell and (prev is None or prev > sell or (same and ltp < prev)):
            if self._fire_entry('SELL', ltp, sell):
                self._sell_reentry_after_exit = None
        buy = self._buy_setup_close + n if self._buy_setup_close is not None else None
        handoff = self._buy_reentry_after_exit
        same = bool(handoff and handoff['setup_bar_at'] == self._buy_setup_bar_at and handoff['trigger_level'] == buy)
        if buy is not None and ltp >= buy and prev is not None and (not same or ltp > prev):
            if self._fire_entry('BUY', ltp, buy):
                self._buy_reentry_after_exit = None

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
            else:
                # A renewed move after target can re-enter on the next assumed tick.
                buy_level = self._buy_setup_close + self.settings['silver_breakout_points'] if self._buy_setup_close is not None else None
                sell_level = self._sell_setup_close - self.settings['silver_breakout_points'] if self._sell_setup_close is not None else None
                renewed_sell = self._sell_reentry_after_exit and sell_level is not None and price <= sell_level and direction < 0
                renewed_buy = buy_level is not None and price >= buy_level and (not self._buy_reentry_after_exit or direction > 0)
                if not self._post_sl_cooldown_remaining() and (renewed_sell or renewed_buy):
                    levels.append(price + direction * tick_size)
                cooldown = getattr(self, 'replay_cooldown', 0)
                if start_time < cooldown < end_time:
                    levels.append(start_price + (end_price - start_price) * (cooldown - start_time) / (end_time - start_time))
            candidates = [p for p in levels if direction * (p - price) > 1e-9 and direction * (end_price - p) >= 0]
            next_price = min(candidates) if direction > 0 else max(candidates)
            stamp = start_time + (end_time - start_time) * abs((next_price - start_price) / (end_price - start_price))
            self.tick(next_price, stamp)
            price = next_price
            if price == end_price:
                return
        raise ValueError('Intraminute replay exceeded safety limit; review settings/range')


def replay(product, region, minutes, settings, references, minute_rows, start, end, path):
    settings = validate_settings({**DELTA_DEFAULTS, **settings, 'scan_enabled': True, 'trading_enabled': True})
    broker = ReplayBroker(settings, product)
    strategy = ReplayGold(minutes, product['symbol'], broker)
    interval = minutes * 60
    ref_map = {r['time']: r for r in references}
    bucket = start // interval * interval
    warmup = [r for r in references if r['time'] < bucket]
    strategy.ingest_history(warmup, bucket)
    previous_bucket = None
    equity = []
    deadline = time.monotonic() + 60
    for row in minute_rows:
        stamp = row['time']
        if not start <= stamp < end:
            continue
        if time.monotonic() > deadline:
            raise ValueError('Replay time limit reached; select a shorter range')
        bucket = stamp // interval * interval
        if bucket != previous_bucket:
            # Only reference bars completed BEFORE this minute can affect entries.
            strategy.ingest_history([ref_map[bucket - interval], {'time': bucket, 'open': ref_map[bucket]['open']}], stamp)
            previous_bucket = bucket
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
    return {'symbol': product['symbol'], 'minutes': minutes, 'settings': settings, 'path': path, 'start': start, 'end': end,
            'currency': product.get('quoting_asset', {}).get('symbol'), 'inr_rate': rate,
            'trades': [paper_row(t, region) for t in broker.closed], 'open_position': paper_row(position, region) if position else None,
            'summary': {'trades': len(broker.closed), 'wins': sum(t['net_pnl'] > 0 for t in broker.closed),
                        'gross': broker.state['gross_pnl'], 'fees': broker.state['fees'], 'net': net,
                        'net_inr': net * rate if rate else None, 'max_drawdown': getattr(strategy, 'replay_drawdown', 0)},
            'equity': equity[::max(1, len(equity) // 1000)] + (equity[-1:] if equity else []),
            'coverage': {'minutes': len(minute_rows), 'reference_bars': len(references)},
            'warnings': ['1-minute OHLC replay uses an assumed price path, not historical ticks. Intraminute times and fills are simulated.',
                         '30-second cooldown uses the assumed timeline. Compare both paths; neither is a guaranteed best/worst bound.',
                         'No daily square-off. Any remaining position stays open at range end and is excluded from realized net.',
                         'Current product size, base margin and taker fee metadata are used for the whole range. Funding, taxes, slippage and liquidation are excluded.']}


def run_backtest(minutes, start_date, end_date, settings, path):
    if minutes not in (15, 60) or path not in ('high_first', 'low_first'):
        raise ValueError('Invalid Delta timeframe or intraminute path')
    start, end = date_range(start_date, end_date)
    settings = validate_settings({**DELTA_DEFAULTS, **settings})
    client = DeltaClient()  # Separate HTTP session; never use the running engine's state.
    try:
        product = client.product()
        interval = minutes * 60
        first_bucket, final_bucket = start // interval * interval, (end - 60) // interval * interval
        references = fetch_candles(client, '15m' if minutes == 15 else '1h', first_bucket - 300 * interval, final_bucket + interval, interval)
        minute_rows = fetch_candles(client, '1m', start, end, 60)
        return replay(product, client.region, minutes, settings, references, minute_rows, start, end, path)
    finally:
        client.session.close()
