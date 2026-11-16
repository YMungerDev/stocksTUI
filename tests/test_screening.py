"""Tests for watchlist screening expressions and output formats (#12)."""

import csv
import json
import unittest

from stockstui.presentation import screening
from stockstui.presentation.screening import FilterError, apply_filter


def _quotes() -> list[dict]:
    return [
        {
            "symbol": "AAPL",
            "description": "Apple Inc.",
            "currency": "USD",
            "price": 351.5,
            "previous_close": 356.27,
            "pe_ratio": 39.2,
            "eps": 8.98,
            "dividend_yield": 0.003,
            "market_cap": 5.1e12,
            "all_time_high": 360.83,
            "volume": 18_000_000,
        },
        {
            "symbol": "XOM",
            "description": "Exxon Mobil",
            "currency": "USD",
            "price": 110.0,
            "previous_close": 112.0,
            "pe_ratio": 14.0,
            "eps": 7.5,
            "dividend_yield": 0.033,
            "market_cap": 4.5e11,
            "all_time_high": 125.0,
            "volume": 15_000_000,
        },
        {
            "symbol": "SPY",
            "description": "S&P 500 ETF",
            "currency": "USD",
            "price": 670.0,
            "previous_close": None,
            "pe_ratio": None,
            "eps": None,
            "dividend_yield": 0.011,
            "market_cap": None,
            "all_time_high": None,
            "volume": 3_000_000,
        },
    ]


class TestFilterExpressions(unittest.TestCase):
    def test_filter_by_pe(self):
        result = apply_filter(_quotes(), "pe_ratio < 20")
        self.assertEqual([q["symbol"] for q in result], ["XOM"])

    def test_filter_by_off_ath(self):
        result = apply_filter(_quotes(), "off_ath < -0.10")
        self.assertEqual([q["symbol"] for q in result], ["XOM"])

    def test_filter_by_pct_change(self):
        # AAPL: 351.5/356.27-1 = -1.34%; XOM: 110/112-1 = -1.79%.
        result = apply_filter(_quotes(), "pct_change < -0.015")
        self.assertEqual([q["symbol"] for q in result], ["XOM"])

    def test_compound_expression(self):
        result = apply_filter(_quotes(), "pe_ratio < 20 and off_ath < -0.10")
        self.assertEqual([q["symbol"] for q in result], ["XOM"])

    def test_combined_with_or(self):
        result = apply_filter(_quotes(), "pe_ratio > 30 or dividend_yield > 0.02")
        self.assertEqual([q["symbol"] for q in result], ["AAPL", "XOM"])

    def test_null_fields_do_not_match_comparisons(self):
        """SPY has pe_ratio None: comparisons must exclude it, not crash."""
        result = apply_filter(_quotes(), "pe_ratio < 100")
        self.assertEqual([q["symbol"] for q in result], ["AAPL", "XOM"])

    def test_empty_expression_returns_all(self):
        result = apply_filter(_quotes(), "   ")
        self.assertEqual(len(result), 3)

    def test_invalid_expression_raises_filter_error(self):
        with self.assertRaises(FilterError) as ctx:
            apply_filter(_quotes(), "pe < bad syntax here !!!")
        self.assertIn("Invalid filter expression", str(ctx.exception))

    def test_unknown_column_raises_filter_error(self):
        with self.assertRaises(FilterError):
            apply_filter(_quotes(), "no_such_field < 5")

    def test_error_message_mentions_normalised_fields(self):
        with self.assertRaises(FilterError) as ctx:
            apply_filter(_quotes(), "bogus !!!")
        self.assertIn("pe_ratio", str(ctx.exception))


class TestJsonOutput(unittest.TestCase):
    def test_raw_numbers_not_formatted_strings(self):
        payload = json.loads(screening.to_json(_quotes()))
        aapl = next(q for q in payload if q["symbol"] == "AAPL")
        self.assertEqual(aapl["price"], 351.5)
        self.assertIsInstance(aapl["pe_ratio"], float)
        self.assertEqual(aapl["market_cap"], 5.1e12)
        # off_ath/pct_change computed and raw
        self.assertAlmostEqual(aapl["off_ath"], 351.5 / 360.83 - 1)

    def test_none_for_missing_fields(self):
        payload = json.loads(screening.to_json(_quotes()))
        spy = next(q for q in payload if q["symbol"] == "SPY")
        self.assertIsNone(spy["pe_ratio"])
        self.assertIsNone(spy["off_ath"])

    def test_nan_becomes_none(self):
        quotes = [{"symbol": "X", "price": float("nan")}]
        payload = json.loads(screening.to_json(quotes))
        self.assertIsNone(payload[0]["price"])


