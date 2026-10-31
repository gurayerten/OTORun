import time
import unittest
from unittest.mock import patch

from crypto_fear_greed import fmt_fear_greed, run_fear_greed_check


class _FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class CryptoFearGreedTests(unittest.TestCase):
    def setUp(self):
        now = int(time.time())
        self.rows = [
            {"value": str(value), "value_classification": label, "timestamp": str(now - day * 86400)}
            for day, value, label in [
                (0, 24, "Extreme Fear"),
                (1, 33, "Fear"),
                (2, 41, "Fear"),
                (3, 45, "Fear"),
                (4, 52, "Neutral"),
                (5, 57, "Greed"),
                (6, 61, "Greed"),
                (7, 68, "Greed"),
            ]
        ]
        self.response = _FakeResponse({"data": self.rows, "metadata": {"error": None}})

    def test_first_report_includes_daily_weekly_context_and_attribution(self):
        with patch("crypto_fear_greed.requests.get", return_value=self.response), \
             patch("crypto_fear_greed._save_fng_state") as save, \
             patch("crypto_fear_greed.SETTINGS.CRYPTO_FNG_MAX_AGE_HOURS", 48), \
             patch("crypto_fear_greed.SETTINGS.CRYPTO_FNG_ALERT_DELTA", 8):
            report = run_fear_greed_check({})

        self.assertEqual(report["value"], 24)
        self.assertEqual(report["day_delta"], -9)
        self.assertEqual(report["week_delta"], -44)
        self.assertTrue(report["notable_change"])
        save.assert_called_once()
        message = fmt_fear_greed(report)
        self.assertIn("Alternative.me", message)
        self.assertIn("24/100", message)

    def test_same_daily_snapshot_does_not_create_another_report(self):
        current = {"value": 24, "classification": "Extreme Fear", "timestamp": int(self.rows[0]["timestamp"])}
        state = {**current, "updatedAt": current["timestamp"]}
        with patch("crypto_fear_greed.requests.get", return_value=self.response), \
             patch("crypto_fear_greed._save_fng_state") as save:
            report = run_fear_greed_check(state)

        self.assertIsNone(report)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
