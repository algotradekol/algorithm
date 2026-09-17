"""Structured Delta-only stdout diagnostics.

These lines are intentionally safe for Railway logs: API keys/secrets are not
accepted here, and long nested exchange payloads are compacted.
"""
from __future__ import annotations

import datetime
import json
import os


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
    payload = {
        "event": event,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **{key: _safe(value) for key, value in fields.items()},
    }
    print("[delta-trace] " + json.dumps(payload, separators=(",", ":"), sort_keys=True))
