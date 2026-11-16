"""Tests for earnings/ex-dividend dates: columns, calendar tab, caching (#5)."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from textual.app import App
from textual.widgets import DataTable

from stockstui.data_providers import market_provider
from stockstui.presentation import formatter
from stockstui.ui.views.calendar_view import CalendarView, collect_upcoming_events
from stockstui.ui.views.watchlist_view import (
    DEFAULT_COLUMN_SETTINGS,
    sort_price_table,
)


class TestEventColumns(unittest.TestCase):
    def test_default_column_settings_include_event_columns_hidden(self):
        keys = {col["key"]: col["visible"] for col in DEFAULT_COLUMN_SETTINGS}
        self.assertIn("Next Earnings", keys)
        self.assertIn("Ex-Div Date", keys)
        self.assertFalse(keys["Next Earnings"])
        self.assertFalse(keys["Ex-Div Date"])

    def test_existing_user_settings_gain_new_columns_via_config_merge(self):
        import json
        import tempfile
        from pathlib import Path

        from stockstui.config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            default_dir = app_root / "default_configs"
            default_dir.mkdir()
            import shutil

            shutil.copy(
                Path("stockstui/default_configs/settings.json"),
                default_dir / "settings.json",
            )
            user_dir = app_root / "user"
            user_dir.mkdir()
            old_settings = {
                "theme": "x",
                "column_settings": [{"key": "Ticker", "visible": True}],
            }
            (user_dir / "settings.json").write_text(json.dumps(old_settings))

            with patch("stockstui.config_manager.PlatformDirs") as mock_dirs:
                mock_dirs.return_value.user_config_dir = str(user_dir)
                config = ConfigManager(app_root=app_root)
            keys = [c["key"] for c in config.settings["column_settings"]]
            self.assertIn("Next Earnings", keys)
            self.assertIn("Ex-Div Date", keys)

    def test_formatter_renders_dates_and_blanks(self):
        rows = formatter.format_price_data_for_table(
            [
                {
                    "symbol": "AAPL",
                    "description": "Apple",
                    "next_earnings": date(2026, 12, 15),
                    "ex_dividend": datetime(2026, 11, 12, 9, 30),
                },
                {
                    "symbol": "SPY",
                    "description": "S&P 500 ETF",
                    "next_earnings": None,
                    "ex_dividend": None,
                },
            ],
            {},
            {},
        )
        self.assertEqual(rows[0]["Next Earnings"], "2026-12-15")
        self.assertEqual(rows[0]["Ex-Div Date"], "2026-11-12")
        # ETFs and crypto show blank, not errors.
        self.assertEqual(rows[1]["Next Earnings"], "")
        self.assertEqual(rows[1]["Ex-Div Date"], "")

    def test_sort_price_table_puts_blank_dates_last(self):
        app = _TableHostApp()

        async def run():
            async with app.run_test():
                table = app.query_one("#price-table", DataTable)
                for col in ("Ticker", "Next Earnings"):
                    table.add_column(col, key=col)
                rows = [
                    {"Ticker": "AAA", "Next Earnings": ""},
                    {"Ticker": "BBB", "Next Earnings": "2026-12-01"},
                    {"Ticker": "CCC", "Next Earnings": "2026-11-20"},
                ]
                from stockstui.ui.views.watchlist_view import (
                    style_and_populate_price_table,
                )

                style_and_populate_price_table(
                    table, rows, ["Ticker", "Next Earnings"], {}
                )
                sort_price_table(table, "Next Earnings", reverse=False)
                ordered = [row.key.value for row in table.ordered_rows]
                self.assertEqual(ordered, ["CCC", "BBB", "AAA"])

        import asyncio

        asyncio.run(run())


class _TableHostApp(App):
    def compose(self):
        yield DataTable(id="price-table")


class TestCalendarProvider(unittest.TestCase):
    def setUp(self):
        market_provider._calendar_cache.clear()
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()

    def tearDown(self):
        market_provider._calendar_cache.clear()
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_calendar_cached_at_most_daily(self, mock_yf_ticker):
        mock_yf_ticker.return_value.calendar = {
            "Earnings Date": [date(2027, 1, 28)],
            "Ex-Dividend Date": date(2026, 11, 12),
        }

        first = market_provider.get_ticker_calendar("AAPL")
        second = market_provider.get_ticker_calendar("AAPL")

        mock_yf_ticker.assert_called_once()
        self.assertEqual(first["earnings_date"], date(2027, 1, 28))
        self.assertEqual(second, first)

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_no_calendar_ticker_returns_none_and_caches_empty(self, mock_yf_ticker):
        mock_yf_ticker.return_value.calendar = {}

        result = market_provider.get_ticker_calendar("BTC-USD")

        # An empty dict means "fetched successfully, no events" — distinct
        # from None, which signals a failed fetch and preserves stale dates.
        self.assertEqual(result, {})
        mock_yf_ticker.assert_called_once()
        # The empty result is cached for the full TTL: repeated refreshes
        # within a day must not re-hit the API.
        market_provider.get_ticker_calendar("BTC-USD")
        mock_yf_ticker.assert_called_once()

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_earliest_earnings_date_is_chosen(self, mock_yf_ticker):
        mock_yf_ticker.return_value.calendar = {
            "Earnings Date": [date(2027, 2, 2), date(2027, 1, 28)]
        }

        result = market_provider.get_ticker_calendar("AAPL")

        self.assertEqual(result["earnings_date"], date(2027, 1, 28))

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_calendar_error_returns_none(self, mock_yf_ticker):
        mock_yf_ticker.return_value.calendar = {}
        type(mock_yf_ticker.return_value).calendar = property(
            lambda self: (_ for _ in ()).throw(Exception("scraper broke"))
        )

        self.assertIsNone(market_provider.get_ticker_calendar("AAPL"))

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_slow_fetch_includes_event_dates_in_cache(self, mock_yf_ticker):
        ticker_obj = MagicMock()
        ticker_obj.info = {
            "currency": "USD",
            "longName": "Apple Inc.",
            "exchange": "NMS",
        }
        ticker_obj.fast_info = {"lastPrice": 155.0}
        ticker_obj.calendar = {
            "Earnings Date": [date(2027, 1, 28)],
            "Ex-Dividend Date": date(2026, 11, 12),
        }
        mock_yf_ticker.return_value = ticker_obj

        market_provider._fetch_and_cache_slow_data(["AAPL"])

        cached = market_provider.get_cached_price("AAPL")
        self.assertEqual(cached["next_earnings"], date(2027, 1, 28))
        self.assertEqual(cached["ex_dividend"], date(2026, 11, 12))

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_slow_fetch_clears_stale_event_dates(self, mock_yf_ticker):
        from datetime import datetime as dt

        market_provider._price_cache["AAPL"] = {
            "expiry": dt.now(timezone.utc) + timedelta(hours=1),
            "data": {"symbol": "AAPL", "price": 150.0, "next_earnings": "old"},
        }
        ticker_obj = MagicMock()
        ticker_obj.info = {"currency": "USD", "exchange": "NMS"}
        ticker_obj.fast_info = {"lastPrice": 155.0}
        ticker_obj.calendar = {}
        mock_yf_ticker.return_value = ticker_obj

        market_provider._fetch_and_cache_slow_data(["AAPL"])

        cached = market_provider.get_cached_price("AAPL")
        self.assertNotIn("next_earnings", cached)


class TestCalendarTab(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        market_provider._price_cache.clear()

    def tearDown(self):
        market_provider._price_cache.clear()

    def test_collect_upcoming_events_sorts_and_filters(self):
        today = date.today()
        market_provider._price_cache["AAPL"] = {
            "data": {
                "symbol": "AAPL",
                "description": "Apple Inc.",
                "next_earnings": today + timedelta(days=5),
                "ex_dividend": today - timedelta(days=2),
            }
        }
        market_provider._price_cache["MSFT"] = {
            "data": {
                "symbol": "MSFT",
                "description": "Microsoft",
                "next_earnings": today + timedelta(days=1),
            }
        }
        market_provider._price_cache["SPY"] = {
            "data": {"symbol": "SPY", "description": "S&P ETF"}
        }

        rows = collect_upcoming_events(
            {
                "stocks": [
                    {"ticker": "AAPL", "alias": "Apple"},
                    {"ticker": "MSFT"},
                ],
                "etf": [{"ticker": "SPY"}],
            },
            set(),
        )

        self.assertEqual(
            [(r["Ticker"], r["Event"]) for r in rows],
            [("MSFT", "Earnings"), ("AAPL", "Earnings")],
        )
        # Alias falls back to the provider description when unset.
        self.assertEqual(rows[0]["Alias"], "Microsoft")
        self.assertEqual(rows[1]["Alias"], "Apple")
        # Past ex-dividend date was dropped; ex-div present date would show.
        self.assertNotIn("Ex-Dividend", [r["Event"] for r in rows])

    def test_collect_events_respects_hidden_tabs(self):
        today = date.today()
        market_provider._price_cache["NVDA"] = {
            "data": {
                "symbol": "NVDA",
                "next_earnings": today + timedelta(days=3),
            }
        }

        rows = collect_upcoming_events(
            {"secret": [{"ticker": "NVDA"}]}, hidden_tabs={"secret"}
        )

        self.assertEqual(rows, [])

    def test_collect_events_skips_uncached_tickers(self):
        rows = collect_upcoming_events(
            {"stocks": [{"ticker": "NOTCACHED"}]}, set()
        )
        self.assertEqual(rows, [])

    async def test_calendar_view_renders_rows(self):
        market_provider._price_cache["AAPL"] = {
            "data": {
                "symbol": "AAPL",
                "description": "Apple",
                "next_earnings": date.today() + timedelta(days=2),
            }
        }

        class CalendarHostApp(App):
            config = MagicMock()
            _hidden_tabs = set()

            def compose(self):
                yield CalendarView()

        app = CalendarHostApp()
        async with app.run_test():
            view = app.query_one(CalendarView)
            view.repopulate()

            table = view.query_one("#calendar-table", DataTable)
            self.assertEqual(table.row_count, 1)


if __name__ == "__main__":
    unittest.main()
