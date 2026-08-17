"""Proxy reachability check for the live-order path (F2, 2026-08-17).

Before 2026-08-17 the backend fell back silently to a direct connection
when the Squid/bore proxy was unreachable. That let 30 client orders leak
out from Railway's egress IP and get rejected by Fyers with `-99, Bad
request` (IP not whitelisted). The failure was invisible for ~8 minutes
before someone noticed.

This module keeps a short-lived health flag: on demand, it makes one
small request through the configured proxy and caches the result for
`_CACHE_TTL_SECONDS`. Callers in the order path consult it and refuse
to place orders when the proxy is configured-but-unreachable, instead
of silently going direct.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

_CACHE_TTL_SECONDS = 30
_PROBE_TIMEOUT = (3, 5)
# Fyers's own auth endpoint — small, always up, and CORS-friendly. HEAD
# through the proxy is enough to prove the tunnel is alive without
# consuming Fyers's rate budget.
_PROBE_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"

_lock = threading.Lock()
_cache: dict[str, tuple[float, bool, str | None]] = {}


def check_proxy_reachable(proxy_url: str) -> tuple[bool, str | None]:
    """Return (reachable, error_message). Cached for _CACHE_TTL_SECONDS.

    A blank/None proxy_url is treated as "no proxy configured" and returns
    (True, None) — callers that require a proxy check `bool(proxy_url)`
    themselves before calling this.
    """
    if not proxy_url:
        return True, None
    if requests is None:  # pragma: no cover
        return False, "requests library unavailable"

    now = time.monotonic()
    with _lock:
        cached = _cache.get(proxy_url)
        if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1], cached[2]

    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        result = (False, f"proxy url is malformed: {proxy_url!r}")
        with _lock:
            _cache[proxy_url] = (now, result[0], result[1])
        return result

    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        r = requests.head(_PROBE_URL, proxies=proxies, timeout=_PROBE_TIMEOUT, allow_redirects=False)
        # Any HTTP response — even 4xx — proves the tunnel is delivering
        # traffic to Fyers's edge. We're not checking Fyers itself, only
        # the path.
        reachable = bool(r.status_code)
        err = None if reachable else f"probe returned no status"
    except Exception as exc:
        reachable = False
        err = f"{type(exc).__name__}: {exc}"

    with _lock:
        _cache[proxy_url] = (now, reachable, err)
    return reachable, err


def invalidate_cache() -> None:
    """Force the next check to re-probe. Call after known network events
    (proxy env var change, redeploy)."""
    with _lock:
        _cache.clear()
