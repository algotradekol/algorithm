from __future__ import annotations

import datetime
from typing import Any


def audit_log(component: str, message: str, **fields: Any) -> None:
    """Emit a compact structured audit line to stdout."""
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    suffix = ""
    if fields:
        suffix = " | " + " ".join(f"{key}={value!r}" for key, value in fields.items())
    print(f"[audit] {timestamp} [{component}] {message}{suffix}")
