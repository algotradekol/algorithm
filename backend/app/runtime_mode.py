"""
runtime_mode.py - runtime trading mode selection shared by the backend.

The app still uses the environment variable TRADING_MODE as a default,
but the active mode can now be overridden from the UI and stored in
Supabase so you can flip between paper and live without redeploying.
"""
from __future__ import annotations

import datetime
import os

from .config import (
    PAPER_FYERS_CLIENT_ID,
    PAPER_FYERS_FY_ID,
    PAPER_FYERS_PIN,
    PAPER_FYERS_REDIRECT_URI,
    PAPER_FYERS_SECRET_KEY,
    PAPER_FYERS_TOTP_KEY,
    FYERS_PROXY_URL,
    LIVE_FYERS_CLIENT_ID,
    LIVE_FYERS_SECRET_KEY,
)
from .supabase_client import run_with_supabase

RUNTIME_MODE_TABLE = "app_runtime_settings"
RUNTIME_MODE_KEY = "trading_mode"
VALID_TRADING_MODES = {"paper", "live"}
_runtime_mode_cache: str | None = None


def normalize_trading_mode(mode: str | None) -> str:
    value = str(mode or "").strip().lower()
    return value if value in VALID_TRADING_MODES else "paper"


def get_default_trading_mode() -> str:
    return normalize_trading_mode(os.environ.get("TRADING_MODE", "paper"))


def get_runtime_trading_mode(force_refresh: bool = False) -> str:
    global _runtime_mode_cache
    if not force_refresh and _runtime_mode_cache in VALID_TRADING_MODES:
        return _runtime_mode_cache

    default_mode = get_default_trading_mode()
    try:
        result = run_with_supabase(
            lambda supabase: (
                supabase.table(RUNTIME_MODE_TABLE)
                .select("setting_value")
                .eq("setting_key", RUNTIME_MODE_KEY)
                .limit(1)
                .execute()
            )
        )
        row = (result.data or [{}])[0] if result.data else None
        mode = normalize_trading_mode((row or {}).get("setting_value") or default_mode)
    except Exception:
        mode = default_mode

    _runtime_mode_cache = mode
    return mode


def set_runtime_trading_mode(mode: str) -> str:
    normalized = normalize_trading_mode(mode)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run_with_supabase(
        lambda supabase: supabase.table(RUNTIME_MODE_TABLE).upsert(
            {
                "setting_key": RUNTIME_MODE_KEY,
                "setting_value": normalized,
                "updated_at": now,
            }
        ).execute()
    )
    global _runtime_mode_cache
    _runtime_mode_cache = normalized
    return normalized


def get_active_broker_key(mode: str | None = None) -> str:
    return "fyers_live" if normalize_trading_mode(mode or get_runtime_trading_mode()) == "live" else "fyers"


def get_fyers_config(mode: str | None = None) -> dict[str, str]:
    active_mode = normalize_trading_mode(mode or get_runtime_trading_mode())
    if active_mode == "live":
        config = {
            "client_id": LIVE_FYERS_CLIENT_ID,
            "secret_key": LIVE_FYERS_SECRET_KEY,
            "redirect_uri": PAPER_FYERS_REDIRECT_URI,
            "fy_id": PAPER_FYERS_FY_ID,
            "pin": PAPER_FYERS_PIN,
            "totp_key": PAPER_FYERS_TOTP_KEY,
            "proxy_url": FYERS_PROXY_URL,
        }
    else:
        config = {
            "client_id": PAPER_FYERS_CLIENT_ID,
            "secret_key": PAPER_FYERS_SECRET_KEY,
            "redirect_uri": PAPER_FYERS_REDIRECT_URI,
            "fy_id": PAPER_FYERS_FY_ID,
            "pin": PAPER_FYERS_PIN,
            "totp_key": PAPER_FYERS_TOTP_KEY,
            "proxy_url": FYERS_PROXY_URL,
        }

    missing = [name for name, value in config.items() if not value]
    if missing:
        raise RuntimeError(f"Missing Fyers configuration for {active_mode} mode: {', '.join(sorted(missing))}")
    return config
