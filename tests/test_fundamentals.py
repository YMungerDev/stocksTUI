"""Tests for fundamentals: statements, caching, and derived ratios (#6)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from stockstui.data_providers import fundamentals_provider as F
from stockstui.database.db_manager import DbManager
from stockstui.ui.views.fundamentals_view import format_statement_rows


def _payload(
    rows: dict[str, list], columns: list[str] | None = None
) -> dict:
    return {
        "columns": columns or ["2026-09-30", "2025-09-30", "2024-09-30"],
        "rows": rows,
    }


def _aapl_like_statements() -> dict:
    """Fixture mirroring AAPL's real 2026 numbers (hand-checked ratios)."""
    return {
        "income": _payload(
            {
                "Total Revenue": [478.834e9, 416.161e9, 391.035e9],
                "Gross Profit": [233.602e9, 195.201e9, 180.683e9],
                "Operating Income": [158.063e9, 133.050e9, 123.216e9],
                "Net Income": [132.175e9, 112.010e9, 93.736e9],
                "Diluted EPS": [8.98, 7.46, 6.08],
                "EBITDA": [171.486e9, 144.748e9, 134.661e9],
            }
        ),
        "balance": _payload(
            {
                "Total Debt": [80.111e9, 98.657e9, 106.629e9],
                "Cash And Cash Equivalents": [36.305e9, 35.934e9, 29.943e9],
                "Invested Capital": [160.0e9, 150.0e9, 140.0e9],
            }
        ),
        "cash": _payload(
            {
                "Free Cash Flow": [136.056e9, 98.767e9, 108.807e9],
                "Operating Cash Flow": [145.097e9, 111.482e9, 118.254e9],
                "Capital Expenditure": [-9.041e9, -12.715e9, -9.447e9],
            }
        ),
    }


class TestDerivedRatios(unittest.TestCase):
    """Each ratio against hand-computed values from the fixture."""

    def setUp(self):
        self.ratios = F.compute_ratios(_aapl_like_statements())

    def test_revenue_growth(self):
        self.assertAlmostEqual(self.ratios["revenue_growth"], 478.834e9 / 416.161e9 - 1)

    def test_net_income_growth(self):
        self.assertAlmostEqual(
            self.ratios["net_income_growth"], 132.175e9 / 112.010e9 - 1
        )

    def test_eps_growth(self):
        self.assertAlmostEqual(self.ratios["eps_growth"], 8.98 / 7.46 - 1)

    def test_margins(self):
        self.assertAlmostEqual(self.ratios["gross_margin"], 233.602e9 / 478.834e9)
        self.assertAlmostEqual(
            self.ratios["operating_margin"], 158.063e9 / 478.834e9
        )
        self.assertAlmostEqual(self.ratios["net_margin"], 132.175e9 / 478.834e9)

    def test_roic_hand_checked(self):
        # NOPAT = operating * 0.79; ROIC = NOPAT / invested capital.
        nopat = 158.063e9 * 0.79
        self.assertAlmostEqual(self.ratios["roic"], nopat / 160.0e9)

    def test_fcf(self):
        self.assertAlmostEqual(self.ratios["fcf"], 136.056e9)

    def test_net_debt_to_ebitda(self):
        net_debt = 80.111e9 - 36.305e9
        self.assertAlmostEqual(
            self.ratios["net_debt_to_ebitda"], net_debt / 171.486e9
        )

    def test_missing_statements_yield_none_ratios(self):
        ratios = F.compute_ratios({})
        self.assertTrue(
            all(v is None for v in ratios.values()),
            "all ratios must be None without statements",
        )

    def test_zero_prior_growth_is_none(self):
        statements = {
            "income": _payload({"Total Revenue": [100.0, 0.0, 50.0]})
        }
        self.assertIsNone(F.compute_ratios(statements)["revenue_growth"])


