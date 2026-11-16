"""Tests for history chart overlays: computation, toggles, keybindings (#8)."""

import unittest
from datetime import date

import pandas as pd

from stockstui.presentation.overlays import (
    drawdown_from_peak,
    earnings_dates_in_range,
    moving_average,
    relative_volume_series,
)


def _close_frame(values: list[float]) -> pd.DataFrame:
    idx = pd.bdate_range("2026-01-01", periods=len(values))
    return pd.DataFrame(
        {
            "Open": values,
            "High": [v + 1 for v in values],
            "Low": [v - 1 for v in values],
            "Close": values,
            "Volume": [1000.0 + i for i in range(len(values))],
        },
        index=idx,
    )


class TestMovingAverage(unittest.TestCase):
    def test_hand_checked_sma(self):
        closes = pd.Series([10.0, 20.0, 30.0])
        sma = moving_average(closes, 2)
        # min_periods=2: first value NaN, then 15, 25.
        self.assertIsNone(sma.iloc[0] if pd.isna(sma.iloc[0]) else None) if False else self.assertTrue(pd.isna(sma.iloc[0]))
        self.assertAlmostEqual(sma.iloc[1], 15.0)
        self.assertAlmostEqual(sma.iloc[2], 25.0)

    def test_no_truncated_average(self):
        """Fewer bars than the window -> None, never a partial average."""
        closes = pd.Series([10.0, 20.0, 30.0])
        self.assertIsNone(moving_average(closes, 50))

    def test_sma200_needs_200_bars(self):
        closes = pd.Series([100.0] * 199)
        self.assertIsNone(moving_average(closes, 200))
        closes = pd.Series([100.0] * 200)
        sma = moving_average(closes, 200)
        self.assertAlmostEqual(sma.iloc[-1], 100.0)


class TestDrawdown(unittest.TestCase):
    def test_hand_checked(self):
        closes = pd.Series([100.0, 120.0, 90.0, 110.0])
        dd = drawdown_from_peak(closes)
        self.assertAlmostEqual(dd.iloc[0], 0.0)
        self.assertAlmostEqual(dd.iloc[1], 0.0)  # at peak
        self.assertAlmostEqual(dd.iloc[2], 90 / 120 - 1)  # -25%
        self.assertAlmostEqual(dd.iloc[3], 110 / 120 - 1)  # still below peak

    def test_never_positive(self):
        closes = pd.Series([100.0, 110.0, 105.0, 130.0])
        self.assertTrue((drawdown_from_peak(closes) <= 0).all())


class TestRelativeVolumeSeries(unittest.TestCase):
    def test_hand_checked(self):
        volumes = pd.Series([100.0] * 21 + [300.0])
        rv = relative_volume_series(volumes)
        # Last bar: 300 / mean(previous 20 = 100) = 3x
        self.assertAlmostEqual(rv.iloc[-1], 3.0)
        # Earlier bars have no full 20-bar history -> NaN
        self.assertTrue(pd.isna(rv.iloc[0]))


class TestEarningsMarkers(unittest.TestCase):
    def test_cached_date_inside_range(self):
        frame = _close_frame([100.0] * 5)
        frame.attrs["earnings_date"] = date(2026, 1, 3)
        found = earnings_dates_in_range(frame, date(2026, 1, 1), date(2026, 1, 10))
        self.assertEqual(found, [date(2026, 1, 3)])

    def test_cached_date_outside_range_is_dropped(self):
        frame = _close_frame([100.0] * 5)
        frame.attrs["earnings_date"] = "2026-02-01"  # ISO string form
        found = earnings_dates_in_range(frame, date(2026, 1, 1), date(2026, 1, 10))
        self.assertEqual(found, [])

    def test_cached_iso_string_inside_range(self):
        frame = _close_frame([100.0] * 5)
        frame.attrs["earnings_date"] = "2026-01-03"
        found = earnings_dates_in_range(frame, date(2026, 1, 1), date(2026, 1, 10))
        self.assertEqual(found, [date(2026, 1, 3)])

    def test_no_cache_is_empty(self):
        frame = _close_frame([100.0] * 5)
        self.assertEqual(earnings_dates_in_range(frame, date(2026, 1, 1), date(2026, 1, 10)), [])


