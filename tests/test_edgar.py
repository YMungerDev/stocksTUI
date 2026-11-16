"""Tests for the SEC EDGAR filings provider and Filings tab (#10)."""

import unittest
from unittest.mock import MagicMock, patch

from textual.app import App
from textual.widgets import DataTable

from stockstui.data_providers import edgar_provider
from stockstui.ui.views.filings_view import FilingsView, format_filing_rows


def _submissions_payload(forms: list[str]) -> dict:
    n = len(forms)
    return {
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": [f"2026-11-{(i % 28) + 1:02d}" for i in range(n)],
                "reportDate": [f"2026-10-{(i % 28) + 1:02d}" for i in range(n)],
                "accessionNumber": [f"0000320193-26-{i:06d}" for i in range(n)],
                "primaryDocument": [f"doc{i}.htm" for i in range(n)],
                "primaryDocDescription": ["8-K current report"] * n,
            }
        }
    }


class TestTickerToCik(unittest.TestCase):
    def setUp(self):
        edgar_provider.reset_caches()

    def tearDown(self):
        edgar_provider.reset_caches()

    def test_user_agent_header_is_sent(self):
        """Every request must carry an identifying User-Agent (SEC policy)."""
        captured: dict = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    str(i): {"ticker": "AAPL", "cik_str": 320193, "title": "Apple"}
                    for i in range(3)
                }

        def fake_get(url, timeout=None):
            captured["headers"] = edgar_provider._get_session().headers
            return FakeResponse()

        with patch.object(edgar_provider._get_session(), "get", side_effect=fake_get):
            edgar_provider.get_cik_for_ticker("AAPL")

        ua = captured["headers"].get("User-Agent", "")
        self.assertIn("stocksTUI", ua)
        self.assertIn("contact", ua)

    @patch.object(edgar_provider, "requests")
    def test_network_failure_returns_none(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        mock_session = MagicMock()
        mock_session.get.side_effect = real_requests.ConnectionError("down")
        with patch.object(
            edgar_provider, "_session", mock_session
        ), patch.object(edgar_provider, "_get_session", return_value=mock_session):
            self.assertIsNone(edgar_provider.get_cik_for_ticker("AAPL"))


class TestFetchFilings(unittest.TestCase):
    def setUp(self):
        edgar_provider.reset_caches()

    def tearDown(self):
        edgar_provider.reset_caches()

    def _mock_mapping(self):
        return patch.object(
            edgar_provider,
            "_load_ticker_to_cik",
            return_value={"AAPL": 320193},
        )

    @patch.object(edgar_provider, "requests")
    def test_fetch_filings_parses_recent_rows(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        mock_session = MagicMock()
        response = MagicMock()
        response.json.return_value = _submissions_payload(["10-K", "10-Q", "8-K"])
        mock_session.get.return_value = response

        with self._mock_mapping(), patch.object(
            edgar_provider, "_get_session", return_value=mock_session
        ):
            filings = edgar_provider.fetch_filings("AAPL")

        self.assertEqual(len(filings), 3)
        self.assertEqual(filings[0]["form"], "10-K")
        self.assertIn("sec.gov/Archives/edgar/data/0000320193/", filings[0]["url"])
        self.assertTrue(filings[0]["url"].endswith("doc0.htm"))
        self.assertNotIn("-", filings[0]["url"].split("/edgar/data/0000320193/")[1].split("/")[0])

    @patch.object(edgar_provider, "requests")
    def test_fetch_filings_caps_at_100(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        mock_session = MagicMock()
        response = MagicMock()
        response.json.return_value = _submissions_payload(
            ["8-K"] * 500
        )
        mock_session.get.return_value = response

        with self._mock_mapping(), patch.object(
            edgar_provider, "_get_session", return_value=mock_session
        ):
            filings = edgar_provider.fetch_filings("AAPL")

        self.assertEqual(len(filings), 100)

    @patch.object(edgar_provider, "requests")
    def test_fetch_filings_uses_ttl_cache(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        mock_session = MagicMock()
        response = MagicMock()
        response.json.return_value = _submissions_payload(["10-Q"])
        mock_session.get.return_value = response

        with self._mock_mapping(), patch.object(
            edgar_provider, "_get_session", return_value=mock_session
        ):
            edgar_provider.fetch_filings("AAPL")
            edgar_provider.fetch_filings("AAPL")

        mock_session.get.assert_called_once()

    @patch.object(edgar_provider, "requests")
    def test_unknown_ticker_yields_empty_state_not_error(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        mock_session = MagicMock()
        with patch.object(
            edgar_provider,
            "_load_ticker_to_cik",
            return_value={"AAPL": 320193},
        ), patch.object(edgar_provider, "_get_session", return_value=mock_session):
            filings = edgar_provider.fetch_filings("SAP.DE")

        self.assertEqual(filings, [])
        mock_session.get.assert_not_called()

    @patch.object(edgar_provider, "requests")
    def test_submissions_failure_serves_stale_cache(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        stale = [{"form": "10-K", "filing_date": "2026-01-01", "report_date": "",
                  "description": "", "url": "x"}]
        edgar_provider._submissions_cache["AAPL"] = (0.0, stale)

        mock_session = MagicMock()
        mock_session.get.side_effect = real_requests.ConnectionError("EDGAR down")
        with self._mock_mapping(), patch.object(
            edgar_provider, "_get_session", return_value=mock_session
        ):
            filings = edgar_provider.fetch_filings("AAPL", force_refresh=True)

        self.assertEqual(filings, stale)


class TestFormFiltering(unittest.TestCase):
    def test_default_filter_shows_common_forms_only(self):
        filings = [
            {"form": "10-K", "filing_date": "2026-01-01"},
            {"form": "4", "filing_date": "2026-01-02"},
            {"form": "PX14A6G", "filing_date": "2026-01-03"},
            {"form": "424B2", "filing_date": "2026-01-04"},
        ]
        visible = edgar_provider.filter_filings_by_form(filings, None)
        self.assertEqual({f["form"] for f in visible}, {"10-K", "4", "424B2"})

    def test_category_filter(self):
        filings = [
            {"form": "10-K", "filing_date": "2026-01-01"},
            {"form": "8-K", "filing_date": "2026-01-02"},
        ]
        quarterly = edgar_provider.filter_filings_by_form(
            filings, "Quarterly (10-Q)"
        )
        self.assertEqual(quarterly, [])
        current = edgar_provider.filter_filings_by_form(filings, "Current (8-K)")
        self.assertEqual(len(current), 1)

    def test_all_shows_everything(self):
        filings = [{"form": "PX14A6G"}, {"form": "4"}]
        self.assertEqual(
            len(edgar_provider.filter_filings_by_form(filings, "All")), 2
        )

    def test_format_rows_handles_missing_description(self):
        rows = format_filing_rows([{"form": "10-K", "filing_date": "2026-01-01"}])
        self.assertEqual(rows[0][2].plain, "—")


class TestFilingsView(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        edgar_provider.reset_caches()

    def tearDown(self):
        edgar_provider.reset_caches()

    async def test_view_renders_and_filters_filings(self):
        class FilingsHostApp(App):
            config = MagicMock()
            config.lists = {"stocks": [{"ticker": "AAPL", "alias": "Apple"}]}
            filings_ticker = None
            fetch_filings = MagicMock()

            def compose(self):
                yield FilingsView()

        app = FilingsHostApp()
        async with app.run_test():
            view = app.query_one(FilingsView)
            view.update_filings(
                [
                    {"form": "10-K", "filing_date": "2026-01-01", "report_date": "2025-09-27",
                     "description": "Annual report", "url": "https://x"},
                    {"form": "8-K", "filing_date": "2026-02-01", "report_date": "",
                     "description": "Current report", "url": "https://y"},
                ]
            )
            table = view.query_one("#filings-table", DataTable)
            self.assertEqual(table.row_count, 2)

            # Filter to annual reports only.
            view._category = "Annual (10-K)"
            view._render_table()
            self.assertEqual(table.row_count, 1)

    async def test_view_shows_empty_state_for_no_filings(self):
        class FilingsHostApp(App):
            config = MagicMock()
            config.lists = {"stocks": [{"ticker": "SAP.DE"}]}
            filings_ticker = None
            fetch_filings = MagicMock()

            def compose(self):
                yield FilingsView()

        app = FilingsHostApp()
        async with app.run_test():
            view = app.query_one(FilingsView)
            view.update_filings([])
            table = view.query_one("#filings-table", DataTable)
            self.assertEqual(table.row_count, 1)

    async def test_submitting_ticker_dispatches_fetch(self):
        class FilingsHostApp(App):
            config = MagicMock()
            config.lists = {"stocks": [{"ticker": "AAPL"}]}
            filings_ticker = None
            fetch_filings = MagicMock()

            def compose(self):
                yield FilingsView()

        app = FilingsHostApp()
        async with app.run_test() as pilot:
            view = app.query_one(FilingsView)
            inp = view.query_one("#filings-ticker-input")
            inp.value = "aapl"
            view.on_filings_ticker_submitted(
                inp.Submitted(inp, "aapl")
            )
            await pilot.pause()
            app.fetch_filings.assert_called_once_with("AAPL")
            self.assertEqual(app.filings_ticker, "AAPL")


if __name__ == "__main__":
    unittest.main()
