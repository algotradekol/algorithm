import asyncio

_manager = None
_loop = None


def set_manager(manager):
    global _manager, _loop
    _manager = manager
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None


def broadcast_sync(data: dict):
    """Call this from synchronous engine/strategy code."""
    if _manager is None or _loop is None or not _loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(_manager.broadcast(data), _loop)
