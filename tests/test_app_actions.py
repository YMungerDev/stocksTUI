import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

# Import the real exception Textual raises when a query finds nothing.
from textual.actions import SkipAction
from textual.css.query import NoMatches
from textual.widgets import ContentSwitcher

from stockstui.main import StocksTUI
from stockstui.ui.views.config_view import ConfigContainer
from stockstui.ui.widgets.search_box import SearchBox
from tests.test_utils import create_test_app


class TestAppRefreshActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await create_test_app()

    def test_action_refresh_smart(self):
        self.app.get_active_category = MagicMock(return_value="stocks")
        self.app.config.lists = {"stocks": [{"ticker": "AAPL"}, {"ticker": "TSLA"}]}
        self.app._filter_symbols_by_tags = MagicMock(side_effect=lambda c, s: s)

        self.app.action_refresh(force=False)
        self.app.fetch_prices.assert_called_once_with(
            ["AAPL", "TSLA"],
            force=False,
            category="stocks",
            enable_pre_post_market=False,
        )

    def test_action_refresh_force(self):
        self.app.get_active_category = MagicMock(return_value="crypto")
        self.app.config.lists = {"crypto": [{"ticker": "BTC-USD"}]}
        self.app._filter_symbols_by_tags = MagicMock(side_effect=lambda c, s: s)

        self.app.action_refresh(force=True)
        self.app.fetch_prices.assert_called_once_with(
            ["BTC-USD"], force=True, category="crypto", enable_pre_post_market=False
        )

    def test_action_refresh_with_tag_filter(self):
        self.app.get_active_category = MagicMock(return_value="stocks")
        self.app.config.lists = {"stocks": [{"ticker": "AAPL"}, {"ticker": "TSLA"}]}
        self.app._filter_symbols_by_tags = MagicMock(return_value=["TSLA"])

        self.app.action_refresh(force=False)
        self.app.fetch_prices.assert_called_once_with(
            ["TSLA"], force=False, category="stocks", enable_pre_post_market=False
        )

    def test_action_refresh_all_category(self):
        self.app.get_active_category = MagicMock(return_value="all")
        self.app.config.lists = {
            "stocks": [{"ticker": "AAPL"}, {"ticker": "TSLA"}],
            "indices": [{"ticker": "TSLA"}, {"ticker": "^GSPC"}],
        }
        self.app._filter_symbols_by_tags = MagicMock(side_effect=lambda c, s: s)

        self.app.action_refresh(force=False)
        expected_symbols = ["AAPL", "TSLA", "^GSPC"]
        self.app.fetch_prices.assert_called_once_with(
            expected_symbols, force=False, category="all", enable_pre_post_market=False
        )


class TestAppSortActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await create_test_app()

    def test_action_enter_sort_mode(self):
        """Entering sort mode should set _sort_mode and update the status label."""
        self.app.get_active_category = MagicMock(return_value="stocks")

        # Mock the status label that gets updated
        mock_label = MagicMock()
        self.app.query_one = MagicMock(return_value=mock_label)

        self.app.action_enter_sort_mode()

        # Check that sort mode is enabled and status label was updated
        self.assertTrue(self.app._sort_mode)
        mock_label.update.assert_called_once_with(
            "SORT BY: \[d]escription, \[p]rice, \[c]hange, p\[e]rcent, \[t]icker, \[u]ndo, \[ESC]ape"
        )

    def test_action_enter_sort_mode_on_invalid_tab(self):
        self.app.get_active_category = MagicMock(return_value="news")
        self.app.action_enter_sort_mode()
        # Expecting a bell or some form of feedback on invalid context.
        self.app.bell.assert_called_once()


class TestAppNavigationActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await create_test_app()

    def test_action_back_or_dismiss_clears_sort_mode(self):
        # This test keeps behavior validation local and simple.
        self.app._sort_mode = True
        self.app.get_active_category = MagicMock(return_value="stocks")
        self.app.action_back_or_dismiss()
        self.assertFalse(self.app._sort_mode)

    def test_action_back_or_dismiss_removes_search_box(self):
        """When a SearchBox is present, dismiss should remove it."""
        # Provide a fake mounted SearchBox the app can 'find' and remove.
        mock_search_box = MagicMock(spec=SearchBox)
        self.app.query_one = MagicMock(return_value=mock_search_box)

        self.app.action_back_or_dismiss()

        # We don't rely on Textual's async remove semantics in tests; just assert it's invoked.
        mock_search_box.remove.assert_called_once()

    def test_action_back_or_dismiss_navigates_config_view(self):
        """If no SearchBox is mounted and the current view is Configs, delegate to go_back."""
        self.app.get_active_category = MagicMock(return_value="configs")

        mock_container = MagicMock(spec=ConfigContainer)
        mock_container.action_go_back.return_value = True

        # First call: looking for SearchBox -> raise the *real* NoMatches
        # Second call: looking for ConfigContainer -> return the container
        self.app.query_one = MagicMock(
            side_effect=[NoMatches("no SearchBox"), mock_container]
        )

        self.app.action_back_or_dismiss()

        mock_container.action_go_back.assert_called_once()


class TestAppFilterActions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await create_test_app()
        self.app.get_active_category = MagicMock(return_value="stocks")

    def test_action_toggle_tag_filter_with_no_tags(self):
        # Provide a TagFilter-like object with no tags available.
        mock_tag_filter = MagicMock()
        mock_tag_filter.available_tags = []
        mock_tag_filter.display = False
        self.app.query_one = MagicMock(return_value=mock_tag_filter)

        self.app.action_toggle_tag_filter()

        self.app.notify.assert_called_once_with(
            "No tags available for this list.", severity="information"
        )
        self.app.bell.assert_called_once()
        self.assertFalse(mock_tag_filter.display)


class TestGetActiveCategory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = await create_test_app()

    def test_cache_fast_path_bypasses_query(self):
        """When _last_active_category is already set, the live query must be skipped entirely."""
        self.app._last_active_category = "stocks"
        self.app.query_one = MagicMock()

        result = self.app.get_active_category()

        self.assertEqual(result, "stocks")
        self.app.query_one.assert_not_called()

    def test_returns_correct_category_for_valid_tab_map(self):
        """Happy path: ensure correct category is returned when tab_map is valid."""
        self.app.tab_map = [
            {"name": "All", "category": "all"},
            {"name": "Stocks", "category": "stocks"},
            {"name": "Crypto", "category": "crypto"},
        ]
        self.app._last_active_category = None

        mock_tabs = MagicMock()
        mock_tabs.active = "tab-2"  # Index 1 → "stocks"
        self.app.query_one = MagicMock(return_value=mock_tabs)

        result = self.app.get_active_category()
        self.assertEqual(result, "stocks")


class _IsolatedAppMixin:
    def _make_app(self, tmpdir: str, cli_overrides: dict | None = None) -> StocksTUI:
        with patch("stockstui.config_manager.dirs") as mock_dirs:
            mock_dirs.user_config_dir = str(Path(tmpdir) / "config")
            mock_dirs.user_cache_dir = str(Path(tmpdir) / "cache")
            return StocksTUI(cli_overrides=cli_overrides or {})