class TestChartWidgetToggles(unittest.IsolatedAsyncioTestCase):
    async def test_overlays_toggle_and_render(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from stockstui.ui.widgets.history_chart import HistoryChart

        data = _close_frame([100.0 + i for i in range(260)])

        class HostApp(App):
            theme_variables = {
                "orange": "#d65d0e",
                "accent": "blue",
                "text-muted": "dim",
                "success": "green",
                "error": "red",
                "warning": "yellow",
            }

            def compose(self):
                yield HistoryChart(data, period="1y")

        app = HostApp()
        async with app.run_test():
            chart = app.query_one(HistoryChart)
            for key in ("sma50", "sma200", "volume", "drawdown", "earnings"):
                self.assertFalse(chart._overlay_states[key])
                chart.toggle_overlay(key)
                self.assertTrue(chart._overlay_states[key])
            # Toggle off again.
            chart.toggle_overlay("sma200")
            self.assertFalse(chart._overlay_states["sma200"])

    async def test_unknown_overlay_is_ignored(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from stockstui.ui.widgets.history_chart import HistoryChart

        class HostApp(App):
            theme_variables = {}

            def compose(self):
                yield HistoryChart(_close_frame([100.0, 101.0]), period="5d")

        app = HostApp()
        async with app.run_test():
            chart = app.query_one(HistoryChart)
            chart.toggle_overlay("nonexistent")  # must not raise

    async def test_short_data_toggles_do_not_crash(self):
        """sma200 on a 5-day window renders nothing extra and survives."""
        from textual.app import App
        from stockstui.ui.widgets.history_chart import HistoryChart

        class HostApp(App):
            theme_variables = {"orange": "orange"}

            def compose(self):
                yield HistoryChart(_close_frame([100.0, 101.0]), period="5d")

        app = HostApp()
        async with app.run_test():
            chart = app.query_one(HistoryChart)
            chart.toggle_overlay("sma200")
            chart.toggle_overlay("volume")
            chart.toggle_overlay("drawdown")


class TestHistoryViewKeybindings(unittest.IsolatedAsyncioTestCase):
    async def test_overlay_keybindings_toggle_chart(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from stockstui.ui.views.history_view import HistoryView
        from stockstui.ui.widgets.history_chart import HistoryChart

        data = _close_frame([100.0 + i for i in range(260)])

        class HostApp(App):
            theme_variables = {}
            history_ticker = None
            config = MagicMock()
            cli_overrides = {}
            _history_period = "1y"
            _last_historical_data = data
            _history_sort_column_key = None
            _history_sort_reverse = False
            notify = MagicMock()

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            # The chart mounts only after toggling to chart view.
            view.query_one("#history-view-toggle").value = True
            await pilot.pause()
            chart = view.query_one(HistoryChart)
            await pilot.press("ctrl+m")
            self.assertTrue(chart._overlay_states["sma50"])
            await pilot.press("ctrl+t")
            self.assertTrue(chart._overlay_states["volume"])
            await pilot.press("ctrl+o")
            self.assertTrue(chart._overlay_states["drawdown"])
            await pilot.press("ctrl+y")
            self.assertTrue(chart._overlay_states["earnings"])
            await pilot.press("ctrl+n")
            self.assertTrue(chart._overlay_states["sma200"])
            # Pressing again toggles off.
            await pilot.press("ctrl+m")
            self.assertFalse(chart._overlay_states["sma50"])


class TestHistoryViewOverlayPersistence(unittest.IsolatedAsyncioTestCase):
    async def test_sma50_refetch_and_overlay_state_preserved_across_remount(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from stockstui.ui.views.history_view import HistoryView
        from stockstui.ui.widgets.history_chart import HistoryChart

        short_data = _close_frame([100.0 + i for i in range(20)])

        class HostApp(App):
            theme_variables = {}
            history_ticker = "AAPL"
            config = MagicMock()
            cli_overrides = {}
            _history_period = "1mo"
            _last_historical_data = short_data
            _history_sort_column_key = None
            _history_sort_reverse = False
            fetch_historical_data = MagicMock()

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            await pilot.pause()
            app.fetch_historical_data.reset_mock()

            view.query_one("#history-view-toggle").value = True
            await pilot.pause()

            await pilot.press("ctrl+t")
            await pilot.press("ctrl+m")
            await pilot.pause()

            self.assertTrue(view._overlay_states["volume"])
            self.assertTrue(view._overlay_states["sma50"])
            app.fetch_historical_data.assert_called_once_with("AAPL", "6mo", "1d")

            # Simulate wider data arriving and remounting HistoryChart.
            app._last_historical_data = _close_frame([100.0 + i for i in range(130)])
            app._history_period = "6mo"
            await view._render_historical_data()
            await pilot.pause()

            remounted = view.query_one(HistoryChart)
            self.assertTrue(remounted._overlay_states["sma50"])
            self.assertTrue(remounted._overlay_states["volume"])

    async def test_sma200_refetch_and_overlay_state_preserved_across_remount(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from stockstui.ui.views.history_view import HistoryView
        from stockstui.ui.widgets.history_chart import HistoryChart

        medium_data = _close_frame([100.0 + i for i in range(120)])

        class HostApp(App):
            theme_variables = {}
            history_ticker = "AAPL"
            config = MagicMock()
            cli_overrides = {}
            _history_period = "6mo"
            _last_historical_data = medium_data
            _history_sort_column_key = None
            _history_sort_reverse = False
            fetch_historical_data = MagicMock()

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            await pilot.pause()
            app.fetch_historical_data.reset_mock()

            view.query_one("#history-view-toggle").value = True
            await pilot.pause()

            await pilot.press("ctrl+n")
            await pilot.pause()

            self.assertTrue(view._overlay_states["sma200"])
            app.fetch_historical_data.assert_called_once_with("AAPL", "1y", "1d")

            # Simulate 1y data arriving and remounting HistoryChart.
            app._last_historical_data = _close_frame([100.0 + i for i in range(250)])
            app._history_period = "1y"
            await view._render_historical_data()
            await pilot.pause()

            remounted = view.query_one(HistoryChart)
            self.assertTrue(remounted._overlay_states["sma200"])

    async def test_request_historical_data_widens_period_without_mounted_chart(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from stockstui.ui.views.history_view import HistoryView

        class HostApp(App):
            theme_variables = {}
            history_ticker = "AAPL"
            config = MagicMock()
            cli_overrides = {}
            _history_period = "1mo"
            _last_historical_data = _close_frame([100.0, 101.0])
            _history_sort_column_key = None
            _history_sort_reverse = False
            fetch_historical_data = MagicMock()

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            await pilot.pause()
            app.fetch_historical_data.reset_mock()

            # View is still in table mode (no HistoryChart mounted).
            view._overlay_states["sma50"] = True
            view._request_historical_data()
            app.fetch_historical_data.assert_called_once_with("AAPL", "6mo", "1d")

            # Also verify 1D radio selection widens to 6mo daily bars with sma50 active.
            app.fetch_historical_data.reset_mock()
            radio_set = view.query_one("#history-range-select")
            for btn in radio_set.query("RadioButton"):
                btn.value = str(btn.label) == "1D"
            await pilot.pause()
            app.fetch_historical_data.reset_mock()
            view._request_historical_data()
            app.fetch_historical_data.assert_called_once_with("AAPL", "6mo", "1d")

            app.fetch_historical_data.reset_mock()
            view._overlay_states["sma200"] = True
            view._request_historical_data()
            app.fetch_historical_data.assert_called_once_with("AAPL", "1y", "1d")


class TestHistoryEarningsAttachment(unittest.TestCase):
    def setUp(self):
        from stockstui.data_providers import market_provider

        self._orig_price = dict(market_provider._price_cache)
        self._orig_info = dict(market_provider._info_cache)
        self._orig_cal = dict(market_provider._calendar_cache)
        self._orig_db = market_provider._db_manager
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        market_provider._calendar_cache.clear()
        market_provider.set_bar_store(None)

    def tearDown(self):
        from stockstui.data_providers import market_provider

        market_provider._price_cache.clear()
        market_provider._price_cache.update(self._orig_price)
        market_provider._info_cache.clear()
        market_provider._info_cache.update(self._orig_info)
        market_provider._calendar_cache.clear()
        market_provider._calendar_cache.update(self._orig_cal)
        market_provider.set_bar_store(self._orig_db)

    def test_network_path_attaches_earnings_from_price_cache(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from stockstui.data_providers import market_provider

        market_provider._info_cache["AAPL"] = {"currency": "USD"}
        market_provider._price_cache["AAPL"] = {
            "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
            "data": {"symbol": "AAPL", "next_earnings": "2026-01-15"},
        }
        raw = _close_frame([100.0, 101.0])
        with patch("stockstui.data_providers.market_provider.yf.Ticker") as mock_tkr:
            mock_tkr.return_value.history.return_value = raw
            df = market_provider.get_historical_data("AAPL", "1mo", "1d")

        self.assertEqual(df.attrs.get("earnings_date"), "2026-01-15")

    def test_local_store_path_falls_back_to_calendar_cache(self):
        import tempfile
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from stockstui.data_providers import market_provider
        from stockstui.database.db_manager import DbManager

        with tempfile.TemporaryDirectory() as tmpdir:
            dbm = DbManager(Path(tmpdir) / "bars.db")
            try:
                market_provider.set_bar_store(dbm)
                recent_start = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
                    "%Y-%m-%d"
                )
                idx = pd.to_datetime(
                    [pd.Timestamp(recent_start) + pd.Timedelta(days=i) for i in range(3)]
                ).tz_localize("UTC")
                bars = pd.DataFrame(
                    {
                        "Open": [100.0, 101.0, 102.0],
                        "High": [101.0, 102.0, 103.0],
                        "Low": [99.0, 100.0, 101.0],
                        "Close": [100.5, 101.5, 102.5],
                        "Volume": [1000, 1001, 1002],
                    },
                    index=idx,
                )
                dbm.save_daily_bars("AAPL", bars)

                market_provider._info_cache["AAPL"] = {"currency": "USD"}
                market_provider._calendar_cache["AAPL"] = (
                    datetime.now(timezone.utc),
                    {"earnings_date": "2026-02-20"},
                )

                df = market_provider.get_historical_data("AAPL", "1mo", "1d")
                self.assertEqual(df.attrs.get("source"), "local bar store")
                self.assertEqual(df.attrs.get("earnings_date"), "2026-02-20")
            finally:
                market_provider.set_bar_store(None)
                dbm.close()

    def test_uncached_earnings_date_is_omitted(self):
        from unittest.mock import patch
        from stockstui.data_providers import market_provider

        market_provider._info_cache["AAPL"] = {"currency": "USD"}
        raw = _close_frame([100.0, 101.0])
        with patch("stockstui.data_providers.market_provider.yf.Ticker") as mock_tkr:
            mock_tkr.return_value.history.return_value = raw
            df = market_provider.get_historical_data("AAPL", "1mo", "1d")

        self.assertNotIn("earnings_date", df.attrs)


if __name__ == "__main__":
    unittest.main()
