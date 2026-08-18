"""
config.py — all environment-driven settings in one place.
On Railway, set these as environment variables in the service settings
(never commit real values to git).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH)

TRADING_MODE = os.environ.get("TRADING_MODE", "paper").strip().lower()
IS_LIVE_TRADING = TRADING_MODE == "live"


def _mode_value(paper_value: str, live_value: str, env_name: str) -> str:
    value = live_value if IS_LIVE_TRADING else paper_value
    if IS_LIVE_TRADING and not value:
        raise RuntimeError(f"{env_name} is required when TRADING_MODE=live")
    return value


# Fyers
# Paper trading uses the non-trading FYERS app credentials.
PAPER_FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "")
PAPER_FYERS_SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")
PAPER_FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "https://www.google.com")
PAPER_FYERS_FY_ID = os.environ.get("FYERS_FY_ID", "")
PAPER_FYERS_PIN = os.environ.get("FYERS_PIN", "")
PAPER_FYERS_TOTP_KEY = os.environ.get("FYERS_TOTP_KEY", "")
PAPER_FYERS_PROXY_URL = os.environ.get("PAPER_FYERS_PROXY_URL", "")

# Live trading can use the real trading FYERS app credentials.
# If you leave fy_id / pin / totp / redirect empty here, the backend can
# fall back to the paper values for those shared personal-login fields.
LIVE_FYERS_CLIENT_ID = os.environ.get("LIVE_FYERS_CLIENT_ID", "")
LIVE_FYERS_SECRET_KEY = os.environ.get("LIVE_FYERS_SECRET_KEY", "")
LIVE_FYERS_REDIRECT_URI = os.environ.get("LIVE_FYERS_REDIRECT_URI", PAPER_FYERS_REDIRECT_URI)
LIVE_FYERS_FY_ID = os.environ.get("LIVE_FYERS_FY_ID", PAPER_FYERS_FY_ID)
LIVE_FYERS_PIN = os.environ.get("LIVE_FYERS_PIN", PAPER_FYERS_PIN)
LIVE_FYERS_TOTP_KEY = os.environ.get("LIVE_FYERS_TOTP_KEY", PAPER_FYERS_TOTP_KEY)
LIVE_FYERS_PROXY_URL = os.environ.get("LIVE_FYERS_PROXY_URL", os.environ.get("FYERS_PROXY_URL", ""))

# Backward-compatible aliases for older imports. These still point at the
# paper-trading app credentials; mode-aware code should use runtime_mode.py.
FYERS_CLIENT_ID = PAPER_FYERS_CLIENT_ID
FYERS_SECRET_KEY = PAPER_FYERS_SECRET_KEY
FYERS_REDIRECT_URI = PAPER_FYERS_REDIRECT_URI
FYERS_FY_ID = PAPER_FYERS_FY_ID
FYERS_PIN = PAPER_FYERS_PIN
FYERS_TOTP_KEY = PAPER_FYERS_TOTP_KEY
FYERS_PROXY_URL = os.environ.get("FYERS_PROXY_URL", "")
ACTIVE_BROKER_KEY = "fyers_live" if IS_LIVE_TRADING else "fyers"

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")  # backend only, never expose to frontend
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")  # for verifying frontend auth tokens

# App
_configured_origins = [origin.strip().rstrip("/") for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if origin.strip()]
# Keep both production hostnames working when one redirects to the other. Railway
# still receives the browser's original Origin header before that redirect occurs.
_first_party_origins = {"https://kolkatalgo.in", "https://www.kolkatalgo.in"}
ALLOWED_ORIGINS = ["*"] if "*" in _configured_origins else sorted(set(_configured_origins) | _first_party_origins)
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://your-app.vercel.app")
APP_PIN = os.environ.get("APP_PIN", "1402")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
SQUARE_OFF_TIME = "15:15"  # 3:15 PM, both algos exit everything by this time
MARKET_OPEN_TIME = "09:15"
ENTRY_CHECK_TIME = "09:16"  # algo 1 fires its entries at this time

# Deployment-level tab/strategy hide-list. Same convention as
# frontend's NEXT_PUBLIC_HIDDEN_TABS (comma-separated tab keys). A hidden
# strategy tab is not just invisible in the UI — the backend also refuses
# to load its strategy, so the client-side toggle can never accidentally
# start it. Missing/empty = load everything (safe default).
#
# Tab-key → strategy mapping enforced in engine.py: simple→algo1,
# filter→algo2, silver→algo3 (silver micro). Non-strategy tabs
# (backtest, compare, history, calendar, charges) are UI-only.
HIDDEN_TABS = {
    key.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    for key in os.environ.get("HIDDEN_TABS", "").split(",")
    if key.strip()
}

# Multi-tenant token isolation (2026-08-19). When two backends share the
# same Supabase (e.g. one runs Simple on the client's Fyers, another runs
# Silver on the developer's Fyers), the broker_tokens table would collide
# on a single "fyers_live" key. Set BROKER_KEY_SUFFIX per deployment
# (e.g. "client", "dev") so each backend stores tokens under its own key
# (e.g. "fyers_live__client"). Empty = no suffix, matches historical
# single-backend behavior.
BROKER_KEY_SUFFIX = os.environ.get("BROKER_KEY_SUFFIX", "").strip().lower().replace(" ", "").replace("-", "_")
