from __future__ import annotations

from .runtime_mode import get_runtime_trading_mode
from .live_broker import LiveBroker
from .paper_broker import PaperBroker


def create_broker(algo_id: str, starting_capital: float):
    if get_runtime_trading_mode() == "live":
        return LiveBroker(algo_id=algo_id, starting_capital=starting_capital)
    return PaperBroker(algo_id=algo_id, starting_capital=starting_capital)


def trading_mode() -> str:
    return get_runtime_trading_mode()
