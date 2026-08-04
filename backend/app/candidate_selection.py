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


def execute_candidates_first_come(
    candidates: list[dict],
    settings: dict,
    enter_candidate,
    existing_counts: dict | None = None,
) -> tuple[list[dict], set[str]]:
    """Open candidates in encounter order and consume slots only on success."""
    existing_counts = existing_counts or {}
    total_cap = max(0, _int_setting(settings, "max_trades_per_day", 10))
    buy_cap = max(0, _int_setting(settings, "max_buy_trades", total_cap))
    sell_cap = max(0, _int_setting(settings, "max_sell_trades", total_cap))
    total_count = max(0, int(existing_counts.get("trade_count_today") or 0))
    buy_count = max(0, int(existing_counts.get("buy_count_today") or 0))
    sell_count = max(0, int(existing_counts.get("sell_count_today") or 0))

    opened: list[dict] = []
    attempted: set[str] = set()
    deferred: list[dict] = []
    seen: set[str] = set()

    def attempt(row: dict) -> bool:
        nonlocal total_count, buy_count, sell_count
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").upper()
        if not symbol or symbol in attempted or total_count >= total_cap:
            return False
        attempted.add(symbol)
        if not enter_candidate(row):
            return False
        opened.append(row)
        total_count += 1
        if side == "BUY":
            buy_count += 1
        elif side == "SELL":
            sell_count += 1
        return True

    for row in candidates:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").upper()
        if not symbol or symbol in seen or total_count >= total_cap:
            continue
        seen.add(symbol)
        if side == "BUY":
            if buy_count < buy_cap:
                attempt(row)
            else:
                deferred.append(row)
        elif side == "SELL":
            if sell_count < sell_cap:
                attempt(row)
            else:
                deferred.append(row)

    # Preserve the existing overflow behavior: unused side capacity can be
    # filled by the opposite side, but only successful broker entries count.
    for row in deferred:
        if total_count >= total_cap:
            break
        attempt(row)

    return opened, attempted
