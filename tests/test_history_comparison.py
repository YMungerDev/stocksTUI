"""Tests for history comparison mode: alignment, rebasing, beta (#7)."""

import unittest

import pandas as pd

from stockstui.presentation.overlays import (
    align_series,
    beta_against,
    pairwise_correlations,
    rebase_to_100,
)


def _series(values: list[float], start: str = "2026-01-01") -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


class TestAlignment(unittest.TestCase):
    def test_common_dates_only(self):
        """Series are aligned on common trading dates before rebasing."""
        a = _series([100.0, 101.0, 102.0, 103.0, 104.0])
        # b is missing the middle day (different date range start).
        b = _series([50.0, 51.0, 52.0], start="2026-01-02")
        aligned = align_series({"A": a, "B": b})
        # Only the overlapping bdates count: Jan 2, 5, 6 -> wait, inner join
        # on b's index (Jan 2, 5, 6) intersected with a's (1,2,5,6,7) is
        # (2,5,6): 3 rows.
        self.assertEqual(len(aligned), 3)
        self.assertEqual(
            list(aligned.columns), ["A", "B"]
        )

    def test_no_overlap_is_empty(self):
        a = _series([100.0, 101.0], start="2026-01-01")
        b = _series([50.0, 51.0], start="2027-01-01")
        self.assertTrue(align_series({"A": a, "B": b}).empty)

    def test_empty_input(self):
        self.assertTrue(align_series({}).empty)


class TestRebasing(unittest.TestCase):
    def test_all_series_start_at_100(self):
        frame = align_series(
            {"A": _series([100.0, 110.0, 120.0]), "B": _series([50.0, 45.0, 60.0])}
        )
        rebased = rebase_to_100(frame)
        self.assertAlmostEqual(rebased.iloc[0]["A"], 100.0)
        self.assertAlmostEqual(rebased.iloc[0]["B"], 100.0)
        # A rose 20% -> 120; B fell from 50 to 60 vs 50 start = 120.
        self.assertAlmostEqual(rebased.iloc[-1]["A"], 120.0)
        self.assertAlmostEqual(rebased.iloc[-1]["B"], 120.0)

    def test_preserves_shape(self):
        frame = align_series({"A": _series([1.0, 2.0, 3.0])})
        self.assertEqual(rebase_to_100(frame).shape, frame.shape)


class TestCorrelation(unittest.TestCase):
    def test_perfect_correlation(self):
        frame = align_series(
            {
                "A": _series([100.0, 110.0, 120.0, 130.0]),
                "B": _series([50.0, 55.0, 60.0, 65.0]),  # identical returns
            }
        )
        corr = pairwise_correlations(frame)
        self.assertAlmostEqual(corr.loc["A", "B"], 1.0)

    def test_inverse_correlation(self):
        frame = align_series(
            {
                # B's returns mirror A's (+10/-10/+10 vs -10/+10/-10).
                "A": _series([100.0, 110.0, 99.0, 108.9]),
                "B": _series([50.0, 45.0, 49.5, 44.55]),
            }
        )
        corr = pairwise_correlations(frame)
        self.assertAlmostEqual(corr.loc["A", "B"], -1.0)


class TestBeta(unittest.TestCase):
    def test_beta_two_hand_checked(self):
        """Series moves 2x the benchmark -> beta 2."""
        # Benchmark returns +10%, -5%, +10%. A stock whose returns vary
        # twice as widely around the same mean has beta 2: scale the
        # benchmark's deviations from its mean return by 2.
        benchmark = _series([100.0, 110.0, 104.5, 114.95])
        bench_rets = benchmark.pct_change().dropna()
        mean_ret = bench_rets.mean()
        stock_values = [50.0]
        for r in bench_rets:
            stock_values.append(stock_values[-1] * (1 + mean_ret + 2 * (r - mean_ret)))
        stock = pd.Series(stock_values, index=benchmark.index, dtype=float)
        self.assertAlmostEqual(beta_against(stock, benchmark), 2.0, places=6)

    def test_beta_one(self):
        benchmark = _series([100.0, 110.0, 104.5, 114.95])
        # Identical return stream: scale prices, not returns.
        stock = benchmark * 2.0
        self.assertAlmostEqual(beta_against(stock, benchmark), 1.0)

    def test_negative_beta(self):
        benchmark = _series([100.0, 110.0, 104.5, 114.95])
        # A perfect mirror of the benchmark's returns -> beta -1.
        stock = _series([100.0, 90.0, 94.5, 85.05])
        self.assertAlmostEqual(beta_against(stock, benchmark), -1.0)

    def test_insufficient_data_returns_none(self):
        benchmark = _series([100.0, 110.0])
        stock = _series([50.0, 60.0])
        self.assertIsNone(beta_against(stock, benchmark))

    def test_flat_benchmark_returns_none(self):
        benchmark = _series([100.0, 100.0, 100.0, 100.0])
        stock = _series([50.0, 60.0, 55.0, 70.0])
        self.assertIsNone(beta_against(stock, benchmark))


