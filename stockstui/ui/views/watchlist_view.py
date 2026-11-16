import logging
import re
from collections.abc import Callable
from typing import Any

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Label
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist

from stockstui.data_providers import market_provider
from stockstui.presentation import formatter
from stockstui.ui.quick_edit_ticker_modal import QuickEditTickerModal
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable
from stockstui.ui.widgets.tag_filter import TagFilterWidget
from stockstui.utils import extract_cell_text, match_tags, parse_tags

logger = logging.getLogger(__name__)

DEFAULT_COLUMN_SETTINGS: list[dict[str, Any]] = [
    {"key": "Ticker", "visible": True},
    {"key": "Description", "visible": True},
    {"key": "Price", "visible": True},
    {"key": "Change", "visible": True},
    {"key": "% Change", "visible": True},
    {"key": "Day's Range", "visible": True},
    {"key": "52-Wk Range", "visible": True},
    {"key": "All Time High", "visible": True},
    {"key": "% Off ATH", "visible": True},
    {"key": "Next Earnings", "visible": False},
    {"key": "Ex-Div Date", "visible": False},
]

NUMERIC_SORT_COLUMNS: frozenset[str] = frozenset(
    {
        "Price",
        "Change",
        "% Change",
        "Volume",
        "Open",
        "Prev Close",
    }
)


def get_visible_column_keys(column_settings: list[dict[str, Any]] | None) -> list[str]:
    """Extracts ordered visible column keys from config column_settings."""
    settings = column_settings or DEFAULT_COLUMN_SETTINGS
    return [
        col["key"]
        for col in settings
        if isinstance(col, dict) and col.get("visible", True)
    ]


def build_alias_map(
    lists: dict[str, list[dict[str, Any]]], hidden_tabs: set[str]
) -> dict[str, str]:
    """Creates a mapping from ticker symbol to its user-defined alias across visible lists."""
    alias_map: dict[str, str] = {}
    for list_name, list_data in lists.items():
        if list_name not in hidden_tabs:
            for item in list_data:
                ticker = item.get("ticker")
                alias = item.get("alias")
                if ticker and alias:
                    alias_map[ticker] = alias
    return alias_map


def get_symbols_for_category(
    lists: dict[str, list[dict[str, Any]]],
    hidden_tabs: set[str],
    category: str,
) -> list[str]:
    """Returns deduplicated ordered symbols for 'all' or a named list category."""
    if category == "all":
        seen: set[str] = set()
        symbols: list[str] = []
        for list_name, lst in lists.items():
            if list_name not in hidden_tabs:
                for item in lst:
                    ticker = item["ticker"]
                    if ticker not in seen:
                        symbols.append(ticker)
                        seen.add(ticker)
        return symbols
    return [item["ticker"] for item in lists.get(category, [])]


def get_available_tags_for_category(
    lists: dict[str, list[dict[str, Any]]],
    hidden_tabs: set[str],
    category: str,
) -> list[str]:
    """Gets sorted unique tags across tickers in the specified category."""
    all_tags: set[str] = set()
    lists_to_check: list[list[dict[str, Any]]] = []

    if category == "all":
        for list_name, list_data in lists.items():
            if list_name not in hidden_tabs:
                lists_to_check.append(list_data)
    elif category in lists and category not in hidden_tabs:
        lists_to_check.append(lists[category])

    for list_data in lists_to_check:
        for item in list_data:
            tags_str = item.get("tags", "")
            if tags_str and isinstance(tags_str, str):
                all_tags.update(parse_tags(tags_str))

    return sorted(all_tags)


def filter_symbols_by_tags(
    lists: dict[str, list[dict[str, Any]]],
    hidden_tabs: set[str],
    category: str,
    symbols: list[str],
    active_tags: list[str],
) -> list[str]:
    """Filters symbols by active tag filter, preserving original order and deduplicating."""
    if not active_tags:
        return symbols

    seen_symbols: set[str] = set()
    ordered_filtered_symbols: list[str] = []
    lists_to_check: list[list[dict[str, Any]]] = []

    if category == "all":
        for list_name, list_data in lists.items():
            if list_name not in hidden_tabs:
                lists_to_check.append(list_data)
    elif category in lists and category not in hidden_tabs:
        lists_to_check.append(lists.get(category, []))

    for list_data in lists_to_check:
        for item in list_data:
            ticker = item.get("ticker")
            if ticker in symbols and ticker not in seen_symbols:
                item_tags_str = item.get("tags", "")
                item_tags = parse_tags(item_tags_str) if item_tags_str else []
                if match_tags(item_tags, active_tags):
                    ordered_filtered_symbols.append(ticker)
                seen_symbols.add(ticker)

    logger.info(f"Filtered symbols (ordered): {ordered_filtered_symbols}")
    return ordered_filtered_symbols


