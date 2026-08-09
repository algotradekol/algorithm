"""
margin_lookup.py — per-stock intraday margin multiplier.

The broker's approved-securities list assigns each NSE symbol its own
intraday leverage (1x / 2x / 4x / 5x). Margin is NOT a fixed global
value: the client sends an updated list periodically, so this reads a
bundled CSV (backend/app/data/approved_securities.csv) rather than a
Supabase table — Supabase egress is capped and this data changes rarely.

To refresh: drop a new NSE-filtered CSV (columns: symbol,
intraday_multiplier) at that path and restart. Any symbol not present
in the list is treated as NOT margin-approved -> multiplier 1 (cash).
"""
from __future__ import annotations

import csv
from pathlib import Path

_CSV_PATH = Path(__file__).with_name("data") / "approved_securities.csv"

# Symbols absent from the approved list get no leverage. Sizing then
# falls back to plain cash (capital // price), never inflating exposure.
DEFAULT_MULTIPLIER = 1

_cache: dict[str, int] | None = None


def _normalize(symbol: str) -> str:
    """Strip Fyers formatting to the bare NSE trading symbol.

    'NSE:RELIANCE-EQ' -> 'RELIANCE', 'reliance' -> 'RELIANCE'.
    """
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith("-EQ"):
        s = s[:-3]
    return s


def _load() -> dict[str, int]:
    global _cache
    if _cache is not None:
        return _cache
    table: dict[str, int] = {}
    try:
        with _CSV_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sym = _normalize(row.get("symbol", ""))
                if not sym:
                    continue
                try:
                    table[sym] = int(float(row.get("intraday_multiplier", 1)))
                except (TypeError, ValueError):
                    table[sym] = DEFAULT_MULTIPLIER
    except FileNotFoundError:
        print(f"[margin_lookup] approved-securities CSV missing at {_CSV_PATH}; defaulting all symbols to {DEFAULT_MULTIPLIER}x")
    _cache = table
    return table


def get_intraday_multiplier(symbol: str) -> int:
    """Return the broker-approved intraday multiplier for a symbol.

    Accepts either a bare trading symbol ('RELIANCE') or a Fyers symbol
    ('NSE:RELIANCE-EQ'). Unknown symbols return DEFAULT_MULTIPLIER (1x).
    """
    return _load().get(_normalize(symbol), DEFAULT_MULTIPLIER)


def effective_multiplier(symbol: str, cap: float | None = None) -> int:
    """Per-stock multiplier, optionally throttled by a global cap.

    The strategy-settings 'margin_multiplier' acts as a ceiling so the
    client can de-risk every trade at once without editing the list. With
    the default cap of 5 (>= every value in the list) this is a no-op and
    the raw per-stock multiplier applies.
    """
    stock = get_intraday_multiplier(symbol)
    if cap is None:
        return stock
    try:
        cap_int = int(float(cap))
    except (TypeError, ValueError):
        return stock
    if cap_int < 1:
        return stock
    return min(stock, cap_int)
