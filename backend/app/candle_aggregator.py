"""
candle_aggregator.py — turns a stream of live ticks into 1-minute
candles per symbol, and keeps running VWAP / EMA20 / average volume
for Algo 2. One instance shared across both algos so they see
identical bars.
"""
import datetime
from collections import defaultdict, deque
# Railway containers run in UTC. Strategies, however, are defined against NSE
# market time, so every live candle must be bucketed in IST. A fixed offset is
# deliberate: India has no daylight-saving time and it works without tzdata.
MARKET_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")


class SymbolState:
    def __init__(self):
        self.current_minute = None
        self.open = self.high = self.low = self.close = None
        self.volume = 0
        self.cum_volume_traded = 0
        self.cum_turnover = 0.0  # for VWAP: sum(price * volume)
        self.closed_candles = deque(maxlen=50)  # keeps last 50 1-min candles
        self.ema20 = None
        self.last_ltp = None
        self.last_volume_total = None  # Fyers sends cumulative day volume per tick


class CandleAggregator:
    def __init__(self):
        self.symbols: dict[str, SymbolState] = defaultdict(SymbolState)

    def on_tick(self, symbol: str, ltp: float, day_volume: int, on_candle_close=None):
        """
        Call this on every tick. day_volume is Fyers' cumulative traded
        volume for the day (what the tick payload gives you) -- we diff
        it to get per-tick incremental volume.
        """
        state = self.symbols[symbol]
        now = datetime.datetime.now(MARKET_TZ).replace(tzinfo=None)
        minute_bucket = now.replace(second=0, microsecond=0)

        incremental_volume = 0
        if state.last_volume_total is not None:
            incremental_volume = max(0, day_volume - state.last_volume_total)
        state.last_volume_total = day_volume
        state.last_ltp = ltp

        if state.current_minute is None:
            state.current_minute = minute_bucket
            state.open = state.high = state.low = state.close = ltp
            state.volume = incremental_volume
        elif minute_bucket > state.current_minute:
            # close out the previous candle
            closed = {
                "time": state.current_minute, "open": state.open, "high": state.high,
                "low": state.low, "close": state.close, "volume": state.volume,
            }
            state.closed_candles.append(closed)
            state.cum_turnover += state.close * state.volume
            state.cum_volume_traded += state.volume
            self._update_ema(state, closed["close"])
            if on_candle_close:
                on_candle_close(symbol, closed, self.get_indicators(symbol))

            # start the new candle
            state.current_minute = minute_bucket
            state.open = state.high = state.low = state.close = ltp
            state.volume = incremental_volume
        else:
            state.high = max(state.high, ltp)
            state.low = min(state.low, ltp)
            state.close = ltp
            state.volume += incremental_volume

    def _update_ema(self, state: SymbolState, close_price: float, period: int = 20):
        k = 2 / (period + 1)
        state.ema20 = close_price if state.ema20 is None else close_price * k + state.ema20 * (1 - k)

    def get_indicators(self, symbol: str) -> dict:
        state = self.symbols[symbol]
        vwap = (state.cum_turnover / state.cum_volume_traded) if state.cum_volume_traded else None
        recent = list(state.closed_candles)[-20:]
        avg_volume = (sum(c["volume"] for c in recent) / len(recent)) if recent else None
        return {
            "vwap": vwap, "ema20": state.ema20, "avg_volume_20": avg_volume,
            "last_ltp": state.last_ltp,
        }

    def get_first_candle(self, symbol: str) -> dict | None:
        """The very first closed candle of the day -- what Algo 1's 9:15 check needs."""
        candles = self.symbols[symbol].closed_candles
        return candles[0] if candles else None
