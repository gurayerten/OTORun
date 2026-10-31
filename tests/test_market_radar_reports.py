import unittest
import time
from unittest.mock import patch

import exchange
from exchange_risk_watch import _build_diff
from market_sentiment_radar import _build_report as build_sentiment_report
from oi_market_breadth import _build_report as build_oi_report


class OIBreadthReportTests(unittest.TestCase):
    def test_symbol_changes_and_price_quadrants_use_symbol_state(self):
        state = {"symbols": {
            "AAAUSDT": {"oi": 100.0, "price": 10.0},
            "BBBUSDT": {"oi": 100.0, "price": 10.0},
        }, "last_report": {"rising_pct": 50.0}}
        payload = [
            {"symbol": "AAAUSDT", "oi": 105.0, "price": 11.0},
            {"symbol": "BBBUSDT", "oi": 95.0, "price": 11.0},
        ]
        with patch("oi_market_breadth.SETTINGS.OI_BREADTH_NOISE_PCT", 0.03):
            report = build_oi_report(payload, state, 2.0)

        self.assertEqual(report["rising"], 1)
        self.assertEqual(report["falling"], 1)
        self.assertEqual(report["quadrants"]["long_opening"], 1)
        self.assertEqual(report["quadrants"]["short_closing"], 1)


class SentimentReportTests(unittest.TestCase):
    def test_populations_remain_separate_and_divergence_is_symbol_level(self):
        payload = [{"symbol": "AAAUSDT", "metrics": {
            "global_account": 2.0,
            "top_account": 1.1,
            "top_position": 0.8,
            "taker": 1.3,
        }}]
        report = build_sentiment_report(payload, {"symbols": {}, "last_averages": {}}, 0.05)

        self.assertEqual(report["averages"]["global_account"], 2.0)
        self.assertEqual(report["averages"]["top_account"], 1.1)
        self.assertEqual(report["averages"]["top_position"], 0.8)
        self.assertEqual(report["averages"]["taker"], 1.3)
        self.assertEqual(report["divergences"][0]["symbol"], "AAAUSDT")
        self.assertNotIn("sentiment_score", report)


class ExchangeWrapperTests(unittest.TestCase):
    def test_ratio_wrapper_requests_only_latest_observation(self):
        with patch("exchange._get", return_value=[]) as getter:
            exchange.get_top_long_short_position_ratio("BTCUSDT", "15m")

        getter.assert_called_once_with(
            "/futures/data/topLongShortPositionRatio",
            {"symbol": "BTCUSDT", "period": "15m", "limit": 1},
        )

    def test_market_cache_copies_values_and_does_not_fallback_after_expiry_error(self):
        with exchange._MARKET_CACHE_LOCK:
            exchange._MARKET_CACHE.clear()
        source = [{"symbol": "BTCUSDT", "quoteVolume": "10"}]
        with patch("exchange.SETTINGS.MARKET_DATA_CACHE_TTL_SECONDS", 30.0), \
             patch("exchange._get", return_value=source) as getter:
            first = exchange.get_24hr_tickers()
            first[0]["quoteVolume"] = "corrupted by caller"
            second = exchange.get_24hr_tickers()
        getter.assert_called_once()
        self.assertEqual(second[0]["quoteVolume"], "10")

        with exchange._MARKET_CACHE_LOCK:
            exchange._MARKET_CACHE["futures:/fapi/v1/ticker/24hr"] = (time.monotonic() - 1, source)
        with patch("exchange.SETTINGS.MARKET_DATA_CACHE_TTL_SECONDS", 30.0), \
             patch("exchange._get", side_effect=RuntimeError("API offline")):
            with self.assertRaisesRegex(RuntimeError, "API offline"):
                exchange.get_24hr_tickers()
        with exchange._MARKET_CACHE_LOCK:
            exchange._MARKET_CACHE.clear()


class ExchangeRiskDiffTests(unittest.TestCase):
    def test_status_transition_and_new_symbol_are_detected(self):
        old = {"symbols": {"AAAUSDT": {
            "status": "TRADING", "contractType": "PERPETUAL",
            "maxLeverage": None, "maintMarginRatio": None,
        }}}
        new = {"symbols": {
            "AAAUSDT": {"status": "SETTLING", "contractType": "PERPETUAL",
                         "maxLeverage": None, "maintMarginRatio": None},
            "BBBUSDT": {"status": "TRADING", "contractType": "PERPETUAL",
                          "onboardDate": 123, "maxLeverage": None, "maintMarginRatio": None},
        }}
        report = _build_diff(old, new)

        self.assertEqual(report["status"][0]["to"], "SETTLING")
        self.assertEqual(report["new"][0]["symbol"], "BBBUSDT")


if __name__ == "__main__":
    unittest.main()
