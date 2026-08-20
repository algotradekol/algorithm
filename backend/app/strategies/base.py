"""
base.py — every strategy (current and future) implements this
interface. The engine doesn't know or care what's inside a strategy,
it just calls these hooks on every tick/candle.
"""
from abc import ABC, abstractmethod

from ..config import MARKET_OPEN_TIME, SQUARE_OFF_TIME


class Strategy(ABC):
    algo_id: str        # unique short id, e.g. "algo1", "algo2"
    display_name: str   # shown in the frontend tab

    def market_session_start(self) -> str:
        """Inclusive session start in HH:MM IST."""
        return MARKET_OPEN_TIME

    def market_session_end(self) -> str:
        """Exclusive session end in HH:MM IST."""
        return "15:30"

    def square_off_time(self) -> str:
        """Force-close cutoff in HH:MM IST."""
        return SQUARE_OFF_TIME

    @abstractmethod
    def on_tick(self, symbol: str, ltp: float, timestamp):
        """Called on every live tick for every watchlist symbol."""
        ...

    @abstractmethod
    def on_candle_close(self, symbol: str, candle: dict, indicators: dict):
        """Called whenever candle_aggregator closes a 1-minute bar for a symbol."""
        ...

    @abstractmethod
    def check_exits(self):
        """Called on every tick -- check open positions against SL/target."""
        ...

    @abstractmethod
    def square_off_all(self):
        """Called once at the strategy's configured cutoff -- force-close every open position."""
        ...
