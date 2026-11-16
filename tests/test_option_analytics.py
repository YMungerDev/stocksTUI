"""Tests for options analytics: implied move, skew, ratios, max pain (#11).

Each calculation is checked against hand-built fixture chains per the
acceptance criteria; illiquid strikes (zero bid, stale last) are excluded.
"""

import unittest
from datetime import date

import numpy as np
import pandas as pd

from stockstui.presentation import option_analytics as A


def _side(
    strikes: list[float],
    bids: list[float] | None = None,
    asks: list[float] | None = None,
    lasts: list[float] | None = None,
    vols: list[float] | None = None,
    oi: list[float] | None = None,
    ivs: list[float] | None = None,
) -> pd.DataFrame:
    n = len(strikes)
    return pd.DataFrame(
        {
            "strike": strikes,
            "bid": bids if bids is not None else [1.0] * n,
            "ask": asks if asks is not None else [2.0] * n,
            "lastPrice": lasts if lasts is not None else [1.5] * n,
            "volume": vols if vols is not None else [100.0] * n,
            "openInterest": oi if oi is not None else [200.0] * n,
            "impliedVolatility": ivs if ivs is not None else [0.3] * n,
        }
    )


class TestImpliedMove(unittest.TestCase):
    def test_hand_checked_straddle(self):
        calls = _side([95.0, 100.0, 105.0], bids=[5.0, 5.5, 1.0], asks=[6.0, 6.5, 2.0])
        puts = _side([95.0, 100.0, 105.0], bids=[1.0, 5.0, 9.0], asks=[2.0, 6.0, 10.0])
        move = A.implied_move(calls, puts, 100.0, days_to_expiry=4)
        # ATM call mid (5.5+6.5)/2=6.0; put mid (5+6)/2=5.5 -> straddle 11.5
        self.assertAlmostEqual(move["straddle"], 11.5)
        self.assertAlmostEqual(move["move"], 11.5)
        self.assertAlmostEqual(move["move_pct"], 11.5)
        # sqrt(4)=2 -> daily = 11.5% / 2
        self.assertAlmostEqual(move["daily_move_pct"], 5.75)

    def test_illiquid_strikes_excluded(self):
        """Zero-bid/stale rows must not be used for the ATM straddle."""
        # ATM row (strike 100) is illiquid: bid 0, stale last 0.
        calls = _side(
            [95.0, 100.0, 105.0],
            bids=[5.0, 0.0, 1.0],
            asks=[6.0, 0.0, 2.0],
            lasts=[5.5, 0.0, 1.5],
        )
        puts = _side([95.0, 100.0, 105.0], bids=[1.0, 0.0, 9.0], asks=[2.0, 0.0, 10.0])
        move = A.implied_move(calls, puts, 98.0)
        # Nearest liquid call strike to 98 is 95 (mid 5.5), put is 95 (mid 1.5)
        self.assertAlmostEqual(move["straddle"], 7.0)
        self.assertEqual(move["atm_strike"], 95.0)

    def test_all_illiquid_returns_none(self):
        calls = _side([100.0], bids=[0.0], asks=[0.0], lasts=[0.0])
        puts = _side([100.0], bids=[0.0], asks=[0.0], lasts=[0.0])
        self.assertIsNone(A.implied_move(calls, puts, 100.0))

    def test_bad_underlying_returns_none(self):
        calls, puts = _side([100.0]), _side([100.0])
        self.assertIsNone(A.implied_move(calls, puts, 0))
        self.assertIsNone(A.implied_move(calls, puts, None))


class TestIvSkew(unittest.TestCase):
    def test_put_wing_higher_than_call_wing(self):
        """Classic equity skew: OTM put IV > OTM call IV."""
        strikes = [75.0, 80.0, 100.0, 120.0, 125.0]
        calls = _side(
            [75.0, 100.0, 125.0],
            ivs=[0.20, 0.30, 0.40],
        )
        puts = _side(
            [75.0, 100.0, 125.0],
            ivs=[0.60, 0.30, 0.25],
        )
        skew = A.iv_skew(calls, puts)
        self.assertAlmostEqual(skew["atm_iv"], 0.30)
        self.assertAlmostEqual(skew["put_wing_iv"], 0.60)
        self.assertAlmostEqual(skew["call_wing_iv"], 0.40)
        self.assertAlmostEqual(skew["skew"], 0.20)

    def test_illiquid_wings_excluded(self):
        # The 25%-moneyness put wing (strike 75) is illiquid; nearest
        # liquid put strike to 75 should be used.
        calls = _side([75.0, 100.0, 125.0], ivs=[0.20, 0.30, 0.40])
        puts = _side(
            [75.0, 100.0],
            ivs=[0.90, 0.30],
            bids=[0.0, 5.0],
            lasts=[0.0, 5.5],
        )
        skew = A.iv_skew(calls, puts)
        self.assertAlmostEqual(skew["put_wing_iv"], 0.30)  # 100 is nearest liquid

    def test_empty_chain_returns_none(self):
        self.assertIsNone(A.iv_skew(_side([]), _side([])))


class TestTermStructure(unittest.TestCase):
    def test_orders_expiries_and_skips_missing(self):
        atm_ivs = {"2026-01-16": 0.30, "2026-02-20": None, "2026-03-20": 0.28}
        structure = A.term_structure(atm_ivs, ["2026-03-20", "2026-01-16", "2026-02-20"])
        self.assertEqual([e for e, _ in structure], ["2026-01-16", "2026-03-20"])
        self.assertAlmostEqual(structure[0][1], 0.30)

    def test_nan_is_skipped(self):
        atm_ivs = {"2026-01-16": float("nan")}
        self.assertEqual(A.term_structure(atm_ivs, ["2026-01-16"]), [])


