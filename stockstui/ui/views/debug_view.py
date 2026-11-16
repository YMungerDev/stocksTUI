import time
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Label, Static
from textual.worker import get_current_worker

from stockstui.common import (
    CacheTestDataUpdated,
    FredDebugDataUpdated,
    ListDebugDataUpdated,
    TickerDebugDataUpdated,
    TickerInfoComparisonUpdated,
)
from stockstui.data_providers import fred_provider, market_provider
from stockstui.presentation import formatter
from stockstui.ui.modals import CompareInfoModal, FredSeriesModal
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable


def populate_info_comparison_table(
    dt: DataTable,
    theme_vars: dict[str, Any],
    message: TickerInfoComparisonUpdated,
) -> None:
    """Renders fast/batch/prepost/slow info comparison rows into a debug DataTable."""
    dt.loading = False
    dt.clear()
    rows = formatter.format_info_comparison(
        message.fast_info,
        message.slow_info,
        message.batch_info,
        message.prepost_info,
    )
    muted_color = theme_vars.get("text-muted", "dim")
    warning_color = theme_vars.get("warning", "yellow")
    for key, fast_val, batch_val, prepost_val, slow_val, is_mismatch in rows:
        fast_text = Text(
            fast_val,
            style=warning_color
            if is_mismatch
            else (muted_color if fast_val == "N/A" else ""),
        )
        batch_text = Text(
            batch_val,
            style=warning_color
            if is_mismatch
            else (muted_color if batch_val == "N/A" else ""),
        )
        prepost_text = Text(
            prepost_val,
            style=warning_color
            if is_mismatch
            else (muted_color if prepost_val == "N/A" else ""),
        )
        slow_text = Text(
            slow_val,
            style=warning_color
            if is_mismatch
            else (muted_color if slow_val == "N/A" else ""),
        )
        dt.add_row(key, fast_text, batch_text, prepost_text, slow_text)


def populate_ticker_debug_table(
    dt: DataTable,
    theme_vars: dict[str, Any],
    message: TickerDebugDataUpdated,
) -> Text:
    """Renders ticker latency test rows and returns the status-bar summary text."""
    dt.loading = False
    dt.clear()
    rows = formatter.format_ticker_debug_data_for_table(message.data)
    success_color = theme_vars.get("success", "green")
    error_color = theme_vars.get("error", "red")
    lat_high = theme_vars.get("latency-high", "red")
    lat_med = theme_vars.get("latency-medium", "yellow")
    lat_low = theme_vars.get("latency-low", "blue")
    muted_color = theme_vars.get("text-muted", "dim")
    for symbol, is_valid, description, latency in rows:
        valid_text = (
            Text("Yes", style=success_color)
            if is_valid
            else Text("No", style=f"bold {error_color}")
        )
        if latency > 2.0:
            latency_style = lat_high
        elif latency > 0.5:
            latency_style = lat_med
        else:
            latency_style = lat_low
        latency_text = Text(f"{latency:.3f}s", style=latency_style, justify="right")
        desc_text = Text(
            description,
            style=muted_color if not is_valid or description == "N/A" else "",
        )
        dt.add_row(symbol, valid_text, desc_text, latency_text)
    return Text.assemble(
        "Test Completed. Total time: ",
        (f"{message.total_time:.2f}s", f"bold {theme_vars.get('warning')}"),
    )


def populate_list_debug_table(
    dt: DataTable,
    theme_vars: dict[str, Any],
    message: ListDebugDataUpdated,
) -> Text:
    """Renders list batch network test rows and returns the status-bar summary text."""
    dt.loading = False
    dt.clear()
    rows = formatter.format_list_debug_data_for_table(message.data)
    lat_high = theme_vars.get("latency-high", "red")
    lat_med = theme_vars.get("latency-medium", "yellow")
    lat_low = theme_vars.get("latency-low", "blue")
    muted_color = theme_vars.get("text-muted", "dim")
    for list_name, ticker_count, latency in rows:
        if latency > 5.0:
            latency_style = lat_high
        elif latency > 2.0:
            latency_style = lat_med
        else:
            latency_style = lat_low
        latency_text = Text(f"{latency:.3f}s", style=latency_style, justify="right")
        list_name_text = Text(
            list_name, style=muted_color if list_name == "N/A" else ""
        )
        dt.add_row(list_name_text, str(ticker_count), latency_text)
    return Text.assemble(
        "Test Completed. Total time: ",
        (f"{message.total_time:.2f}s", f"bold {theme_vars.get('warning')}"),
    )


