from __future__ import annotations

from .runtime_mode import get_runtime_trading_mode
from .live_broker import LiveBroker
from .paper_broker import PaperBroker


# Silver Micro 2.0 is an isolated paper/backtest experiment.  Keep this
# guard in the broker factory as the final protection even if a caller bypasses
# the dashboard and switches the global runtime mode to live.
PAPER_ONLY_ALGO_IDS = frozenset({"algo5"})


def create_broker(algo_id: str, starting_capital: float):
    if algo_id in PAPER_ONLY_ALGO_IDS:
        return PaperBroker(algo_id=algo_id, starting_capital=starting_capital)
    if get_runtime_trading_mode() == "live":
        return LiveBroker(algo_id=algo_id, starting_capital=starting_capital)
    return PaperBroker(algo_id=algo_id, starting_capital=starting_capital)


def trading_mode() -> str:
    return get_runtime_trading_mode()
