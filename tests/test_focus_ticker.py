"""Shared selected ticker (#23) and context-sensitive footer/help (#25)."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rich.console import Console
from textual.widgets import Input

from stockstui.config_manager import ConfigManager
from stockstui.main import StocksTUI
from stockstui.ui.help_screen import GoToTickerModal, HelpScreen, build_help
from stockstui.ui.ticker_focus import (
    TickerHeader,
    TickerHistory,
    header_text,
    is_single_ticker,
    normalize_ticker,
)
from tests.test_utils import TEST_APP_ROOT

FETCHERS = (
    "fetch_prices",
    "fetch_market_status",
    "fetch_historical_data",
    "fetch_historical_comparison",
    "fetch_news",
    "fetch_filings",
    "fetch_fundamentals",
    "fetch_research",
    "fetch_options_expirations",
    "fetch_options_chain",
)


class TestTickerHistory(unittest.TestCase):
    def test_back_forward_and_truncation(self):
        h = TickerHistory()
        for t in ("AAPL", "MSFT", "NVDA"):
            h.visit(t)
        self.assertEqual(h.back(), "MSFT")
        self.assertEqual(h.back(), "AAPL")
        self.assertIsNone(h.back())
        self.assertEqual(h.forward(), "MSFT")
        # Visiting from the middle drops the forward entries.
        h.visit("TSLA")
        self.assertFalse(h.can_go_forward())
        self.assertEqual(h.back(), "MSFT")

    def test_repeat_visit_is_ignored_and_limit_applies(self):
        h = TickerHistory(limit=3)
        for t in ("A", "A", "B", "C", "D"):
            h.visit(t)
        self.assertEqual(h.current, "D")
        self.assertEqual(h.back(), "C")
        self.assertEqual(h.back(), "B")
        self.assertIsNone(h.back())


class TestTickerHelpers(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_ticker(" aapl "), "AAPL")
        self.assertEqual(normalize_ticker("msft - Microsoft"), "MSFT")
        self.assertIsNone(normalize_ticker("  "))
        self.assertIsNone(normalize_ticker(None))

    def test_single(self):
        self.assertTrue(is_single_ticker("AAPL"))
        self.assertFalse(is_single_ticker("NVDA,AMD"))
        self.assertFalse(is_single_ticker("NVDA/SMH"))
        self.assertFalse(is_single_ticker(""))

    def test_header_with_cached_quote(self):
        app = SimpleNamespace(
            config=SimpleNamespace(lists={"s": [{"ticker": "AAPL", "alias": "Apple"}]}),
            theme_variables={"green": "#00ff00", "red": "#ff0000"},
        )
        quote = {"price": 353.78, "previous_close": 352.37, "currency": "USD"}
        with patch(
            "stockstui.ui.ticker_focus.market_provider.get_cached_price",
            return_value=quote,
        ):
            text = header_text(app, "AAPL")
        self.assertEqual(text.plain, "AAPL · Apple · $353.78 +0.4%")

    def test_header_comparison_and_empty(self):
        app = SimpleNamespace()
        self.assertEqual(header_text(app, "NVDA,AMD").plain, "Comparing NVDA, AMD")
        self.assertIn("No ticker selected", header_text(app, None).plain)


class AppPipelineCase(unittest.IsolatedAsyncioTestCase):
    """Runs the real app with every network fetcher replaced by a mock."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.mocks = {
            name: self.stack.enter_context(patch.object(StocksTUI, name, MagicMock()))
            for name in FETCHERS
        }
        mock_dirs = patch("stockstui.config_manager.PlatformDirs").start()
        mock_dirs.return_value.user_config_dir = str(Path(self.tmpdir.name))
        self.app = StocksTUI(cli_overrides={"tab": "stocks"})
        self.app.config = ConfigManager(app_root=TEST_APP_ROOT.parent)
        patch.stopall()
        self.app.config.lists["stocks"] = [
            {"ticker": "AAPL", "alias": "Apple"},
            {"ticker": "MSFT", "alias": "Microsoft"},
        ]
        self.app.config.settings["hidden_tabs"] = []
        self.app._load_and_register_themes()

    async def asyncTearDown(self):
        self.stack.close()
        self.tmpdir.cleanup()

    async def goto(self, pilot, category):
        self.assertTrue(self.app._activate_category(category), category)
        await pilot.pause()
        await pilot.pause()
        self.assertEqual(self.app.get_active_category(), category)

    def input_value(self, input_id):
        return self.app.query_one(f"#{input_id}", Input).value