class TestCsvOutput(unittest.TestCase):
    def test_header_and_raw_numbers(self):
        text = screening.to_csv(_quotes())
        rows = list(csv.reader(text.splitlines()))
        header, data = rows[0], rows[1:]
        self.assertEqual(header[:2], ["symbol", "description"])
        self.assertIn("off_ath", header)
        self.assertIn("pct_change", header)
        aapl = data[0]
        aapl_dict = dict(zip(header, aapl))
        self.assertEqual(aapl_dict["symbol"], "AAPL")
        self.assertEqual(float(aapl_dict["price"]), 351.5)
        self.assertEqual(float(aapl_dict["market_cap"]), 5.1e12)

    def test_missing_values_are_empty_cells(self):
        text = screening.to_csv(_quotes())
        rows = list(csv.reader(text.splitlines()))
        spy = next(dict(zip(rows[0], r)) for r in rows[1:] if r[0] == "SPY")
        self.assertEqual(spy["pe_ratio"], "")
        self.assertEqual(spy["off_ath"], "")


class TestFilterPipeline(unittest.TestCase):
    """Filter then format: the CLI path end to end."""

    def test_filter_then_json(self):
        filtered = apply_filter(_quotes(), "pe_ratio < 20")
        payload = json.loads(screening.to_json(filtered))
        self.assertEqual([q["symbol"] for q in payload], ["XOM"])

    def test_filter_then_csv(self):
        filtered = apply_filter(_quotes(), "off_ath < -0.05")
        rows = list(csv.reader(screening.to_csv(filtered).splitlines()))
        symbols = [r[0] for r in rows[1:]]
        self.assertEqual(symbols, ["XOM"])


class TestMetricPreservation(unittest.TestCase):
    def test_without_metrics_preserves_exact_19_csv_columns(self):
        text = screening.to_csv(_quotes())
        rows = list(csv.reader(text.splitlines()))
        self.assertEqual(len(screening.CSV_COLUMNS), 19)
        self.assertEqual(rows[0], screening.CSV_COLUMNS)

        payload = json.loads(screening.to_json(_quotes()))
        for item in payload:
            self.assertEqual(list(item.keys()), screening.CSV_COLUMNS)

    def test_empty_quotes_preserves_exact_19_csv_columns(self):
        rows = list(csv.reader(screening.to_csv([]).splitlines()))
        self.assertEqual(rows, [screening.CSV_COLUMNS])
        self.assertEqual(json.loads(screening.to_json([])), [])

    def test_attached_metrics_preserved_in_json_and_csv(self):
        quotes = _quotes()[:2]
        for key in screening.METRIC_KEYS:
            quotes[0][key] = 0.15 if key != "RSI(14)" else 62.5
        quotes[0]["1Y Return"] = ""
        quotes[1]["1W Return"] = None

        payload = json.loads(screening.to_json(quotes))
        expected_keys = screening.CSV_COLUMNS + list(screening.METRIC_KEYS)
        self.assertEqual(list(payload[0].keys()), expected_keys)
        self.assertEqual(list(payload[1].keys()), expected_keys)
        self.assertEqual(payload[0]["1W Return"], 0.15)
        self.assertEqual(payload[0]["RSI(14)"], 62.5)
        self.assertIsNone(payload[0]["1Y Return"])
        for key in screening.METRIC_KEYS:
            self.assertIsNone(payload[1][key])

        rows = list(csv.reader(screening.to_csv(quotes).splitlines()))
        self.assertEqual(rows[0], expected_keys)
        row0 = dict(zip(rows[0], rows[1]))
        row1 = dict(zip(rows[0], rows[2]))
        self.assertEqual(float(row0["1W Return"]), 0.15)
        self.assertEqual(float(row0["RSI(14)"]), 62.5)
        self.assertEqual(row0["1Y Return"], "")
        for key in screening.METRIC_KEYS:
            self.assertEqual(row1[key], "")


if __name__ == "__main__":
    unittest.main()
