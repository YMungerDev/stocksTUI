import logging
import re
from typing import Any

from rich.text import Text
from textual.app import ComposeResult, on
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.dom import NoMatches
from textual.widgets import DataTable, Input, RadioButton, Static, Switch
from textual.widgets.data_table import CellDoesNotExist

from stockstui.presentation import formatter
from stockstui.presentation.overlays import beta_against, pairwise_correlations
from stockstui.ui.suggesters import TickerSuggester
from stockstui.ui.widgets.history_chart import HistoryChart
from stockstui.ui.widgets.vim_radio_set import VimRadioSet
from stockstui.utils import extract_cell_text

logger = logging.getLogger(__name__)


def sort_history_table(
    table: DataTable, sort_column_key: str | None, reverse: bool
) -> None:
    """Applies the given sort column and direction to a history DataTable."""
    if sort_column_key is None:
        return

    def sort_key(row_values: tuple[Any, ...]) -> tuple[int, Any]:
        try:
            column_index = table.get_column_index(sort_column_key)
        except CellDoesNotExist:
            return (1, 0)
        if column_index >= len(row_values):
            return (1, 0)
        text_content = extract_cell_text(row_values[column_index])
        if sort_column_key == "Date":
            try:
                return (0, text_content)
            except (ValueError, TypeError):
                return (1, "")
        cleaned_text = re.sub(r"^[^\d\.\-]+", "", text_content).replace(",", "")
        try:
            return (0, float(cleaned_text))
        except (ValueError, TypeError):
            return (1, 0)

    try:
        table.sort(key=sort_key, reverse=reverse)
    except KeyError:
        logger.error(
            f"Could not find history column for sort key '{sort_column_key}'"
        )