class TestSortModeKeybindings(unittest.IsolatedAsyncioTestCase, _IsolatedAppMixin):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_sort_mode_skip_actions_raise_skip_action(self):
        app = self._make_app(self.tmpdir.name)
        app._sort_mode = True
        with self.assertRaises(SkipAction):
            await app.action_enter_open_mode()
        with self.assertRaises(SkipAction):
            app.action_edit_ticker_quick()
        with self.assertRaises(SkipAction):
            app.action_screen()
        with self.assertRaises(SkipAction):
            app.action_manage_alerts()
        app.db_manager.close()

    async def test_sort_mode_t_and_e_trigger_sort_on_watchlist(self):
        with (
            patch("stockstui.main.market_provider.get_market_price_data", return_value=[]),
            patch(
                "stockstui.main.market_provider.get_market_status",
                return_value={"status": "closed"},
            ),
            patch("stockstui.main.market_provider.refresh_daily_bars"),
        ):
            app = self._make_app(self.tmpdir.name, cli_overrides={"tab": "stocks"})
            async with app.run_test() as pilot:
                await pilot.pause()
                price_table = app.query_one("#price-table")
                price_table.focus()
                await pilot.pause()

                with (
                    patch.object(app, "_set_and_apply_sort") as mock_sort,
                    patch.object(app, "push_screen") as mock_push_screen,
                ):
                    await pilot.press("s")
                    self.assertTrue(app._sort_mode)

                    await pilot.press("t")
                    mock_sort.assert_called_once_with("Ticker", "key 't'")
                    mock_push_screen.assert_not_called()
                    self.assertFalse(app._sort_mode)

                    mock_sort.reset_mock()
                    await pilot.press("s")
                    self.assertTrue(app._sort_mode)

                    await pilot.press("e")
                    mock_sort.assert_called_once_with("% Change", "key 'e'")
                    mock_push_screen.assert_not_called()
                    self.assertFalse(app._sort_mode)

    async def test_sort_mode_o_H_L_trigger_history_sort(self):
        with (
            patch("stockstui.main.market_provider.get_market_price_data", return_value=[]),
            patch(
                "stockstui.main.market_provider.get_market_status",
                return_value={"status": "closed"},
            ),
            patch("stockstui.main.market_provider.refresh_daily_bars"),
        ):
            app = self._make_app(self.tmpdir.name, cli_overrides={"tab": "history"})
            async with app.run_test() as pilot:
                await pilot.pause()
                idx = pd.bdate_range("2026-01-01", periods=2)
                app._last_historical_data = pd.DataFrame(
                    {
                        "Open": [10.0, 12.0],
                        "High": [15.0, 14.0],
                        "Low": [9.0, 11.0],
                        "Close": [11.0, 13.0],
                        "Volume": [100, 200],
                    },
                    index=idx,
                )
                history_view = app.query_one("#history-view")
                await history_view._render_historical_data()
                await pilot.pause()
                app.query_one("#history-table").focus()
                await pilot.pause()

                with patch.object(app, "_set_and_apply_history_sort") as mock_hist_sort:
                    for key, column_name in [("o", "Open"), ("H", "High"), ("L", "Low")]:
                        mock_hist_sort.reset_mock()
                        await pilot.press("s")
                        self.assertTrue(app._sort_mode)

                        await pilot.press(key)
                        mock_hist_sort.assert_called_once_with(
                            column_name, f"key '{key}'"
                        )
                        self.assertFalse(app._open_mode)
                        self.assertFalse(app._sort_mode)


class TestWorkerGroupIsolation(unittest.IsolatedAsyncioTestCase, _IsolatedAppMixin):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_fetch_fundamentals_and_research_do_not_cancel_each_other(self):
        fundamentals_started = threading.Event()
        allow_finish = threading.Event()

        def fake_fundamentals(ticker, db_manager):
            fundamentals_started.set()
            allow_finish.wait(timeout=2.0)
            return {"income": [{"period": "2025"}]}

        def fake_research(ticker, db_manager):
            self.assertTrue(
                fundamentals_started.wait(timeout=2.0),
                "Expected fundamentals worker to still be running concurrently",
            )
            return {"analyst": []}

        with (
            patch("stockstui.main.market_provider.get_market_price_data", return_value=[]),
            patch(
                "stockstui.main.market_provider.get_market_status",
                return_value={"status": "closed"},
            ),
            patch("stockstui.main.market_provider.refresh_daily_bars"),
            patch(
                "stockstui.main.fundamentals_provider.get_statements",
                side_effect=fake_fundamentals,
            ) as mock_fund,
            patch(
                "stockstui.main.research_provider.get_research",
                side_effect=fake_research,
            ) as mock_res,
        ):
            app = self._make_app(self.tmpdir.name)
            async with app.run_test() as pilot:
                await pilot.pause()
                w_fund = app.fetch_fundamentals("AAPL")
                w_res = app.fetch_research("AAPL")

                self.assertEqual(w_fund.group, "fundamentals")
                self.assertEqual(w_res.group, "research")

                await w_res.wait()
                allow_finish.set()
                await w_fund.wait()

                self.assertFalse(w_fund.is_cancelled)
                self.assertFalse(w_res.is_cancelled)
                mock_fund.assert_called_once_with("AAPL", app.db_manager)
                mock_res.assert_called_once_with("AAPL", app.db_manager)

    async def test_top_up_daily_bars_redisplays_price_table(self):
        with (
            patch("stockstui.main.market_provider.get_market_price_data", return_value=[]),
            patch(
                "stockstui.main.market_provider.get_market_status",
                return_value={"status": "closed"},
            ),
            patch("stockstui.main.market_provider.refresh_daily_bars") as mock_refresh,
        ):
            app = self._make_app(self.tmpdir.name, cli_overrides={"tab": "stocks"})
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch.object(app, "_redisplay_price_table") as mock_redisplay:
                    worker = app._top_up_daily_bars()
                    self.assertEqual(worker.group, "daily_bars")
                    await worker.wait()
                    await pilot.pause()
                    self.assertGreaterEqual(mock_refresh.call_count, 1)
                    mock_redisplay.assert_called()