def style_and_populate_price_table(
    price_table: DataTable,
    rows: list[dict[str, Any]],
    visible_columns: list[str],
    theme_vars: dict[str, Any],
    on_flash: Callable[[str, str, str], None] | None = None,
) -> None:
    """Applies dynamic theme styling and populates the watchlist DataTable."""
    price_color = theme_vars.get("price", "cyan")
    success_color = theme_vars.get("success", "green")
    error_color = theme_vars.get("error", "red")
    muted_color = theme_vars.get("text-muted", "dim")

    for item in rows:
        symbol = item.get("Ticker")
        if not symbol:
            continue

        row_values: list[Text] = []
        change_direction = item.get("_change_direction")
        cur_sym = item.get("_currency_symbol", "$")

        for col_key in visible_columns:
            val = item.get(col_key)

            if col_key == "Description":
                if val == "Invalid Ticker":
                    text = Text(str(val), style=error_color)
                elif val == "N/A":
                    text = Text(str(val), style=muted_color)
                else:
                    text = Text(str(val))
            elif col_key == "Price":
                text = (
                    Text(
                        f"{cur_sym}{val:,.2f}",
                        style=price_color,
                        justify="right",
                    )
                    if val is not None
                    else Text("N/A", style=muted_color, justify="right")
                )
            elif col_key == "Change":
                if val is not None:
                    style = (
                        success_color
                        if val > 0
                        else (error_color if val < 0 else "")
                    )
                    text = Text(f"{val:,.2f}", style=style, justify="right")
                else:
                    text = Text("N/A", style=muted_color, justify="right")
            elif col_key == "% Change":
                if val is not None:
                    style = (
                        success_color
                        if val > 0
                        else (error_color if val < 0 else "")
                    )
                    text = Text(f"{val:.2%}", style=style, justify="right")
                else:
                    text = Text("N/A", style=muted_color, justify="right")
            elif col_key == "All Time High":
                text = (
                    Text(
                        f"{cur_sym}{val:,.2f}",
                        style=price_color,
                        justify="right",
                    )
                    if val is not None
                    else Text("N/A", style=muted_color, justify="right")
                )
            elif col_key == "% Off ATH":
                if val is not None:
                    style = (
                        error_color
                        if val < 0
                        else (success_color if val > 0 else "")
                    )
                    text = Text(f"{val:.2%}", style=style, justify="right")
                else:
                    text = Text("N/A", style=muted_color, justify="right")
            elif col_key in (
                "Volume",
                "Open",
                "Prev Close",
                "Day's Range",
                "52-Wk Range",
            ):
                text = Text(
                    str(val),
                    style=muted_color if val == "N/A" else "",
                    justify="right",
                )
            elif col_key == "Ticker":
                text = Text(str(val), style=muted_color)
            else:
                text = Text(str(val))

            row_values.append(text)

        price_table.add_row(*row_values, key=symbol)

        if change_direction and on_flash is not None:
            flash_kind = "positive" if change_direction == "up" else "negative"
            if "Change" in visible_columns:
                on_flash(symbol, "Change", flash_kind)
            if "% Change" in visible_columns:
                on_flash(symbol, "% Change", flash_kind)


def sort_price_table(
    table: DataTable, sort_column_key: str | None, reverse: bool
) -> None:
    """Applies the given sort column and direction to a watchlist DataTable."""
    if sort_column_key is None:
        return

    def sort_key(row_values: tuple[Any, ...]) -> tuple[int, Any]:
        try:
            col_index = table.get_column_index(sort_column_key)
        except CellDoesNotExist:
            return (1, 0)
        if col_index >= len(row_values):
            return (1, 0)
        text_content = extract_cell_text(row_values[col_index])
        if text_content in ("N/A", "Invalid Ticker"):
            return (1, 0)
        if text_content == "":
            return (1, 0)
        if sort_column_key in ("Description", "Ticker"):
            return (0, text_content.lower())
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_content):
            # ISO dates sort correctly as plain strings; float() would reject them.
            return (0, text_content)
        cleaned_text = re.sub(r"^[^\d\.\-]+", "", text_content)
        cleaned_text = (
            cleaned_text.replace(",", "").replace("%", "").replace("+", "")
        )
        try:
            return (0, float(cleaned_text))
        except (ValueError, TypeError):
            return (1, 0)

    try:
        table.sort(key=sort_key, reverse=reverse)
    except KeyError:
        logger.error(f"Could not find column for sort key '{sort_column_key}'")


