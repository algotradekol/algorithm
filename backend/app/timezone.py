from __future__ import annotations

import datetime

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - fallback for environments without tzdata
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
