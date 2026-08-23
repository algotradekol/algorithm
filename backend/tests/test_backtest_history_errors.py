"""Regression tests for Silver backtest history error reporting."""

import datetime
import unittest
from unittest.mock import patch

from app import fyers_client


class _FakeFyers:
    def __init__(self, response):
        self.response = response

    def history(self, payload):
        return self.response


class BacktestHistoryErrorTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime.date(2026, 8, 21)
        self.symbol = "MCX:SILVERMIC26AUGFUT"

    def test_legacy_call_keeps_empty_history_behavior(self):
        response = {"s": "error", "code": -99, "message": "Token is expired"}
        with patch.object(fyers_client, "get_fyers_model", return_value=_FakeFyers(response)):
            self.assertEqual(
                fyers_client.get_intraday_candles_for_range(self.symbol, self.day, self.day),
                [],
            )

    def test_backtest_call_exposes_expired_token(self):
        response = {"s": "error", "code": -99, "message": "Token is expired"}
        with patch.object(fyers_client, "get_fyers_model", return_value=_FakeFyers(response)):
            with self.assertRaisesRegex(RuntimeError, "Token is expired") as raised:
                fyers_client.get_intraday_candles_for_range(
                    self.symbol,
                    self.day,
                    self.day,
                    resolution="1",
                    raise_on_error=True,
                )
        self.assertIn(self.symbol, str(raised.exception))
        self.assertIn("1m", str(raised.exception))

    def test_valid_history_still_normalizes(self):
        response = {"s": "ok", "candles": [[1787317800, 100, 105, 99, 103, 12]]}
        with patch.object(fyers_client, "get_fyers_model", return_value=_FakeFyers(response)):
            rows = fyers_client.get_intraday_candles_for_range(
                self.symbol,
                self.day,
                self.day,
                raise_on_error=True,
            )
        self.assertEqual(rows[0]["close"], 103.0)
        self.assertEqual(rows[0]["volume"], 12.0)


if __name__ == "__main__":
    unittest.main()
