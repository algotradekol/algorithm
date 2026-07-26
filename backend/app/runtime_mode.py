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
    LIVE_FYERS_CLIENT_ID,
    LIVE_FYERS_FY_ID,
    LIVE_FYERS_PIN,
    LIVE_FYERS_REDIRECT_URI,
    LIVE_FYERS_PROXY_URL,
    LIVE_FYERS_SECRET_KEY,
    LIVE_FYERS_TOTP_KEY,
    PAPER_FYERS_CLIENT_ID,
    PAPER_FYERS_FY_ID,
    PAPER_FYERS_PIN,
    PAPER_FYERS_REDIRECT_URI,
    PAPER_FYERS_SECRET_KEY,
    PAPER_FYERS_TOTP_KEY,
    PAPER_FYERS_PROXY_URL,
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
    except Exception as exc:
        if "PGRST205" in str(exc) or RUNTIME_MODE_TABLE in str(exc):
            mode = default_mode
        else:
            mode = default_mode

    _runtime_mode_cache = mode
    return mode


def set_runtime_trading_mode(mode: str) -> str:
    normalized = normalize_trading_mode(mode)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        run_with_supabase(
            lambda supabase: supabase.table(RUNTIME_MODE_TABLE).upsert(
                {
                    "setting_key": RUNTIME_MODE_KEY,
                    "setting_value": normalized,
                    "updated_at": now,
                }
            ).execute()
        )
    except Exception as exc:
        if "PGRST205" not in str(exc) and RUNTIME_MODE_TABLE not in str(exc):
            raise
        print(f"[runtime_mode] {RUNTIME_MODE_TABLE} missing; keeping mode in memory only until SQL migration is applied.")
    global _runtime_mode_cache
    _runtime_mode_cache = normalized
    return normalized


def get_active_broker_key(mode: str | None = None) -> str:
    return "fyers_live" if normalize_trading_mode(mode or get_runtime_trading_mode()) == "live" else "fyers"


def get_fyers_config(mode: str | None = None) -> dict[str, str]:
    active_mode = normalize_trading_mode(mode or get_runtime_trading_mode())
    if active_mode == "live":
        live_client_id = LIVE_FYERS_CLIENT_ID or PAPER_FYERS_CLIENT_ID
        live_secret_key = LIVE_FYERS_SECRET_KEY or PAPER_FYERS_SECRET_KEY
        live_redirect_uri = LIVE_FYERS_REDIRECT_URI or PAPER_FYERS_REDIRECT_URI
        live_fy_id = LIVE_FYERS_FY_ID or PAPER_FYERS_FY_ID
        live_pin = LIVE_FYERS_PIN or PAPER_FYERS_PIN
        live_totp_key = LIVE_FYERS_TOTP_KEY or PAPER_FYERS_TOTP_KEY
        config = {
            "client_id": live_client_id,
            "secret_key": live_secret_key,
            "redirect_uri": live_redirect_uri,
            "fy_id": live_fy_id,
            "pin": live_pin,
            "totp_key": live_totp_key,
            "proxy_url": LIVE_FYERS_PROXY_URL,
        }
    else:
        config = {
            "client_id": PAPER_FYERS_CLIENT_ID,
            "secret_key": PAPER_FYERS_SECRET_KEY,
            "redirect_uri": PAPER_FYERS_REDIRECT_URI,
            "fy_id": PAPER_FYERS_FY_ID,
            "pin": PAPER_FYERS_PIN,
            "totp_key": PAPER_FYERS_TOTP_KEY,
            # Paper mode does not require a proxy. Keep it optional so the
            # paper login and history routes still work when only the original
            # legacy FYERS_PROXY_URL or no proxy at all is configured.
            "proxy_url": PAPER_FYERS_PROXY_URL,
        }

    required_missing = [name for name, value in config.items() if not value and name != "proxy_url"]
    if active_mode == "live" and not config.get("proxy_url"):
        required_missing.append("proxy_url")
    missing = required_missing
    if missing:
        raise RuntimeError(f"Missing Fyers configuration for {active_mode} mode: {', '.join(sorted(missing))}")
    return config
