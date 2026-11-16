"""
The price/info caches must reach the database during a session, not only on a
clean exit: when the app is closed by killing its terminal, on_unmount never
runs, and the next launch would start empty and block on a full fetch.
"""

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stockstui.config_manager import ConfigManager
from stockstui.data_providers import market_provider
from stockstui.database.db_manager import DbManager
from stockstui.main import StocksTUI
from tests.test_utils import TEST_APP_ROOT


class TestDbManagerConcurrentWrites(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dbm = DbManager(Path(self.tmpdir.name) / "cache.db")

    def tearDown(self):
        self.dbm.close()
        self.tmpdir.cleanup()

    def test_saves_from_many_threads_do_not_interleave(self):
        expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        errors: list = []

        def writer(n: int):
            try:
                for i in range(25):
                    self.dbm.save_price_cache_to_db(
                        {f"T{n}_{i}": {"expiry": expiry, "data": {"price": float(i)}}}
                    )
                    self.dbm.save_info_cache_to_db({f"T{n}_{i}": {"exchange": "NMS"}})
            except Exception as e:  # noqa: BLE001 - surfaced via the assertion below
                errors.append(e)

        with self.assertNoLogs(level="ERROR"):
            threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.dbm.load_price_cache_from_db()), 8 * 25)

    def test_save_after_close_is_a_no_op(self):
        self.dbm.close()
        expiry = datetime.now(timezone.utc)
        self.dbm.save_price_cache_to_db({"AAPL": {"expiry": expiry, "data": {"price": 1.0}}})
        self.dbm.save_info_cache_to_db({"AAPL": {"exchange": "NMS"}})


class TestFetchPersistsCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()

    def tearDown(self):
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        self.tmpdir.cleanup()

    async def test_successful_fetch_writes_cache_to_db(self):
        app = StocksTUI()
        with patch("stockstui.config_manager.PlatformDirs") as mock_dirs:
            mock_dirs.return_value.user_config_dir = self.tmpdir.name
            app.config = ConfigManager(app_root=TEST_APP_ROOT.parent)
        app._load_and_register_themes()
        app.db_manager.close()
        app.db_manager = DbManager(Path(self.tmpdir.name) / "cache.db")

        expiry = datetime.now(timezone.utc) + timedelta(hours=1)

        def fake_fetch(tickers, force_refresh=False, enable_pre_post_market=False):
            for t in tickers:
                market_provider._price_cache[t] = {
                    "expiry": expiry,
                    "data": {"symbol": t, "price": 10.0},
                }
                market_provider._info_cache[t] = {"exchange": "NMS"}
            return [market_provider._price_cache[t]["data"] for t in tickers]

        with patch.object(market_provider, "get_market_price_data", side_effect=fake_fetch):
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                market_provider._price_cache.clear()
                app.fetch_prices(["ZZZ1", "ZZZ2"], force=True, category="stocks")
                await app.workers.wait_for_complete()

                # Read back through a second connection, as the next launch would,
                # while the app is still running (i.e. before on_unmount).
                reader = DbManager(Path(self.tmpdir.name) / "cache.db")
                try:
                    saved = reader.load_price_cache_from_db()
                finally:
                    reader.close()

        self.assertIn("ZZZ1", saved)
        self.assertIn("ZZZ2", saved)
        self.assertEqual(saved["ZZZ1"]["data"]["price"], 10.0)


if __name__ == "__main__":
    unittest.main()