class TestComparisonFetch(unittest.IsolatedAsyncioTestCase):
    """The comparison worker builds a rebased multi-column frame."""

    async def test_view_renders_comparison_chart_and_table(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from textual.widgets import DataTable
        from stockstui.ui.views.history_view import HistoryView
        from stockstui.ui.widgets.history_chart import HistoryChart

        idx = pd.bdate_range("2026-01-01", periods=60)
        frame = pd.DataFrame(
            {
                "NVDA": [100.0 + i for i in range(60)],
                "AMD": [100.0 + 0.5 * i for i in range(60)],
            },
            index=idx,
        )
        frame.attrs["comparison_tickers"] = ["NVDA", "AMD"]

        class HostApp(App):
            theme_variables = {}
            history_ticker = None
            config = MagicMock()
            cli_overrides = {}
            _history_period = "6mo"
            _last_historical_data = frame
            _history_sort_column_key = None
            _history_sort_reverse = False

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            view.query_one("#history-view-toggle").value = True
            await view._render_historical_data()
            await pilot.pause()

            chart = view.query_one(HistoryChart)
            self.assertTrue(chart._comparison)
            table = view.query_one("#history-correlation-table", DataTable)
            self.assertEqual(table.row_count, 2)  # one row per ticker
            headers = [str(col.label) for col in table.ordered_columns]
            self.assertEqual(
                headers, ["Ticker", "ρ vs NVDA", "ρ vs AMD", "β vs benchmark"]
            )
            nvda_row = [cell.plain for cell in table.get_row_at(0)]
            amd_row = [cell.plain for cell in table.get_row_at(1)]
            self.assertEqual(nvda_row[0], "NVDA")
            self.assertEqual(nvda_row[1], "1.00")
            self.assertEqual(nvda_row[3], "—")  # benchmark beta is last
            self.assertEqual(amd_row[0], "AMD")
            self.assertEqual(amd_row[2], "1.00")
            expected_beta = beta_against(frame["AMD"], frame["NVDA"])
            self.assertEqual(amd_row[3], f"{expected_beta:.2f}")

    async def test_correlation_table_column_alignment(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from textual.widgets import DataTable
        from stockstui.ui.views.history_view import HistoryView

        idx = pd.bdate_range("2026-01-01", periods=6)
        frame = pd.DataFrame(
            {
                "SPY": [100.0, 110.0, 104.5, 115.0, 109.0, 120.0],
                "NVDA": [100.0, 125.0, 122.0, 140.0, 118.0, 145.0],
            },
            index=idx,
        )
        frame.attrs["comparison_tickers"] = ["SPY", "NVDA"]
        corr = pairwise_correlations(frame)
        expected_corr = corr.loc["NVDA", "SPY"]
        expected_beta = beta_against(frame["NVDA"], frame["SPY"])
        # Ensure correlation and beta are distinct so misalignment is caught.
        self.assertNotAlmostEqual(expected_corr, expected_beta, places=2)

        class HostApp(App):
            theme_variables = {}
            history_ticker = None
            config = MagicMock()
            cli_overrides = {}
            _history_period = "1mo"
            _last_historical_data = frame
            _history_sort_column_key = None
            _history_sort_reverse = False

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            view.query_one("#history-view-toggle").value = True
            await view._render_historical_data()
            await pilot.pause()

            table = view.query_one("#history-correlation-table", DataTable)
            headers = [str(col.label) for col in table.ordered_columns]
            self.assertEqual(
                headers, ["Ticker", "ρ vs SPY", "ρ vs NVDA", "β vs benchmark"]
            )
            spy_row = [cell.plain for cell in table.get_row_at(0)]
            nvda_row = [cell.plain for cell in table.get_row_at(1)]
            self.assertEqual(
                spy_row, ["SPY", "1.00", f"{expected_corr:.2f}", "—"]
            )
            self.assertEqual(
                nvda_row,
                ["NVDA", f"{expected_corr:.2f}", "1.00", f"{expected_beta:.2f}"],
            )

    async def test_view_renders_comparison_table_mode(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from textual.widgets import DataTable
        from stockstui.ui.views.history_view import HistoryView
        from stockstui.ui.widgets.history_chart import HistoryChart

        idx = pd.bdate_range("2026-01-01", periods=5)
        frame = pd.DataFrame(
            {
                "NVDA": [100.0, 105.0, float("nan"), 110.0, 112.5],
                "AMD": [100.0, 98.5, 101.25, 103.0, 104.0],
            },
            index=idx,
        )
        frame.attrs["comparison_tickers"] = ["NVDA", "AMD"]

        class HostApp(App):
            theme_variables = {}
            history_ticker = None
            config = MagicMock()
            cli_overrides = {}
            _history_period = "1mo"
            _last_historical_data = frame
            _history_sort_column_key = None
            _history_sort_reverse = False

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            await view._render_historical_data()
            await pilot.pause()

            self.assertEqual(len(view.query(HistoryChart)), 0)
            history_table = view.query_one("#history-table", DataTable)
            self.assertEqual(history_table.row_count, 5)
            headers = [str(col.label) for col in history_table.ordered_columns]
            self.assertEqual(headers, ["Date", "NVDA", "AMD"])
            self.assertEqual(history_table.get_row_at(0), ["2026-01-01", "100.00", "100.00"])
            self.assertEqual(history_table.get_row_at(2), ["2026-01-05", "—", "101.25"])

            corr_table = view.query_one("#history-correlation-table", DataTable)
            self.assertEqual(corr_table.row_count, 2)

    async def test_multi_ticker_input_dispatches_comparison(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from textual.widgets import Input
        from stockstui.ui.views.history_view import HistoryView

        class HostApp(App):
            theme_variables = {}
            history_ticker = None
            config = MagicMock()
            cli_overrides = {}
            _history_period = None
            _last_historical_data = None
            _history_sort_column_key = None
            _history_sort_reverse = False
            fetch_historical_comparison = MagicMock()
            fetch_historical_data = MagicMock()

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            inp = view.query_one("#history-ticker-input", Input)
            inp.value = "nvda,amd"
            view.on_history_ticker_submitted(Input.Submitted(inp, "nvda,amd"))
            await pilot.pause()

            app.fetch_historical_comparison.assert_called_once()
            args = app.fetch_historical_comparison.call_args
            self.assertEqual(args[0][0], ["NVDA", "AMD"])
            self.assertEqual(app.history_ticker, "NVDA,AMD")
            # Single-ticker fetch was NOT used.
            app.fetch_historical_data.assert_not_called()

    async def test_single_ratio_input_dispatches_comparison(self):
        from unittest.mock import MagicMock

        from textual.app import App
        from textual.widgets import Input
        from stockstui.ui.views.history_view import HistoryView

        class HostApp(App):
            theme_variables = {}
            history_ticker = None
            config = MagicMock()
            cli_overrides = {}
            _history_period = None
            _last_historical_data = None
            _history_sort_column_key = None
            _history_sort_reverse = False
            fetch_historical_comparison = MagicMock()
            fetch_historical_data = MagicMock()

            def compose(self):
                yield HistoryView()

        app = HostApp()
        async with app.run_test() as pilot:
            view = app.query_one(HistoryView)
            inp = view.query_one("#history-ticker-input", Input)
            inp.value = "nvda/smh"
            view.on_history_ticker_submitted(Input.Submitted(inp, "nvda/smh"))
            await pilot.pause()

            app.fetch_historical_comparison.assert_called_once()
            args = app.fetch_historical_comparison.call_args
            self.assertEqual(args[0][0], ["NVDA/SMH"])
            self.assertEqual(app.history_ticker, "NVDA/SMH")
            app.fetch_historical_data.assert_not_called()

            # Period changes on an active single ratio also dispatch comparison.
            app.fetch_historical_comparison.reset_mock()
            view._request_historical_data()
            await pilot.pause()
            app.fetch_historical_comparison.assert_called_once()
            self.assertEqual(app.fetch_historical_comparison.call_args[0][0], ["NVDA/SMH"])
            app.fetch_historical_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
