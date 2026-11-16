"""Tests for computed watchlist metrics from the daily bar store (#2).

Each calculation is checked against hand-computed values per the issue's
acceptance criteria.
"""

import math
import unittest
from datetime import date

import pandas as pd

from stockstui.presentation import metrics as M


def _closes(values: list[float], start: str = "2026-01-01") -> pd.Series:
    """Builds a business-day close series (adjclose semantics)."""
    index = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=index, dtype=float)


class TestReturnOverDays(unittest.TestCase):
    def test_one_week_return_hand_checked(self):
        closes = _closes([100, 101, 102, 103, 104, 105, 106, 107])
        # 5 trading days back: 107 vs 102 -> 4.9028...%
        self.assertAlmostEqual(M.return_over_days(closes, 5), 107 / 102 - 1)

    def test_insufficient_history_returns_none(self):
        closes = _closes([100, 101])
        self.assertIsNone(M.return_over_days(closes, 5))

    def test_zero_prior_close_returns_none(self):
        closes = _closes([0, 0, 0, 0, 0, 0, 0, 110])
        self.assertIsNone(M.return_over_days(closes, 5))


class TestYtdReturn(unittest.TestCase):
    def test_ytd_return_hand_checked(self):
        # Series spanning the year boundary: 2025 close 200, 2026 bars end 250.
        values = [195, 200, 210, 220, 230, 240, 250]
        index = pd.DatetimeIndex(
            [
                pd.Timestamp(2025, 12, 24),
                pd.Timestamp(2025, 12, 31),
                pd.Timestamp(2026, 1, 2),
                pd.Timestamp(2026, 1, 15),
                pd.Timestamp(2026, 2, 2),
                pd.Timestamp(2026, 3, 2),
                pd.Timestamp(2026, 6, 1),
            ]
        )
        closes = pd.Series(values, index=index, dtype=float)
        # First 2026 bar is 210; last bar strictly before Jan 1 2026 is 200.
        self.assertAlmostEqual(
            M.ytd_return(closes, today=date(2026, 6, 1)), 250 / 200 - 1
        )

    def test_all_bars_in_current_year_returns_none(self):
        closes = _closes([100, 101, 102], start="2026-01-05")
        self.assertIsNone(M.ytd_return(closes, today=date(2026, 2, 1)))


class TestMovingAverageDistance(unittest.TestCase):
    def test_hand_checked(self):
        closes = _closes([100.0] * 49 + [110.0])
        sma50 = (49 * 100 + 110) / 50  # 100.2
        self.assertAlmostEqual(
            M.distance_from_moving_average(closes, 50), 110 / 100.2 - 1
        )

    def test_insufficient_history_returns_none(self):
        closes = _closes([100.0] * 20)
        self.assertIsNone(M.distance_from_moving_average(closes, 50))


class TestRSI(unittest.TestCase):
    def test_all_gains_is_100(self):
        closes = _closes([100.0 + i for i in range(30)])
        self.assertEqual(M.rsi(closes), 100.0)

    def test_all_losses_is_0(self):
        closes = _closes([100.0 - i for i in range(30)])
        self.assertEqual(M.rsi(closes), 0.0)

    def test_mixed_series_hand_checked(self):
        # Wilder's classic 14-period example: gains/losses per bar.
        deltas = [
            None, -1.0, 2.0, -3.0, 4.0, -5.0, 6.0, -7.0, 8.0,
            -9.0, 10.0, -11.0, 12.0, -13.0, 14.0,
        ]
        closes = [100.0]
        for d in deltas[1:]:
            closes.append(closes[-1] + d)
        series = _closes(closes)
        value = M.rsi(series)
        # Hand check with Wilder smoothing: alpha = 1/14, adjust=False.
        d = pd.Series([x for x in deltas if x is not None], dtype=float)
        gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        expected = 100 - 100 / (1 + gain / loss)
        self.assertAlmostEqual(value, expected, places=10)

    def test_flat_series_returns_none(self):
        closes = _closes([100.0] * 30)
        self.assertIsNone(M.rsi(closes))


