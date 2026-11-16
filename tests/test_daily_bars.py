"""Tests for the local daily bar store (#1).

Covers the DbManager daily_bars table, the market_provider offline history
path, incremental top-up, split/dividend adjustment handling, and the app's
background top-up worker.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from stockstui.data_providers import base as provider_base
from stockstui.data_providers import market_provider
from stockstui.database.db_manager import DbManager


def _daily_frame(
    closes: list[float],
    start: str = "2026-11-02",
    adj_closes: list[float] | None = None,
    tz: str | None = "UTC",
) -> pd.DataFrame:
    """Builds a small raw-OHLCV daily frame with optional Adj Close."""
    index = pd.to_datetime(
        [pd.Timestamp(start) + pd.Timedelta(days=i) for i in range(len(closes))]
    )
    if tz:
        index = index.tz_localize(tz)
    frame = pd.DataFrame(
        {
            "Open": [c - 1.0 for c in closes],
            "High": [c + 1.0 for c in closes],
            "Low": [c - 2.0 for c in closes],
            "Close": closes,
            "Volume": [1000 + i for i in range(len(closes))],
        },
        index=index,
    )
    if adj_closes is not None:
        frame["Adj Close"] = adj_closes
    return frame


class TestDailyBarsDbManager(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbm = DbManager(Path(self.tmpdir.name) / "bars.db")
        market_provider.set_bar_store(self.dbm)

    def tearDown(self):
        market_provider.set_bar_store(None)
        self.dbm.close()
        self.tmpdir.cleanup()
        provider_base._outcomes.clear()

    def test_daily_bars_table_created(self):
        cursor = self.dbm.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        self.assertIn("daily_bars", tables)

    def test_save_and_load_round_trip(self):
        frame = _daily_frame([100.0, 101.0, 102.0], adj_closes=[99.0, 100.0, 101.0])
        self.dbm.save_daily_bars("AAPL", frame)

        loaded = self.dbm.load_daily_bars("aapl")
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded["Close"].iloc[0], 100.0)
        self.assertEqual(loaded["Volume"].iloc[1], 1001)
        self.assertEqual(loaded.attrs["adjclose"], [99.0, 100.0, 101.0])
        self.assertEqual(loaded.attrs["currency"], "USD")

    def test_load_with_start_date_filter(self):
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0, 101.0, 102.0]))
        loaded = self.dbm.load_daily_bars("AAPL", "2026-11-04")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded["Close"].iloc[0], 102.0)

    def test_upsert_replaces_overlapping_rows(self):
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0, 101.0]))
        self.dbm.save_daily_bars("AAPL", _daily_frame([999.0, 103.0], start="2026-11-03"))
        loaded = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded["Close"].iloc[1], 999.0)

    def test_delete_daily_bars_scoped(self):
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0, 101.0, 102.0]))
        self.dbm.delete_daily_bars("AAPL", "2026-11-04")
        loaded = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(len(loaded), 2)
        self.dbm.delete_daily_bars("AAPL")
        self.assertTrue(self.dbm.load_daily_bars("AAPL").empty)

    def test_load_currency_from_ticker_info(self):
        self.dbm.save_daily_bars("SAP.DE", _daily_frame([100.0]))
        self.dbm.save_info_cache_to_db({"SAP.DE": {"currency": "EUR"}})
        loaded = self.dbm.load_daily_bars("SAP.DE")
        self.assertEqual(loaded.attrs["currency"], "EUR")

    def test_load_unknown_ticker_is_empty(self):
        self.assertTrue(self.dbm.load_daily_bars("NOPE").empty)

    def test_daily_bars_survive_reconnect(self):
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0]))
        self.dbm.close()
        reopened = DbManager(Path(self.tmpdir.name) / "bars.db")
        try:
            self.assertFalse(reopened.load_daily_bars("AAPL").empty)
        finally:
            reopened.close()


class TestOfflineHistoryPath(unittest.TestCase):
    """get_historical_data must serve daily intervals from the store."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbm = DbManager(Path(self.tmpdir.name) / "bars.db")
        market_provider.set_bar_store(self.dbm)
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    def tearDown(self):
        market_provider.set_bar_store(None)
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        self.dbm.close()
        self.tmpdir.cleanup()
        provider_base._outcomes.clear()

    def _seed_info(self):
        market_provider._info_cache["AAPL"] = {
            "currency": "USD",
            "exchange": "NMS",
            "quoteType": "EQUITY",
        }

    def test_daily_history_served_from_store_without_network(self):
        self._seed_info()
        settled = _daily_frame([100.0, 101.0, 102.0], adj_closes=[100.0, 101.0, 102.0])
        self.dbm.save_daily_bars("AAPL", settled)

        with patch.object(market_provider.yf.Ticker, "history") as mock_history, \
                patch.object(market_provider.yf, "download") as mock_download:
            data = market_provider.get_historical_data("AAPL", "1mo", "1d")

        mock_history.assert_not_called()
        mock_download.assert_not_called()
        self.assertEqual(len(data), 3)
        self.assertEqual(data.attrs["source"], "local bar store")
        # Raw close 102.0 with adjclose ratio 102/102 stays 102.0.
        self.assertEqual(data["Close"].iloc[-1], 102.0)
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "history"
        )
        self.assertEqual(row["status"], "ok")
        self.assertIn("local", row["detail"])

    def test_stored_adjustment_ratios_are_applied_on_serve(self):
        self._seed_info()
        # Raw close 50.0 but adjusted close 25.0: a 2:1 split happened after
        # this bar. Displayed prices must be scaled to 25.0.
        settled = _daily_frame([50.0], adj_closes=[25.0])
        self.dbm.save_daily_bars("AAPL", settled)

        with patch.object(market_provider.yf.Ticker, "history"), \
                patch.object(market_provider.yf, "download"):
            data = market_provider.get_historical_data("AAPL", "1mo", "1d")

        self.assertEqual(data["Close"].iloc[0], 25.0)
        self.assertEqual(data["Open"].iloc[0], 24.5)

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_intraday_interval_skips_store(self, mock_yf_ticker):
        self._seed_info()
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0]))

        mock_yf_ticker.return_value.history.return_value = _daily_frame([101.0])
        data = market_provider.get_historical_data("AAPL", "1d", "5m")

        mock_yf_ticker.return_value.history.assert_called_once()
        self.assertEqual(data["Close"].iloc[0], 101.0)

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_empty_store_falls_back_to_network(self, mock_yf_ticker):
        self._seed_info()
        mock_yf_ticker.return_value.history.return_value = pd.DataFrame()

        data = market_provider.get_historical_data("AAPL", "1mo", "1d")

        mock_yf_ticker.return_value.history.assert_called_once()
        self.assertTrue(data.empty)

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_network_fetch_stores_settled_bars_and_adjusts_display(
        self, mock_yf_ticker
    ):
        self._seed_info()
        # Two settled bars plus today's provisional bar (dropped on store).
        raw = _daily_frame(
            [100.0, 101.0, 150.0],
            start=(datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d"),
            adj_closes=[100.0, 101.0, 150.0],
        )
        mock_yf_ticker.return_value.history.return_value = raw

        data = market_provider.get_historical_data("AAPL", "1mo", "1d")

        stored = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(len(stored), 2)
        # Display path applied Adj Close/Close (ratio 1.0 here).
        self.assertEqual(data["Close"].iloc[0], 100.0)
        self.assertNotIn("Adj Close", data.columns)

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_no_bar_store_network_path_unchanged(self, mock_yf_ticker):
        market_provider.set_bar_store(None)
        self._seed_info()
        raw = _daily_frame([100.0, 101.0], adj_closes=[100.0, 101.0])
        mock_yf_ticker.return_value.history.return_value = raw

        data = market_provider.get_historical_data("AAPL", "1mo", "1d")

        self.assertEqual(len(data), 2)
        self.assertNotIn("Adj Close", data.columns)


class TestIncrementalTopUp(unittest.TestCase):
    """refresh_daily_bars must fetch one batch and only top up missing days."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbm = DbManager(Path(self.tmpdir.name) / "bars.db")
        market_provider.set_bar_store(self.dbm)
        provider_base._outcomes.clear()

    def tearDown(self):
        market_provider.set_bar_store(None)
        self.dbm.close()
        self.tmpdir.cleanup()
        provider_base._outcomes.clear()

    def test_second_run_fetches_only_overlap_window(self):
        """A repeat run re-downloads only the overlap tail, not full history."""
        # Seed bars covering the requested window with settled dates only.
        now = datetime.now(timezone.utc)
        settled_dates = [
            (now - timedelta(days=10)).strftime("%Y-%m-%d"),
            (now - timedelta(days=9)).strftime("%Y-%m-%d"),
        ]
        frame = _daily_frame([100.0, 101.0], start=settled_dates[0])
        self.dbm.save_daily_bars("AAPL", frame)

        with patch.object(market_provider.yf, "download") as mock_download:
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=11))

        # The repeat run uses one batched call starting just before the newest
        # stored bar (overlap), not the full requested window.
        mock_download.assert_called_once()
        start_arg = mock_download.call_args.kwargs["start"]
        newest_stored = settled_dates[-1]
        expected_start = (
            datetime.strptime(newest_stored, "%Y-%m-%d")
            - timedelta(days=market_provider._TOPUP_OVERLAP_DAYS)
        ).strftime("%Y-%m-%d")
        self.assertEqual(start_arg, expected_start)

    def test_top_up_stores_only_settled_rows(self):
        now = datetime.now(timezone.utc)
        three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        batch = _daily_frame([100.0, 101.0, 150.0], start=three_days_ago, adj_closes=[100.0, 101.0, 150.0])

        with patch.object(
            market_provider.yf, "download", return_value=batch
        ) as mock_download:
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=10))

        mock_download.assert_called_once()
        stored = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored["Close"].iloc[-1], 101.0)
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "history"
        )
        self.assertEqual(row["status"], "ok")

    def test_batch_download_extracts_each_ticker(self):
        now = datetime.now(timezone.utc)
        three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        aapl = _daily_frame([100.0, 101.0], start=three_days_ago)
        googl = _daily_frame([2000.0, 2010.0], start=three_days_ago)
        batch = pd.concat(
            {"AAPL": aapl, "GOOGL": googl}, axis=1, names=["Ticker", "Metric"]
        )

        with patch.object(market_provider.yf, "download", return_value=batch):
            market_provider.refresh_daily_bars(["AAPL", "GOOGL"], now - timedelta(days=10))

        aapl_stored = self.dbm.load_daily_bars("AAPL")
        googl_stored = self.dbm.load_daily_bars("GOOGL")
        self.assertEqual(aapl_stored["Close"].iloc[0], 100.0)
        self.assertEqual(googl_stored["Close"].iloc[0], 2000.0)

    def test_network_failure_keeps_stored_bars(self):
        now = datetime.now(timezone.utc)
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0, 101.0]))

        with patch.object(
            market_provider.yf, "download", side_effect=Exception("network down")
        ):
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=10))

        loaded = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(len(loaded), 2)
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "history"
        )
        self.assertEqual(row["status"], "fallback")

    def test_download_returns_empty_keeps_stored_bars(self):
        now = datetime.now(timezone.utc)
        self.dbm.save_daily_bars("AAPL", _daily_frame([100.0]))

        with patch.object(
            market_provider.yf, "download", return_value=pd.DataFrame()
        ):
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=10))

        self.assertEqual(len(self.dbm.load_daily_bars("AAPL")), 1)

    def test_no_bar_store_is_a_no_op(self):
        market_provider.set_bar_store(None)
        with patch.object(market_provider.yf, "download") as mock_download:
            market_provider.refresh_daily_bars(["AAPL"], datetime.now(timezone.utc))
        mock_download.assert_not_called()


class TestAdjustmentDivergence(unittest.TestCase):
    """Split/dividend changes must trigger a full-range refetch."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbm = DbManager(Path(self.tmpdir.name) / "bars.db")
        market_provider.set_bar_store(self.dbm)
        provider_base._outcomes.clear()

    def tearDown(self):
        market_provider.set_bar_store(None)
        self.dbm.close()
        self.tmpdir.cleanup()
        provider_base._outcomes.clear()

    def test_divergence_triggers_full_refetch(self):
        now = datetime.now(timezone.utc)
        # Stored history: two settled bars from 10 days ago. The first bar's
        # adjusted close (50.0 vs raw 100.0) already reflects a past split,
        # the second matches raw (101.0) — the mix keeps the divergence check
        # honest, since only the newly observed disagreement must trigger.
        old_start = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        stored = _daily_frame([100.0, 101.0], start=old_start, adj_closes=[50.0, 101.0])
        self.dbm.save_daily_bars("AAPL", stored)

        # Fresh overlap covering the newest stored bar's date: the provider
        # now reports adjclose 50.5 for the same date where 101.0 was stored —
        # a new split changed factors for the entire history.
        fresh = _daily_frame([101.0, 102.0], start=old_start, adj_closes=[50.0, 50.5])
        refetched = _daily_frame(
            [100.0, 101.0, 102.0],
            start=old_start,
            adj_closes=[50.0, 50.5, 50.5],
        )

        with patch.object(
            market_provider.yf, "download", side_effect=[fresh, refetched]
        ) as mock_download:
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=5))

        self.assertEqual(mock_download.call_count, 2)
        stored_after = self.dbm.load_daily_bars("AAPL")
        # The full range was replaced with the refetched adjustment factors.
        self.assertEqual(stored_after.attrs["adjclose"][0], 50.0)
        self.assertEqual(stored_after.attrs["adjclose"][1], 50.5)

    def test_matching_adjcloses_do_not_refetch(self):
        now = datetime.now(timezone.utc)
        old_start = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        self.dbm.save_daily_bars(
            "AAPL", _daily_frame([100.0, 101.0], start=old_start, adj_closes=[100.0, 101.0])
        )
        overlap_start = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        fresh = _daily_frame([101.0, 102.0], start=overlap_start, adj_closes=[101.0, 102.0])

        with patch.object(
            market_provider.yf, "download", return_value=fresh
        ) as mock_download:
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=5))

        self.assertEqual(mock_download.call_count, 1)

    def test_new_split_after_stored_bars_triggers_full_refetch(self):
        """A Stock Splits row after the newest stored bar refreshes history."""
        now = datetime.now(timezone.utc)
        old_start = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        self.dbm.save_daily_bars(
            "AAPL",
            _daily_frame([100.0, 101.0], start=old_start, adj_closes=[100.0, 101.0]),
        )
        # Fresh overlap: matching adjcloses, but a 2:1 split occurred
        # yesterday — after the newest stored bar (from 10 days ago).
        split_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        fresh = _daily_frame([101.0, 102.0], start=old_start, adj_closes=[101.0, 102.0])
        fresh["Stock Splits"] = 0.0
        fresh.loc[pd.Timestamp(split_day, tz=fresh.index.tz), "Stock Splits"] = 2.0
        refetched = _daily_frame(
            [100.0, 101.0, 102.0],
            start=old_start,
            adj_closes=[50.0, 50.5, 51.0],
        )

        with patch.object(
            market_provider.yf, "download", side_effect=[fresh, refetched]
        ) as mock_download:
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=5))

        self.assertEqual(mock_download.call_count, 2)
        stored_after = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(stored_after.attrs["adjclose"][0], 50.0)

    def test_dividends_only_also_trigger_refetch(self):
        now = datetime.now(timezone.utc)
        old_start = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        self.dbm.save_daily_bars(
            "AAPL",
            _daily_frame([100.0, 101.0], start=old_start, adj_closes=[100.0, 101.0]),
        )
        dividend_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        fresh = _daily_frame([101.0, 102.0], start=old_start, adj_closes=[101.0, 102.0])
        fresh["Dividends"] = 0.0
        fresh.loc[pd.Timestamp(dividend_day, tz=fresh.index.tz), "Dividends"] = 0.25
        refetched = _daily_frame(
            [100.0, 101.0, 102.0], start=old_start, adj_closes=[99.0, 100.0, 101.0]
        )

        with patch.object(
            market_provider.yf, "download", side_effect=[fresh, refetched]
        ):
            market_provider.refresh_daily_bars(["AAPL"], now - timedelta(days=5))

        stored_after = self.dbm.load_daily_bars("AAPL")
        self.assertEqual(stored_after.attrs["adjclose"][0], 99.0)