def flash_table_cell(
    dt: DataTable,
    theme_vars: dict[str, Any],
    set_timer: Callable[[float, Callable[[], None]], Any],
    row_key: str,
    column_key: str,
    flash_type: str,
) -> None:
    """Applies a brief positive/negative background flash to a DataTable cell."""
    try:
        current_content = dt.get_cell(row_key, column_key)
        if not isinstance(current_content, Text):
            return

        flash_color_name = (
            theme_vars.get("success")
            if flash_type == "positive"
            else theme_vars.get("error")
        )
        if not flash_color_name:
            return
        bg_color = Color.parse(flash_color_name).with_alpha(0.3)
        flash_text_color = theme_vars.get("background")
        new_style = Style(color=flash_text_color, bgcolor=bg_color.rich_color)
        flashed_content = Text(
            current_content.plain,
            style=new_style,
            justify=current_content.justify,
        )
        dt.update_cell(row_key, column_key, flashed_content, update_width=False)
        set_timer(
            0.8,
            lambda: unflash_table_cell(dt, row_key, column_key, current_content),
        )
    except (KeyError, NoMatches, AttributeError, CellDoesNotExist):
        return


def unflash_table_cell(
    dt: DataTable, row_key: str, column_key: str, original_content: Text
) -> None:
    """Restores a DataTable cell after a flash expires."""
    try:
        dt.update_cell(row_key, column_key, original_content, update_width=False)
    except (KeyError, NoMatches, CellDoesNotExist):
        return


def handle_price_data_update(
    app: Any, data: list[dict[str, Any]], active_category: str | None, now_str: str
) -> None:
    """Updates #price-table when fresh market data arrives."""
    try:
        dt = app.query_one("#price-table", DataTable)
        if len(dt.columns) == 0:
            return

        app._pre_refresh_cursor_key = None
        app._pre_refresh_cursor_column = None
        if not app._is_filter_refresh and dt.row_count > 0 and dt.cursor_row >= 0:
            try:
                coordinate = Coordinate(row=dt.cursor_row, column=0)
                app._pre_refresh_cursor_key = dt.coordinate_to_cell_key(
                    coordinate
                ).row_key
                app._pre_refresh_cursor_column = dt.cursor_column
            except CellDoesNotExist:
                app._pre_refresh_cursor_key = None
                app._pre_refresh_cursor_column = None

        dt.loading = False
        dt.clear()

        symbols_on_screen = (
            get_symbols_for_category(
                app.config.lists,
                app._hidden_tabs,
                active_category,
            )
            if active_category
            else []
        )
        ordered_filtered_symbols = (
            app._filter_symbols_by_tags(active_category, symbols_on_screen)
            if active_category
            else symbols_on_screen
        )

        data_map = {item["symbol"]: item for item in data}
        data_for_table = [
            data_map[symbol]
            for symbol in ordered_filtered_symbols
            if symbol in data_map
        ]

        if not data_for_table:
            if ordered_filtered_symbols:
                dt.add_row(
                    "[dim]Could not fetch data for any symbols in this list.[/dim]"
                )
            elif symbols_on_screen and not ordered_filtered_symbols:
                dt.add_row("[dim]No symbols match the current tag filter.[/dim]")
            else:
                dt.add_row(
                    f"[dim]No symbols in list '{active_category}'. Add some in the Configs tab.[/dim]"
                )
            return

        alias_map = app._get_alias_map()
        rows = formatter.format_price_data_for_table(
            data_for_table, app._price_comparison_data, alias_map
        )
        app._style_and_populate_price_table(dt, rows)

        app._price_comparison_data = {
            item["symbol"]: item.get("price")
            for item in data_for_table
            if item.get("price") is not None
        }

        app._apply_price_table_sort()
        app.query_one("#last-refresh-time", Label).update(now_str)

        if app._pre_refresh_cursor_key:
            try:
                new_row_index = dt.get_row_index(app._pre_refresh_cursor_key)
                if app._pre_refresh_cursor_column is not None:
                    dt.move_cursor(
                        row=new_row_index,
                        column=app._pre_refresh_cursor_column,
                    )
                else:
                    dt.move_cursor(row=new_row_index)
            except (KeyError, RowDoesNotExist):
                pass

        app._is_filter_refresh = False
    except NoMatches:
        return


