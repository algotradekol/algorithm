"""Viewer JWT role separation regression tests."""

import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import jwt
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth


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
        viewer = token({"sub": "viewer:abc", "role": "viewer", "login_method": "viewer_invite"})
        header = f"Bearer {viewer}"

        with patch.object(auth, "SUPABASE_JWT_SECRET", SECRET):
            payload = auth.require_delta_auth(header)
            self.assertEqual(payload["role"], "viewer")

            with self.assertRaises(HTTPException) as raised:
                auth.require_auth(header)
            self.assertEqual(raised.exception.status_code, 403)

    def test_admin_token_keeps_admin_access(self):
        admin = token({"sub": "admin-user", "role": "authenticated"})

        with patch.object(auth, "SUPABASE_JWT_SECRET", SECRET):
            payload = auth.require_auth(f"Bearer {admin}")
            self.assertEqual(payload["sub"], "admin-user")


if __name__ == "__main__":
    unittest.main()
