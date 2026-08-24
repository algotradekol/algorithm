"""Regression tests for Supabase transport lifecycle and retry behavior."""

import unittest
from unittest.mock import patch

import httpx

from app import supabase_client


class _FakeSession:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


class _FakePostgrest:
    def __init__(self):
        self.session = _FakeSession()


class _FakeClient:
    def __init__(self):
        self.postgrest = _FakePostgrest()


class SupabaseResilienceTests(unittest.TestCase):
    def test_successful_operation_closes_transport(self):
        client = _FakeClient()
        with patch.object(supabase_client, "get_supabase", return_value=client):
            result = supabase_client.run_with_supabase(lambda _: {"ok": True})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.postgrest.session.close_count, 1)

    def test_transient_operation_retries_and_closes_each_attempt(self):
        clients = [_FakeClient(), _FakeClient()]
        calls = {"count": 0}

        def operation(_client):
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpx.ReadError("temporary transport failure")
            return "recovered"

        with patch.object(supabase_client, "get_supabase", side_effect=clients):
            result = supabase_client.run_with_supabase(operation, delay_seconds=0)

        self.assertEqual(result, "recovered")
        self.assertEqual(calls["count"], 2)
        self.assertEqual([c.postgrest.session.close_count for c in clients], [1, 1])


if __name__ == "__main__":
    unittest.main()