class TestSharedTicker(AppPipelineCase):
    async def test_switching_views_keeps_the_ticker(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            self.app.on_main_datatable_row_selected(
                SimpleNamespace(row_key=SimpleNamespace(value="aapl"))
            )
            self.assertEqual(self.app.focus_ticker, "AAPL")

            await self.goto(pilot, "filings")
            self.assertEqual(self.input_value("filings-ticker-input"), "AAPL")
            self.mocks["fetch_filings"].assert_called_with("AAPL")

            await self.goto(pilot, "fundamentals")
            self.assertEqual(self.input_value("fundamentals-ticker-input"), "AAPL")
            self.mocks["fetch_fundamentals"].assert_called_with("AAPL")
            header = self.app.query_one("#fundamentals-view").query_one(TickerHeader)
            self.assertTrue(str(header.render()).startswith("AAPL · Apple"))

            # Returning to a view already on the ticker does not refetch.
            calls = self.mocks["fetch_filings"].call_count
            await self.goto(pilot, "filings")
            self.assertEqual(self.mocks["fetch_filings"].call_count, calls)

    async def test_typing_a_ticker_in_one_view_moves_the_others(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await self.goto(pilot, "filings")
            inp = self.app.query_one("#filings-ticker-input", Input)
            inp.value = "msft"
            await inp.action_submit()
            await pilot.pause()
            self.assertEqual(self.app.focus_ticker, "MSFT")
            self.assertEqual(self.mocks["fetch_filings"].call_count, 1)

            await self.goto(pilot, "options")
            self.assertEqual(self.input_value("options-ticker-input"), "MSFT")
            self.mocks["fetch_options_expirations"].assert_called_with("MSFT")

            # An already-mounted view catches up when it is next shown.
            self.app.set_focus_ticker("AAPL")
            await pilot.pause()
            self.mocks["fetch_options_expirations"].assert_called_with("AAPL")
            await self.goto(pilot, "filings")
            self.mocks["fetch_filings"].assert_called_with("AAPL")

    async def test_history_comparison_stays_local(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            self.app.set_focus_ticker("AAPL")
            await self.goto(pilot, "history")
            inp = self.app.query_one("#history-ticker-input", Input)
            inp.value = "nvda,amd"
            await inp.action_submit()
            await pilot.pause()
            self.assertEqual(self.app.history_ticker, "NVDA,AMD")
            self.assertEqual(self.app.focus_ticker, "AAPL")
            self.assertEqual(self.app.filings_ticker, "AAPL")

            # The next shared-ticker change replaces the comparison.
            self.app.set_focus_ticker("MSFT")
            await pilot.pause()
            self.assertEqual(self.app.history_ticker, "MSFT")
            self.assertEqual(self.input_value("history-ticker-input"), "MSFT")

    async def test_back_and_forward(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            self.app.set_focus_ticker("AAPL")
            self.app.set_focus_ticker("MSFT")
            await self.goto(pilot, "filings")
            self.app.screen.set_focus(None)
            await pilot.press("<")
            await pilot.pause()
            self.assertEqual(self.app.focus_ticker, "AAPL")
            self.mocks["fetch_filings"].assert_called_with("AAPL")
            await pilot.press(">")
            await pilot.pause()
            self.assertEqual(self.app.focus_ticker, "MSFT")

    async def test_goto_ticker_from_watchlist_opens_ticker_view(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await pilot.press("colon")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, GoToTickerModal)
            await pilot.press("n", "v", "d", "a", "enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(self.app.focus_ticker, "NVDA")
            self.assertEqual(self.app.get_active_category(), "history")
            self.assertEqual(self.input_value("history-ticker-input"), "NVDA")

    async def test_open_mode_sets_the_shared_ticker(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            table = self.app.query_one("#price-table")
            table.add_row(*(["AAPL"] + [""] * (len(table.columns) - 1)), key="AAPL")
            table.move_cursor(row=0)
            self.app._open_mode = True
            await self.app.action_handle_open_key("o")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(self.app.focus_ticker, "AAPL")
            self.assertEqual(self.app.get_active_category(), "options")
            self.assertEqual(self.app.fundamentals_ticker, "AAPL")


class TestContextFooter(AppPipelineCase):
    def shown(self):
        return {
            key: b.binding.action
            for key, b in self.app.screen.active_bindings.items()
            if b.binding.show
        }

    async def test_watchlist_keys_only_on_watchlist_tabs(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            shown = self.shown()
            for key in ("s", "o", "a", "t", "f", "r"):
                self.assertIn(key, shown, key)
            self.assertEqual(shown["o"], "enter_open_mode")

            await self.goto(pilot, "news")
            shown = self.shown()
            for key in ("s", "o", "a", "t", "f", "r"):
                self.assertNotIn(key, shown, key)
            self.assertIn("question_mark", shown)

    async def test_view_o_wins_on_filings(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await self.goto(pilot, "filings")
            self.app.query_one("#filings-table").focus()
            await pilot.pause()
            self.assertEqual(self.shown().get("o"), "open_filing")
            # 'o' no longer enters watchlist open mode here.
            await pilot.press("o")
            self.assertFalse(self.app._open_mode)

    async def test_history_shows_its_overlay_keys(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await self.goto(pilot, "history")
            self.app.query_one("#history-range-select").focus()
            await pilot.pause()
            shown = self.shown()
            self.assertEqual(shown.get("ctrl+m"), "toggle_overlay('sma50')")
            self.assertNotIn("o", shown)
            # History keeps sort mode.
            self.assertIn("s", shown)

    async def test_sort_keys_inactive_outside_sort_mode(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            self.assertFalse(self.app.check_action("handle_sort_key", ("p",)))
            self.app._sort_mode = True
            self.assertTrue(self.app.check_action("handle_sort_key", ("p",)))


class TestPerViewHelp(AppPipelineCase):
    async def test_help_matches_the_view(self):
        async with self.app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.assertEqual(self.app.screen._help.title, "Watchlist")
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertNotIsInstance(self.app.screen, HelpScreen)

            await self.goto(pilot, "options")
            self.app.screen.set_focus(None)
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(self.app.screen, HelpScreen)
            self.assertEqual(self.app.screen._help.title, "Options")
            console = Console(width=100)
            with console.capture() as capture:
                console.print(build_help(self.app.screen._help, self.app.screen._live_keys))
            body = capture.get()
            self.assertIn("Previous / next expiry", body)
            self.assertIn("Previous ticker viewed", body)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(self.app.screen, HelpScreen)


if __name__ == "__main__":
    unittest.main()