class HistoryView(Vertical):
    """A view for displaying historical stock data, allowing selection of tickers and time ranges."""

    BINDINGS = [
        # ctrl+<n> avoids app-level bindings (plain letters are sort-mode or
        # view actions, digits switch tabs — both consumed by the app first).
        Binding("ctrl+m", "toggle_overlay('sma50')", "50d MA", show=True),
        Binding("ctrl+n", "toggle_overlay('sma200')", "200d MA", show=True),
        Binding("ctrl+t", "toggle_overlay('volume')", "Volume", show=True),
        Binding("ctrl+o", "toggle_overlay('drawdown')", "Drawdown", show=True),
        Binding("ctrl+y", "toggle_overlay('earnings')", "Earnings", show=True),
    ]

    app: Any

    def compose(self) -> ComposeResult:
        """Creates the layout for the historical data view."""
        # Prepare data for the ticker suggester (all unique tickers from user's lists)
        all_tickers_data = [s for lst in self.app.config.lists.values() for s in lst]
        suggester_data = [
            (s["ticker"], s.get("note") or s.get("alias", s["ticker"]))
            for s in all_tickers_data
        ]
        unique_suggester_data = list(
            {t[0]: t for t in suggester_data}.values()
        )  # Deduplicate by ticker
        suggester = TickerSuggester(unique_suggester_data, case_sensitive=False)

        # Horizontal container for ticker input
        with Horizontal(classes="history_controls"):
            yield Input(
                placeholder="Enter a ticker...",
                suggester=suggester,
                id="history-ticker-input",
                value=self.app.history_ticker
                or "",  # Pre-fill with last selected ticker
            )
        # Horizontal container for range selection and view toggle
        with Horizontal(classes="history_controls"):
            yield VimRadioSet(
                RadioButton("1D"),
                RadioButton("5D"),
                RadioButton("1M", value=True),
                RadioButton("6M"),
                RadioButton("YTD"),
                RadioButton("1Y"),
                RadioButton("5Y"),
                RadioButton("All"),
                id="history-range-select",  # Radio buttons for selecting historical period
            )
            yield Switch(
                id="history-view-toggle"
            )  # Toggle between table and chart view
        # Container to display the historical data (table or chart)
        yield Container(id="history-display-container")

    def on_mount(self) -> None:
        """
        Called when the HistoryView is mounted.
        Applies any CLI overrides and triggers initial data rendering.
        """
        # --- Apply CLI Overrides ---
        if self.app.cli_overrides:
            # If --chart was passed, enable the chart view toggle
            if self.app.cli_overrides.get("chart"):
                self.query_one("#history-view-toggle", Switch).value = True

            # If --period was passed, select the corresponding radio button
            if period_arg := self.app.cli_overrides.get("period"):
                period_map = {
                    "1d": "1D",
                    "5d": "5D",
                    "1mo": "1M",
                    "6mo": "6M",
                    "ytd": "YTD",
                    "1y": "1Y",
                    "5y": "5Y",
                    "max": "All",
                }
                target_label = period_map.get(period_arg)
                if target_label:
                    radio_set = self.query_one("#history-range-select", VimRadioSet)
                    for button in radio_set.query(RadioButton):
                        button.value = str(button.label) == target_label

        # Trigger data fetch if a ticker is already set (either from CLI or app state)
        if self.app.history_ticker:
            self.call_after_refresh(self._request_historical_data)
        else:
            self.call_after_refresh(self._render_historical_data)

    def action_toggle_overlay(self, overlay: str) -> None:
        """
        Toggles a chart overlay on the currently displayed chart.

        Turning on the 200-day SMA while the visible window is shorter than
        a year refetches with a wider period instead of silently drawing
        nothing.
        """
        try:
            chart = self.query_one(HistoryChart)
            chart.toggle_overlay(overlay)
            if overlay == "sma200" and chart._overlay_states["sma200"]:
                if not self.app._history_period:
                    return
                from stockstui.presentation.overlays import moving_average

                closes = (
                    self.app._last_historical_data["Close"]
                    if self.app._last_historical_data is not None
                    and "Close" in self.app._last_historical_data
                    else None
                )
                if closes is None or moving_average(closes, 200) is None:
                    self._request_historical_data()
        except NoMatches:
            self.app.bell()

    async def _render_historical_data(self):
        """
        Renders the appropriate view (table or chart) in the history tab.
        It uses the `_last_historical_data` from the app state.
        """
        try:
            display_container = self.query_one("#history-display-container")
            await display_container.remove_children()  # Clear previous content

            last_data = self.app._last_historical_data
            if last_data is None:
                await display_container.mount(
                    Static(
                        "Enter a ticker symbol to view its historical data.",
                        id="info-message",
                    )
                )
                return

            if last_data.empty:
                # Check for a specific error message from the data provider
                error_type = last_data.attrs.get("error")
                ticker = last_data.attrs.get("symbol", "the selected ticker")

                error_text = Text()
                if error_type == "Invalid Ticker":
                    error_text = Text.assemble(
                        ("Error: ", "bold red"), f"Invalid ticker symbol '{ticker}'."
                    )
                elif error_type == "Network Error":
                    error_text = Text.assemble(
                        ("Network Error: ", "bold red"),
                        f"Could not retrieve data for '{ticker}'.",
                    )
                elif error_type == "Data Error":
                    error_text = Text.assemble(
                        ("Data Error: ", "bold red"),
                        f"Could not process data for '{ticker}'.",
                    )
                else:  # Generic error for other cases (e.g., no data in range)
                    error_text = Text(
                        f"No historical data found for '{ticker}' in the selected range."
                    )

                await display_container.mount(Static(error_text))
                return

            view_toggle = self.query_one("#history-view-toggle", Switch)
            if view_toggle.value:
                # Display as a chart
                is_comparison = last_data.attrs.get("comparison_tickers")
                chart_widget = HistoryChart(
                    last_data,
                    period=self.app._history_period,
                    comparison=bool(is_comparison),
                )
                await display_container.mount(chart_widget)
                if is_comparison:
                    await self._mount_correlation_table(last_data)
            else:
                # Display as a table
                table = formatter.format_historical_data_as_table(last_data)
                await display_container.mount(table)
                self.apply_table_sort()
        except NoMatches:
            pass

    async def _mount_correlation_table(self, data) -> None:
        """Mounts the pairwise correlation/beta table under a comparison chart."""
        from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

        try:
            correlations = pairwise_correlations(data)
            if correlations.empty:
                return
            columns = list(correlations.columns)
            benchmark = columns[0]
            betas = {
                ticker: beta_against(data[ticker], data[benchmark])
                for ticker in columns
                if ticker != benchmark
            }
            table = NavigableDataTable(id="history-correlation-table", zebra_stripes=True)
            table.add_columns("Ticker", *[f"ρ vs {c}" for c in columns], "β vs benchmark")
            for ticker in columns:
                beta_value = betas.get(ticker)
                row: list = [
                    Text(ticker, style="bold"),
                    Text(f"{beta_value:.2f}" if beta_value is not None else "—"),
                ]
                for other in columns:
                    if other == ticker:
                        row.append(Text("1.00", style="dim"))
                        continue
                    value = correlations.loc[ticker, other]
                    row.append(Text(f"{value:.2f}"))
                table.add_row(*row)
            # The chart container hosts the table beneath the chart.
            await self.query_one("#history-display-container").mount(table)
        except Exception as e:  # noqa: BLE001 - stats must never break render
            logger.error(f"Failed to render correlation table: {e}")

    def _request_historical_data(self):
        """
        Initiates the fetching of historical data based on the selected ticker
        and time range. Dispatches a worker to perform the actual data fetch.
        """
        if not self.app.history_ticker:
            return
        # Comparison mode: comma-separated tickers (ratio entries pass through).
        entries = [
            t.strip().upper()
            for t in str(self.app.history_ticker).split(",")
            if t.strip()
        ]
        if len(entries) > 1:
            try:
                display_container = self.query_one("#history-display-container")
                display_container.loading = True
                self.app.fetch_historical_comparison(
                    entries, self._current_period(), "1d"
                )
            except NoMatches:
                pass
            return
        try:
            radio_set = self.query_one("#history-range-select", VimRadioSet)
            if radio_set.pressed_button:
                period_map = {
                    "1D": "1d",
                    "5D": "5d",
                    "1M": "1mo",
                    "6M": "6mo",
                    "YTD": "ytd",
                    "1Y": "1y",
                    "5Y": "5y",
                    "All": "max",
                }
                button_label = str(radio_set.pressed_button.label)
                period = period_map.get(button_label)
                if period:
                    self.app._history_period = period
                    # Use a 5-minute interval for the 1D period, otherwise default to daily.
                    interval = "5m" if button_label == "1D" else "1d"

                    # The 200-day SMA needs more history than shorter
                    # periods provide; fetch a year so the average is not
                    # truncated (per #8's acceptance criteria).
                    fetch_period = period
                    try:
                        chart = self.query_one(HistoryChart)
                        if (
                            chart._overlay_states.get("sma200")
                            and period not in ("1y", "5y", "max")
                        ):
                            fetch_period = "1y"
                    except NoMatches:
                        pass

                    display_container = self.query_one("#history-display-container")
                    display_container.loading = True  # Show loading indicator
                    self.app.fetch_historical_data(
                        self.app.history_ticker, fetch_period, interval
                    )
        except NoMatches:
            pass

    def _current_period(self) -> str:
        """The period from the pressed range radio button (default 1mo)."""
        try:
            radio_set = self.query_one("#history-range-select", VimRadioSet)
            period_map = {
                "1D": "1d",
                "5D": "5d",
                "1M": "1mo",
                "6M": "6mo",
                "YTD": "ytd",
                "1Y": "1y",
                "5Y": "5y",
                "All": "max",
            }
            if radio_set.pressed_button:
                return period_map.get(str(radio_set.pressed_button.label), "1mo")
        except NoMatches:
            pass
        return "1mo"

    def _parse_ticker_from_input(self, value: str) -> str:
        """Extracts the ticker symbol from a suggestion string ('TICKER - Desc') or raw input."""
        if " - " in value:
            return value.strip().split(" - ")[0].upper()
        return value.strip().upper()

    @on(Input.Submitted, "#history-ticker-input")
    def on_history_ticker_submitted(self, event: Input.Submitted):
        """Handles submission of the ticker input, triggering data fetch."""
        if event.value:
            raw = event.value.strip()
            # Multi-ticker and ratio syntax: 'NVDA,AMD' or 'NVDA/SMH'.
            entries = [part.strip() for part in raw.split(",") if part.strip()]
            if len(entries) > 1 or (len(entries) == 1 and "/" in entries[0]):
                self.app.history_ticker = ",".join(
                    e.upper() for e in entries
                )
                self._request_historical_data()
                return
            self.app.history_ticker = self._parse_ticker_from_input(event.value)
            self._request_historical_data()

    @on(VimRadioSet.Changed, "#history-range-select")
    def on_history_range_changed(self, event: VimRadioSet.Changed):
        """Handles changes in the historical data range selection, triggering data fetch."""
        self._request_historical_data()

    @on(Switch.Changed, "#history-view-toggle")
    async def on_history_view_toggled(self, event: Switch.Changed):
        """Handles toggling between table and chart view for historical data."""
        await self._render_historical_data()

    @on(DataTable.HeaderSelected, "#history-table")
    def on_history_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Handle header clicks to sort the history table."""
        self.set_and_apply_sort(str(event.column_key.value), "click")

    def apply_table_sort(self) -> None:
        """Applies the current sort order stored on the app to #history-table."""
        sort_column_key = getattr(self.app, "_history_sort_column_key", None)
        reverse = getattr(self.app, "_history_sort_reverse", False)
        if sort_column_key is None:
            return
        try:
            table = self.query_one("#history-table", DataTable)
            sort_history_table(table, sort_column_key, reverse)
        except NoMatches:
            logger.error(
                f"Could not find history table for sort key '{sort_column_key}'"
            )

    def set_and_apply_sort(self, column_key_str: str, source: str) -> None:
        """Updates the history sort key/direction and sorts #history-table."""
        if getattr(self.app, "_history_sort_column_key", None) == column_key_str:
            self.app._history_sort_reverse = not getattr(
                self.app, "_history_sort_reverse", False
            )
        else:
            self.app._history_sort_column_key = column_key_str
            self.app._history_sort_reverse = column_key_str == "Date"
        self.apply_table_sort()
