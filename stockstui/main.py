import logging
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from platformdirs import PlatformDirs
from rich.text import Text
from textual import events, on, work
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ListView,
    Select,
    Switch,
    Tab,
    Tabs,
)
from textual.worker import get_current_worker

from stockstui.cli import run_cli_output, show_manual
from stockstui.common import (
    CacheTestDataUpdated,
    FilingsDataUpdated,
    FredDebugDataUpdated,
    HistoricalDataUpdated,
    ListDebugDataUpdated,
    MarketStatusUpdated,
    NewsDataUpdated,
    OptionsDataUpdated,
    OptionsExpirationsUpdated,
    PriceDataUpdated,
    TickerDebugDataUpdated,
    TickerInfoComparisonUpdated,
)
from stockstui.config_manager import ConfigManager
from stockstui.data_providers import edgar_provider, market_provider, options_provider
from stockstui.data_providers.portfolio import PortfolioManager
from stockstui.database.db_manager import DbManager
from stockstui.log_handler import TextualHandler
from stockstui.parser import create_arg_parser
from stockstui.presentation import formatter
from stockstui.ui.themes import (
    BASE_THEME_STRUCTURE,
    extract_theme_variables,
    resolve_themes,
    substitute_colors,
)
from stockstui.ui.view_registry import (
    CONFIGS_TAB_SPEC,
    ORDERED_TOGGLEABLE_SPECIAL_CATEGORIES,
    SPECIAL_TAB_BY_CATEGORY,
    is_watchlist_category,
)
from stockstui.ui.views.calendar_view import CalendarView
from stockstui.ui.views.config_view import ConfigContainer
from stockstui.ui.views.config_views.general_config_view import GeneralConfigView
from stockstui.ui.views.config_views.lists_config_view import ListsConfigView
from stockstui.ui.views.debug_view import (
    DebugView,
    populate_cache_test_table,
    populate_fred_debug_table,
    populate_info_comparison_table,
    populate_list_debug_table,
    populate_ticker_debug_table,
)
from stockstui.ui.views.filings_view import FilingsView
from stockstui.ui.views.fred_view import FredView
from stockstui.ui.views.history_view import HistoryView, sort_history_table
from stockstui.ui.views.news_view import NewsView
from stockstui.ui.views.options_view import OptionsView
from stockstui.ui.views.watchlist_view import (
    NUMERIC_SORT_COLUMNS,
    WatchlistView,
    build_alias_map,
    filter_symbols_by_tags,
    flash_table_cell,
    get_available_tags_for_category,
    get_symbols_for_category,
    get_visible_column_keys,
    handle_price_data_update,
    open_quick_edit_ticker_modal,
    redisplay_price_table_from_cache,
    sort_price_table,
    style_and_populate_price_table,
    toggle_tag_filter_widget,
    unflash_table_cell,
    update_tag_filter_status_widget,
)
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable
from stockstui.ui.widgets.search_box import SearchBox
from stockstui.ui.widgets.tag_filter import TagFilterChanged
from stockstui.utils import extract_cell_text

__all__ = [
    "BASE_THEME_STRUCTURE",
    "StocksTUI",
    "main",
    "run_cli_output",
    "show_manual",
    "substitute_colors",
]

logger = logging.getLogger(__name__)


