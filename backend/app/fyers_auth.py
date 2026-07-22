"""
fyers_auth.py — automated daily login. Same approach as before (drives
Fyers' actual login endpoints with username/PIN/TOTP), but now stores
the resulting access token in Supabase instead of a local file, so it
survives Railway deploys/restarts and both the API process and the
background engine can read the same token.

CAVEAT (same as before, still true): this uses Fyers' internal login
endpoints, not an officially documented headless-login API. It's a
widely used community pattern, but Fyers could change these without
notice -- if the scheduled refresh starts failing, check this file
first.
"""
import sys
import base64
import datetime
import hashlib
import pyotp
import requests
from fyers_apiv3 import fyersModel

from .config import FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI, FYERS_FY_ID, FYERS_PIN, FYERS_TOTP_KEY, FYERS_PROXY_URL
from .supabase_client import run_with_supabase

BASE = "https://api-t2.fyers.in/vagator/v2"
TOKEN_URL = "https://api.fyers.in/api/v2/token"
REFRESH_TOKEN_URL = "https://api-t1.fyers.in/api/v3/validate-refresh-token"
AUTH_CODE_EXCHANGE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
FYERS_PROXIES = {"http": FYERS_PROXY_URL, "https": FYERS_PROXY_URL} if FYERS_PROXY_URL else None


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def _raise_for_fyers_step(response: requests.Response, step: str):
    if response.ok:
        return

    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}

    message = payload.get("message", response.text)
    if step == "send_login_otp_v2" and "User does not exist" in message:
        raise RuntimeError(
            "Fyers login failed: FYERS_FY_ID does not match a real Fyers user. "
            "Use your Fyers login id here, not the app client id."
        )
    raise RuntimeError(f"Fyers login failed at {step}: {message}")


def exchange_auth_code(auth_code: str) -> dict:
    """Exchange an OAuth callback code without bypassing FYERS_PROXY_URL.

    The SDK's SessionModel.generate_token() always performs a direct request.
    On Railway that can return an HTML gateway/proxy response, which the SDK
    then crashes while parsing as JSON. Keeping this request here makes OAuth,
    refresh-token validation, and legacy auth use the same outbound network
    configuration and gives the caller a diagnosable error.
    """
    app_id_hash = hashlib.sha256(f"{FYERS_CLIENT_ID}:{FYERS_SECRET_KEY}".encode()).hexdigest()
    response = requests.post(
        AUTH_CODE_EXCHANGE_URL,
        headers={"Content-Type": "application/json; charset=utf-8"},
        json={
            "grant_type": "authorization_code",
            "appIdHash": app_id_hash,
            "code": auth_code,
        },
        proxies=FYERS_PROXIES,
        timeout=30,
    )
    try:
        data = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown")
        raise RuntimeError(
            "Fyers auth-code exchange returned a non-JSON response "
            f"(HTTP {response.status_code}, content-type {content_type}). "
            "Check FYERS_PROXY_URL or the Railway outbound connection."
        ) from exc
    if not response.ok or not data.get("access_token"):
        raise RuntimeError(
            "Fyers auth-code exchange failed: "
            f"{data.get('message') or data.get('error') or data}"
        )
    return data


def refresh_access_token() -> str:
    session = requests.Session()
    if FYERS_PROXIES:
        session.proxies.update(FYERS_PROXIES)

    r1 = session.post(f"{BASE}/send_login_otp_v2", json={"fy_id": _b64(FYERS_FY_ID), "app_id": "2"})
    _raise_for_fyers_step(r1, "send_login_otp_v2")
    request_key = r1.json()["request_key"]

    totp_code = pyotp.TOTP(FYERS_TOTP_KEY).now()
    r2 = session.post(f"{BASE}/verify_otp", json={"request_key": request_key, "otp": totp_code})
    _raise_for_fyers_step(r2, "verify_otp")
    request_key = r2.json()["request_key"]

    r3 = session.post(f"{BASE}/verify_pin_v2", json={
        "request_key": request_key, "identity_type": "pin", "identifier": _b64(FYERS_PIN)
    })
    _raise_for_fyers_step(r3, "verify_pin_v2")
    access_token_temp = r3.json()["data"]["access_token"]

    headers = {"authorization": f"Bearer {access_token_temp}"}
    payload = {
        "fyers_id": FYERS_FY_ID,
        "app_id": FYERS_CLIENT_ID.split("-")[0],
        "redirect_uri": FYERS_REDIRECT_URI,
        "appType": "100",
        "code_challenge": "",
        "state": "sample",
        "scope": "",
        "nonce": "",
        "response_type": "code",
        "create_cookie": True,
    }
    r4 = session.post(TOKEN_URL, headers=headers, json=payload, allow_redirects=False)
    _raise_for_fyers_step(r4, "token")
    redirect_location = r4.headers.get("Location", "")
    if "auth_code=" not in redirect_location:
        raise RuntimeError(f"Auth code redirect missing: status={r4.status_code}, body={r4.text}")
    auth_code = redirect_location.split("auth_code=")[1].split("&")[0]

    response = exchange_auth_code(auth_code)

    token = response["access_token"]
    store_broker_tokens(response)
    return token