class TestStatementsCaching(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = DbManager(Path(self.tmpdir.name) / "fund.db")

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def test_statements_survive_reconnect(self):
        fixture = _aapl_like_statements()
        self.db.save_fundamentals_cache("AAPL", json.dumps({"statements": fixture}))
        self.db.close()

        db2 = DbManager(Path(self.tmpdir.name) / "fund.db")
        try:
            loaded = F.load_cached_statements(db2, "AAPL")
        finally:
            db2.close()
        self.assertEqual(loaded["income"]["rows"]["Total Revenue"][0], 478.834e9)

    def test_get_statements_prefers_cache(self):
        fixture = _aapl_like_statements()
        self.db.save_fundamentals_cache("AAPL", json.dumps({"statements": fixture}))
        with patch.object(F, "fetch_statements") as mock_fetch:
            result = F.get_statements("AAPL", self.db)
        mock_fetch.assert_not_called()
        self.assertEqual(result["income"]["columns"], fixture["income"]["columns"])

    def test_force_refresh_bypasses_cache(self):
        fixture = _aapl_like_statements()
        self.db.save_fundamentals_cache("AAPL", json.dumps({"statements": fixture}))
        with patch.object(
            F, "fetch_statements", return_value={"income": _payload({"X": [1.0]})}
        ) as mock_fetch:
            F.get_statements("AAPL", self.db, force_refresh=True)
        mock_fetch.assert_called_once()

    def test_fetch_failure_still_serves_cache(self):
        fixture = _aapl_like_statements()
        self.db.save_fundamentals_cache("AAPL", json.dumps({"statements": fixture}))
        with patch.object(F, "fetch_statements", return_value={}):
            result = F.get_statements("AAPL", self.db, force_refresh=True)
        # Network produced nothing; the previous cache remains intact for
        # subsequent non-forced reads.
        with patch.object(F, "fetch_statements") as mock_fetch:
            result = F.get_statements("AAPL", self.db)
        mock_fetch.assert_not_called()
        self.assertEqual(result["income"]["rows"]["Total Revenue"][0], 478.834e9)

    def test_stale_cache_is_ignored(self):
        self.db.save_fundamentals_cache("AAPL", json.dumps({"statements": {}}))
        # Force the timestamp older than the TTL.
        import time as time_module

        old = time_module.time() - F._CACHE_TTL_SECONDS - 10
        self.db.conn.execute(
            "UPDATE fundamentals_cache SET timestamp = ? WHERE ticker = 'AAPL'",
            (old,),
        )
        self.db.conn.commit()
        with patch.object(
            F, "fetch_statements", return_value={"income": _payload({"Y": [2.0]})}
        ):
            result = F.get_statements("AAPL", self.db)
        self.assertEqual(result["income"]["rows"]["Y"], [2.0])


class TestStatementRendering(unittest.TestCase):
    def test_rows_render_with_aliases(self):
        statements = _aapl_like_statements()
        rows = format_statement_rows(
            statements["income"],
            [("Total Revenue", ("Total Revenue", "Revenues")), ("EBITDA", ("EBITDA", "Normalized EBITDA"))],
            max_columns=3,
        )
        self.assertEqual(len(rows), 2)
        # First data cell of Total Revenue is 478.834e9 formatted.
        self.assertIn("478", str(rows[0][1].plain).replace(",", ""))
        self.assertEqual(len(rows[0]), 4)  # label + 3 columns

    def test_missing_payload_blanks_cells(self):
        rows = format_statement_rows(None, [("Total Revenue", ("Total Revenue",))], max_columns=2)
        self.assertEqual(rows[0][1].plain, "—")

    def test_missing_line_item_blanks_row(self):
        payload = _payload({"Net Income": [1.0, 2.0]})
        rows = format_statement_rows(payload, [("Total Revenue", ("Total Revenue",))], max_columns=2)
        self.assertEqual(rows[0][1].plain, "—")


class TestFundamentalsViewRendering(unittest.IsolatedAsyncioTestCase):
    async def test_statement_headers_match_newest_first_row_order(self):
        from textual.app import App
        from stockstui.ui.views.fundamentals_view import FundamentalsView
        from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

        class HostApp(App):
            config = MagicMock()
            config.lists = {}
            fundamentals_ticker = None

            def compose(self):
                yield FundamentalsView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(FundamentalsView)
            view.update_statements(_aapl_like_statements())
            await pilot.pause()

            table = view.query_one("#fundamentals-table", NavigableDataTable)
            headers = [str(col.label) for col in table.columns.values()]
            self.assertEqual(
                headers, ["Line Item", "2026-09-30", "2025-09-30", "2024-09-30"]
            )
            first_row = [str(cell) for cell in table.get_row_at(0)]
            self.assertEqual(first_row[0], "Total Revenue")
            self.assertIn("478", first_row[1].replace(",", ""))
            self.assertIn("416", first_row[2].replace(",", ""))
            self.assertIn("391", first_row[3].replace(",", ""))


if __name__ == "__main__":
    unittest.main()
