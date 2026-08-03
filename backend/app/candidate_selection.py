def _int_setting(settings: dict, key: str, default: int) -> int:
    try:
        return int(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def select_candidates_first_come(candidates: list[dict], settings: dict) -> list[dict]:
    """Select candidates in encounter order with per-side caps and overflow."""
    total_cap = max(0, _int_setting(settings, "max_trades_per_day", 10))
    buy_cap = max(0, _int_setting(settings, "max_buy_trades", total_cap))
    sell_cap = max(0, _int_setting(settings, "max_sell_trades", total_cap))

    selected: list[dict] = []
    deferred: list[dict] = []
    selected_symbols: set[str] = set()
    buy_count = 0
    sell_count = 0

    for row in candidates:
        symbol = row.get("symbol")
        side = str(row.get("side") or "").upper()
        if not symbol or symbol in selected_symbols or len(selected) >= total_cap:
            continue
        if side == "BUY":
            if buy_count < buy_cap:
                selected.append(row)
                selected_symbols.add(symbol)
                buy_count += 1
            else:
                deferred.append(row)
        elif side == "SELL":
            if sell_count < sell_cap:
                selected.append(row)
                selected_symbols.add(symbol)
                sell_count += 1
            else:
                deferred.append(row)

    for row in deferred:
        symbol = row.get("symbol")
        if not symbol or symbol in selected_symbols or len(selected) >= total_cap:
            continue
        side = str(row.get("side") or "").upper()
        selected.append(row)
        selected_symbols.add(symbol)
        if side == "BUY":
            buy_count += 1
        elif side == "SELL":
            sell_count += 1

    return selected
