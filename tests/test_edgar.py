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

    def test_user_agent_header_carries_the_contact(self):
        """Every request must carry an identifying User-Agent (SEC policy)."""
        edgar_provider.set_contact("Jane Doe jane@example.org")
        try:
            ua = edgar_provider._get_session().headers.get("User-Agent", "")
        finally:
            edgar_provider.set_contact("")
        self.assertIn("stocksTUI", ua)
        self.assertIn("jane@example.org", ua)

    def test_contact_without_an_email_is_rejected(self):
        edgar_provider.set_contact("project maintainer")
        self.assertFalse(edgar_provider.has_contact())
        with self.assertRaises(edgar_provider.EdgarContactMissing):
            edgar_provider._get_session()

    @patch.object(edgar_provider, "requests")
    def test_network_failure_raises_instead_of_reporting_no_cik(self, mock_requests):
        import requests as real_requests

        mock_requests.RequestException = real_requests.RequestException
        mock_session = MagicMock()
        mock_session.get.side_effect = real_requests.ConnectionError("down")
        with patch.object(edgar_provider, "_get_session", return_value=mock_session):
            with self.assertRaises(edgar_provider.EdgarError):
                edgar_provider.get_cik_for_ticker("AAPL")


class TestContactAndErrors(unittest.TestCase):
    def setUp(self):
        edgar_provider.reset_caches()

    def tearDown(self):
        edgar_provider.set_contact("")
        edgar_provider.reset_caches()

    def test_no_contact_raises_without_any_request(self):
        edgar_provider.set_contact("")
        with patch.object(edgar_provider.requests, "Session") as session_cls:
            with self.assertRaises(edgar_provider.EdgarContactMissing):
                edgar_provider.fetch_filings("AAPL")
        session_cls.assert_not_called()

    def test_403_names_the_contact_setting(self):
        response = MagicMock(status_code=403)
        mock_session = MagicMock()
        mock_session.get.return_value = response
        with patch.object(edgar_provider, "_get_session", return_value=mock_session):
            with self.assertRaises(edgar_provider.EdgarError) as ctx:
                edgar_provider.fetch_filings("AAPL")
        self.assertIn("contact", str(ctx.exception))

    def test_outage_is_not_cached_as_an_uncovered_ticker(self):
        import requests as real_requests

        ok_mapping = MagicMock(status_code=200)
        ok_mapping.json.return_value = {"0": {"ticker": "AAPL", "cik_str": 320193}}
        ok_submissions = MagicMock(status_code=200)
        ok_submissions.json.return_value = _submissions_payload(["10-K"])
        mock_session = MagicMock()
        mock_session.get.side_effect = [
            real_requests.ConnectionError("down"),
            ok_mapping,
            ok_submissions,
        ]
        with patch.object(edgar_provider, "_get_session", return_value=mock_session):
            with self.assertRaises(edgar_provider.EdgarError):
                edgar_provider.fetch_filings("AAPL")
            filings = edgar_provider.fetch_filings("AAPL")
        self.assertEqual([f["form"] for f in filings], ["10-K"])

    def test_stale_list_is_served_when_a_refresh_fails(self):
        import requests as real_requests

        edgar_provider._submissions_cache["AAPL"] = (0.0, [{"form": "10-K"}])
        mock_session = MagicMock()
        mock_session.get.side_effect = real_requests.ConnectionError("down")
        with patch.object(
            edgar_provider, "_load_ticker_to_cik", return_value={"AAPL": 320193}
        ), patch.object(edgar_provider, "_get_session", return_value=mock_session):
            self.assertEqual(edgar_provider.fetch_filings("AAPL"), [{"form": "10-K"}])


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
        self.assertEqual(rows[0][3].plain, "—")


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
            # The empty state is a note above the table, not a fake row (#22).
            self.assertEqual(table.row_count, 0)
            note = view.query_one("#filings-note")
            self.assertTrue(note.display)
            self.assertIn("No SEC filings", str(note.render()))

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

    async def test_o_binding_opens_selected_filing(self):
        """'o Open' on the Filings tab opens the highlighted filing (#10)."""
        opened: list[str] = []

        class FilingsHostApp(App):
            config = MagicMock()
            config.lists = {"stocks": [{"ticker": "AAPL"}]}
            filings_ticker = None
            fetch_filings = MagicMock()
            notify = MagicMock()

            def compose(self):
                yield FilingsView()

        app = FilingsHostApp()
        async with app.run_test() as pilot:
            view = app.query_one(FilingsView)
            view.update_filings(
                [
                    {"form": "10-K", "filing_date": "2026-01-01",
                     "report_date": "", "description": "Annual", "url": "https://sec.gov/x"},
                    {"form": "8-K", "filing_date": "2026-02-01",
                     "report_date": "", "description": "Current", "url": "https://sec.gov/y"},
                ]
            )
            table = view.query_one("#filings-table")
            table.focus()
            await pilot.pause()
            table.move_cursor(row=1)
            await pilot.pause()

            with patch("stockstui.ui.views.filings_view.webbrowser.open") as mock_open:
                mock_open.side_effect = lambda url: opened.append(url)
                await pilot.press("o")
                await pilot.pause()

            mock_open.assert_called_once_with("https://sec.gov/y")

    async def test_o_binding_with_no_selection_notifies(self):
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
            view.update_filings(
                [{"form": "10-K", "filing_date": "2026-01-01",
                  "report_date": "", "description": "", "url": "https://sec.gov/x"}]
            )
            await pilot.press("o")
            await pilot.pause()
            # No crash, no browser: cursor defaults to row -1 until focus.
            self.assertEqual(app.filings_ticker, None)


if __name__ == "__main__":
    unittest.main()


class TestFilingsViewErrors(unittest.IsolatedAsyncioTestCase):
    async def test_error_is_shown_instead_of_the_empty_state(self):
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
            view.update_filings(None, edgar_provider.CONTACT_MISSING_MESSAGE)
            table = view.query_one("#filings-table", DataTable)
            self.assertEqual(table.row_count, 0)
            text = str(view.query_one("#filings-note").render())
            self.assertIn("SEC Contact", text)
            self.assertNotIn("Non-US", text)


class TestContactFromConfig(unittest.TestCase):
    def tearDown(self):
        edgar_provider.set_contact("")

    def _make_app(self, tmpdir, sec_contact, env):
        import json
        import os
        from pathlib import Path

        from stockstui import main as main_module

        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "settings.json").write_text(json.dumps({"sec_contact": sec_contact}))
        with patch("stockstui.config_manager.dirs") as mock_dirs, patch.dict(
            os.environ, env, clear=False
        ):
            os.environ.pop("STOCKSTUI_SEC_CONTACT", None) if not env else None
            mock_dirs.user_config_dir = str(config_dir)
            mock_dirs.user_cache_dir = str(Path(tmpdir) / "cache")
            main_module.StocksTUI(cli_overrides={})
        return main_module.edgar_provider

    def test_setting_is_applied_at_startup(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            provider = self._make_app(tmp, "Ann Lee ann@example.org", {})
        self.assertIn("ann@example.org", provider._user_agent())

    def test_environment_variable_wins(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            provider = self._make_app(
                tmp,
                "Ann Lee ann@example.org",
                {"STOCKSTUI_SEC_CONTACT": "Bo Kim bo@example.org"},
            )
        self.assertIn("bo@example.org", provider._user_agent())
