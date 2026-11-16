import unittest

from textual.app import App, ComposeResult
from textual.widgets import DataTable

from stockstui.ui.themes import (
    extract_theme_variables,
    resolve_themes,
    substitute_colors,
)
from stockstui.ui.view_registry import (
    ORDERED_TOGGLEABLE_SPECIAL_CATEGORIES,
    SPECIAL_TAB_BY_CATEGORY,
    is_watchlist_category,
)
from stockstui.ui.views.watchlist_view import (
    build_alias_map,
    filter_symbols_by_tags,
    get_available_tags_for_category,
    get_symbols_for_category,
    get_visible_column_keys,
    sort_price_table,
    style_and_populate_price_table,
)


class TestThemes(unittest.TestCase):
    def test_substitute_colors_resolves_nested_variables_and_flags_missing(self) -> None:
        template = {"primary": "$blue", "variables": {"muted": "$missing"}, "dark": True}
        resolved = substitute_colors(template, {"blue": "#0000ff"})
        self.assertEqual(resolved["primary"], "#0000ff")
        self.assertEqual(resolved["variables"]["muted"], "UNDEFINED_MISSING")
        self.assertTrue(resolved["dark"])

    def test_resolve_themes_skips_incomplete_palettes_and_extracts_vars(self) -> None:
        full_palette = {
            "blue": "#458588",
            "cyan": "#689d6a",
            "orange": "#d65d0e",
            "green": "#98971a",
            "yellow": "#d79921",
            "red": "#cc241d",
            "bg3": "#1d2021",
            "bg2": "#282828",
            "bg1": "#3c3836",
            "bg0": "#504945",
            "fg0": "#fbf1c7",
            "fg1": "#ebdbb2",
            "fg2": "#d5c4a1",
            "fg3": "#bdae93",
        }
        themes, resolved = resolve_themes(
            {
                "valid": {"dark": True, "palette": full_palette},
                "broken": {"dark": False, "palette": {"blue": "#00f"}},
                "empty": {"dark": False},
            }
        )
        self.assertEqual(list(themes.keys()), ["valid"])
        vars_map = extract_theme_variables(resolved["valid"])
        self.assertEqual(vars_map["price"], "#689d6a")
        self.assertEqual(vars_map["primary"], "#458588")


class TestViewRegistry(unittest.TestCase):
    def test_watchlist_category_classification(self) -> None:
        self.assertTrue(is_watchlist_category("all"))
        self.assertTrue(is_watchlist_category("stocks"))
        self.assertFalse(is_watchlist_category(None))
        for special in (*ORDERED_TOGGLEABLE_SPECIAL_CATEGORIES, "configs"):
            self.assertFalse(is_watchlist_category(special))
            self.assertIn(special, SPECIAL_TAB_BY_CATEGORY)


class TestWatchlistHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self.lists = {
            "stocks": [
                {"ticker": "AAPL", "alias": "Apple", "tags": "tech, mega"},
                {"ticker": "MSFT", "alias": "Microsoft", "tags": "tech"},
            ],
            "energy": [
                {"ticker": "XOM", "alias": "Exxon", "tags": "oil"},
                {"ticker": "AAPL", "alias": "Duplicate Apple", "tags": "tech"},
            ],
            "secret": [
                {"ticker": "NVDA", "alias": "Nvidia", "tags": "semi"},
            ],
        }
        self.hidden = {"secret"}

    def test_symbols_and_aliases_exclude_hidden_lists_and_deduplicate(self) -> None:
        symbols = get_symbols_for_category(self.lists, self.hidden, "all")
        self.assertEqual(symbols, ["AAPL", "MSFT", "XOM"])
        aliases = build_alias_map(self.lists, self.hidden)
        self.assertNotIn("NVDA", aliases)
        self.assertIn("MSFT", aliases)

    def test_tag_discovery_and_filtering(self) -> None:
        tags = get_available_tags_for_category(self.lists, self.hidden, "all")
        self.assertEqual(tags, ["mega", "oil", "tech"])

        symbols = get_symbols_for_category(self.lists, self.hidden, "all")
        filtered = filter_symbols_by_tags(
            self.lists, self.hidden, "all", symbols, ["oil"]
        )
        self.assertEqual(filtered, ["XOM"])

    def test_visible_columns_defaults_when_empty(self) -> None:
        cols = get_visible_column_keys([])
        self.assertIn("Ticker", cols)
        self.assertIn("Price", cols)

        custom = get_visible_column_keys(
            [{"key": "Ticker", "visible": True}, {"key": "Price", "visible": False}]
        )
        self.assertEqual(custom, ["Ticker"])

class _TableHostApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable(id="price-table")


class TestWatchlistTableRendering(unittest.IsolatedAsyncioTestCase):
    async def test_style_populate_and_sort_price_table(self) -> None:
        app = _TableHostApp()
        async with app.run_test():
            table = app.query_one("#price-table", DataTable)
            cols = ["Ticker", "Description", "Price", "Change", "% Change"]
            for col in cols:
                table.add_column(col, key=col)

            flashed: list[tuple[str, str, str]] = []
            rows = [
                {
                    "Ticker": "AAPL",
                    "Description": "Apple",
                    "Price": 200.0,
                    "Change": 5.0,
                    "% Change": 0.025,
                    "_currency_symbol": "$",
                    "_change_direction": "up",
                },
                {
                    "Ticker": "MSFT",
                    "Description": "Microsoft",
                    "Price": 420.0,
                    "Change": -2.0,
                    "% Change": -0.005,
                    "_currency_symbol": "$",
                    "_change_direction": None,
                },
                {
                    "Ticker": "BAD",
                    "Description": "Invalid Ticker",
                    "Price": None,
                    "Change": None,
                    "% Change": None,
                    "_currency_symbol": "$",
                },
            ]

            style_and_populate_price_table(
                table,
                rows,
                cols,
                {},
                on_flash=lambda r, c, t: flashed.append((r, c, t)),
            )
            self.assertEqual(table.row_count, 3)
            self.assertEqual(
                flashed,
                [("AAPL", "Change", "positive"), ("AAPL", "% Change", "positive")],
            )

            sort_price_table(table, "Price", reverse=False)
            ordered_keys = [row.key.value for row in table.ordered_rows]
            self.assertEqual(ordered_keys, ["AAPL", "MSFT", "BAD"])


if __name__ == "__main__":
    unittest.main()
