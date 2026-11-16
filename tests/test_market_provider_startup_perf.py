"""
Regression tests for startup latency of the slow (metadata) fetch.

yfinance's ``fast_info`` is lazy: each field read can make its own HTTP
request. These tests pin that those reads happen inside the thread pool,
not serially afterwards on the calling thread.
"""

import threading
import unittest
from unittest.mock import patch

from stockstui.data_providers import market_provider


class LazyFastInfo:
    """Stand-in for yfinance's FastInfo that records which thread reads each field."""

    def __init__(self, values: dict, reads: list, raising: frozenset = frozenset()):
        self._values = values
        self._reads = reads
        self._raising = raising

    def __getitem__(self, key):
        self._reads.append((key, threading.get_ident()))
        if key in self._raising:
            raise RuntimeError(f"network error reading {key}")
        return self._values[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def _make_ticker(symbol: str, reads: list, raising: frozenset = frozenset()):
    class FakeTicker:
        def __init__(self):
            self.info = {
                "currency": "USD",
                "longName": f"{symbol} Inc.",
                "exchange": "NMS",
                "quoteType": "EQUITY",
                "regularMarketPreviousClose": 100.0,
            }
            self.fast_info = LazyFastInfo(
                {"lastPrice": 101.0, "dayLow": 99.0, "dayHigh": 102.0, "shares": 1_000},
                reads,
                raising,
            )

    return FakeTicker()


class TestSlowFetchReadsFastInfoInWorkers(unittest.TestCase):
    def setUp(self):
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        market_provider._market_calendars.clear()

    def test_fast_info_fields_are_read_off_the_calling_thread(self):
        reads: list = []
        with patch(
            "stockstui.data_providers.market_provider.yf.Ticker",
            side_effect=lambda s: _make_ticker(s, reads),
        ):
            market_provider._fetch_and_cache_slow_data(["AAA", "BBB", "CCC"])

        self.assertTrue(reads, "fast_info was never read")
        caller = threading.get_ident()
        on_caller = sorted({k for k, tid in reads if tid == caller})
        self.assertEqual(on_caller, [], f"fast_info fields read serially on the caller: {on_caller}")

    def test_values_are_carried_into_the_cache(self):
        reads: list = []
        with patch(
            "stockstui.data_providers.market_provider.yf.Ticker",
            side_effect=lambda s: _make_ticker(s, reads),
        ):
            market_provider._fetch_and_cache_slow_data(["AAA"])

        data = market_provider._price_cache["AAA"]["data"]
        self.assertEqual(data["price"], 101.0)
        self.assertEqual(data["day_low"], 99.0)
        self.assertEqual(data["day_high"], 102.0)
        self.assertEqual(data["shares"], 1_000)
        self.assertEqual(data["market_cap"], 101_000.0)
        # Missing fast_info fields fall back to info.
        self.assertEqual(data["currency"], "USD")
        self.assertEqual(data["previous_close"], 100.0)

    def test_a_failing_field_falls_back_instead_of_dropping_the_ticker(self):
        reads: list = []
        with patch(
            "stockstui.data_providers.market_provider.yf.Ticker",
            side_effect=lambda s: _make_ticker(s, reads, raising=frozenset({"lastPrice"})),
        ):
            market_provider._fetch_and_cache_slow_data(["AAA"])

        data = market_provider._price_cache["AAA"]["data"]
        self.assertEqual(data["description"], "AAA Inc.")
        self.assertEqual(data["day_low"], 99.0)


class TestMaterializeFastInfo(unittest.TestCase):
    def test_skips_missing_and_raising_fields(self):
        reads: list = []
        fi = LazyFastInfo({"lastPrice": 5.0, "open": 4.0}, reads, raising=frozenset({"open"}))
        self.assertEqual(market_provider._materialize_fast_info(fi), {"lastPrice": 5.0})

    def test_accepts_plain_dicts(self):
        self.assertEqual(
            market_provider._materialize_fast_info({"lastPrice": 1.0, "unused": 2}),
            {"lastPrice": 1.0},
        )


class TestExchangeCalendarMapping(unittest.TestCase):
    """
    Every yfinance exchange code our users commonly hit must resolve to a real
    pandas_market_calendars calendar; otherwise its metadata expires hourly and
    each launch blocks on a full refetch. Uses the real library on purpose, so a
    misspelled calendar name fails here.
    """

    CODES = [
        "NMS", "NGM", "NCM", "NYQ", "PCX", "ASE", "BTS", "SNP", "DJI", "CXI", "WCB", "NIM",
        "LSE", "FGI", "GER", "PAR", "ZRH", "OSA", "HKG", "SHH", "KSC", "NSI", "TSI", "ASX",
        "SAO", "MEX", "CCC",
    ]

    def setUp(self):
        market_provider._market_calendars.clear()

    def test_common_exchange_codes_resolve_to_a_calendar(self):
        if market_provider.mcal is None:
            self.skipTest("pandas_market_calendars not installed")
        unresolved = [c for c in self.CODES if market_provider._get_calendar(c) is None]
        self.assertEqual(unresolved, [])

    def test_us_listings_share_the_nyse_expiry(self):
        if market_provider.mcal is None:
            self.skipTest("pandas_market_calendars not installed")
        nyse = market_provider._calculate_info_expiry("NYSE")
        for code in ("PCX", "NGM", "CXI", "WCB"):
            self.assertEqual(market_provider._calculate_info_expiry(code), nyse, code)


if __name__ == "__main__":
    unittest.main()
