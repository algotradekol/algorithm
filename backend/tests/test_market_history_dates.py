"""Regression tests for weekend-safe frontend history requests."""

import datetime
import unittest
from unittest.mock import patch

from app import fyers_client


class _FakeFyers:
    def __init__(self):
        self.payload = None

    def history(self, payload):
        self.payload = payload
        return {"s": "ok", "candles": []}


class MarketHistoryDateTests(unittest.TestCase):
    def test_history_range_ends_on_last_weekday(self):
        fake = _FakeFyers()
        with patch.object(fyers_client, "get_fyers_model", return_value=fake):
            fyers_client.get_price_history("NSE:360ONE-EQ", resolution="15", days=30)

        expected_end = datetime.date.today()
        while expected_end.weekday() >= 5:
            expected_end -= datetime.timedelta(days=1)
        self.assertEqual(fake.payload["range_to"], expected_end.isoformat())
        self.assertLess(datetime.date.fromisoformat(fake.payload["range_to"]).weekday(), 5)


if __name__ == "__main__":
    unittest.main()