def store_broker_tokens(response: dict) -> None:
    now = _now()
    payload = {
        "broker": "fyers",
        "access_token": response["access_token"],
        "access_token_updated_at": now,
        "last_refresh_attempt_at": now,
        "last_refresh_error": None,
        "updated_at": now,
    }
    if response.get("refresh_token"):
        payload["refresh_token"] = response["refresh_token"]
        payload["refresh_token_updated_at"] = now
    run_with_supabase(lambda supabase: supabase.table("broker_tokens").upsert(payload).execute())
    _record_refresh_log("success", None)


def refresh_access_token_from_refresh_token() -> str:
    stored = get_stored_token_row()
    refresh_token = stored.get("refresh_token") if stored else None
    if not refresh_token:
        raise RuntimeError("No Fyers refresh token in Supabase. Complete manual Fyers login first.")
    if not FYERS_PIN:
        raise RuntimeError("FYERS_PIN is not configured. It is required for Fyers refresh-token validation.")

    last_error = None
    for app_id_hash in _candidate_app_id_hashes():
        response = requests.post(
            REFRESH_TOKEN_URL,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "grant_type": "refresh_token",
                "appIdHash": app_id_hash,
                "refresh_token": refresh_token,
                "pin": FYERS_PIN,
            },
            proxies=FYERS_PROXIES,
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"s": "error", "message": response.text}
        if response.ok and data.get("access_token"):
            store_broker_tokens(data)
            return data["access_token"]
        last_error = data
        if data.get("code") != -371:
            break
    message = f"Fyers refresh-token validation failed: {last_error}"
    _record_refresh_error(message)
    _record_refresh_log("failed", message)
    raise RuntimeError(message)


def get_stored_access_token() -> str | None:
    row = get_stored_token_row()
    return row.get("access_token") if row else None


def get_stored_token_row() -> dict | None:
    result = run_with_supabase(
        lambda supabase: supabase.table("broker_tokens").select("*").eq("broker", "fyers").execute()
    )
    if result.data:
        return result.data[0]
    return None


def get_token_status() -> dict:
    row = get_stored_token_row() or {}
    refresh_updated_at = row.get("refresh_token_updated_at")
    refresh_expires_at = _add_days(refresh_updated_at, 15) if refresh_updated_at else None
    days_left = _days_until(refresh_expires_at) if refresh_expires_at else None
    logs = run_with_supabase(
        lambda supabase: supabase.table("fyers_token_refresh_logs")
        .select("*")
        .order("attempted_at", desc=True)
        .limit(20)
        .execute()
    )
    return {
        "refresh_token_present": bool(row.get("refresh_token")),
        "access_token_updated_at": row.get("access_token_updated_at") or row.get("updated_at"),
        "refresh_token_updated_at": refresh_updated_at,
        "refresh_token_estimated_expires_at": refresh_expires_at,
        "refresh_token_days_left": days_left,
        "last_refresh_attempt_at": row.get("last_refresh_attempt_at"),
        "last_refresh_error": row.get("last_refresh_error"),
        "logs": logs.data or [],
    }


def _candidate_app_id_hashes() -> list[str]:
    values = [
        f"{FYERS_CLIENT_ID}:{FYERS_SECRET_KEY}",
        f"{FYERS_CLIENT_ID}{FYERS_SECRET_KEY}",
    ]
    app_id_without_type = FYERS_CLIENT_ID.split("-")[0]
    if app_id_without_type and app_id_without_type != FYERS_CLIENT_ID:
        values.extend([
            f"{app_id_without_type}:{FYERS_SECRET_KEY}",
            f"{app_id_without_type}{FYERS_SECRET_KEY}",
        ])
    seen = set()
    hashes = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            hashes.append(hashlib.sha256(value.encode()).hexdigest())
    return hashes


def _record_refresh_error(message: str) -> None:
    run_with_supabase(lambda supabase: supabase.table("broker_tokens").update({
        "last_refresh_attempt_at": _now(),
        "last_refresh_error": message,
        "updated_at": _now(),
    }).eq("broker", "fyers").execute())


def _record_refresh_log(status: str, error: str | None) -> None:
    try:
        run_with_supabase(lambda supabase: supabase.table("fyers_token_refresh_logs").insert({
            "status": status,
            "error": error,
            "attempted_at": _now(),
        }).execute())
    except Exception as exc:
        print(f"[fyers_auth] refresh log insert skipped: {exc}")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _add_days(value: str | None, days: int) -> str | None:
    if not value:
        return None
    return (datetime.datetime.fromisoformat(value.replace("Z", "+00:00")) + datetime.timedelta(days=days)).isoformat()


def _days_until(value: str | None) -> float | None:
    if not value:
        return None
    target = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    remaining = target - datetime.datetime.now(datetime.timezone.utc)
    return max(0, round(remaining.total_seconds() / 86400, 1))


if __name__ == "__main__":
    try:
        refresh_access_token()
        print("Fyers access token refreshed and stored in Supabase.")
    except Exception as e:
        print(f"Token refresh failed: {e}", file=sys.stderr)
        sys.exit(1)
