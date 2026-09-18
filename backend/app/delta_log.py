"""Structured Delta-only stdout diagnostics.

These lines are intentionally safe for Railway logs: API keys/secrets are not
accepted here, and long nested exchange payloads are compacted.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time


_summary_lock = threading.Lock()
_summaries = {}
_routine_events = {
    "price_accepted", "price_seen_by_strategy", "price_ignored_duplicate",
    "price_ignored_stale", "rest_trade", "trigger_check",
    "trigger_check_skipped_no_open", "history_refresh_ok",
    "live_reconcile_no_tracked_positions", "live_reconcile_snapshot",
    "live_active_orders", "live_product_size",
}


def _compact_fields(event, fields):
    if event not in _routine_events or os.environ.get("DELTA_LOG_MODE", "compact").lower() == "verbose":
        return fields
    # Never suppress a crossing or an explicit settings-save recheck.
    if event == "trigger_check" and (
        fields.get("reason") == "settings_change"
        or any((fields.get(side) or {}).get("crossed") for side in ("buy_checks", "sell_checks"))
    ):
        return fields
    key = (event, fields.get("mode"), fields.get("asset"), fields.get("strategy"), fields.get("minutes"), fields.get("source"))
    state_keys = (
        "settings", "scan_enabled", "trading_enabled", "data_error", "ready_for_triggers",
        "current_bucket", "bucket", "buy_level", "sell_level", "buy_reference_time",
        "sell_reference_time", "last_candle_epoch", "live_size", "tracked_minutes", "orders",
    )
    signature = json.dumps({k: _safe(fields[k]) for k in state_keys if k in fields}, sort_keys=True)
    now = time.monotonic()
    with _summary_lock:
        previous = _summaries.get(key)
        if previous and previous[1] == signature and now - previous[0] < 60:
            previous[2] += 1
            return None
        suppressed = previous[2] if previous else 0
        _summaries[key] = [now, signature, 0]
    return {**fields, "suppressed_repeats": suppressed}


def delta_debug_enabled() -> bool:
    value = os.environ.get("DELTA_DEBUG_LOGS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _safe(value, depth=0):
    if depth > 4:
        return "<nested>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        blocked = {"api_key", "api_secret", "secret", "authorization", "token", "access_token"}
        return {
            str(key): ("<redacted>" if str(key).lower() in blocked else _safe(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        clipped = [_safe(item, depth + 1) for item in items[:12]]
        if len(items) > 12:
            clipped.append(f"<+{len(items) - 12} more>")
        return clipped
    return str(value)


def delta_log(event: str, **fields) -> None:
    if not delta_debug_enabled():
        return
    fields = _compact_fields(event, fields)
    if fields is None:
        return
    payload = {
        "event": event,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **{key: _safe(value) for key, value in fields.items()},
    }
    sys.stdout.write("[delta-trace] " + json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