def redisplay_price_table_from_cache(app: Any, active_category: str | None) -> None:
    """Re-draws #price-table using only in-memory cached prices."""
    if not active_category:
        return
    try:
        dt = app.query_one("#price-table", DataTable)
        dt.clear()

        symbols_on_screen = get_symbols_for_category(
            app.config.lists, app._hidden_tabs, active_category
        )
        ordered_filtered_symbols = app._filter_symbols_by_tags(
            active_category, symbols_on_screen
        )

        data_for_table: list[dict[str, Any]] = []
        for symbol in ordered_filtered_symbols:
            cached = market_provider.get_cached_price(symbol)
            if cached is not None:
                data_for_table.append(cached)

        if not data_for_table:
            if ordered_filtered_symbols:
                dt.add_row(
                    "[dim]No cached data for symbols in this filter. Press 'r' to refresh.[/dim]"
                )
            else:
                dt.add_row("[dim]No symbols match the current tag filter.[/dim]")
            return

        alias_map = app._get_alias_map()
        rows = formatter.format_price_data_for_table(
            data_for_table, app._price_comparison_data, alias_map
        )
        app._style_and_populate_price_table(dt, rows)
        app._apply_price_table_sort()
        app._is_filter_refresh = False
    except NoMatches:
        return


def update_tag_filter_status_widget(app: Any, category: str) -> None:
    """Updates the tag filter count summary for `category`."""
    try:
        tag_filter = app.query_one("#tag-filter", TagFilterWidget)
        hidden_tabs = app._hidden_tabs
        if category == "all":
            total_symbols = list(
                {
                    s["ticker"]
                    for list_name, lst in app.config.lists.items()
                    for s in lst
                    if list_name not in hidden_tabs
                }
            )
        elif category not in hidden_tabs:
            total_symbols = [
                s["ticker"] for s in app.config.lists.get(category, [])
            ]
        else:
            total_symbols = []

        filtered_symbols = app._filter_symbols_by_tags(category, total_symbols)
        tag_filter.update_filter_status(len(filtered_symbols), len(total_symbols))
    except NoMatches:
        logger.debug("TagFilter widget not found during update")


def open_quick_edit_ticker_modal(app: Any, category: str) -> None:
    """Opens QuickEditTickerModal for the currently selected watchlist row."""
    try:
        price_table = app.query_one("#price-table", DataTable)
        if price_table.cursor_row < 0:
            app.notify("Select a ticker to edit.", severity="warning")
            return

        coordinate = Coordinate(row=price_table.cursor_row, column=0)
        ticker_val = price_table.coordinate_to_cell_key(coordinate).row_key.value
        if not ticker_val:
            app.notify("Invalid ticker selected.", severity="error")
            return
        ticker = ticker_val

        ticker_data: dict[str, Any] | None = None
        list_names: list[str] = []

        if category == "all":
            hidden_tabs = app._hidden_tabs
            for list_name, list_data in app.config.lists.items():
                if list_name not in hidden_tabs:
                    for item in list_data:
                        item_ticker = item.get("ticker")
                        if item_ticker and item_ticker.upper() == ticker.upper():
                            if ticker_data is None:
                                ticker_data = item.copy()
                            list_names.append(list_name)
                            break
        else:
            list_data = app.config.lists.get(category, [])
            for item in list_data:
                item_ticker = item.get("ticker")
                if item_ticker and item_ticker.upper() == ticker.upper():
                    ticker_data = item.copy()
                    list_names.append(category)
                    break

        if not ticker_data:
            app.notify(
                f"Could not find ticker '{ticker}' in configuration.",
                severity="error",
            )
            return

        def on_close(result: tuple[str, str] | None) -> None:
            if result:
                field, value = result
                for list_name in list_names:
                    for item in app.config.lists[list_name]:
                        if item.get("ticker", "").upper() == ticker.upper():
                            item[field] = value
                            break
                app.config.save_lists()
                app.action_refresh(force=False)
                app.notify(f"Updated {field} for '{ticker}'.")

        app.push_screen(
            QuickEditTickerModal(ticker, category, ticker_data), on_close
        )
    except NoMatches:
        app.bell()