class TestPutCallRatios(unittest.TestCase):
    def test_hand_checked(self):
        calls = _side([100.0, 105.0], vols=[300.0, 100.0], oi=[400.0, 100.0])
        puts = _side([100.0, 105.0], vols=[200.0, 300.0], oi=[250.0, 250.0])
        ratios = A.put_call_ratios(calls, puts)
        self.assertAlmostEqual(ratios["put_call_volume"], 500 / 400)
        self.assertAlmostEqual(ratios["put_call_open_interest"], 500 / 500)

    def test_zero_call_volume_is_nan(self):
        calls = _side([100.0], vols=[0.0])
        puts = _side([100.0], vols=[10.0])
        ratios = A.put_call_ratios(calls, puts)
        self.assertTrue(np.isnan(ratios["put_call_volume"]))


class TestMaxPain(unittest.TestCase):
    def test_hand_checked(self):
        """Max pain = the strike minimising writer payouts weighted by OI."""
        # Writer pain per settlement S: calls pay max(S-K,0)*OI for each
        # call strike K; puts pay max(K-S,0)*OI. Verified by hand:
        calls = _side([95.0, 100.0, 105.0], oi=[30.0, 10.0, 0.0])
        puts = _side([95.0, 100.0, 105.0], oi=[0.0, 0.0, 5.0])
        # S=95: calls pay (95-95)*30 + (95-100 clip 0) + 0 -> 0... plus
        # puts pay (105-95)*5=50 -> total 50.
        # S=100: calls pay (100-95)*30=150 + 0 -> 150; puts pay 25 -> 175.
        # S=105: calls pay (105-95)*30 + (105-100)*10 = 350; puts 0 -> 350.
        self.assertEqual(A.max_pain(calls, puts), 95.0)

    def test_single_strike(self):
        calls = _side([100.0], oi=[5.0])
        puts = _side([100.0], oi=[5.0])
        self.assertEqual(A.max_pain(calls, puts), 100.0)

    def test_empty_chain_returns_none(self):
        self.assertIsNone(A.max_pain(_side([]), _side([])))


class TestDaysUntil(unittest.TestCase):
    def test_positive_difference(self):
        self.assertEqual(A.days_until("2026-01-10", today=date(2026, 1, 1)), 9)

    def test_past_expiry_is_zero_or_negative(self):
        self.assertEqual(A.days_until("2025-01-01", today=date(2026, 1, 1)), -365)

    def test_invalid_string_is_zero(self):
        self.assertEqual(A.days_until("not-a-date"), 0)


class TestEarningsHighlight(unittest.TestCase):
    def test_spans_earnings(self):
        from stockstui.ui.views.options_view import OptionsView

        expirations = ["2026-11-20", "2026-12-18", "2027-01-15"]
        # Earnings on 12-01: spans the 12-18 expiry only.
        self.assertTrue(
            OptionsView._spans_earnings("2026-12-18", expirations, "2026-12-01")
        )
        self.assertFalse(
            OptionsView._spans_earnings("2026-11-20", expirations, "2026-12-01")
        )
        self.assertFalse(
            OptionsView._spans_earnings("2027-01-15", expirations, "2026-12-01")
        )

    def test_earnings_on_expiry_day_counts(self):
        from stockstui.ui.views.options_view import OptionsView

        expirations = ["2026-12-18", "2027-01-15"]
        self.assertTrue(
            OptionsView._spans_earnings("2026-12-18", expirations, "2026-12-18")
        )


class TestAnalyticsPanelRenders(unittest.IsolatedAsyncioTestCase):
    async def test_summary_strip_renders_with_real_chain_shape(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from textual.widgets import Static

        from stockstui.data_providers import market_provider
        from stockstui.ui.views.options_view import OptionsView

        market_provider._info_cache["AAPL"] = {"currency": "USD", "exchange": "NMS"}
        calls = _side([95.0, 100.0, 105.0], bids=[5.0, 5.5, 1.0], asks=[6.0, 6.5, 2.0])
        puts = _side([95.0, 100.0, 105.0], bids=[1.0, 5.0, 9.0], asks=[2.0, 6.0, 10.0])

        class HostApp(App):
            theme_variables = {}
            options_ticker = "AAPL"
            option_positions = {}
            notify = MagicMock()
            fetch_options_expirations = MagicMock()
            config = MagicMock()
            config.lists = {"stocks": [{"ticker": "AAPL"}]}
            _last_options_data = {
                "ticker": "AAPL",
                "expiration": "2027-01-15",
                "calls": calls,
                "puts": puts,
                "underlying": {"regularMarketPrice": 100.0},
            }

            def compose(self):
                yield OptionsView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(OptionsView)
            await view._render_options_data()
            await pilot.pause()
            strip = view.query_one(".options-analytics-header", Static)
            text = str(strip.render())
            self.assertIn("Implied move", text)
            self.assertIn("P/C Vol", text)
            self.assertIn("Max pain", text)
            self.assertIn("d to expiry", text)

        market_provider._info_cache.pop("AAPL", None)


if __name__ == "__main__":
    unittest.main()
