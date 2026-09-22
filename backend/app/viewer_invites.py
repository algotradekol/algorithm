from __future__ import annotations

import datetime
import hashlib
import secrets

import jwt

from .config import SUPABASE_JWT_SECRET
from .supabase_client import run_with_supabase

VIEWER_TTL_HOURS = 24
INVITE_CODE_DIGITS = 6


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _code_hash(code: str) -> str:
    normalized = "".join(ch for ch in str(code or "") if ch.isdigit())
    pepper = SUPABASE_JWT_SECRET or "viewer-invite"
    return hashlib.sha256(f"{pepper}:{normalized}".encode("utf-8")).hexdigest()


def _public_invite(row: dict) -> dict:
    redeemed_at = row.get("redeemed_at")
    revoked_at = row.get("revoked_at")
    expires_at = row.get("expires_at")
    status = "unused"
    try:
        expires = datetime.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        expires = None
    if revoked_at:
        status = "revoked"
    elif redeemed_at:
        status = "redeemed"
    elif expires and expires <= _now():
        status = "expired"
    return {
        "id": row.get("id"),
        "label": row.get("label"),
        "created_at": row.get("created_at"),
        "expires_at": expires_at,
        "redeemed_at": redeemed_at,
        "revoked_at": revoked_at,
        "status": status,
    }


def create_invite(label: str | None = None) -> dict:
    if not SUPABASE_JWT_SECRET:
        raise RuntimeError("SUPABASE_JWT_SECRET is required for viewer invites")
    expires_at = _now() + datetime.timedelta(hours=VIEWER_TTL_HOURS)
    last_conflict = None
    for _ in range(8):
        code = f"{secrets.randbelow(10 ** INVITE_CODE_DIGITS):0{INVITE_CODE_DIGITS}d}"
        payload = {
            "code_hash": _code_hash(code),
            "label": (label or "").strip()[:80] or None,
            "expires_at": expires_at.isoformat(),
        }
        try:
            result = run_with_supabase(lambda db, p=payload: db.table("viewer_invites").insert(p).execute())
            row = (result.data or [payload])[0]
            return {**_public_invite(row), "code": code, "ttl_hours": VIEWER_TTL_HOURS}
        except Exception as exc:
            last_conflict = exc
    raise RuntimeError(f"Could not generate a unique viewer code: {last_conflict}") from last_conflict


def list_invites(limit: int = 50) -> list[dict]:
    result = run_with_supabase(
        lambda db: db.table("viewer_invites")
        .select("id,label,created_at,expires_at,redeemed_at,revoked_at")
        .order("created_at", desc=True)
        .limit(max(1, min(int(limit), 100)))
        .execute()
    )
    return [_public_invite(row) for row in result.data or []]


def revoke_invite(invite_id: str) -> dict:
    result = run_with_supabase(
        lambda db: db.table("viewer_invites")
        .update({"revoked_at": _now().isoformat()})
        .eq("id", invite_id)
        .is_("redeemed_at", "null")
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise ValueError("Viewer invite is already used, revoked, or missing")
    return _public_invite(rows[0])


def redeem_invite(code: str) -> dict:
    normalized = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(normalized) != INVITE_CODE_DIGITS:
        raise ValueError("Enter the 6-digit viewer code")
    result = run_with_supabase(lambda db: db.rpc("redeem_viewer_invite", {"p_code_hash": _code_hash(normalized)}).execute())
    row = result.data
    if isinstance(row, list):
        row = row[0] if row else None
    if not row:
        raise ValueError("This viewer code is invalid, expired, or already used. Please ask for a new code.")
    return issue_viewer_token(row)


def issue_viewer_token(invite: dict) -> dict:
    if not SUPABASE_JWT_SECRET:
        raise RuntimeError("SUPABASE_JWT_SECRET is required for viewer invites")
    now = _now()
    expires_at = now + datetime.timedelta(hours=VIEWER_TTL_HOURS)
    token = jwt.encode(
        {
            "sub": f"viewer:{invite.get('id')}",
            "role": "viewer",
            "aud": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "login_method": "viewer_invite",
            "invite_id": invite.get("id"),
        },
        SUPABASE_JWT_SECRET,
        algorithm="HS256",
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": VIEWER_TTL_HOURS * 60 * 60,
        "expires_at": expires_at.isoformat(),
        "viewer": True,
    }