def populate_cache_test_table(
    dt: DataTable,
    theme_vars: dict[str, Any],
    message: CacheTestDataUpdated,
) -> Text:
    """Renders local cache speed test rows and returns the status-bar summary text."""
    dt.loading = False
    dt.clear()
    rows = formatter.format_cache_test_data_for_table(message.data)
    price_color = theme_vars.get("price", "cyan")
    muted_color = theme_vars.get("text-muted", "dim")
    for list_name, ticker_count, latency in rows:
        latency_text = Text(
            f"{latency * 1000:.3f} ms", style=price_color, justify="right"
        )
        list_name_text = Text(
            list_name, style=muted_color if list_name == "N/A" else ""
        )
        dt.add_row(list_name_text, str(ticker_count), latency_text)
    return Text.assemble(
        "Test Completed. Total time: ",
        (f"{message.total_time * 1000:.2f} ms", f"bold {theme_vars.get('price')}"),
    )


def populate_fred_debug_table(
    dt: DataTable,
    message: FredDebugDataUpdated,
) -> str:
    """Renders raw FRED API debug rows and returns the status-bar summary text."""
    dt.loading = False
    dt.clear()
    for item in message.data:
        if "_error" in item:
            dt.add_row("Error:", item["_error"])
        elif "_section" in item:
            dt.add_row(item["_section"], item.get("id", "N/A"))
            if "observations" in item:
                for obs in item["observations"]:
                    dt.add_row(f"  {obs['date']}", f"  {obs['value']}")
            elif item.get("info"):
                for key, value in item["info"].items():
                    if key != "id":
                        dt.add_row(f"  {key}:", f"  {value}")
        else:
            dt.add_row("Raw Data:", str(item))
    return "Raw FRED API Data Loaded"


