"""Tests for the provider capability registry and per-capability fallbacks (#14).

yfinance is the only implementation today, so "no behaviour change with
yfinance healthy" is pinned by the existing provider test suites. These tests
cover the new surface: outcome recording, the Debug tab's provider report,
and the fallback behaviour when a yfinance call fails.
"""

import unittest
from unittest.mock import MagicMock, patch

from stockstui.data_providers import base as provider_base
from stockstui.data_providers import market_provider


class TestProviderRegistry(unittest.TestCase):
    def setUp(self):
        provider_base._outcomes.clear()

    def test_report_starts_idle(self):
        rows = {r["capability"]: r for r in provider_base.get_provider_report()}
        self.assertEqual(set(rows), set(provider_base.CAPABILITIES))
        self.assertTrue(all(r["status"] == "idle" for r in rows.values()))
        self.assertTrue(all(r["provider"] == "yfinance" for r in rows.values()))

    def test_recorded_outcome_appears_in_report(self):
        provider_base.record_outcome("history", ok=True, detail="AAPL 1mo/1d")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "history"
        )
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["detail"], "AAPL 1mo/1d")
        self.assertIsNotNone(row["age_seconds"])

    def test_failed_outcome_without_fallback_reports_error(self):
        provider_base.record_outcome("info", ok=False, detail="BAD: boom")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "info"
        )
        self.assertEqual(row["status"], "error")
        self.assertFalse(row["fallback"] == "none" and row["status"] == "fallback")

    def test_failed_outcome_with_fallback_reports_fallback(self):
        provider_base.record_outcome(
            "quotes", ok=False, detail="AAPL: network error", fallback_used=True
        )
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "quotes"
        )
        self.assertEqual(row["status"], "fallback")
        self.assertEqual(row["fallback"], "in-memory price cache")

    def test_every_capability_declares_a_fallback(self):
        for key in provider_base.CAPABILITIES:
            self.assertIn(key, provider_base.FALLBACKS)
            self.assertTrue(provider_base.FALLBACKS[key])

    def test_unknown_capability_is_rejected(self):
        with self.assertLogs(level="WARNING"):
            provider_base.record_outcome("made_up", ok=True)

    def test_market_provider_satisfies_the_protocol(self):
        from typing import runtime_checkable

        runtime_checkable(provider_base.MarketDataProvider)
        self.assertIsInstance(market_provider, provider_base.MarketDataProvider)

    def test_long_details_are_truncated(self):
        provider_base.record_outcome("news", ok=False, detail="x" * 500)
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "news"
        )
        self.assertEqual(len(row["detail"]), 160)


class TestQuotesFallback(unittest.TestCase):
    """The price cache must survive a yfinance outage (#14 acceptance)."""

    def setUp(self):
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    def tearDown(self):
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_failed_slow_fetch_keeps_cached_data_and_records_fallback(
        self, mock_yf_ticker
    ):
        from datetime import datetime, timedelta, timezone

        market_provider._price_cache["AAPL"] = {
            "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
            "data": {"symbol": "AAPL", "price": 150.0, "description": "Apple Inc."},
        }
        mock_yf_ticker.side_effect = Exception("yfinance is down")

        market_provider._fetch_and_cache_slow_data(["AAPL"])

        cached = market_provider._price_cache["AAPL"]["data"]
        self.assertEqual(cached["price"], 150.0)
        self.assertEqual(cached["description"], "Apple Inc.")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "quotes"
        )
        self.assertEqual(row["status"], "fallback")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_failed_slow_fetch_without_cache_records_error(self, mock_yf_ticker):
        mock_yf_ticker.side_effect = Exception("yfinance is down")

        market_provider._fetch_and_cache_slow_data(["NEW"])

        self.assertEqual(
            market_provider._price_cache["NEW"]["data"]["description"],
            "Data Unavailable",
        )
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "quotes"
        )
        self.assertEqual(row["status"], "error")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_successful_slow_fetch_records_ok(self, mock_yf_ticker):
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {
            "currency": "USD",
            "longName": "Apple Inc.",
            "exchange": "NMS",
        }
        mock_ticker_obj.fast_info = {"lastPrice": 155.0}
        mock_yf_ticker.return_value = mock_ticker_obj

        market_provider._fetch_and_cache_slow_data(["AAPL"])

        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "quotes"
        )
        self.assertEqual(row["status"], "ok")

    @patch("stockstui.data_providers.market_provider.yf.download")
    def test_failed_fast_fetch_records_error(self, mock_yf_download):
        mock_yf_download.side_effect = Exception("connection reset")

        result = market_provider._fetch_fast_data(["AAPL"])

        self.assertEqual(result, {})
        row = next(
            r
            for r in provider_base.get_provider_report()
            if r["capability"] == "quotes_live"
        )
        self.assertEqual(row["status"], "error")

    @patch("stockstui.data_providers.market_provider.yf.download")
    def test_successful_fast_fetch_records_ok(self, mock_yf_download):
        import pandas as pd

        mock_df = pd.DataFrame(
            {
                ("Close", "AAPL"): [151.0],
                ("High", "AAPL"): [152.0],
                ("Low", "AAPL"): [150.0],
                ("Open", "AAPL"): [150.5],
                ("Volume", "AAPL"): [1000],
            },
            index=[pd.Timestamp.now(tz="UTC")],
        )
        mock_df.columns = pd.MultiIndex.from_tuples(mock_df.columns)
        mock_yf_download.return_value = mock_df

        market_provider._fetch_fast_data(["AAPL"])

        row = next(
            r
            for r in provider_base.get_provider_report()
            if r["capability"] == "quotes_live"
        )
        self.assertEqual(row["status"], "ok")