class TestHistoricalComparisonRatio(unittest.IsolatedAsyncioTestCase, _IsolatedAppMixin):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_fetch_historical_comparison_expands_ratio_tickers(self):
        idx = pd.bdate_range("2026-01-01", periods=3)
        frames = {
            "AAPL": pd.DataFrame({"Close": [100.0, 120.0, 150.0]}, index=idx),
            "MSFT": pd.DataFrame({"Close": [50.0, 50.0, 50.0]}, index=idx),
            "NVDA": pd.DataFrame({"Close": [200.0, 220.0, 240.0]}, index=idx),
        }

        def fake_hist(symbol, period, interval="1d"):
            if symbol not in frames:
                raise AssertionError(f"Unexpected ticker lookup: {symbol}")
            return frames[symbol]

        with (
            patch("stockstui.main.market_provider.get_market_price_data", return_value=[]),
            patch(
                "stockstui.main.market_provider.get_market_status",
                return_value={"status": "closed"},
            ),
            patch("stockstui.main.market_provider.refresh_daily_bars"),
            patch(
                "stockstui.main.market_provider.get_historical_data",
                side_effect=fake_hist,
            ) as mock_hist,
        ):
            app = self._make_app(self.tmpdir.name, cli_overrides={"tab": "history"})
            async with app.run_test() as pilot:
                await pilot.pause()
                worker = app.fetch_historical_comparison(["AAPL/MSFT"], "1mo")
                self.assertEqual(worker.group, "history")
                await worker.wait()
                await pilot.pause()

                fetched = [call.args[0] for call in mock_hist.call_args_list]
                self.assertEqual(fetched, ["AAPL", "MSFT"])
                df = app._last_historical_data
                self.assertIsNotNone(df)
                self.assertEqual(list(df.columns), ["AAPL/MSFT"])
                self.assertEqual(df.attrs.get("comparison_tickers"), ["AAPL/MSFT"])
                self.assertAlmostEqual(df["AAPL/MSFT"].iloc[0], 100.0)
                self.assertAlmostEqual(df["AAPL/MSFT"].iloc[1], 120.0)
                self.assertAlmostEqual(df["AAPL/MSFT"].iloc[2], 150.0)

                mock_hist.reset_mock()
                worker2 = app.fetch_historical_comparison(["NVDA", "AAPL/MSFT"], "1mo")
                await worker2.wait()
                await pilot.pause()
                df2 = app._last_historical_data
                self.assertEqual(list(df2.columns), ["NVDA", "AAPL/MSFT"])


class TestFredConfigSubView(unittest.IsolatedAsyncioTestCase, _IsolatedAppMixin):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    async def test_rebuild_app_shows_fred_config_sub_view(self):
        with (
            patch("stockstui.main.market_provider.get_market_price_data", return_value=[]),
            patch(
                "stockstui.main.market_provider.get_market_status",
                return_value={"status": "closed"},
            ),
            patch("stockstui.main.market_provider.refresh_daily_bars"),
        ):
            app = self._make_app(self.tmpdir.name)
            async with app.run_test() as pilot:
                await pilot.pause()
                await app._rebuild_app("configs", config_sub_view="fred")
                await pilot.pause()
                config_container = app.query_one(ConfigContainer)
                self.assertEqual(
                    config_container.query_one(ContentSwitcher).current,
                    "fred",
                )

