"""
auth.py — verifies the Supabase JWT the frontend sends with every
request (in the Authorization header). This is what makes the backend
password-gated the same way the frontend is: Supabase issues the
token at login, we just check it's valid and not expired.
"""
from functools import lru_cache
import datetime

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

from .config import SUPABASE_JWT_SECRET, SUPABASE_URL
from .supabase_client import run_with_supabase
from .viewer_invites import device_hash


@lru_cache(maxsize=1)
def get_jwks_client() -> PyJWKClient | None:
    if not SUPABASE_URL:
        return None
    return PyJWKClient(f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json")


def _decode_bearer(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.split(" ", 1)[1]
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")

        if algorithm == "HS256" and SUPABASE_JWT_SECRET:
            payload = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
            )
        else:
            jwks_client = get_jwks_client()
            if not jwks_client:
                raise HTTPException(status_code=500, detail="Supabase JWKS endpoint is not configured")

            signing_key = jwks_client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=[algorithm] if algorithm else None,
                audience="authenticated",
            )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")

    return payload  # contains the Supabase user id (payload["sub"]) etc.


def require_auth(authorization: str = Header(None)):
    payload = _decode_bearer(authorization)
    if payload.get("role") == "viewer" or payload.get("login_method") == "viewer_invite":
        raise HTTPException(status_code=403, detail="Viewer access is read-only and Delta-only")
    return payload


def _require_viewer_device(payload: dict, viewer_device: str | None):
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Viewer session is missing. Please redeem a new code.")
    if not viewer_device:
        raise HTTPException(status_code=401, detail="This viewer session is locked to its original browser.")
    try:
        result = run_with_supabase(
            lambda db: db.table("viewer_sessions")
            .select("id,expires_at,revoked_at,device_hash")
            .eq("id", session_id)
            .eq("device_hash", device_hash(viewer_device))
            .limit(1)
            .execute()
        )
        row = (result.data or [None])[0]
    except Exception:
        raise HTTPException(status_code=503, detail="Viewer session validation is temporarily unavailable") from None
    if not row or row.get("revoked_at"):
        raise HTTPException(status_code=401, detail="This viewer session is no longer valid.")
    try:
        expires_at = datetime.datetime.fromisoformat(str(row.get("expires_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="This viewer session is no longer valid.") from None
    if expires_at <= datetime.datetime.now(datetime.timezone.utc):
        raise HTTPException(status_code=401, detail="This viewer session has expired.")
    return payload


def require_delta_auth(authorization: str = Header(None), x_viewer_device: str | None = Header(None)):
    payload = _decode_bearer(authorization)
    if is_viewer(payload):
        return _require_viewer_device(payload, x_viewer_device)
    return payload


def is_viewer(payload: dict | None) -> bool:
    return bool(payload and (payload.get("role") == "viewer" or payload.get("login_method") == "viewer_invite"))