class TestNewsFallback(unittest.TestCase):
    """A yfinance news outage must serve stale cache instead of raising (#14)."""

    def setUp(self):
        market_provider._news_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    def tearDown(self):
        market_provider._news_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    def _seed_stale_news(self, ticker: str, title: str = "Old headline") -> None:
        from datetime import datetime, timedelta, timezone

        old = datetime.now(timezone.utc) - timedelta(hours=1)
        market_provider._news_cache[ticker.upper()] = (
            old,
            [
                {
                    "source_ticker": ticker.upper(),
                    "title": title,
                    "summary": "N/A",
                    "publisher": "N/A",
                    "link": "#",
                    "publish_time": "N/A",
                    "publish_datetime_utc": None,
                }
            ],
        )

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_news_failure_returns_stale_cache(self, mock_yf_ticker):
        self._seed_stale_news("AAPL")
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {"currency": "USD"}
        type(mock_ticker_obj).news = property(lambda self: (_ for _ in ()).throw(Exception("scraper broke")))
        mock_yf_ticker.return_value = mock_ticker_obj

        news = market_provider.get_news_data("AAPL")

        self.assertIsNotNone(news)
        self.assertEqual(news[0]["title"], "Old headline")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "news"
        )
        self.assertEqual(row["status"], "fallback")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_news_failure_without_cache_returns_none(self, mock_yf_ticker):
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {"currency": "USD"}
        type(mock_ticker_obj).news = property(lambda self: (_ for _ in ()).throw(Exception("scraper broke")))
        mock_yf_ticker.return_value = mock_ticker_obj

        self.assertIsNone(market_provider.get_news_data("AAPL"))
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "news"
        )
        self.assertEqual(row["status"], "error")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_invalid_ticker_with_stale_cache_returns_stale(self, mock_yf_ticker):
        self._seed_stale_news("AAPL")
        mock_yf_ticker.return_value.info = {}

        news = market_provider.get_news_data("AAPL")

        self.assertIsNotNone(news)
        self.assertEqual(news[0]["title"], "Old headline")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "news"
        )
        self.assertEqual(row["status"], "fallback")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_fresh_news_fetch_still_works_and_records_ok(self, mock_yf_ticker):
        self._seed_stale_news("AAPL")
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {"currency": "USD"}
        mock_ticker_obj.news = [
            {"content": {"title": "Fresh headline", "pubDate": "2026-11-16T12:00:00Z"}}
        ]
        mock_yf_ticker.return_value = mock_ticker_obj

        news = market_provider.get_news_data("AAPL")

        self.assertEqual(news[0]["title"], "Fresh headline")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "news"
        )
        self.assertEqual(row["status"], "ok")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_no_news_records_ok_with_detail(self, mock_yf_ticker):
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {"currency": "USD"}
        mock_ticker_obj.news = []
        mock_yf_ticker.return_value = mock_ticker_obj

        self.assertEqual(market_provider.get_news_data("AAPL"), [])
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "news"
        )
        self.assertEqual(row["status"], "ok")


class TestHistoryOutcome(unittest.TestCase):
    def setUp(self):
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    def tearDown(self):
        market_provider._price_cache.clear()
        market_provider._info_cache.clear()
        provider_base._outcomes.clear()

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_successful_history_records_ok(self, mock_yf_ticker):
        import pandas as pd

        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {"currency": "USD"}
        mock_ticker_obj.history.return_value = pd.DataFrame({"Close": [1.0, 2.0]})
        mock_yf_ticker.return_value = mock_ticker_obj

        df = market_provider.get_historical_data("AAPL", "1mo")

        self.assertFalse(df.empty)
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "history"
        )
        self.assertEqual(row["status"], "ok")

    @patch("stockstui.data_providers.market_provider.yf.Ticker")
    def test_failed_history_records_error(self, mock_yf_ticker):
        mock_ticker_obj = MagicMock()
        mock_ticker_obj.info = {"currency": "USD"}
        mock_ticker_obj.history.side_effect = Exception("yfinance broke")
        mock_yf_ticker.return_value = mock_ticker_obj

        df = market_provider.get_historical_data("AAPL", "1mo")

        self.assertTrue(df.empty)
        self.assertEqual(df.attrs.get("error"), "Data Error")
        row = next(
            r for r in provider_base.get_provider_report() if r["capability"] == "history"
        )
        self.assertEqual(row["status"], "error")


if __name__ == "__main__":
    unittest.main()
