"""Viewer JWT role separation regression tests."""

import time
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth
from app.viewer_invites import device_hash


SECRET = "viewer-test-secret"


def token(payload):
    now = int(time.time())
    return jwt.encode(
        {
            "aud": "authenticated",
            "iat": now,
            "exp": now + 3600,
            **payload,
        },
        SECRET,
        algorithm="HS256",
    )


class ViewerAuthTests(unittest.TestCase):
    def test_viewer_can_read_delta_but_not_admin_routes(self):
        device_key = "a" * 32
        viewer = token({
            "sub": "viewer:session-abc",
            "role": "viewer",
            "login_method": "viewer_invite",
            "session_id": "session-abc",
        })
        header = f"Bearer {viewer}"
        session_row = {
            "id": "session-abc",
            "device_hash": device_hash(device_key),
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600)),
            "revoked_at": None,
        }

        with patch.object(auth, "SUPABASE_JWT_SECRET", SECRET), patch.object(auth, "run_with_supabase") as run_db:
            run_db.side_effect = [SimpleNamespace(data=[session_row]), SimpleNamespace(data=[])]
            payload = auth.require_delta_auth(header, x_viewer_device=device_key)
            self.assertEqual(payload["role"], "viewer")

            with self.assertRaises(HTTPException) as raised:
                auth.require_auth(header)
            self.assertEqual(raised.exception.status_code, 403)

            with self.assertRaises(HTTPException) as raised:
                auth.require_delta_auth(header, x_viewer_device="b" * 32)
            self.assertEqual(raised.exception.status_code, 401)

    def test_admin_token_keeps_admin_access(self):
        admin = token({"sub": "admin-user", "role": "authenticated"})

        with patch.object(auth, "SUPABASE_JWT_SECRET", SECRET):
            payload = auth.require_auth(f"Bearer {admin}")
            self.assertEqual(payload["sub"], "admin-user")


if __name__ == "__main__":
    unittest.main()
