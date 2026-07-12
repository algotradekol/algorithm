"""
auth.py — verifies the Supabase JWT the frontend sends with every
request (in the Authorization header). This is what makes the backend
password-gated the same way the frontend is: Supabase issues the
token at login, we just check it's valid and not expired.
"""
from functools import lru_cache

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

from app.config import SUPABASE_JWT_SECRET, SUPABASE_URL


@lru_cache(maxsize=1)
def get_jwks_client() -> PyJWKClient | None:
    if not SUPABASE_URL:
        return None
    return PyJWKClient(f"{SUPABASE_URL.rstrip('/')}/auth/v1/.well-known/jwks.json")


def require_auth(authorization: str = Header(None)):
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