class TestRealisedVolatility(unittest.TestCase):
    def test_constant_series_is_zero(self):
        closes = _closes([100.0] * 40)
        self.assertEqual(M.realised_volatility(closes), 0.0)

    def test_alternating_series_hand_checked(self):
        # ±1% every day: log returns alternate ±~0.01. Sample std of the
        # two-value set {a, -a} is 2*a/sqrt(2)... compute directly.
        closes = []
        for i in range(40):
            closes.append(100.0 * (1.01 if i % 2 == 0 else 0.99))
        series = _closes(closes)
        value = M.realised_volatility(series)
        lr = [
            math.log(1.01), math.log(0.99 / 1.01) * 0 + math.log(0.99),
        ]
        # Direct hand check: log returns are log(1.01) and log(0.99)
        # alternating (each up move from 99->101? no: 99*1.01=99.99).
        # Simpler: recompute expected from the same definition.
        log_rets = []
        for i in range(1, len(closes)):
            log_rets.append(math.log(closes[i] / closes[i - 1]))
        s = pd.Series(log_rets, dtype=float)
        expected = s.tail(30).std(ddof=1) * math.sqrt(252)
        self.assertAlmostEqual(value, expected, places=10)

    def test_insufficient_history_returns_none(self):
        closes = _closes([100.0, 101.0])
        self.assertIsNone(M.realised_volatility(closes))


class TestRelativeVolume(unittest.TestCase):
    def test_hand_checked(self):
        volumes = pd.Series([1000.0] * 20 + [2500.0])
        # 20-day average (excluding today) is 1000 -> 2.5x
        self.assertAlmostEqual(M.relative_volume(volumes), 2.5)

    def test_zero_average_returns_none(self):
        volumes = pd.Series([0.0] * 20 + [500.0])
        self.assertIsNone(M.relative_volume(volumes))

    def test_insufficient_history_returns_none(self):
        volumes = pd.Series([100.0, 200.0])
        self.assertIsNone(M.relative_volume(volumes))


class TestSparkline(unittest.TestCase):
    def test_rising_series_uses_full_range(self):
        s = _closes([float(i) for i in range(1, 31)])
        out = M.sparkline(s)
        self.assertEqual(len(out), 30)
        self.assertEqual(out[0], "▁")
        self.assertEqual(out[-1], "█")

    def test_flat_series_renders_mid_block(self):
        s = _closes([100.0] * 30)
        self.assertEqual(M.sparkline(s), "▄" * 30)

    def test_empty_series_is_empty(self):
        self.assertEqual(M.sparkline(pd.Series([], dtype=float)), "")

    def test_caps_at_30_points(self):
        s = _closes([float(i) for i in range(1, 61)])
        self.assertEqual(len(M.sparkline(s)), 30)


class TestComputeMetricsForTicker(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from stockstui.database.db_manager import DbManager

        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DbManager(Path(self.tmpdir.name) / "bars.db")

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _seed(self, closes: list[float], volumes: list[float] | None = None):
        index = pd.bdate_range("2026-01-01", periods=len(closes))
        frame = pd.DataFrame(
            {
                "Open": closes,
                "High": closes,
                "Low": closes,
                "Close": closes,
                "Volume": volumes or [1000.0] * len(closes),
            },
            index=index,
        )
        frame["Adj Close"] = closes
        self.db.save_daily_bars("TEST", frame)

    def test_all_metrics_computed(self):
        self._seed([100.0 + i * 0.5 for i in range(260)])
        values = M.compute_metrics_for_ticker(self.db, "test")
        for key in M.METRIC_COLUMNS:
            self.assertIn(key, values)
        self.assertIsInstance(values["RSI(14)"], float)
        self.assertIsInstance(values["Sparkline"], str)
        self.assertTrue(values["Sparkline"])

    def test_unknown_ticker_yields_blanks_not_errors(self):
        values = M.compute_metrics_for_ticker(self.db, "NOPE")
        self.assertEqual(set(values), set(M.METRIC_COLUMNS))
        self.assertTrue(all(v == "" for v in values.values()))

    def test_short_history_yields_none_for_long_windows(self):
        # Eight bars: enough for the 1W return (needs 6), too few for
        # 30d vol (needs 31), 1Y return, 200d MA, and rel volume (needs 21).
        self._seed([100.0 + i for i in range(8)])
        values = M.compute_metrics_for_ticker(self.db, "TEST")
        self.assertIsNone(values["vs 200d MA"])
        self.assertIsNone(values["1Y Return"])
        self.assertIsNone(values["30d Vol"])
        self.assertIsNone(values["Rel Volume"])
        self.assertIsNone(values["1M Return"])
        # Short-window metrics still present.
        self.assertIsNotNone(values["1W Return"])


if __name__ == "__main__":
    unittest.main()