class StocksTUI(App):
    """
    Main Textual application orchestrating layout composition, global bindings,
    background workers, and routing across view modules.
    """

    CSS_PATH = "main.css"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit", priority=True, show=True),
        Binding("Z", "quit", "Quit", priority=True, show=False),
        Binding("r", "refresh(False)", "Refresh", show=True),
        Binding("R", "refresh(True)", "Force Refresh", show=True),
        Binding("s", "enter_sort_mode", "Sort", show=True),
        Binding("o", "enter_open_mode", "Open", show=True),
        Binding("/", "focus_search", "Search", show=True),
        Binding("f", "toggle_tag_filter", "Filter", show=True),
        Binding("?", "toggle_help", "Toggle Help", show=True),
        Binding("i", "focus_input", "Input", show=False),
        Binding("enter", "activate_tab", "Activate Tab", show=False),
        Binding("d", "handle_sort_key('d')", "Sort by Description/Date", show=False),
        Binding("p", "handle_sort_key('p')", "Sort by Price", show=False),
        Binding("c", "handle_sort_key('c')", "Sort by Change/Close", show=False),
        Binding("e", "handle_sort_key('e')", "Sort by % Change", show=False),
        Binding("t", "handle_sort_key('t')", "Sort by Ticker", show=False),
        Binding("u", "handle_sort_key('u')", "Undo Sort", show=False),
        Binding("v", "handle_sort_key('v')", "Sort by Volume", show=False),
        Binding("o", "handle_sort_key('o')", "Sort by Open", show=False),
        Binding("ctrl+c", "copy_text", "Copy", show=False),
        Binding("ctrl+C", "copy_text", "Copy", show=False),
        Binding(
            "escape,ctrl+[",
            "dismiss_or_unfocus",
            "Dismiss / Unfocus",
            show=False,
            priority=True,
        ),
        Binding("backspace", "back_or_dismiss", "Back", show=False),
        Binding("k,up", "move_cursor('up')", "Up", show=False),
        Binding("j,down", "move_cursor('down')", "Down", show=False),
        Binding("h,left", "move_cursor('left')", "Left", show=False),
        Binding("l,right", "move_cursor('right')", "Right", show=False),
    ]

    active_list_category: reactive[str | None] = reactive(None)
    news_ticker: reactive[str | None] = reactive(None)
    filings_ticker: reactive[str | None] = reactive(None)
    history_ticker: reactive[str | None] = reactive(None)
    options_ticker: reactive[str | None] = reactive(None)
    search_target_table: reactive[DataTable | None] = reactive(None)
    selected_portfolio = reactive("default")
    active_tag_filter: reactive[list[str]] = reactive([])

    def __init__(self, cli_overrides: dict | None = None):
        super().__init__()
        self.cli_overrides = cli_overrides or {}
        self.last_key: events.Key | None = None

        self.config = ConfigManager(Path(__file__).resolve().parent)
        self.price_refresh_timer: Timer | None = None
        self.market_status_timer: Timer | None = None
        self._price_comparison_data: dict[str, Any] = {}

        self.db_manager = DbManager(self.config.db_path)
        self.portfolio_manager = PortfolioManager(self.config)

        market_provider.populate_price_cache(self.db_manager.load_price_cache_from_db())
        market_provider.populate_info_cache(self.db_manager.load_info_cache_from_db())
        market_provider.set_bar_store(self.db_manager)

        self._last_refresh_times: dict[str, str] = {}
        self._available_theme_names: list[str] = []
        self._processed_themes: dict[str, Any] = {}
        self.theme_variables: dict[str, str] = {}
        self._original_table_data: list[tuple[Any, list[Any]]] = []
        self._last_historical_data = None
        self._last_options_data: dict[str, Any] | None = None
        self.option_positions = self.db_manager.get_all_option_positions()
        self._news_content_for_ticker: str | None = None
        self._last_news_content: tuple[str | Text, list[str]] | None = None
        self._sort_column_key: str | None = None
        self._sort_reverse: bool = False
        self._history_sort_column_key: str | None = None
        self._history_sort_reverse: bool = False
        self._history_period = "1mo"
        self._sort_mode = False
        self._open_mode = False
        self._original_status_text: Any = None
        self._last_active_category: str | None = None
        self._last_config_sub_view: str | None = None
        self._is_rebuilding = False
        self._force_config_sub_view: str | None = None
        self._pre_refresh_cursor_key: Any = None
        self._pre_refresh_cursor_column: int | None = None
        self._is_filter_refresh = False

        self._refresh_hidden_tabs()
        self._rebuild_visible_columns()

        if session_lists := self.cli_overrides.get("session_list"):
            for name, tickers in session_lists.items():
                self.config.lists[name] = [
                    {"ticker": ticker, "alias": ticker, "note": ""}
                    for ticker in tickers
                ]

        self._setup_dynamic_tabs()
        self._load_and_register_themes()

    def add_option_position(
        self, symbol: str, ticker: str, quantity: float, avg_cost: float
    ) -> None:
        """Adds or updates an option position."""
        self.db_manager.save_option_position(symbol, ticker, quantity, avg_cost)
        self.option_positions[symbol] = {
            "symbol": symbol,
            "ticker": ticker,
            "quantity": quantity,
            "avg_cost": avg_cost,
        }
        self.notify(f"Position saved: {symbol}")

    def remove_option_position(self, symbol: str) -> None:
        """Removes an option position."""
        self.db_manager.delete_option_position(symbol)
        if symbol in self.option_positions:
            del self.option_positions[symbol]
        self.notify(f"Position removed: {symbol}")
        self._pre_refresh_cursor_key = None
        self._pre_refresh_cursor_column = None
        self._is_filter_refresh = False

    def compose(self) -> ComposeResult:
        with Vertical(id="app-body"):
            yield Tabs(id="tabs-container")
            with Container(id="app-grid"):
                with ContentSwitcher(
                    id="output-container", initial="price-table-container"
                ):
                    yield WatchlistView(id="price-table-container")
                yield ConfigContainer(id="config-container")
            with Horizontal(id="status-bar-container"):
                yield Label("Market Status: Unknown", id="market-status")
                yield Label("Last Refresh: Never", id="last-refresh-time")
        yield Footer()

    def on_mount(self) -> None:
        logger.info("Application mounting.")

        if not self._available_theme_names:
            self._load_and_register_themes()

        default_theme = "gruvbox_soft_dark"
        active_theme = self.config.get_setting("theme", default_theme)

        if active_theme not in self._available_theme_names:
            self.notify(
                f"Theme '{active_theme}' not found. Falling back to default.",
                title="Theme Error",
                severity="warning",
            )
            logger.warning(
                f"Configured theme '{active_theme}' not found. Falling back to '{default_theme}'."
            )
            active_theme = default_theme

        self.theme = active_theme
        self._update_theme_variables(active_theme)

        config_container = self.query_one(ConfigContainer)
        general_view = config_container.query_one(GeneralConfigView)

        general_view._loading = True
        try:
            general_view.query_one("#theme-select", Select).set_options(
                [(t, t) for t in self._available_theme_names]
            )
            general_view.query_one("#theme-select", Select).value = active_theme
            general_view.query_one(
                "#enable-pre-post-market-switch", Switch
            ).value = self.config.get_setting("enable_pre_post_market", False)
            general_view.query_one(
                "#auto-refresh-switch", Switch
            ).value = self.config.get_setting("auto_refresh", False)
            general_view.query_one(
                "#suppress-tui-logs-switch", Switch
            ).value = self.config.get_setting("suppress_tui_logs", False)
            general_view.query_one("#refresh-interval-input", Input).value = str(
                self.config.get_setting("refresh_interval", 300.0)
            )
            general_view.query_one(
                "#market-calendar-select", Select
            ).value = self.config.get_setting("market_calendar", "NYSE")
        finally:
            general_view._loading = False

        start_category = self.config.get_setting("default_tab_category", "all")
        if self.cli_overrides:
            if cli_tab := self.cli_overrides.get("tab"):
                start_category = cli_tab
            elif cli_history := self.cli_overrides.get("history"):
                start_category = "history"
                if isinstance(cli_history, str):
                    self.history_ticker = cli_history
            elif cli_news := self.cli_overrides.get("news"):
                start_category = "news"
                if isinstance(cli_news, str):
                    self.news_ticker = cli_news
            elif cli_filings := self.cli_overrides.get("filings"):
                start_category = "filings"
                if isinstance(cli_filings, str):
                    self.filings_ticker = cli_filings
            elif cli_options := self.cli_overrides.get("options"):
                start_category = "options"
                if isinstance(cli_options, str):
                    self.options_ticker = cli_options
            elif self.cli_overrides.get("fred"):
                start_category = "fred"
            elif self.cli_overrides.get("debug"):
                start_category = "debug"
            elif self.cli_overrides.get("configs"):
                start_category = "configs"
            elif session_lists := self.cli_overrides.get("session_list"):
                start_category = next(iter(session_lists))

        if self.cli_overrides.get("period"):
            self._history_period = self.cli_overrides["period"]

        if start_category:
            hidden_tabs = self.config.get_setting("hidden_tabs", [])
            if start_category in hidden_tabs:
                hidden_tabs.remove(start_category)
                self.config.settings["hidden_tabs"] = hidden_tabs
                self.config.save_settings()
                self._refresh_hidden_tabs()
                self.notify(f"Auto-enabled '{start_category}' tab visibility.")

        self.call_after_refresh(self._rebuild_app, new_active_category=start_category)
        self.call_after_refresh(self._start_refresh_loops)

    def on_unmount(self) -> None:
        if self.price_refresh_timer:
            self.price_refresh_timer.stop()
        if self.market_status_timer:
            self.market_status_timer.stop()

        self._persist_market_caches()
        self.db_manager.close()
        self.workers.cancel_all()

    @on(events.Key)
    async def on_key(self, event: events.Key) -> None:
        self.last_key = event
        if self._open_mode and event.key in ("n", "h", "y"):
            await self.action_handle_open_key(event.key)
            event.stop()

    def action_dismiss_or_unfocus(self) -> None:
        try:
            if isinstance(self.focused, Input):
                self.focused.blur()
                return
            self.action_back_or_dismiss()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error in dismiss_or_unfocus: {e}")

    def action_back_or_dismiss(self) -> None:
        if isinstance(self.focused, Input):
            last_key = getattr(self, "last_key", None)
            if last_key is not None and getattr(last_key, "key", None) != "escape":
                return

        if self._sort_mode:
            self._sort_mode = False
            self._restore_status_label()
            return
        if self._open_mode:
            self._open_mode = False
            self._restore_status_label()
            return

        try:
            self.query_one(SearchBox).remove()
            self._original_table_data = []
            return
        except NoMatches:
            logger.debug("SearchBox not found during back action")

        try:
            config_container = self.query_one(ConfigContainer)
            if config_container.action_go_back():
                return
        except NoMatches:
            logger.debug("ConfigContainer not found during back action")

        try:
            self.query_one(Tabs).focus()
        except NoMatches:
            logger.debug("Tabs not found during back action fallback")

    def _restore_status_label(self) -> None:
        try:
            status_label = self.query_one("#last-refresh-time", Label)
            if self._original_status_text is not None:
                status_label.update(self._original_status_text)
        except NoMatches:
            logger.debug("Status label not found for restoration")

    def _start_refresh_loops(self) -> None:
        self.action_refresh()
        self._manage_price_refresh_timer()

        calendar = self.config.get_setting("market_calendar", "NYSE")
        try:
            initial_status = market_provider.get_market_status(calendar)
            self._update_market_status_display(initial_status)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Initial market status fetch failed: {e}")

        self._top_up_daily_bars()

    @work(exclusive=True, thread=True)
    def _top_up_daily_bars(self) -> None:
        """
        Refreshes the local bar store for watchlist tickers in the background.

        Runs after the first price refresh so history views and future
        computed metrics work offline on the next launch. Errors are
        non-fatal: the store simply keeps its previous bars.
        """
        seen: set[str] = set()
        tickers: list[str] = []
        for entries in self.config.lists.values():
            for entry in entries:
                ticker = entry.get("ticker", "").upper()
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    tickers.append(ticker)
        if not tickers:
            return
        logger.info(f"Topping up daily bars for {len(tickers)} tickers.")
        one_year_ago = datetime.now(timezone.utc) - timedelta(days=366)
        market_provider.refresh_daily_bars(tickers, one_year_ago)

    def _get_alias_map(self) -> dict[str, str]:
        return build_alias_map(self.config.lists, self._hidden_tabs)

    def _get_available_tags_for_category(self, category: str) -> list[str]:
        return get_available_tags_for_category(
            self.config.lists, self._hidden_tabs, category
        )

    def _filter_symbols_by_tags(self, category: str, symbols: list[str]) -> list[str]:
        return filter_symbols_by_tags(
            self.config.lists,
            self._hidden_tabs,
            category,
            symbols,
            self.active_tag_filter,
        )

    def _update_tag_filter_status(self) -> None:
        category = self.get_active_category()
        if is_watchlist_category(category) and category is not None:
            update_tag_filter_status_widget(self, category)

    def _load_and_register_themes(self) -> None:
        textual_themes, valid_themes = resolve_themes(self.config.themes)
        for theme in textual_themes.values():
            self.register_theme(theme)
        self._processed_themes = valid_themes
        self._available_theme_names = sorted(valid_themes.keys())

    def _update_theme_variables(self, theme_name: str) -> None:
        if theme_name in self._processed_themes:
            self.theme_variables = extract_theme_variables(
                self._processed_themes[theme_name]
            )

    def _refresh_hidden_tabs(self) -> None:
        self._hidden_tabs = set(self.config.get_setting("hidden_tabs", []))

    def _rebuild_visible_columns(self) -> None:
        self._visible_columns = get_visible_column_keys(
            self.config.get_setting("column_settings", [])
        )

    def _setup_dynamic_tabs(self) -> None:
        self.tab_map = []
        hidden_tabs = self._hidden_tabs

        all_list_categories = list(self.config.lists.keys())
        all_possible_categories = list(
            dict.fromkeys(
                ["all", *all_list_categories, *ORDERED_TOGGLEABLE_SPECIAL_CATEGORIES]
            )
        )

        for category in all_possible_categories:
            if category not in hidden_tabs:
                spec = SPECIAL_TAB_BY_CATEGORY.get(category)
                display_name = (
                    spec.label
                    if spec is not None
                    else category.replace("_", " ").capitalize()
                )
                self.tab_map.append({"name": display_name, "category": category})
        self.tab_map.append(
            {"name": CONFIGS_TAB_SPEC.label, "category": CONFIGS_TAB_SPEC.category}
        )

    async def _rebuild_app(
        self, new_active_category: str | None = None, config_sub_view: str | None = None
    ) -> None:
        self._refresh_hidden_tabs()
        self._rebuild_visible_columns()
        self._setup_dynamic_tabs()
        tabs_widget = self.query_one(Tabs)
        current_active_cat = new_active_category or self.get_active_category()
        self._last_active_category = None

        await tabs_widget.clear()
        self._is_rebuilding = True
        for i, tab_data in enumerate(self.tab_map, start=1):
            tab_id = f"tab-{i}"
            if tabs_widget.query(f"#{tab_id}"):
                await tabs_widget.query(f"#{tab_id}").remove()
            await tabs_widget.add_tab(Tab(f"{i}: {tab_data['name']}", id=tab_id))
        self._update_tab_bindings()

        try:
            idx_to_activate = next(
                i
                for i, t in enumerate(self.tab_map, start=1)
                if t["category"] == current_active_cat
            )
        except (StopIteration, NoMatches):
            default_cat = self.config.get_setting("default_tab_category", "all")
            try:
                idx_to_activate = next(
                    i
                    for i, t in enumerate(self.tab_map, start=1)
                    if t["category"] == default_cat
                )
            except (StopIteration, NoMatches):
                idx_to_activate = 1

        self._is_rebuilding = False

        if tabs_widget.tab_count >= idx_to_activate:
            tabs_widget.active = f"tab-{idx_to_activate}"

        if current_active_cat == "configs" and config_sub_view:
            config_container = self.query_one(ConfigContainer)
            view_map = {
                "lists": config_container.show_lists,
                "general": config_container.show_general,
                "portfolios": config_container.show_portfolios,
            }
            show_method = view_map.get(config_sub_view)
            if show_method:
                show_method()
            self._force_config_sub_view = config_sub_view

        config_container = self.query_one(ConfigContainer)
        general_view = config_container.query_one(GeneralConfigView)

        general_view._loading = True
        try:
            default_tab_select = general_view.query_one("#default-tab-select", Select)
            options = [
                (t["name"], t["category"])
                for t in self.tab_map
                if is_watchlist_category(t["category"])
                or SPECIAL_TAB_BY_CATEGORY[t["category"]].can_be_default_tab
            ]
            default_tab_select.set_options(options)

            default_cat_value = self.config.get_setting("default_tab_category", "all")
            valid_option_values = [opt[1] for opt in options]

            if default_cat_value in valid_option_values:
                default_tab_select.value = default_cat_value
            elif options:
                default_tab_select.value = options[0][1]
            else:
                default_tab_select.clear()

            general_view.repopulate_visible_tabs()
        finally:
            general_view._loading = False

        self.query_one(ListsConfigView).repopulate_lists()

        if not (current_active_cat == "configs" and config_sub_view):
            config_container.show_main()

    def get_active_category(self) -> str | None:
        if self._last_active_category is not None:
            return self._last_active_category
        try:
            active_tab_id = self.query_one(Tabs).active
            if active_tab_id:
                return self.tab_map[int(active_tab_id.split("-")[1]) - 1]["category"]
        except (NoMatches, IndexError, ValueError, KeyError):
            return None
        return None

    def _update_tab_bindings(self) -> None:
        for i in range(1, 10):
            self.bind(str(i), f"select_tab({i})", description=f"Tab {i}", show=False)
        self.bind("0", "select_tab(10)", description="Tab 10", show=False)

    def action_select_tab(self, tab_index: int) -> None:
        try:
            tabs = self.query_one(Tabs)
            if tab_index <= tabs.tab_count:
                tabs.active = f"tab-{tab_index}"
        except NoMatches:
            logger.debug("Tabs widget not found for select_tab action")

    def action_copy_text(self) -> None:
        selection = self.screen.get_selected_text()
        if selection is None:
            raise SkipAction()
        self.copy_to_clipboard(selection)

    def _manage_price_refresh_timer(self) -> None:
        if self.price_refresh_timer:
            self.price_refresh_timer.stop()
            self.price_refresh_timer = None
        if self.config.get_setting("auto_refresh", False):
            try:
                interval = float(self.config.get_setting("refresh_interval", 300.0))
                self.price_refresh_timer = self.set_interval(
                    interval, lambda: self.action_refresh(force=False)
                )
                logger.info(
                    f"Auto-refresh timer started with interval of {interval} seconds."
                )
            except (ValueError, TypeError):
                logger.error("Invalid refresh interval.")
        else:
            logger.info("Auto-refresh is disabled.")

    def _schedule_next_market_status_refresh(self, status: dict) -> None:
        if self.market_status_timer:
            self.market_status_timer.stop()

        now = datetime.now(timezone.utc)
        next_open = status.get("next_open")
        next_close = status.get("next_close")
        current_status = status.get("status")

        interval = 300.0

        if current_status == "open" and next_close:
            time_to_close = (next_close - now).total_seconds()
            interval = 30 if time_to_close <= 900 else 300
        elif current_status == "closed" and next_open:
            time_to_open = (next_open - now).total_seconds()
            if 0 < time_to_open <= 900:
                interval = 30
            elif 900 < time_to_open <= 3600 * 2:
                interval = 300
            else:
                interval = 3600

        interval = max(interval, 5.0)
        logger.info(
            f"Market status is '{current_status}'. Scheduling next poll in {interval:.2f} seconds."
        )

        calendar = self.config.get_setting("market_calendar", "NYSE")
        self.market_status_timer = self.set_timer(
            interval, lambda: self.fetch_market_status(calendar)
        )

    def action_refresh(self, force: bool = False) -> None:
        category = self.get_active_category()
        if is_watchlist_category(category) and category is not None:
            symbols = get_symbols_for_category(
                self.config.lists, self._hidden_tabs, category
            )
            logger.info("Applying tag filter")
            symbols = self._filter_symbols_by_tags(category, symbols)

            if symbols:
                try:
                    price_table = self.query_one("#price-table", DataTable)
                    if force and price_table.row_count == 0:
                        price_table.loading = True
                except NoMatches:
                    logger.debug("Price table not found during refresh")

                enable_pre_post = self.config.get_setting(
                    "enable_pre_post_market", False
                )
                self.fetch_prices(
                    symbols,
                    force=force,
                    category=category,
                    enable_pre_post_market=enable_pre_post,
                )

    def action_toggle_help(self) -> None:
        if self.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_toggle_tag_filter(self) -> None:
        category = self.get_active_category()
        if (
            category
            and not is_watchlist_category(category)
            and category not in ("fred", "calendar")
        ):
            self.bell()
            return

        toggle_tag_filter_widget(self)

    def action_move_cursor(self, direction: str) -> None:
        if self.focused and isinstance(self.focused, (Tabs, Tab)):
            tabs = (
                self.focused
                if isinstance(self.focused, Tabs)
                else self.focused.query_ancestor(Tabs)
            )
            if direction == "left":
                tabs.action_previous_tab()
            elif direction == "right":
                tabs.action_next_tab()
            return

        if self.focused and hasattr(self.focused, f"action_cursor_{direction}"):
            getattr(self.focused, f"action_cursor_{direction}")()
            return

        if direction in ("up", "down") and (
            scrollable := self._get_active_scrollable_widget()
        ):
            if direction == "up":
                scrollable.scroll_up(duration=0.5)
            else:
                scrollable.scroll_down(duration=0.5)

    def _get_primary_view_widget(self) -> Widget | None:
        category = self.get_active_category()
        if not category:
            return None

        if category == "configs":
            try:
                config_container = self.query_one(ConfigContainer)
                switcher = config_container.query_one(ContentSwitcher)
                active_view_id = switcher.current
                active_view = config_container.query_one(f"#{active_view_id}")

                if active_view_id == "fred":
                    try:
                        return active_view.query_one("#fred-series-buttons Button")
                    except NoMatches:
                        pass
                elif active_view_id == "lists":
                    try:
                        return active_view.query_one("#symbol-list-view")
                    except NoMatches:
                        pass

                for widget in active_view.query("*"):
                    if getattr(widget, "can_focus", False) and widget.visible:
                        return widget

                return active_view
            except NoMatches:
                try:
                    return self.query_one("#config-container")
                except NoMatches:
                    return None

        if category == "debug":
            try:
                return self.query_one("DebugView").query_one(".debug-buttons Button")
            except NoMatches:
                pass

        spec = SPECIAL_TAB_BY_CATEGORY.get(category)
        target_id = (
            spec.primary_widget_selector if spec is not None else "#price-table"
        )
        if target_id:
            try:
                return self.query_one(target_id)
            except NoMatches:
                return None
        return None

    def _get_active_scrollable_widget(self) -> Widget | None:
        primary_widget = self._get_primary_view_widget()
        if not primary_widget:
            return None

        category = self.get_active_category()
        if category == "configs":
            return self.query_one("#config-container")

        output_container = self.query_one("#output-container")
        spec = SPECIAL_TAB_BY_CATEGORY.get(category or "")
        if spec and spec.scrollable_selector:
            return output_container.query_one(spec.scrollable_selector)
        return output_container

    async def _display_data_for_category(self, category: str) -> None:
        output_container = self.query_one("#output-container", ContentSwitcher)
        config_container: Any = self.query_one("#config-container")

        is_config_tab = category == "configs"
        config_container.display = is_config_tab
        output_container.display = not is_config_tab
        self.query_one("#status-bar-container").display = not is_config_tab
        if is_config_tab:
            if self._force_config_sub_view:
                view_map = {
                    "lists": config_container.show_lists,
                    "general": config_container.show_general,
                    "portfolios": config_container.show_portfolios,
                }
                show_method = view_map.get(self._force_config_sub_view)
                if show_method:
                    show_method()
                self._force_config_sub_view = None
            else:
                current_config_view = config_container.query_one(
                    "ContentSwitcher"
                ).current
                if current_config_view == "general":
                    config_container.show_general()
                elif current_config_view == "lists":
                    config_container.show_lists()
                elif current_config_view == "portfolios":
                    config_container.show_portfolios()
                else:
                    config_container.show_main()
            return

        spec = SPECIAL_TAB_BY_CATEGORY.get(category)
        if spec and spec.view_id and spec.view_class:
            try:
                view = output_container.query_one(f"#{spec.view_id}")
            except NoMatches:
                view = spec.view_class(id=spec.view_id)
                await output_container.mount(view)
            output_container.current = spec.view_id
        else:
            output_container.current = "price-table-container"
            watchlist_view = output_container.query_one(
                "#price-table-container", WatchlistView
            )
            await watchlist_view.display_category(category)

    @work(exclusive=True, thread=True)
    def fetch_prices(
        self,
        symbols: list[str],
        force: bool,
        category: str,
        enable_pre_post_market: bool = False,
    ) -> None:
        try:
            data = market_provider.get_market_price_data(
                symbols,
                force_refresh=force,
                enable_pre_post_market=enable_pre_post_market,
            )
            if not get_current_worker().is_cancelled:
                self.post_message(PriceDataUpdated(data, category))
                self._persist_market_caches()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Worker fetch_prices failed for category '{category}': {e}")

    def _persist_market_caches(self) -> None:
        self.db_manager.save_price_cache_to_db(
            market_provider.get_price_cache_state().copy()
        )
        self.db_manager.save_info_cache_to_db(
            market_provider.get_info_cache_state().copy()
        )

    @work(exclusive=True, thread=True)
    def fetch_market_status(self, calendar: str) -> None:
        try:
            status = market_provider.get_market_status(calendar)
            if not get_current_worker().is_cancelled:
                self.post_message(MarketStatusUpdated(status))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Market status worker failed: {e}")

    @work(exclusive=True, thread=True)
    def fetch_news(self, tickers_str: str) -> None:
        try:
            tickers = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
            if not tickers:
                if not get_current_worker().is_cancelled:
                    self.post_message(NewsDataUpdated(tickers_str, []))
                return

            data = market_provider.get_news_for_tickers(tickers)
            if not get_current_worker().is_cancelled:
                self.post_message(NewsDataUpdated(tickers_str, data))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Worker fetch_news failed for {tickers_str}: {e}")
            if not get_current_worker().is_cancelled:
                self.post_message(NewsDataUpdated(tickers_str, None))

    @work(exclusive=True, thread=True)
    def fetch_filings(self, ticker: str) -> None:
        """Fetches recent SEC filings for a ticker from EDGAR."""
        try:
            filings = edgar_provider.fetch_filings(ticker)
            if not get_current_worker().is_cancelled:
                self.post_message(FilingsDataUpdated(ticker, filings))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Worker fetch_filings failed for {ticker}: {e}")
            if not get_current_worker().is_cancelled:
                self.post_message(FilingsDataUpdated(ticker, None))

    @work(exclusive=True, thread=True)
    def fetch_historical_data(
        self, ticker: str, period: str, interval: str = "1d"
    ) -> None:
        try:
            data = market_provider.get_historical_data(ticker, period, interval)
            if not get_current_worker().is_cancelled:
                self.post_message(HistoricalDataUpdated(data))
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Worker fetch_historical_data failed for {ticker} over {period} with interval {interval}: {e}"
            )

    @work(exclusive=True, thread=True)
    def fetch_options_expirations(self, ticker: str) -> None:
        try:
            expirations = options_provider.get_available_expirations(ticker)
            if not get_current_worker().is_cancelled:
                self.post_message(
                    OptionsExpirationsUpdated(ticker, expirations or ())
                )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Worker fetch_options_expirations failed for {ticker}: {e}"
            )
            if not get_current_worker().is_cancelled:
                self.post_message(OptionsExpirationsUpdated(ticker, ()))

    @work(exclusive=True, thread=True)
    def fetch_options_chain(self, ticker: str, expiration: str) -> None:
        try:
            options_data = options_provider.get_options_chain(ticker, expiration)
            if not get_current_worker().is_cancelled:
                if options_data:
                    self.post_message(
                        OptionsDataUpdated(
                            ticker,
                            expiration,
                            options_data.get("calls"),
                            options_data.get("puts"),
                            options_data.get("underlying"),
                        )
                    )
                else:
                    self._last_options_data = {
                        "error": f"Could not fetch options data for {ticker}"
                    }
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Worker fetch_options_chain failed for {ticker} expiring {expiration}: {e}"
            )
            if not get_current_worker().is_cancelled:
                self._last_options_data = {"error": str(e)}

    # Delegating hooks to DebugView for direct callers and tests
    def run_info_comparison_test(self, ticker: str) -> None:
        try:
            self.query_one(DebugView).run_info_comparison_test(ticker)
        except NoMatches:
            pass

    def run_ticker_debug_test(self, symbols: list[str]) -> None:
        try:
            self.query_one(DebugView).run_ticker_debug_test(symbols)
        except NoMatches:
            pass

    def run_list_debug_test(self, lists: dict[str, list[str]]) -> None:
        try:
            self.query_one(DebugView).run_list_debug_test(lists)
        except NoMatches:
            pass

    def run_cache_test(self, lists: dict[str, list[str]]) -> None:
        try:
            self.query_one(DebugView).run_cache_test(lists)
        except NoMatches:
            pass

    def run_fred_debug_test(self, series_list: list[str], api_key: str) -> None:
        try:
            self.query_one(DebugView).run_fred_debug_test(series_list, api_key)
        except NoMatches:
            pass

    def _style_and_populate_price_table(
        self, price_table: DataTable, rows: list[dict]
    ) -> None:
        style_and_populate_price_table(
            price_table,
            rows,
            self._visible_columns,
            self.theme_variables,
            on_flash=self.flash_cell,
        )

    @on(PriceDataUpdated)
    async def on_price_data_updated(self, message: PriceDataUpdated) -> None:
        now_str = f"Last Refresh: {datetime.now():%H:%M:%S}"
        if message.category == "all":
            for cat in [*self.config.lists.keys(), "all"]:
                self._last_refresh_times[cat] = now_str
        else:
            self._last_refresh_times[message.category] = now_str

        active_category = self.get_active_category()
        is_relevant = (active_category == message.category) or (
            message.category == "all"
            and (is_watchlist_category(active_category) or active_category == "fred")
        )
        if not is_relevant:
            return

        handle_price_data_update(self, message.data, active_category, now_str)

    def _update_market_status_display(self, status_data: dict) -> None:
        try:
            format_result = formatter.format_market_status(status_data)
            if not format_result:
                self.query_one("#market-status", Label).update(
                    Text("Market: Unknown", style="dim")
                )
                return

            base_text, parts = format_result
            assembled_text = Text.from_markup(base_text)
            for text, style_var in parts:
                style = self.theme_variables.get(style_var, "")
                assembled_text.append(text, style=style)

            self.query_one("#market-status", Label).update(assembled_text)
            self._schedule_next_market_status_refresh(status_data)
        except NoMatches:
            pass

    @on(MarketStatusUpdated)
    async def on_market_status_updated(self, message: MarketStatusUpdated) -> None:
        self._update_market_status_display(message.status)

    @on(HistoricalDataUpdated)
    async def on_historical_data_updated(self, message: HistoricalDataUpdated) -> None:
        try:
            self.query_one("#history-display-container").loading = False
        except NoMatches:
            return

        self._last_historical_data = message.data
        try:
            history_view = self.query_one(HistoryView)
            await history_view._render_historical_data()
        except NoMatches:
            pass

    @on(OptionsExpirationsUpdated)
    async def on_options_expirations_updated(
        self, message: OptionsExpirationsUpdated
    ) -> None:
        if (
            self.get_active_category() != "options"
            or self.options_ticker != message.ticker
        ):
            return

        try:
            options_view = self.query_one(OptionsView)
            expirations = list(message.expirations) if message.expirations else []
            options_view.update_expirations(expirations)
        except NoMatches:
            pass

    @on(OptionsDataUpdated)
    async def on_options_data_updated(self, message: OptionsDataUpdated) -> None:
        if self.options_ticker != message.ticker:
            # A slow chain fetch for a ticker the user already switched away
            # from must not clobber the current view (expirations handler
            # has the same guard).
            return
        try:
            self.query_one("#options-display-container").loading = False
        except NoMatches:
            return

        self._last_options_data = {
            "ticker": message.ticker,
            "expiration": message.expiration,
            "calls": message.calls_data,
            "puts": message.puts_data,
            "underlying": message.underlying,
        }

        try:
            options_view = self.query_one(OptionsView)
            await options_view._render_options_data()
        except NoMatches:
            pass

    @on(NewsDataUpdated)
    async def on_news_data_updated(self, message: NewsDataUpdated) -> None:
        self._news_content_for_ticker = message.tickers_str
        if message.data is None:
            error_markdown = (
                f"**Error:** Could not retrieve news for '{message.tickers_str}'.\n\n"
                "This may be due to an invalid symbol or a network connectivity issue."
            )
            self._last_news_content = (error_markdown, [])
        else:
            self._last_news_content = formatter.format_news_for_display(message.data)

        if (
            self.get_active_category() == "news"
            and self.news_ticker == message.tickers_str
        ):
            try:
                self.query_one(NewsView).update_content(*self._last_news_content)
            except NoMatches:
                pass

    @on(FilingsDataUpdated)
    async def on_filings_data_updated(self, message: FilingsDataUpdated) -> None:
        if self.get_active_category() != "filings" or self.filings_ticker != message.ticker:
            return
        try:
            self.query_one(FilingsView).update_filings(message.data)
        except NoMatches:
            pass

    # Fallback debug message handlers for tests posting directly to app without DebugView mounted
    async def on_ticker_info_comparison_updated(
        self, message: TickerInfoComparisonUpdated
    ) -> None:
        try:
            for button in self.query(".debug-buttons Button"):
                button.disabled = False
            dt = self.query_one("#debug-table", DataTable)
            populate_info_comparison_table(dt, self.theme_variables, message)
        except NoMatches:
            pass

    async def on_ticker_debug_data_updated(
        self, message: TickerDebugDataUpdated
    ) -> None:
        try:
            for button in self.query(".debug-buttons Button"):
                button.disabled = False
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_ticker_debug_table(dt, self.theme_variables, message)
            self.query_one("#last-refresh-time", Label).update(summary)
        except NoMatches:
            pass

    async def on_list_debug_data_updated(self, message: ListDebugDataUpdated) -> None:
        try:
            for button in self.query(".debug-buttons Button"):
                button.disabled = False
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_list_debug_table(dt, self.theme_variables, message)
            self.query_one("#last-refresh-time", Label).update(summary)
        except NoMatches:
            pass

    async def on_cache_test_data_updated(self, message: CacheTestDataUpdated) -> None:
        try:
            for button in self.query(".debug-buttons Button"):
                button.disabled = False
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_cache_test_table(dt, self.theme_variables, message)
            self.query_one("#last-refresh-time", Label).update(summary)
        except NoMatches:
            pass

    async def on_fred_debug_data_updated(self, message: FredDebugDataUpdated) -> None:
        try:
            for button in self.query(".debug-buttons Button"):
                button.disabled = False
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_fred_debug_table(dt, message)
            self.query_one("#last-refresh-time", Label).update(summary)
        except (NoMatches, KeyError):
            pass

    def _apply_price_table_sort(self) -> None:
        if self._sort_column_key is None:
            return
        try:
            table = self.query_one("#price-table", DataTable)
            sort_price_table(table, self._sort_column_key, self._sort_reverse)
        except NoMatches:
            logger.error(
                f"Could not find table or column for sort key '{self._sort_column_key}'"
            )

    def _apply_history_table_sort(self) -> None:
        if self._history_sort_column_key is None:
            return
        try:
            table = self.query_one("#history-table", DataTable)
            sort_history_table(
                table, self._history_sort_column_key, self._history_sort_reverse
            )
        except NoMatches:
            logger.error(
                f"Could not find history table or column for sort key '{self._history_sort_column_key}'"
            )

    def flash_cell(self, row_key: str, column_key: str, flash_type: str) -> None:
        try:
            dt = self.query_one("#price-table", DataTable)
            flash_table_cell(
                dt, self.theme_variables, self.set_timer, row_key, column_key, flash_type
            )
        except NoMatches:
            pass

    def unflash_cell(
        self, row_key: str, column_key: str, original_content: Text
    ) -> None:
        try:
            dt = self.query_one("#price-table", DataTable)
            unflash_table_cell(dt, row_key, column_key, original_content)
        except NoMatches:
            pass

    @on(Tabs.TabActivated)
    async def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if self._is_rebuilding:
            return
        old_category = self._last_active_category
        self._last_active_category = None
        new_category = self.get_active_category()
        if new_category == old_category:
            self._last_active_category = old_category
            return

        try:
            self.query_one(SearchBox).remove()
            self._original_table_data = []
        except NoMatches:
            pass

        self._sort_column_key = None
        self._sort_reverse = False
        self._history_sort_column_key = None
        self._history_sort_reverse = False

        if new_category:
            await self._display_data_for_category(new_category)

        self.action_refresh()

        spec = SPECIAL_TAB_BY_CATEGORY.get(new_category or "")
        try:
            status_label = self.query_one("#last-refresh-time", Label)
            if spec and spec.clears_refresh_status:
                status_label.update("")
        except NoMatches:
            pass

        if spec and spec.resets_config_sub_view:
            self._last_config_sub_view = None

        self._last_active_category = new_category

    @on(DataTable.RowSelected, "#price-table")
    def on_main_datatable_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.news_ticker = event.row_key.value
            self.history_ticker = event.row_key.value
            self.notify(f"Selected {self.news_ticker} for news/history tabs.")

    @on(DataTable.HeaderSelected, "#price-table")
    def on_price_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        self._set_and_apply_sort(str(event.column_key.value), "click")

    def _set_and_apply_sort(self, column_key_str: str, source: str) -> None:
        if self._sort_column_key == column_key_str:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column_key = column_key_str
            self._sort_reverse = column_key_str in NUMERIC_SORT_COLUMNS
        self._apply_price_table_sort()

    def _set_and_apply_history_sort(self, column_key_str: str, source: str) -> None:
        if self._history_sort_column_key == column_key_str:
            self._history_sort_reverse = not self._history_sort_reverse
        else:
            self._history_sort_column_key = column_key_str
            self._history_sort_reverse = column_key_str == "Date"
        self._apply_history_table_sort()

    def action_enter_sort_mode(self) -> None:
        if self._sort_mode:
            return
        category = self.get_active_category()
        if category == "history" or is_watchlist_category(category) or category in ("options", "fred"):
            self._sort_mode = True
            try:
                status_label = self.query_one("#last-refresh-time", Label)
                self._original_status_text = getattr(status_label, "renderable", "")
                if category == "history":
                    status_label.update(
                        "SORT BY: \\[d]ate, \\[o]pen, \\[H]igh, \\[L]ow, \\[c]lose, \\[v]olume, \\[ESC]ape"
                    )
                else:
                    status_label.update(
                        "SORT BY: \\[d]escription, \\[p]rice, \\[c]hange, p\\[e]rcent, \\[t]icker, \\[u]ndo, \\[ESC]ape"
                    )
            except NoMatches:
                self._sort_mode = False
        else:
            self.bell()

    async def _undo_sort(self) -> None:
        self._sort_column_key = None
        self._sort_reverse = False
        active_cat = self.get_active_category()
        if active_cat:
            await self._display_data_for_category(active_cat)

    def action_focus_input(self) -> None:
        if target_widget := self._get_primary_view_widget():
            target_widget.focus()

    async def action_activate_tab(self) -> None:
        if self.focused and isinstance(
            self.focused, (Input, Button, Select, Switch, ListView)
        ):
            raise SkipAction()

        if self.focused and isinstance(self.focused, (Tabs, Tab)):
            if target_widget := self._get_primary_view_widget():
                target_widget.focus()
            return

        if self.focused and isinstance(self.focused, DataTable):
            category = self.get_active_category()
            if category == "options":
                try:
                    self.query_one(OptionsView).action_manage_position()
                except NoMatches:
                    pass
            elif category == "fred":
                try:
                    self.query_one(FredView).action_edit_series()
                except NoMatches:
                    pass
            elif category == "filings":
                try:
                    self.query_one(FilingsView).action_open_filing()
                except NoMatches:
                    pass
            elif category == "calendar":
                try:
                    self.query_one(CalendarView).repopulate()
                except NoMatches:
                    pass
            elif is_watchlist_category(category):
                await self.action_enter_open_mode()
            return

    async def action_enter_open_mode(self) -> None:
        if self._open_mode:
            await self.action_handle_open_key("o")
            return

        category = self.get_active_category()
        if is_watchlist_category(category):
            try:
                price_table = self.query_one("#price-table", NavigableDataTable)
                if price_table.cursor_row >= 0:
                    self._open_mode = True
                    status_label = self.query_one("#last-refresh-time", Label)
                    self._original_status_text = getattr(status_label, "renderable", "")
                    status_label.update(
                        "OPEN IN: \\[n]ews, \\[h]istory, \\[o]ptions, \\[y]ahoo Finance, \\[ESC]ape"
                    )
                else:
                    self.bell()
            except NoMatches:
                self.bell()
        else:
            self.bell()

    async def action_handle_open_key(self, key: str) -> None:
        if not self._open_mode:
            return

        try:
            price_table = self.query_one("#price-table", NavigableDataTable)
            if price_table.cursor_row < 0:
                self.action_back_or_dismiss()
                return

            coordinate = Coordinate(row=price_table.cursor_row, column=0)
            ticker = price_table.coordinate_to_cell_key(coordinate).row_key.value
            if not ticker:
                self.action_back_or_dismiss()
                return

            target_category = None
            if key == "n":
                target_category = "news"
                self.news_ticker = ticker
            elif key == "h":
                target_category = "history"
                self.history_ticker = ticker
            elif key == "o":
                target_category = "options"
                self.options_ticker = ticker
            elif key == "y":
                yahoo_url = f"https://finance.yahoo.com/quote/{ticker}"
                try:
                    self.notify(f"Opening Yahoo Finance for {ticker}...")
                    webbrowser.open(yahoo_url)
                except webbrowser.Error:
                    self.notify(
                        "No web browser found. Please configure your system's default browser.",
                        severity="error",
                        timeout=8,
                    )
                except Exception as e:  # noqa: BLE001
                    self.notify(f"Failed to open browser: {e}", severity="error")
                self.action_back_or_dismiss()
                return

            if target_category:
                try:
                    idx = next(
                        i
                        for i, t in enumerate(self.tab_map, start=1)
                        if t["category"] == target_category
                    )
                    tabs = self.query_one(Tabs)
                    tabs.active = f"tab-{idx}"
                except (StopIteration, NoMatches):
                    self.notify(f"Tab '{target_category}' not found", severity="error")

            self.action_back_or_dismiss()
        except (NoMatches, IndexError):
            self.action_back_or_dismiss()

    async def action_handle_sort_key(self, key: str) -> None:
        if not self._sort_mode:
            return
        target_view = "history" if self.get_active_category() == "history" else "price"
        if key == "u":
            if target_view == "price":
                await self._undo_sort()
                self.action_back_or_dismiss()
            return

        column_map = {
            "d": {"price": "Description", "history": "Date"},
            "p": {"price": "Price"},
            "c": {"price": "Change", "history": "Close"},
            "e": {"price": "% Change"},
            "t": {"price": "Ticker"},
            "o": {"history": "Open"},
            "H": {"history": "High"},
            "L": {"history": "Low"},
            "v": {"history": "Volume"},
        }
        if key not in column_map or target_view not in column_map[key]:
            return

        column_key_str = column_map[key][target_view]
        if target_view == "history":
            self._set_and_apply_history_sort(column_key_str, f"key '{key}'")
        else:
            self._set_and_apply_sort(column_key_str, f"key '{key}'")

        self.action_back_or_dismiss()

    def action_focus_search(self) -> None:
        try:
            self.query_one(SearchBox).focus()
            return
        except NoMatches:
            pass
        category = self.get_active_category()
        target_id = None
        if is_watchlist_category(category) or category in ("options", "fred"):
            target_id = "#price-table"
        elif category == "configs":
            try:
                table_id = self.query_one(ListsConfigView).query_one(DataTable).id
                target_id = "#" + table_id if table_id else None
            except NoMatches:
                target_id = None
        elif spec := SPECIAL_TAB_BY_CATEGORY.get(category or ""):
            target_id = spec.search_table_selector

        if not target_id:
            self.bell()
            return
        try:
            table = self.query_one(target_id, DataTable)
            self.search_target_table = table
            self._original_table_data = [
                (row_key, table.get_row(row_key)) for row_key in table.rows
            ]
            search_box = SearchBox()
            self.mount(search_box)
            search_box.focus()
        except NoMatches:
            self.bell()

    @on(Input.Changed, "#search-box")
    def on_search_changed(self, event: Input.Changed) -> None:
        query = event.value
        if not self.search_target_table:
            return
        from textual.fuzzy import Matcher

        matcher = Matcher(query)
        self.search_target_table.clear()
        if not query:
            for row_key, row_data in self._original_table_data:
                self.search_target_table.add_row(*row_data, key=row_key)
            return
        for row_key, row_data in self._original_table_data:
            searchable_string = " ".join(extract_cell_text(cell) for cell in row_data)
            if matcher.match(searchable_string) > 0:
                self.search_target_table.add_row(*row_data, key=row_key)

    @on(Input.Submitted, "#search-box")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        try:
            self.query_one(SearchBox).remove()
        except NoMatches:
            pass

    def action_edit_ticker_quick(self) -> None:
        if self._sort_mode:
            return

        category = self.get_active_category()
        if not is_watchlist_category(category) or category is None:
            self.bell()
            return

        open_quick_edit_ticker_modal(self, category)

    @on(TagFilterChanged)
    def on_tag_filter_changed(self, message: TagFilterChanged) -> None:
        logger.info(f"TagFilterChanged message received: {message.tags}")
        self.active_tag_filter = message.tags
        self._is_filter_refresh = True
        self._redisplay_price_table()
        self._update_tag_filter_status()

    def _redisplay_price_table(self) -> None:
        redisplay_price_table_from_cache(self, self.get_active_category())


def main() -> None:
    """The main entry point for the application."""
    dirs = PlatformDirs("stockstui", "andriy-git")

    cache_dir = Path(dirs.user_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    log_file = cache_dir / "stockstui.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(module)s - %(message)s",
        filename=log_file,
        filemode="w",
    )

    parser = create_arg_parser()
    args = parser.parse_args()

    if args.man:
        show_manual()
        return

    if args.output:
        run_cli_output(args)
    else:
        app = StocksTUI(cli_overrides=vars(args))
        textual_handler = TextualHandler(app)
        textual_handler.setLevel(logging.WARNING)
        log_formatter = logging.Formatter("%(message)s")
        textual_handler.setFormatter(log_formatter)
        logging.getLogger().addHandler(textual_handler)
        app.run()


if __name__ == "__main__":
    main()
