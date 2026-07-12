"""
supabase_client.py — one Supabase client, using the service role key
(full access, backend-only — never send this key to the frontend).
"""
from supabase import create_client, Client
from app.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