class DebugView(Vertical):
    """A view for running various debug tests related to data fetching and caching."""

    app: Any

    def compose(self) -> ComposeResult:
        """Creates the layout for the debug view."""
        with Horizontal(classes="debug-buttons"):
            yield Button("Compare Ticker Info", id="debug-compare-info")
            yield Button("Test Tickers (Latency)", id="debug-test-tickers")
            yield Button("Test Lists (Network)", id="debug-test-lists")
            yield Button("Test Cache (Local Speed)", id="debug-test-cache")
            yield Button("Test FRED (API)", id="debug-test-fred")

        with Container(id="debug-output-container"):
            yield Static("[dim]Run a test to see results.[/dim]", id="info-message")

    def _enable_buttons(self) -> None:
        for button in self.query(".debug-buttons Button"):
            button.disabled = False

    def _update_status_bar(self, content: str | Text) -> None:
        try:
            self.app.query_one("#last-refresh-time", Label).update(content)
        except NoMatches:
            return

    def _dispatch_test(self, app_method_name: str, fallback, *args: Any) -> None:
        app_method = getattr(self.app, app_method_name, None)
        if callable(app_method):
            app_method(*args)
        else:
            fallback(*args)

    @work(exclusive=True, thread=True)
    def run_info_comparison_test(self, ticker: str) -> None:
        """Worker to fetch fast vs slow vs batch vs prepost ticker info."""
        data = market_provider.get_ticker_info_comparison(ticker)
        if not get_current_worker().is_cancelled:
            self.post_message(
                TickerInfoComparisonUpdated(
                    fast_info=data["fast"],
                    slow_info=data["slow"],
                    batch_info=data.get("batch", {}),
                    prepost_info=data.get("prepost", {}),
                )
            )

    @work(exclusive=True, thread=True)
    def run_ticker_debug_test(self, symbols: list[str]) -> None:
        """Worker to run the individual ticker latency test."""
        start_time = time.perf_counter()
        data = market_provider.run_ticker_debug_test(symbols)
        total_time = time.perf_counter() - start_time
        if not get_current_worker().is_cancelled:
            self.post_message(TickerDebugDataUpdated(data, total_time))

    @work(exclusive=True, thread=True)
    def run_list_debug_test(self, lists: dict[str, list[str]]) -> None:
        """Worker to run the list batch network test."""
        start_time = time.perf_counter()
        data = market_provider.run_list_debug_test(lists)
        total_time = time.perf_counter() - start_time
        if not get_current_worker().is_cancelled:
            self.post_message(ListDebugDataUpdated(data, total_time))

    @work(exclusive=True, thread=True)
    def run_cache_test(self, lists: dict[str, list[str]]) -> None:
        """Worker to run the local cache speed test."""
        start_time = time.perf_counter()
        data = market_provider.run_cache_test(lists)
        total_time = time.perf_counter() - start_time
        if not get_current_worker().is_cancelled:
            self.post_message(CacheTestDataUpdated(data, total_time))

    @work(exclusive=True, group="fred_debug", thread=True)
    def run_fred_debug_test(self, series_list: list[str], api_key: str) -> None:
        """Worker to show raw FRED API response data (not our calculations)."""
        start_time = time.perf_counter()

        data: list[dict[str, Any]] = []
        for series_id in series_list:
            try:
                if not api_key:
                    data.append(
                        {
                            "_error": "No API key configured. Configure in Configs > FRED Settings."
                        }
                    )
                    break

                raw_info = fred_provider.get_series_info(series_id, api_key)
                raw_observations = fred_provider.get_series_observations(
                    series_id, api_key
                )

                data.append(
                    {
                        "_section": "Series Info (from FRED API)",
                        "id": series_id,
                        "info": raw_info,
                    }
                )
                data.append(
                    {
                        "_section": "Latest Observations (from FRED API)",
                        "id": series_id,
                        "observations": raw_observations[:10]
                        if raw_observations
                        else [],
                    }
                )
            except Exception as e:  # noqa: BLE001
                data.append(
                    {"_error": f"Error fetching {series_id}: {e!s}", "id": series_id}
                )

        total_time = time.perf_counter() - start_time
        if not get_current_worker().is_cancelled:
            self.post_message(FredDebugDataUpdated(data, total_time))

    @on(TickerInfoComparisonUpdated)
    def on_ticker_info_comparison_updated(
        self, message: TickerInfoComparisonUpdated
    ) -> None:
        try:
            self._enable_buttons()
            dt = self.query_one("#debug-table", DataTable)
            populate_info_comparison_table(dt, self.app.theme_variables, message)
        except NoMatches:
            return

    @on(TickerDebugDataUpdated)
    def on_ticker_debug_data_updated(self, message: TickerDebugDataUpdated) -> None:
        try:
            self._enable_buttons()
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_ticker_debug_table(dt, self.app.theme_variables, message)
            self._update_status_bar(summary)
        except NoMatches:
            return

    @on(ListDebugDataUpdated)
    def on_list_debug_data_updated(self, message: ListDebugDataUpdated) -> None:
        try:
            self._enable_buttons()
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_list_debug_table(dt, self.app.theme_variables, message)
            self._update_status_bar(summary)
        except NoMatches:
            return

    @on(CacheTestDataUpdated)
    def on_cache_test_data_updated(self, message: CacheTestDataUpdated) -> None:
        try:
            self._enable_buttons()
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_cache_test_table(dt, self.app.theme_variables, message)
            self._update_status_bar(summary)
        except NoMatches:
            return

    @on(FredDebugDataUpdated)
    def on_fred_debug_data_updated(self, message: FredDebugDataUpdated) -> None:
        try:
            self._enable_buttons()
            dt = self.query_one("#debug-table", DataTable)
            summary = populate_fred_debug_table(dt, message)
            self._update_status_bar(summary)
        except (NoMatches, KeyError):
            return

    @on(Button.Pressed, ".debug-buttons Button")
    async def on_debug_button_pressed(self, event: Button.Pressed) -> None:
        """
        Handles button presses for the debug tests.
        Clears previous results, disables buttons, and initiates the selected test.
        """
        button_id = event.button.id

        for button in self.query(".debug-buttons Button"):
            button.disabled = True

        container = self.query_one("#debug-output-container")
        await container.remove_children()

        if button_id == "debug-compare-info":

            async def on_modal_close(ticker: str | None) -> None:
                if ticker:
                    await container.mount(NavigableDataTable(id="debug-table"))
                    dt = self.query_one("#debug-table", NavigableDataTable)
                    dt.clear()
                    dt.add_columns("Info Key", "Fast", "Batch", "PrePost", "Slow")
                    dt.loading = True
                    self._dispatch_test(
                        "run_info_comparison_test",
                        self.run_info_comparison_test,
                        ticker,
                    )
                else:
                    await container.mount(
                        Static(
                            "[dim]Run a test to see results.[/dim]", id="info-message"
                        )
                    )
                    self._enable_buttons()

            self.app.push_screen(CompareInfoModal(), on_modal_close)

        elif button_id == "debug-test-fred":

            async def on_fred_modal_close(series_id: str | None) -> None:
                if series_id:
                    await container.mount(NavigableDataTable(id="debug-table"))
                    dt = self.query_one("#debug-table", NavigableDataTable)
                    dt.clear()
                    dt.add_columns("Section", "Data")
                    dt.loading = True

                    fred_settings = self.app.config.settings.get("fred_settings", {})
                    api_key = fred_settings.get("api_key", "")
                    self._dispatch_test(
                        "run_fred_debug_test",
                        self.run_fred_debug_test,
                        [series_id.strip().upper()],
                        api_key,
                    )
                else:
                    await container.mount(
                        Static(
                            "[dim]Run a test to see results.[/dim]", id="info-message"
                        )
                    )
                    self._enable_buttons()

            self.app.push_screen(FredSeriesModal(), on_fred_modal_close)
        else:
            await container.mount(NavigableDataTable(id="debug-table"))
            dt = self.query_one("#debug-table", NavigableDataTable)
            dt.loading = True

            if button_id == "debug-test-tickers":
                dt.add_columns("Symbol", "Valid?", "Description", "Latency")
                dt.add_row("[yellow]Running individual ticker performance test...[/]")
                all_symbols = list(
                    {
                        s["ticker"]
                        for cat_symbols in self.app.config.lists.values()
                        for s in cat_symbols
                    }
                )
                self._dispatch_test(
                    "run_ticker_debug_test", self.run_ticker_debug_test, all_symbols
                )
            elif button_id == "debug-test-lists":
                dt.add_columns("List Name", "Tickers", "Latency")
                dt.add_row("[yellow]Running list batch network test...[/]")
                lists_to_test = {
                    name: [s["ticker"] for s in tickers]
                    for name, tickers in self.app.config.lists.items()
                }
                self._dispatch_test(
                    "run_list_debug_test", self.run_list_debug_test, lists_to_test
                )
            elif button_id == "debug-test-cache":
                dt.add_columns("List Name", "Tickers", "Latency (From Cache)")
                dt.add_row("[yellow]Running cache speed test...[/]")
                lists_to_test = {
                    name: [s["ticker"] for s in tickers]
                    for name, tickers in self.app.config.lists.items()
                }
                self._dispatch_test("run_cache_test", self.run_cache_test, lists_to_test)

    def on_key(self, event: Any) -> None:
        """Handle keyboard navigation for debug view."""
        buttons = list(self.query(".debug-buttons Button"))
        if not buttons:
            return

        if event.key == "i" and self.app.focused not in buttons:
            buttons[0].focus()
            event.stop()
            return

        if self.app.focused in buttons:
            idx = buttons.index(self.app.focused)
            if event.key in ("h", "left"):
                buttons[(idx - 1) % len(buttons)].focus()
                event.stop()
            elif event.key in ("l", "right"):
                buttons[(idx + 1) % len(buttons)].focus()
                event.stop()
