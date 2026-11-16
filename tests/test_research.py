"""Tests for the research panel: analyst data, surprises, insiders (#9)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stockstui.data_providers import research_provider as R
from stockstui.database.db_manager import DbManager
from stockstui.ui.views.fundamentals_view import format_research_rows


def _frame(data: dict, index=None) -> pd.DataFrame:
    return pd.DataFrame(data, index=index or range(len(next(iter(data.values())))))


class TestFetchResearch(unittest.TestCase):
    """Per-block tolerance: one failing accessor must not break the panel."""

    def test_all_blocks_tolerate_missing(self):
        def failing(*args, **kwargs):
            raise RuntimeError("no data")

        class FakeTicker:
            analyst_price_targets = property(failing)
            recommendations_summary = property(failing)
            earnings_estimate = property(failing)
            revenue_estimate = property(failing)
            earnings_history = property(failing)
            insider_transactions = property(failing)
            institutional_holders = property(failing)
            major_holders = property(failing)

        research = self._run_with_ticker(FakeTicker())
        self.assertTrue(
            all(v is None for v in research.values()),
            "every block must be None when yfinance fails",
        )

    def _run_with_ticker(self, ticker_obj):
        with patch(
            "stockstui.data_providers.research_provider._ticker",
            return_value=ticker_obj,
        ):
            return R.fetch_research("AAPL")

    def test_empty_frames_yield_none(self):
        class EmptyTicker:
            analyst_price_targets = {}
            recommendations_summary = pd.DataFrame()
            earnings_estimate = pd.DataFrame()
            revenue_estimate = pd.DataFrame()
            earnings_history = pd.DataFrame()
            insider_transactions = pd.DataFrame()
            institutional_holders = pd.DataFrame()
            major_holders = pd.DataFrame()

        research = self._run_with_ticker(EmptyTicker())
        self.assertIsNone(research["price_targets"])
        self.assertIsNone(research["recommendations"])
        self.assertIsNone(research["institutional_holders"])

    def test_full_payload_parsing(self):
        targets = {"current": 351.5, "high": 425.0, "low": 250.0, "mean": 345.6, "median": 355.0}
        rec = _frame(
            {
                "strongBuy": [6, 6],
                "buy": [20, 18],
                "hold": [13, 13],
                "sell": [3, 3],
                "strongSell": [3, 3],
            }
        )
        history = _frame(
            {
                "epsActual": [2.84, 2.01],
                "epsEstimate": [2.67, 1.94],
                "epsDifference": [0.17, 0.07],
                "surprisePercent": [0.0634, 0.0346],
            },
            index=["2025-12-31", "2026-03-31"],
        )
        insiders = _frame(
            {"Insider": ["NEWSTEAD J", "SMITH B"], "Text": ["Sale", "Sale"], "Start Date": ["2026-11-10", "2026-11-03"]}
        )
        holders = _frame(
            {"Holder": ["Blackrock", "Vanguard"], "pctHeld": [0.0796, 0.0657]}
        )
        major = _frame({"Value": [0.01781, 0.66489, 0.67695, 7767.0]}, index=[
            "insidersPercentHeld", "institutionsPercentHeld",
            "institutionsFloatPercentHeld", "institutionsCount",
        ])

        class FullTicker:
            analyst_price_targets = targets
            recommendations_summary = rec
            earnings_estimate = _frame({"avg": [2.91], "growth": [0.0253], "numberOfAnalysts": [25]})
            revenue_estimate = _frame({"avg": [156.43e9], "growth": [0.0882]})
            earnings_history = history
            insider_transactions = insiders
            institutional_holders = holders
            major_holders = major

        research = self._run_with_ticker(FullTicker())

        self.assertAlmostEqual(research["price_targets"]["mean"], 345.6)
        self.assertEqual(research["recommendations"]["strong_buy"], 6)
        self.assertEqual(research["recommendations"]["previous"]["buy"], 18)
        self.assertEqual(research["earnings_history"][0]["surprisePercent"], 0.0634)
        self.assertEqual(research["insider_transactions"][0]["Insider"], "NEWSTEAD J")
        self.assertEqual(research["institutional_holders"][0]["Holder"], "Blackrock")
        self.assertAlmostEqual(research["major_holders"]["institutionsPercentHeld"], 0.66489)


class TestResearchCaching(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DbManager(Path(self.tmpdir.name) / "r.db")

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def test_cache_round_trip_is_json_safe(self):
        research = {"price_targets": {"mean": 345.6}, "recommendations": None}
        self.db.save_fundamentals_cache(
            "AAPL::research", json.dumps({"research": research})
        )
        raw = self.db.load_fundamentals_cache("AAPL::research")
        self.assertEqual(json.loads(raw)["research"], research)

    @patch("stockstui.data_providers.research_provider.fetch_research")
    def test_get_research_prefers_cache(self, mock_fetch):
        self.db.save_fundamentals_cache(
            "AAPL::research", json.dumps({"research": {"price_targets": {"mean": 1.0}}})
        )
        result = R.get_research("AAPL", self.db)
        mock_fetch.assert_not_called()
        self.assertEqual(result["price_targets"]["mean"], 1.0)

    @patch("stockstui.data_providers.research_provider.fetch_research")
    def test_force_refresh_bypasses_cache(self, mock_fetch):
        mock_fetch.return_value = {"price_targets": {"mean": 2.0}}
        self.db.save_fundamentals_cache(
            "AAPL::research", json.dumps({"research": {"price_targets": {"mean": 1.0}}})
        )
        result = R.get_research("AAPL", self.db, force_refresh=True)
        mock_fetch.assert_called_once()
        self.assertEqual(result["price_targets"]["mean"], 2.0)


class TestResearchRows(unittest.TestCase):
    def test_format_research_rows_blocks(self):
        research = {
            "price_targets": {"mean": 345.63, "high": 425.0, "low": 250.0},
            "recommendations": {
                "strong_buy": 6, "buy": 20, "hold": 13, "sell": 3,
                "strong_sell": 3,
            },
            "eps_estimate": [{"avg": 2.91, "growth": 0.0253, "numberOfAnalysts": 25}],
            "earnings_history": [
                {"surprisePercent": 0.0634}, {"surprisePercent": 0.0346},
            ],
            "insider_transactions": [
                {"Insider": "NEWSTEAD J", "Text": "Sale at price 351.36 per share.",
                 "Start Date": "2026-11-10 00:00:00"},
            ],
            "institutional_holders": [
                {"Holder": "Blackrock Inc.", "pctHeld": 0.0796},
            ],
            "major_holders": {
                "insidersPercentHeld": 0.01781,
                "institutionsPercentHeld": 0.66489,
                "institutionsCount": 7767.0,
            },
        }
        rows = format_research_rows(research)
        blocks = [r[0] for r in rows]
        self.assertIn("Price Targets", blocks)
        self.assertIn("Recommendations", blocks)
        self.assertIn("EPS Estimates", blocks)
        self.assertIn("Earnings Surprises", blocks)
        self.assertIn("Insider Transactions", blocks)
        self.assertIn("Top Institutional", blocks)
        self.assertIn("Ownership", blocks)
        # Detail content spot checks.
        targets_detail = next(r[1] for r in rows if r[0] == "Price Targets")
        self.assertIn("345.63", targets_detail)
        surprise_detail = next(r[1] for r in rows if r[0] == "Earnings Surprises")
        self.assertIn("+6.3%", surprise_detail)

    def test_all_missing_yields_no_rows(self):
        self.assertEqual(format_research_rows({}), [])
        self.assertEqual(
            format_research_rows({k: None for k in (
                "price_targets", "recommendations", "eps_estimate",
                "revenue_estimate", "earnings_history",
                "insider_transactions", "institutional_holders", "major_holders",
            )}),
            [],
        )

    def test_partial_data_renders_available_blocks_only(self):
        rows = format_research_rows(
            {"recommendations": {"strong_buy": 1, "buy": 2, "hold": 3,
                                 "sell": 0, "strong_sell": 0}}
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Recommendations")


if __name__ == "__main__":
    unittest.main()
