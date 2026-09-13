"""Normal Silver Micro entry rules on isolated, continuous Delta silver candles."""
from .algo3_silver_micro import Algo3SilverMicro
from .delta_gold import DeltaGold

SILVER_TIMEFRAMES = (5, 15, 30, 60, 240)


class DeltaSilver(DeltaGold):
    asset = 'silver'
    _qualifies_as_buy_setup = Algo3SilverMicro._qualifies_as_buy_setup
    _qualifies_as_sell_setup = Algo3SilverMicro._qualifies_as_sell_setup
    _check_triggers = Algo3SilverMicro._check_triggers

    def __init__(self, minutes, symbol, broker):
        if minutes not in SILVER_TIMEFRAMES:
            raise ValueError('Delta Silver timeframe must be 5, 15, 30, 60 or 240 minutes')
        super().__init__(minutes, symbol, broker)
        self.algo_id = f'delta_silver_{minutes}m'
        label = {60: '1 hr', 240: '4 hr'}.get(minutes, f'{minutes} min')
        self.display_name = f'Delta Silver {label}'

    def _entry_trigger(self, side, entry_price, trigger_level):
        return (f'{self.minutes}m Delta Silver {side} normal Silver Micro EMA20 '
                f'reference breakout at {trigger_level:g}; paper fill {entry_price:g}')

    def _signal_snapshot(self, side, entry_price, trigger_level):
        result = super()._signal_snapshot(side, entry_price, trigger_level)
        result['strategy'] = self.algo_id
        result['strategy_version'] = self.settings['strategy_version']
        result['entry_rule'] = 'normal_silver_micro'
        result['volume_filter_enabled'] = False
        return result
