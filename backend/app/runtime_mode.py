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
from .storage_namespace import current_and_legacy_values, namespaced_value
from .supabase_client import run_with_supabase

RUNTIME_MODE_TABLE = "app_runtime_settings"
RUNTIME_MODE_KEY = "trading_mode"
FYERS_LOGIN_MODE_KEY = "pending_fyers_login_mode"
FYERS_LOGIN_ORIGIN_KEY = "pending_fyers_login_origin"
VALID_TRADING_MODES = {"paper", "live"}
_runtime_mode_cache: str | None = None


def get_runtime_setting_storage_key(setting_key: str) -> str:
    return namespaced_value(setting_key)


def _get_runtime_setting_value(setting_key: str) -> str | None:
    storage_key = get_runtime_setting_storage_key(setting_key)
    for candidate in current_and_legacy_values(setting_key):
        result = run_with_supabase(
            lambda supabase, key=candidate: (
                supabase.table(RUNTIME_MODE_TABLE)
                .select("setting_value")
                .eq("setting_key", key)
                .limit(1)
                .execute()
            )
        )
        row = (result.data or [{}])[0] if result.data else None
        if row:
            return row.get("setting_value")
    return None


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
        mode = normalize_trading_mode(_get_runtime_setting_value(RUNTIME_MODE_KEY) or default_mode)
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
                    "setting_key": get_runtime_setting_storage_key(RUNTIME_MODE_KEY),
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


def set_pending_fyers_login_mode(mode: str | None) -> str:
    normalized = normalize_trading_mode(mode)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        run_with_supabase(
            lambda supabase: supabase.table(RUNTIME_MODE_TABLE).upsert(
                {
                    "setting_key": get_runtime_setting_storage_key(FYERS_LOGIN_MODE_KEY),
                    "setting_value": normalized,
                    "updated_at": now,
                }
            ).execute()
        )
    except Exception as exc:
        if "PGRST205" not in str(exc) and RUNTIME_MODE_TABLE not in str(exc):
            raise
        print(f"[runtime_mode] {RUNTIME_MODE_TABLE} missing; pending FYERS login mode kept in memory only until SQL migration is applied.")
    return normalized


def get_pending_fyers_login_mode() -> str | None:
    try:
        value = _get_runtime_setting_value(FYERS_LOGIN_MODE_KEY)
        return normalize_trading_mode(value) if value else None
    except Exception:
        return None


def clear_pending_fyers_login_mode() -> None:
    try:
        for candidate in current_and_legacy_values(FYERS_LOGIN_MODE_KEY):
            run_with_supabase(
                lambda supabase, key=candidate: supabase.table(RUNTIME_MODE_TABLE)
                .delete()
                .eq("setting_key", key)
                .execute()
            )
    except Exception:
        pass


def _normalize_frontend_origin(origin: str | None) -> str | None:
    value = str(origin or "").strip()
    if not value:
        return None
    if "://" not in value:
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def set_pending_fyers_login_origin(origin: str | None) -> str | None:
    normalized = _normalize_frontend_origin(origin)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not normalized:
        return None
    try:
        run_with_supabase(
            lambda supabase: supabase.table(RUNTIME_MODE_TABLE).upsert(
                {
                    "setting_key": get_runtime_setting_storage_key(FYERS_LOGIN_ORIGIN_KEY),
                    "setting_value": normalized,
                    "updated_at": now,
                }
            ).execute()
        )
    except Exception as exc:
        if "PGRST205" not in str(exc) and RUNTIME_MODE_TABLE not in str(exc):
            raise
        print(f"[runtime_mode] {RUNTIME_MODE_TABLE} missing; pending FYERS login origin kept in memory only until SQL migration is applied.")
    return normalized


def get_pending_fyers_login_origin() -> str | None:
    try:
        value = _get_runtime_setting_value(FYERS_LOGIN_ORIGIN_KEY)
        return _normalize_frontend_origin(value) if value else None
    except Exception:
        return None


def clear_pending_fyers_login_origin() -> None:
    try:
        for candidate in current_and_legacy_values(FYERS_LOGIN_ORIGIN_KEY):
            run_with_supabase(
                lambda supabase, key=candidate: supabase.table(RUNTIME_MODE_TABLE)
                .delete()
                .eq("setting_key", key)
                .execute()
            )
    except Exception:
        pass


def get_active_broker_key(mode: str | None = None) -> str:
    base = "fyers_live" if normalize_trading_mode(mode or get_runtime_trading_mode()) == "live" else "fyers"
    return namespaced_value(base)


def get_fyers_config(mode: str | None = None) -> dict[str, str]:
    active_mode = normalize_trading_mode(mode or get_runtime_trading_mode())
    if active_mode == "live":
        config = {
            "client_id": LIVE_FYERS_CLIENT_ID,
            "secret_key": LIVE_FYERS_SECRET_KEY,
            "redirect_uri": LIVE_FYERS_REDIRECT_URI or PAPER_FYERS_REDIRECT_URI,
            "fy_id": LIVE_FYERS_FY_ID or PAPER_FYERS_FY_ID,
            "pin": LIVE_FYERS_PIN or PAPER_FYERS_PIN,
            "totp_key": LIVE_FYERS_TOTP_KEY or PAPER_FYERS_TOTP_KEY,
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
