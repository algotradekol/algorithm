"""
supabase_client.py - helpers for resilient backend-only Supabase access.

Using a single long-lived shared client was causing intermittent transport
disconnects under concurrent polling. We create a fresh client per operation
and retry transient network/protocol failures.
"""
import time

import httpcore
import httpx
from supabase import Client, create_client

from .config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

TRANSIENT_SUPABASE_ERRORS = (
    httpcore.NetworkError,
    httpcore.ProtocolError,
    httpx.NetworkError,
    httpx.ProtocolError,
    httpx.TransportError,
)


def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def run_with_supabase(operation, *, attempts: int = 3, delay_seconds: float = 0.25):
    last_error = None
    for attempt in range(1, attempts + 1):
        client = get_supabase()
        try:
            return operation(client)
        except TRANSIENT_SUPABASE_ERRORS as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(delay_seconds * attempt)
    raise RuntimeError(f"Supabase request failed after {attempts} attempts: {last_error}") from last_error
