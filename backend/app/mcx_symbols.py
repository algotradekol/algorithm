"""
mcx_symbols.py — helpers for resolving live MCX contract symbols.

This keeps MCX-specific symbol lookup separate from the NSE500 universe
builder so one-off commodity strategies can auto-track the active contract
without hardcoding a monthly expiry in the UI.
"""
from __future__ import annotations

import datetime
import io
import csv

import requests

from .config import FYERS_PROXY_URL
from .audit_log import audit_log

MCX_SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/MCX_COM.csv"
MCX_PROXIES = {"http": FYERS_PROXY_URL, "https": FYERS_PROXY_URL} if FYERS_PROXY_URL else None

_cache: dict[str, dict[str, str]] = {"date": None, "symbols": {}}


def _download_symbol_master() -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        try:
            response = requests.get(MCX_SYMBOL_MASTER_URL, headers=headers, timeout=15, proxies=MCX_PROXIES)
            response.raise_for_status()
        except requests.RequestException as proxy_error:
            if not MCX_PROXIES:
                raise
            audit_log("mcx_symbols", "symbol master proxy fetch failed, retrying direct", error=str(proxy_error))
            response = requests.get(MCX_SYMBOL_MASTER_URL, headers=headers, timeout=15)
            response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        audit_log("mcx_symbols", "symbol master unavailable, falling back to cached/default contract", error=str(exc))
        return ""


def _expiry_sort_key(row: dict) -> tuple[int, str]:
    expiry_ts = row.get("expiry_ts")
    return (int(expiry_ts) if expiry_ts is not None else 2**31 - 1, row.get("symbol", ""))


def get_active_mcx_contract(root: str) -> str:
    """Return the currently active Fyers symbol for an MCX root, e.g.
    ``MCX:SILVERMIC26AUGFUT`` for ``root='SILVERMIC'``.

    We choose the nearest expiry listed in Fyers' master file so the app
    keeps following the current front-month contract as it rolls.
    """
    today = datetime.date.today().isoformat()
    root_key = root.strip().upper()
    cached = _cache["symbols"].get(root_key)
    if cached and _cache["date"] == today:
        return cached

    master_text = _download_symbol_master()
    if not master_text:
        fallback = f"MCX:{root_key}31AUGFUT"
        audit_log("mcx_symbols", "using fallback MCX contract after master load failure", root=root_key, fallback=fallback)
        _cache["date"] = today
        _cache["symbols"][root_key] = fallback
        return fallback

    lines = master_text.splitlines()
    candidates: list[dict] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) <= 13:
            continue
        trading_symbol = parts[13].strip().upper()
        fyers_symbol = parts[9].strip()
        if trading_symbol != root_key or not fyers_symbol:
            continue
        expiry_ts = None
        try:
            expiry_ts = int(float(parts[8]))
        except (TypeError, ValueError):
            expiry_ts = None
        candidates.append({
            "symbol": fyers_symbol,
            "expiry_ts": expiry_ts,
        })

    if not candidates:
        fallback = f"MCX:{root_key}31AUGFUT"
        print(f"[mcx_symbols] could not resolve active MCX contract for {root_key}, falling back to {fallback}")
        _cache["date"] = today
        _cache["symbols"][root_key] = fallback
        return fallback

    selected = sorted(candidates, key=_expiry_sort_key)[0]["symbol"]
    _cache["date"] = today
    _cache["symbols"][root_key] = selected
    return selected


def get_active_mcx_watchlist(root: str) -> list[str]:
    symbol = get_active_mcx_contract(root)
    return [symbol] if symbol else []
