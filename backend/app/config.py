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

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")  # backend only, never expose to frontend
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")  # for verifying frontend auth tokens

# App
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")  # set to your Vercel URL in production
SQUARE_OFF_TIME = "15:15"  # 3:15 PM, both algos exit everything by this time
MARKET_OPEN_TIME = "09:15"
ENTRY_CHECK_TIME = "09:16"  # algo 1 fires its entries at this time