def toggle_tag_filter_widget(app: Any) -> None:
    """Toggles visibility of #tag-filter when tags exist for the active list."""
    try:
        tag_filter = app.query_one("#tag-filter", TagFilterWidget)
        if not tag_filter.available_tags:
            app.notify("No tags available for this list.", severity="information")
            app.bell()
            return

        tag_filter.display = not tag_filter.display
        if tag_filter.display:
            try:
                first_button = tag_filter.query_one(".tag-button", Button)
                first_button.focus()
            except NoMatches:
                pass
    except NoMatches:
        app.bell()


class WatchlistView(Vertical):
    """View hosting the tag filter bar and watchlist/price DataTable."""

    app: Any

    def compose(self) -> ComposeResult:
        yield TagFilterWidget(available_tags=[], id="tag-filter")
        yield NavigableDataTable(id="price-table", zebra_stripes=True)

    async def display_category(self, category: str) -> None:
        """Configures tag filters and columns for `category` and renders cached or fetched prices."""
        try:
            old_tag_filter = self.query_one("#tag-filter")
            await old_tag_filter.remove()
        except NoMatches:
            pass

        available_tags = self.app._get_available_tags_for_category(category)
        if available_tags:
            tag_filter = TagFilterWidget(
                available_tags=available_tags, id="tag-filter"
            )
            tag_filter.display = False
            price_table = self.query_one("#price-table")
            await self.mount(tag_filter, before=price_table)

        price_table = self.query_one("#price-table", NavigableDataTable)
        price_table.clear(columns=True)

        for col_key in self.app._visible_columns:
            price_table.add_column(col_key, key=col_key)

        symbols = get_symbols_for_category(
            self.app.config.lists, self.app._hidden_tabs, category
        )
        symbols = self.app._filter_symbols_by_tags(category, symbols)

        if symbols and not any(market_provider.is_cached(s) for s in symbols):
            price_table.loading = True
            enable_pre_post = self.app.config.get_setting(
                "enable_pre_post_market", False
            )
            self.app.fetch_prices(
                symbols,
                force=False,
                category=category,
                enable_pre_post_market=enable_pre_post,
            )
        elif symbols:
            data_map = {
                item["symbol"]: item
                for s in symbols
                if (item := market_provider.get_cached_price(s))
            }
            cached_data = [data_map[s] for s in symbols if s in data_map]

            if cached_data:
                alias_map = self.app._get_alias_map()
                self.app._price_comparison_data = {
                    item["symbol"]: item.get("price")
                    for item in cached_data
                    if item.get("price") is not None
                }
                rows = formatter.format_price_data_for_table(
                    cached_data, self.app._price_comparison_data, alias_map
                )
                self.app._style_and_populate_price_table(price_table, rows)
                self.app._apply_price_table_sort()
            else:
                price_table.clear()
                price_table.loading = True
                enable_pre_post = self.app.config.get_setting(
                    "enable_pre_post_market", False
                )
                self.app.fetch_prices(
                    symbols,
                    force=False,
                    category=category,
                    enable_pre_post_market=enable_pre_post,
                )
        else:
            price_table.add_row(
                f"[dim]No symbols in list '{category}'. Add some in the Configs tab.[/dim]"
            )

    def handle_price_data_updated(
        self, data: list[dict[str, Any]], active_category: str | None, now_str: str
    ) -> None:
        """Updates #price-table when fresh market data arrives."""
        handle_price_data_update(self.app, data, active_category, now_str)

    def redisplay_from_cache(self, active_category: str | None) -> None:
        """Re-draws #price-table using only in-memory cached prices."""
        redisplay_price_table_from_cache(self.app, active_category)

    def toggle_tag_filter(self) -> None:
        """Toggles visibility of #tag-filter when tags exist for the active list."""
        toggle_tag_filter_widget(self.app)

    def update_tag_filter_status(self, category: str) -> None:
        """Updates the tag filter count summary for `category`."""
        update_tag_filter_status_widget(self.app, category)

    def open_quick_edit_modal(self, category: str) -> None:
        """Opens QuickEditTickerModal for the currently selected watchlist row."""
        open_quick_edit_ticker_modal(self.app, category)
