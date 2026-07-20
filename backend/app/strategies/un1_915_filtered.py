from .algo4_opening_range_indicators import Algo4OpeningRangeIndicators


class UN1915Filtered(Algo4OpeningRangeIndicators):
    algo_id = "algo2"
    display_name = "UN1 9:15 v14 - Filter"

    def __init__(self, watchlist: list[str]):
        super().__init__(watchlist)
        self._apply_v14_defaults()

    def reload_settings(self):
        super().reload_settings()
        self._apply_v14_defaults()

    def _apply_v14_defaults(self):
        self.settings.update({
            "rsi_buy_threshold": 50,
            "rsi_sell_threshold": 50,
            "adx_threshold": 20,
            "filter_vwap": True,
            "filter_rsi": True,
            "filter_adx": True,
            "filter_supertrend": True,
            "filter_ema20": True,
            "filter_ema50": True,
            "filter_volume": True,
            "filter_liquidity": True,
            "filter_price_range": True,
            "min_volume": 100000,
            "min_total_value": 100000000,
            "ltp_min": 200,
            "ltp_max": 4000,
            "supertrend_period": 10,
            "supertrend_multiplier": 3,
        })
