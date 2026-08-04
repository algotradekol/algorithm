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
from pathlib import Path

import requests

from .config import FYERS_PROXY_URL

NIFTY500_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
FYERS_SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
FYERS_PROXIES = {"http": FYERS_PROXY_URL, "https": FYERS_PROXY_URL} if FYERS_PROXY_URL else None
NIFTY500_FALLBACK_PATH = Path(__file__).with_name("data") / "ind_nifty500list.csv"
MIN_VALID_CONSTITUENTS = 450

_cache = {"watchlist": None, "date": None, "universe": None, "sector_map": None}


def _parse_nifty500_csv(content: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    rows = [
        {
            "company_name": (row.get("Company Name") or "").strip(),
            "industry": (row.get("Industry") or "").strip(),
            "symbol": (row.get("Symbol") or "").strip(),
            "series": (row.get("Series") or "").strip(),
            "isin_code": (row.get("ISIN Code") or "").strip(),
        }
        for row in reader
        if (row.get("Symbol") or "").strip()
    ]
    if len(rows) < MIN_VALID_CONSTITUENTS:
        raise ValueError(f"NSE 500 CSV contained only {len(rows)} valid constituents")
    return rows


def _load_nifty500_rows() -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
        "Referer": "https://www.niftyindices.com/indices/equity/broad-based-indices/nifty-500",
    }
    try:
        response = requests.get(NIFTY500_CSV_URL, headers=headers, timeout=15)
        response.raise_for_status()
        rows = _parse_nifty500_csv(response.text)
        print(f"[symbols] loaded {len(rows)} NSE 500 constituents from NSE Indices")
        return rows
    except (requests.RequestException, ValueError) as exc:
        print(f"[symbols] NSE Indices download unavailable, using bundled snapshot: {exc}")

    try:
        rows = _parse_nifty500_csv(NIFTY500_FALLBACK_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Unable to load NSE 500 constituents online or from {NIFTY500_FALLBACK_PATH}: {exc}"
        ) from exc
    print(f"[symbols] loaded {len(rows)} NSE 500 constituents from bundled snapshot")
    return rows


def _load_universe() -> list[dict]:
    nifty500_rows = _load_nifty500_rows()

    fyers_symbols = {}
    try:
        try:
            fyers_master = requests.get(FYERS_SYMBOL_MASTER_URL, timeout=10, proxies=FYERS_PROXIES)
            fyers_master.raise_for_status()
        except requests.RequestException as proxy_error:
            if not FYERS_PROXIES:
                raise
            print(f"[symbols] Fyers symbol master proxy fetch failed, retrying direct: {proxy_error}")
            fyers_master = requests.get(FYERS_SYMBOL_MASTER_URL, timeout=10)
            fyers_master.raise_for_status()

        # Fyers symbol master has no header row; columns per their docs, symbol ticker is index 9,
        # trading symbol without exchange prefix is index 13 (verify against current file if this
        # ever breaks -- Fyers has changed this file's shape before)
        for line in fyers_master.text.splitlines():
            parts = line.split(",")
            if len(parts) > 13 and parts[13].strip():
                trading_symbol = parts[13].strip()
                fyers_symbol = parts[9].strip()
                existing = fyers_symbols.get(trading_symbol)
                if not existing or (fyers_symbol.endswith("-EQ") and not existing.endswith("-EQ")):
                    fyers_symbols[trading_symbol] = fyers_symbol  # tradingsymbol -> full Fyers symbol
    except requests.RequestException as exc:
        print(f"[symbols] Fyers symbol master unavailable, using NSE symbols directly: {exc}")

    universe = []
    skipped = []
    for row in nifty500_rows:
        symbol = row["symbol"]
        fyers_symbol = fyers_symbols.get(f"{symbol}-EQ") or fyers_symbols.get(symbol) or f"NSE:{symbol}-EQ"
        if fyers_symbol:
            universe.append({
                **row,
                "fyers_symbol": fyers_symbol,
            })
        else:
            skipped.append(symbol)

    if skipped:
        print(f"[symbols] {len(skipped)} NSE500 symbols had no Fyers match, skipped: {skipped[:10]}...")

    universe.sort(key=lambda row: row["fyers_symbol"])
    return universe


def get_nse500_watchlist(force_refresh: bool = False) -> list[str]:
    """Returns Fyers-format symbols, e.g. ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ', ...]"""
    import datetime
    today = datetime.date.today().isoformat()
    if not force_refresh and _cache["watchlist"] and _cache["date"] == today:
        return _cache["watchlist"]

    universe = get_nse500_universe(force_refresh=force_refresh)
    watchlist = [row["fyers_symbol"] for row in universe]
    _cache["watchlist"] = watchlist
    _cache["date"] = today
    return _cache["watchlist"]


def get_nse500_universe(force_refresh: bool = False) -> list[dict]:
    import datetime
    today = datetime.date.today().isoformat()
    if not force_refresh and _cache["universe"] and _cache["date"] == today:
        return _cache["universe"]
    universe = _load_universe()
    _cache["universe"] = universe
    _cache["date"] = today
    _cache["watchlist"] = [row["fyers_symbol"] for row in universe]
    _cache["sector_map"] = {row["fyers_symbol"]: row["industry"] for row in universe if row.get("industry")}
    return universe


def get_nse500_sector_map(force_refresh: bool = False) -> dict[str, str]:
    import datetime
    today = datetime.date.today().isoformat()
    if not force_refresh and _cache["sector_map"] and _cache["date"] == today:
        return _cache["sector_map"]
    universe = get_nse500_universe(force_refresh=force_refresh)
    sector_map = {row["fyers_symbol"]: row["industry"] for row in universe if row.get("industry")}
    _cache["sector_map"] = sector_map
    _cache["date"] = today
    return sector_map
