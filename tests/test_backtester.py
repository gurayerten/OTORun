import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

import backtester
from risk import TradePlan


def _position(direction=1):
    entry = 100.0
    return {
        "plan": TradePlan(direction, entry, 99.0, 101.0, 102.0, 103.0),
        "tp1": False,
        "tp2": False,
        "tp3": False,
        "be": False,
        "trailing_active": False,
        "extreme_price": entry,
        "opened_i": 0,
        "risk": 1.0,
    }


class BacktesterPositionTests(unittest.TestCase):
    def test_metrics_apply_configured_risk_and_compound_balance(self):
        trades = [
            {"entry": 100.0, "risk_distance": 1.0, "direction": 1, "net_pnl_pct": 3.0,
             "r_multiple": 3.0, "win": True, "outcome": "WIN", "reason": "TP3"},
            {"entry": 100.0, "risk_distance": 1.0, "direction": -1, "net_pnl_pct": -1.0,
             "r_multiple": -1.0, "win": False, "outcome": "LOSS", "reason": "SL"},
        ]
        with patch.object(backtester.SETTINGS, "BACKTEST_INITIAL_BALANCE", 1000.0), \
             patch.object(backtester.SETTINGS, "BACKTEST_RISK_PER_TRADE_PCT", 1.0):
            metrics = backtester._metrics(trades)

        self.assertEqual(trades[0]["account_return_pct"], 3.0)
        self.assertEqual(metrics["final_balance"], 1019.7)
        self.assertEqual(metrics["net_pnl_pct"], 1.97)

    def test_stop_is_processed_without_a_later_signal(self):
        setup = SimpleNamespace(
            bar_index=0, direction=1, anchor_extreme=98.0, market_regime="TREND"
        )
        frame = pd.DataFrame({
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 100.5, 100.5],
            "low": [99.5, 98.5, 98.0],
            "close": [100.0, 99.0, 98.5],
        })
        atr_values = SimpleNamespace(values=np.array([1.0, 1.0, 1.0]))
        plan = TradePlan(1, 100.0, 99.0, 101.0, 102.0, 103.0)
        with patch.object(backtester, "SweepEngine") as engine_cls, \
             patch.object(backtester, "atr", return_value=atr_values), \
             patch.object(backtester, "build_trade_plan", return_value=plan), \
             patch.object(backtester.SETTINGS, "SLIPPAGE", 0.0), \
             patch.object(backtester.SETTINGS, "USE_TRAILING_STOP", False):
            engine_cls.return_value.iter_signals.return_value = iter([setup])
            trades, _ = backtester.run_backtest(frame, "TESTUSDT")

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "SL")
        self.assertEqual(trades[0]["exit"], 99.0)

    def test_tp1_moves_stop_to_configured_break_even_level(self):
        position = _position()
        with patch.object(backtester.SETTINGS, "USE_BREAK_EVEN", True), \
             patch.object(backtester.SETTINGS, "BE_LEVEL_R", 0.5), \
             patch.object(backtester.SETTINGS, "USE_TRAILING_STOP", False):
            remaining = backtester._check_bar(position, 101.2, 99.2, 100.8, 1, [], "TESTUSDT")

        self.assertIs(remaining, position)
        self.assertTrue(position["tp1"])
        self.assertTrue(position["be"])
        self.assertEqual(position["plan"].sl, 100.5)

    def test_trailing_stop_closes_on_a_later_bar(self):
        position = _position()
        trades = []
        with patch.object(backtester.SETTINGS, "USE_BREAK_EVEN", False), \
             patch.object(backtester.SETTINGS, "USE_TRAILING_STOP", True), \
             patch.object(backtester.SETTINGS, "TRAILING_STOP_ACTIVATION_R", 1.0), \
             patch.object(backtester.SETTINGS, "TRAILING_STOP_DISTANCE_R", 0.5):
            remaining = backtester._check_bar(position, 102.0, 100.5, 101.5, 1, trades, "TESTUSDT")
            self.assertIs(remaining, position)
            self.assertEqual(position["plan"].sl, 101.5)
            remaining = backtester._check_bar(position, 101.7, 101.4, 101.5, 2, trades, "TESTUSDT")

        self.assertIsNone(remaining)
        self.assertEqual(trades[0]["reason"], "SL")
        self.assertEqual(trades[0]["exit"], 101.5)


if __name__ == "__main__":
    unittest.main()
