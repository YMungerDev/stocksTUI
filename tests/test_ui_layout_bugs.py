"""Regression tests for the layout and display bugs in #22."""

import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from textual.app import App
from textual.widgets import RadioButton, Select

from stockstui.data_providers import edgar_provider, fundamentals_provider
from stockstui.ui.views.filings_view import FilingsView, format_filing_rows
from stockstui.ui.views.fundamentals_view import FundamentalsView
from stockstui.ui.views.options_view import OptionsView
from stockstui.ui.widgets.vim_radio_set import VimRadioSet

SCRATCHPAD = (110, 50)


def _host(view_factory, css_path="../stockstui/main.css"):
    class Host(App):
        CSS_PATH = css_path
        config = MagicMock()
        config.lists = {"stocks": [{"ticker": "AAPL", "alias": "Apple"}]}
        fundamentals_ticker = None
        filings_ticker = None
        options_ticker = None
        fetch_filings = MagicMock()
        fetch_fundamentals = MagicMock()
        fetch_research = MagicMock()
        fetch_options_expirations = MagicMock()

        def compose(self):
            yield view_factory()

    return Host()


class TestControlRowsFitTheScratchpad(unittest.IsolatedAsyncioTestCase):
    """Selectors beside a ticker input must be on screen at 110 columns (#22.1)."""

    async def _assert_select_visible(self, view_cls, select_id):
        app = _host(view_cls)
        async with app.run_test(size=SCRATCHPAD) as pilot:
            await pilot.pause()
            region = app.query_one(f"#{select_id}", Select).region
            self.assertGreater(region.width, 0, f"{select_id} has no width")
            self.assertLessEqual(region.right, SCRATCHPAD[0], f"{select_id} is off-screen")

    async def test_fundamentals_period_select(self):
        await self._assert_select_visible(FundamentalsView, "fundamentals-period-select")

    async def test_filings_form_select(self):
        await self._assert_select_visible(FilingsView, "filings-form-select")

    async def test_options_expiration_select_and_chart_button(self):
        await self._assert_select_visible(OptionsView, "options-expiration-select")
        app = _host(OptionsView)
        async with app.run_test(size=SCRATCHPAD) as pilot:
            await pilot.pause()
            self.assertLessEqual(app.query_one("#options-view-toggle").region.right, SCRATCHPAD[0])


class TestDaysToExpiry(unittest.TestCase):
    def test_header_uses_calendar_days_not_datetime_difference(self):
        # The header now uses option_analytics.days_until, which is 0 on expiry day.
        from stockstui.presentation import option_analytics

        today = date(2026, 11, 20)
        self.assertEqual(option_analytics.days_until("2026-11-20", today=today), 0)
        self.assertEqual(option_analytics.days_until("2026-11-21", today=today), 1)
        import inspect

        source = inspect.getsource(OptionsView)
        self.assertNotIn("datetime.now()", source)


