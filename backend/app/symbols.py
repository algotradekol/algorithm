"""
symbols.py — builds today's NSE 500 watchlist.

The actual "NSE 500" membership comes from NSE Indices directly (this
is an index definition, not something any broker API exposes,
including Fyers). We fetch it live so it's always current -- NSE
rebalances this list twice a year. We then cross-check each symbol
against Fyers' own symbol master so we only trade names Fyers can
actually quote/execute.
"""
import csv
import io
import requests

from .config import FYERS_PROXY_URL

NIFTY500_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
FYERS_SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
FYERS_PROXIES = {"http": FYERS_PROXY_URL, "https": FYERS_PROXY_URL} if FYERS_PROXY_URL else None

_cache = {"watchlist": None, "date": None}


def get_nse500_watchlist(force_refresh: bool = False) -> list[str]:
    """Returns Fyers-format symbols, e.g. ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ', ...]"""
    import datetime
    today = datetime.date.today().isoformat()
    if not force_refresh and _cache["watchlist"] and _cache["date"] == today:
        return _cache["watchlist"]

    headers = {"User-Agent": "Mozilla/5.0"}  # niftyindices.com blocks requests with no user-agent

    nifty500 = requests.get(NIFTY500_CSV_URL, headers=headers, timeout=15)
    nifty500.raise_for_status()
    reader = csv.DictReader(io.StringIO(nifty500.text))
    nifty500_tradingsymbols = {row["Symbol"].strip() for row in reader}

    fyers_master = requests.get(FYERS_SYMBOL_MASTER_URL, timeout=30, proxies=FYERS_PROXIES)
    fyers_master.raise_for_status()
    # Fyers symbol master has no header row; columns per their docs, symbol ticker is index 9,
    # trading symbol without exchange prefix is index 13 (verify against current file if this
    # ever breaks -- Fyers has changed this file's shape before)
    fyers_symbols = {}
    for line in fyers_master.text.splitlines():
        parts = line.split(",")
        if len(parts) > 13 and parts[13].strip():
            fyers_symbols[parts[13].strip()] = parts[9].strip()  # tradingsymbol -> full Fyers symbol

    watchlist = []
    skipped = []
    for sym in nifty500_tradingsymbols:
        fyers_symbol = fyers_symbols.get(f"{sym}-EQ") or fyers_symbols.get(sym)
        if fyers_symbol:
            watchlist.append(fyers_symbol)
        else:
            skipped.append(sym)

    if skipped:
        print(f"[symbols] {len(skipped)} NSE500 symbols had no Fyers match, skipped: {skipped[:10]}...")

    _cache["watchlist"] = sorted(watchlist)
    _cache["date"] = today
    return _cache["watchlist"]
