from __future__ import annotations

import datetime
import hashlib
import secrets

import jwt

from .config import SUPABASE_JWT_SECRET
from .supabase_client import run_with_supabase

VIEWER_TTL_HOURS = 24
INVITE_CODE_DIGITS = 6
DEVICE_KEY_MIN_LENGTH = 24


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _code_hash(code: str) -> str:
    normalized = "".join(ch for ch in str(code or "") if ch.isdigit())
    pepper = SUPABASE_JWT_SECRET or "viewer-invite"
    return hashlib.sha256(f"{pepper}:{normalized}".encode("utf-8")).hexdigest()


def device_hash(device_key: str) -> str:
    value = str(device_key or "").strip()
    pepper = SUPABASE_JWT_SECRET or "viewer-device"
    return hashlib.sha256(f"{pepper}:device:{value}".encode("utf-8")).hexdigest()


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


def redeem_invite(code: str, device_key: str) -> dict:
    normalized = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(normalized) != INVITE_CODE_DIGITS:
        raise ValueError("Enter the 6-digit viewer code")
    if len(str(device_key or "").strip()) < DEVICE_KEY_MIN_LENGTH:
        raise ValueError("Viewer device could not be verified. Refresh and try again.")
    expires_at = _now() + datetime.timedelta(hours=VIEWER_TTL_HOURS)
    code_hash = _code_hash(normalized)
    browser_hash = device_hash(device_key)
    try:
        result = run_with_supabase(lambda db: db.rpc("redeem_viewer_invite_session", {
            "p_code_hash": code_hash,
            "p_device_hash": browser_hash,
            "p_expires_at": expires_at.isoformat(),
        }).execute())
    except Exception:
        result = run_with_supabase(lambda db: _redeem_invite_with_tables(db, code_hash, browser_hash, expires_at))
    row = result.data
    if isinstance(row, list):
        row = row[0] if row else None
    if not row:
        raise ValueError("This viewer code is invalid, expired, or already used. Please ask for a new code.")
    return issue_viewer_token(row)


def _redeem_invite_with_tables(db, code_hash: str, browser_hash: str, expires_at: datetime.datetime):
    # If the migration was not applied yet, fail here before consuming a
    # one-use code by setting viewer_invites.redeemed_at.
    db.table("viewer_sessions").select("id").limit(1).execute()
    redeemed_at = _now()
    invite_result = (
        db.table("viewer_invites")
        .update({"redeemed_at": redeemed_at.isoformat()})
        .eq("code_hash", code_hash)
        .is_("redeemed_at", "null")
        .is_("revoked_at", "null")
        .gt("expires_at", redeemed_at.isoformat())
        .execute()
    )
    invite = (invite_result.data or [None])[0]
    if not invite:
        return type("RedeemResult", (), {"data": []})()
    session_result = (
        db.table("viewer_sessions")
        .insert({
            "invite_id": invite.get("id"),
            "device_hash": browser_hash,
            "expires_at": expires_at.isoformat(),
        })
        .execute()
    )
    session = (session_result.data or [None])[0]
    if not session:
        raise RuntimeError("Viewer session could not be created")
    return type("RedeemResult", (), {"data": [{
        "invite_id": invite.get("id"),
        "label": invite.get("label"),
        "invite_created_at": invite.get("created_at"),
        "invite_expires_at": invite.get("expires_at"),
        "redeemed_at": invite.get("redeemed_at"),
        "session_id": session.get("id"),
        "session_expires_at": session.get("expires_at"),
    }]})()


def issue_viewer_token(invite: dict) -> dict:
    if not SUPABASE_JWT_SECRET:
        raise RuntimeError("SUPABASE_JWT_SECRET is required for viewer invites")
    now = _now()
    session_id = invite.get("session_id")
    if not session_id:
        raise RuntimeError("Viewer session could not be created")
    expires_raw = invite.get("session_expires_at")
    try:
        expires_at = datetime.datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        expires_at = now + datetime.timedelta(hours=VIEWER_TTL_HOURS)
    token = jwt.encode(
        {
            "sub": f"viewer:{session_id}",
            "role": "viewer",
            "aud": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "login_method": "viewer_invite",
            "invite_id": invite.get("invite_id") or invite.get("id"),
            "session_id": session_id,
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
