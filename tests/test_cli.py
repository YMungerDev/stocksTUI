import argparse
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

from stockstui import cli


def _make_args(**overrides) -> argparse.Namespace:
    defaults = {
        "output": "all",
        "tags": None,
        "session_list": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_config(
    lists: dict | None = None,
    settings: dict | None = None,
    themes: dict | None = None,
) -> MagicMock:
    cfg = MagicMock()
    cfg.lists = lists if lists is not None else {}
    cfg.settings = settings if settings is not None else {}
    cfg.themes = themes if themes is not None else {}
    cfg.get_setting.side_effect = lambda key, default=None: cfg.settings.get(
        key, default
    )
    return cfg


class TestShowManual(unittest.TestCase):
    @patch("stockstui.cli.subprocess.run")
    @patch("stockstui.cli.shutil.which", return_value="/usr/bin/less")
    def test_show_manual_uses_pager_when_available(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        cli.show_manual()
        mock_which.assert_called_once_with("less")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][0], "/usr/bin/less")

    @patch("builtins.print")
    @patch("stockstui.cli.shutil.which", return_value=None)
    def test_show_manual_prints_help_without_pager(
        self, _mock_which: MagicMock, mock_print: MagicMock
    ) -> None:
        cli.show_manual()
        mock_print.assert_called_once()
        self.assertTrue(len(mock_print.call_args[0][0]) > 0)


class TestRunCliOutput(unittest.TestCase):
    def _run_with_captured_console(self, args: argparse.Namespace) -> str:
        buf = StringIO()
        # Extra width: computed metric columns (#2) widened the overview table.
        real_console = Console(file=buf, force_terminal=False, width=250)
        with patch("stockstui.cli.Console", return_value=real_console):
            cli.run_cli_output(args)
        return buf.getvalue()

    @patch("stockstui.cli.market_provider.get_market_price_data")
    @patch("stockstui.cli.ConfigManager")
    def test_renders_ticker_table_for_visible_lists(
        self, mock_cfg_cls: MagicMock, mock_get_prices: MagicMock
    ) -> None:
        mock_cfg_cls.return_value = _make_config(
            lists={
                "stocks": [{"ticker": "AAPL", "alias": "Apple Inc.", "tags": "tech"}],
                "hidden_list": [{"ticker": "MSFT", "alias": "Microsoft"}],
            },
            settings={"hidden_tabs": ["hidden_list", "fred"]},
        )
        mock_get_prices.return_value = [
            {
                "symbol": "AAPL",
                "description": "Apple",
                "price": 200.0,
                "previous_close": 190.0,
                "currency": "USD",
                "day_low": 195.0,
                "day_high": 202.0,
                "fifty_two_week_low": 150.0,
                "fifty_two_week_high": 210.0,
            }
        ]

        output = self._run_with_captured_console(_make_args(output="all"))

        mock_get_prices.assert_called_once_with(
            ["AAPL"], enable_pre_post_market=False
        )
        self.assertIn("Apple Inc.", output)
        self.assertIn("$200.00", output)
        self.assertIn("+5.26%", output)
        self.assertNotIn("MSFT", output)

    @patch("stockstui.cli.market_provider.get_market_price_data")
    @patch("stockstui.cli.ConfigManager")
    def test_filters_by_requested_tags_and_session_lists(
        self, mock_cfg_cls: MagicMock, mock_get_prices: MagicMock
    ) -> None:
        mock_cfg_cls.return_value = _make_config(
            lists={
                "stocks": [
                    {"ticker": "AAPL", "alias": "", "tags": "core,tech"},
                    {"ticker": "XOM", "alias": "", "tags": "energy"},
                ]
            },
            settings={"hidden_tabs": ["fred"]},
        )
        mock_get_prices.return_value = [
            {
                "symbol": "XOM",
                "description": "Exxon Mobil",
                "price": 110.0,
                "previous_close": 115.0,
                "currency": "USD",
                "day_low": 109.0,
                "day_high": 112.0,
                "fifty_two_week_low": 95.0,
                "fifty_two_week_high": 125.0,
            }
        ]

        output = self._run_with_captured_console(
            _make_args(output="stocks", tags="energy")
        )

        mock_get_prices.assert_called_once_with(
            ["XOM"], enable_pre_post_market=False
        )
        self.assertIn("Exxon Mobil", output)
        self.assertIn("-4.35%", output)

    @patch("stockstui.cli.ConfigManager")
    def test_reports_error_when_no_matching_lists(
        self, mock_cfg_cls: MagicMock
    ) -> None:
        mock_cfg_cls.return_value = _make_config(lists={}, settings={})
        output = self._run_with_captured_console(_make_args(output="nonexistent"))
        self.assertIn("No visible lists found", output)

    @patch("stockstui.cli.fred_provider.get_series_summary")
    @patch("stockstui.cli.ConfigManager")
    def test_renders_fred_section_when_requested(
        self, mock_cfg_cls: MagicMock, mock_fred_summary: MagicMock
    ) -> None:
        mock_cfg_cls.return_value = _make_config(
            lists={},
            settings={
                "fred_settings": {
                    "api_key": "test-key",
                    "series_list": ["CPIAUCSL"],
                    "series_aliases": {"CPIAUCSL": "CPI"},
                }
            },
        )
        mock_fred_summary.return_value = {
            "id": "CPIAUCSL",
            "title": "Consumer Price Index",
            "current": 314.5,
            "yoy_pct": 2.9,
            "z_10y": 0.45,
            "pct_of_range": 82.0,
            "date": "2025-01-01",
        }

        output = self._run_with_captured_console(_make_args(output="fred"))

        mock_fred_summary.assert_called_once_with("CPIAUCSL", "test-key")
        self.assertIn("Economic Data (FRED)", output)
        self.assertIn("CPI", output)
        self.assertIn("314.50", output)


if __name__ == "__main__":
    unittest.main()
