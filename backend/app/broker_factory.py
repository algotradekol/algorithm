from __future__ import annotations

from .config import IS_LIVE_TRADING
from .live_broker import LiveBroker
from .paper_broker import PaperBroker


def create_broker(algo_id: str, starting_capital: float):
    if IS_LIVE_TRADING:
        return LiveBroker(algo_id=algo_id, starting_capital=starting_capital)
    return PaperBroker(algo_id=algo_id, starting_capital=starting_capital)


def trading_mode() -> str:
    return "live" if IS_LIVE_TRADING else "paper"