class TestTopUpWorker(unittest.IsolatedAsyncioTestCase):
    """The app schedules a background top-up after the first refresh."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbm = DbManager(Path(self.tmpdir.name) / "bars.db")

    def tearDown(self):
        market_provider.set_bar_store(None)
        self.dbm.close()
        self.tmpdir.cleanup()
        provider_base._outcomes.clear()

    async def test_worker_tops_up_all_watchlist_tickers(self):
        from stockstui.config_manager import ConfigManager
        from stockstui.main import StocksTUI
        from tests.test_utils import TEST_APP_ROOT

        app = StocksTUI()
        with patch("stockstui.config_manager.PlatformDirs") as mock_dirs:
            mock_dirs.return_value.user_config_dir = self.tmpdir.name
            app.config = ConfigManager(app_root=TEST_APP_ROOT.parent)
        # Keep the app off the developer's real cache database, as
        # test_cache_persistence does.
        app.db_manager.close()
        app.db_manager = DbManager(Path(self.tmpdir.name) / "app.db")
        app.config.lists = {
            "stocks": [{"ticker": "AAPL"}, {"ticker": "GOOGL"}],
            "crypto": [{"ticker": "BTC-USD"}, {"ticker": "AAPL"}],
        }
        app._load_and_register_themes()

        with patch.object(
            market_provider, "refresh_daily_bars"
        ) as mock_refresh, patch.object(
            market_provider, "set_bar_store"
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()

        mock_refresh.assert_called_once()
        args = mock_refresh.call_args
        self.assertEqual(set(args[0][0]), {"AAPL", "GOOGL", "BTC-USD"})
        self.assertEqual(args[0][1].year, datetime.now(timezone.utc).year - 1)

    async def test_worker_without_tickers_is_skipped(self):
        from stockstui.config_manager import ConfigManager
        from stockstui.main import StocksTUI
        from tests.test_utils import TEST_APP_ROOT

        app = StocksTUI()
        with patch("stockstui.config_manager.PlatformDirs") as mock_dirs:
            mock_dirs.return_value.user_config_dir = self.tmpdir.name
            app.config = ConfigManager(app_root=TEST_APP_ROOT.parent)
        app.db_manager.close()
        app.db_manager = DbManager(Path(self.tmpdir.name) / "app.db")
        app.config.lists = {}
        app._load_and_register_themes()

        with patch.object(
            market_provider, "refresh_daily_bars"
        ) as mock_refresh, patch.object(
            market_provider, "set_bar_store"
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()

        mock_refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
