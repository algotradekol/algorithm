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

# Fyers
FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "")
FYERS_SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "")
FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "https://www.google.com")
FYERS_FY_ID = os.environ.get("FYERS_FY_ID", "")
FYERS_PIN = os.environ.get("FYERS_PIN", "")
FYERS_TOTP_KEY = os.environ.get("FYERS_TOTP_KEY", "")
FYERS_PROXY_URL = os.environ.get("FYERS_PROXY_URL", "")

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
