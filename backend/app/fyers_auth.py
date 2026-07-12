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
import pyotp
import requests
from fyers_apiv3 import fyersModel

from .config import FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI, FYERS_FY_ID, FYERS_PIN, FYERS_TOTP_KEY
from .supabase_client import run_with_supabase

BASE = "https://api-t2.fyers.in/vagator/v2"
TOKEN_URL = "https://api.fyers.in/api/v2/token"


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


def refresh_access_token() -> str:
    session = requests.Session()

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

    fyers_session = fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        secret_key=FYERS_SECRET_KEY,
        redirect_uri=FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    fyers_session.set_token(auth_code)
    response = fyers_session.generate_token()

    if "access_token" not in response:
        raise RuntimeError(f"Token generation failed: {response}")

    token = response["access_token"]
    run_with_supabase(
        lambda supabase: supabase.table("broker_tokens").upsert({
            "broker": "fyers", "access_token": token,
            "updated_at": "now()",
        }).execute()
    )
    return token


def get_stored_access_token() -> str | None:
    result = run_with_supabase(
        lambda supabase: supabase.table("broker_tokens").select("access_token").eq("broker", "fyers").execute()
    )
    if result.data:
        return result.data[0]["access_token"]
    return None


if __name__ == "__main__":
    try:
        refresh_access_token()
        print("Fyers access token refreshed and stored in Supabase.")
    except Exception as e:
        print(f"Token refresh failed: {e}", file=sys.stderr)
        sys.exit(1)