class TestStaleYfinanceCache(unittest.TestCase):
    """A yfinance payload cached before EDGAR was usable must not hide it (#22.3)."""

    def tearDown(self):
        edgar_provider.set_contact("")

    def _db(self, payload):
        db = MagicMock()
        db.load_fundamentals_cache.return_value = json.dumps({"statements": payload})
        return db

    def test_old_yfinance_cache_is_refetched_once_edgar_is_available(self):
        edgar_provider.set_contact("Ann Lee ann@example.org")
        db = self._db({"income": {"columns": ["x"], "rows": {}}, "meta": {"source": "yfinance"}})
        fresh = {"income": {"columns": ["y"], "rows": {}}, "meta": {"source": "edgar"}}
        with patch.object(fundamentals_provider, "fetch_statements", return_value=fresh) as fetch:
            result = fundamentals_provider.get_statements("AAPL", db)
        fetch.assert_called_once()
        self.assertEqual(result["meta"]["source"], "edgar")

    def test_yfinance_cache_is_kept_when_edgar_already_had_nothing(self):
        edgar_provider.set_contact("Ann Lee ann@example.org")
        db = self._db(
            {"income": {"columns": ["x"], "rows": {}}, "meta": {"source": "yfinance", "edgar_checked": True}}
        )
        with patch.object(fundamentals_provider, "fetch_statements") as fetch:
            fundamentals_provider.get_statements("SAP.DE", db)
        fetch.assert_not_called()

    def test_yfinance_cache_is_kept_without_a_contact(self):
        db = self._db({"income": {"columns": ["x"], "rows": {}}, "meta": {"source": "yfinance"}})
        with patch.object(fundamentals_provider, "fetch_statements") as fetch:
            fundamentals_provider.get_statements("AAPL", db)
        fetch.assert_not_called()

    def test_edgar_saying_nothing_is_recorded_but_an_error_is_not(self):
        edgar_provider.set_contact("Ann Lee ann@example.org")
        yf_payload = {"income": {"columns": ["x"], "rows": {}}}
        with patch.object(fundamentals_provider.edgar_fundamentals, "fetch_statements", return_value={}), patch.object(
            fundamentals_provider, "fetch_yfinance_statements", return_value=dict(yf_payload)
        ):
            self.assertTrue(fundamentals_provider.fetch_statements("SAP.DE")["meta"]["edgar_checked"])
        with patch.object(
            fundamentals_provider.edgar_fundamentals, "fetch_statements", side_effect=edgar_provider.EdgarError("down")
        ), patch.object(fundamentals_provider, "fetch_yfinance_statements", return_value=dict(yf_payload)):
            self.assertFalse(fundamentals_provider.fetch_statements("AAPL")["meta"]["edgar_checked"])


class TestMessagesAreNotTableRows(unittest.IsolatedAsyncioTestCase):
    async def test_valuation_unavailable_is_a_note(self):
        app = _host(FundamentalsView)
        async with app.run_test(size=SCRATCHPAD) as pilot:
            view = app.query_one(FundamentalsView)
            view.update_valuation({"unavailable": "needs SEC share counts"})
            await pilot.pause()
            table = view.query_one("#valuation-table")
            self.assertEqual(table.row_count, 0)
            # Headers keep their natural widths instead of the message's.
            first_column = list(table.columns.values())[0]
            self.assertLess(first_column.get_render_width(table), 20)
            note = view.query_one("#valuation-note")
            self.assertTrue(note.display)
            self.assertIn("needs SEC share counts", str(note.render()))
            view.update_valuation({"summary": {}, "market_cap": None})
            await pilot.pause()
            self.assertFalse(note.display)


class TestFilingDates(unittest.TestCase):
    def test_filed_date_and_period_are_separate_columns(self):
        rows = format_filing_rows(
            [{"form": "10-K", "filing_date": "2026-11-03", "report_date": "2026-09-26", "description": "10-K"}]
        )
        self.assertEqual(rows[0][0].plain, "2026-11-03")
        self.assertEqual(rows[0][2].plain, "2026-09-26")

    def test_missing_period_is_a_dash_not_the_filing_date(self):
        rows = format_filing_rows([{"form": "4", "filing_date": "2026-11-10"}])
        self.assertEqual(rows[0][0].plain, "2026-11-10")
        self.assertEqual(rows[0][2].plain, "—")


class TestRadioCursorFollowsSelection(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_starts_on_the_pressed_button(self):
        class Host(App):
            def compose(self):
                yield VimRadioSet(RadioButton("1D"), RadioButton("5D"), RadioButton("1M", value=True))

        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            radio = app.query_one(VimRadioSet)
            self.assertEqual(radio.pressed_index, 2)
            self.assertEqual(radio._selected, 2)
            highlighted = [b for b in radio.query(RadioButton) if b.has_class("-selected")]
            self.assertEqual([str(b.label) for b in highlighted], ["1M"])


if __name__ == "__main__":
    unittest.main()
