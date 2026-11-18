import math

from rich.text import Text
from textual.app import ComposeResult, on
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.dom import NoMatches
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    Select,
    Static,
)

from stockstui.data_providers import market_provider
from stockstui.presentation import option_analytics as analytics
from stockstui.presentation.formatter import get_currency_symbol
from stockstui.ui.position_modal import PositionModal
from stockstui.ui.suggesters import TickerSuggester
from stockstui.ui.ticker_focus import TickerHeader, TickerViewMixin
from stockstui.ui.widgets.oi_chart import OIChart


class OptionsView(TickerViewMixin, Vertical):
    """A view for displaying stock options chains, allowing selection of tickers and expiration dates."""

    BINDINGS = [
        Binding("p", "manage_position", "Position", show=True),
        Binding("c", "toggle_chart", "Toggle Chart", show=True),
        Binding("[", "prev_expiration", "Prev Exp", show=True),
        Binding("]", "next_expiration", "Next Exp", show=True),
    ]

    ticker_attr = "options_ticker"
    ticker_input_id = "options-ticker-input"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expirations = []

    @staticmethod
    def _clean_number(value, fmt: str) -> str:
        """Formats a number, rendering NaN/None as an em dash."""
        try:
            value_f = float(value)
        except (TypeError, ValueError):
            return "—"
        if math.isnan(value_f):
            return "—"
        return fmt.format(value_f)

    def compose(self) -> ComposeResult:
        """Creates the layout for the options view."""
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

        yield TickerHeader()
        # Horizontal container for ticker input and expiration selector
        with Horizontal(classes="options_controls"):
            yield Input(
                placeholder="Enter a ticker...",
                suggester=suggester,
                id="options-ticker-input",
                value=self.app.options_ticker
                or "",  # Pre-fill with last selected ticker
            )
            yield Select(
                options=[("Select expiration", "")],
                id="options-expiration-select",
                allow_blank=False,
                prompt="Expiration",
            )
            yield Button("Chart", id="options-view-toggle", variant="primary")

        # Container to display the options chain
        with Container(id="options-display-container"):
            yield Static(
                "Enter a ticker symbol to view its options chain.",
                id="options-info-message",
            )

    def on_mount(self) -> None:
        """
        Called when the OptionsView is mounted.
        Triggers initial data rendering if ticker is already set.
        """
        # Trigger expiration fetch if a ticker is already set
        self._mark_shown()
        if self.app.options_ticker:
            self.call_after_refresh(self._request_expirations)

        # Disable expiration selector until expirations are loaded
        try:
            self.query_one("#options-expiration-select", Select).disabled = True
        except NoMatches:
            pass

    async def _render_options_data(self):
        """
        Renders the options chain data (calls and puts tables) in the display container.
        Uses the `_last_options_data` from the app state.
        """
        try:
            display_container = self.query_one("#options-display-container")
            await display_container.remove_children()  # Clear previous content

            last_data = self.app._last_options_data
            if last_data is None:
                await display_container.mount(
                    Static(
                        "Enter a ticker symbol and select an expiration date.",
                        id="options-info-message",
                    )
                )
                return

            # Check for errors
            if last_data.get("error"):
                error_text = Text.assemble(
                    ("Error: ", "bold red"),
                    last_data.get("error", "Unknown error occurred."),
                )
                await display_container.mount(Static(error_text))
                return

            calls_df = last_data.get("calls")
            puts_df = last_data.get("puts")
            underlying_data = last_data.get("underlying", {})

            if (
                calls_df is None
                or puts_df is None
                or (calls_df.empty and puts_df.empty)
            ):
                await display_container.mount(
                    Static(
                        f"No options data found for {self.app.options_ticker}.",
                        id="options-info-message",
                    )
                )
                return

            # Get underlying price for ITM/OTM calculations
            underlying_price = underlying_data.get("regularMarketPrice", 0)

            # Resolve currency from the ticker the chain actually belongs to,
            # not the app's current selection (they can diverge while a fetch
            # is in flight, which mislabeled USD prices with another currency).
            chain_ticker = last_data.get("ticker") or self.app.options_ticker
            info = market_provider.get_ticker_info(chain_ticker) if chain_ticker else None
            currency_code = info.get("currency", "USD") if info else "USD"
            sym = get_currency_symbol(currency_code)

            # Calendar days to expiration, the same figure as the summary
            # strip. Subtracting datetimes gave -1 on expiry day (#22).
            expiration_str = last_data.get("expiration", "")
            days_to_exp = (
                analytics.days_until(expiration_str) if expiration_str else None
            )

            # Create info header
            info_text = f"Underlying: {sym}{underlying_price:.2f}"
            if days_to_exp is not None:
                info_text += f"  |  Days to Expiration: {days_to_exp}"
            info_header = Static(info_text, classes="options-info-header")
            await display_container.mount(info_header)

            # Get theme colors
            success_color = self.app.theme_variables.get("success", "green")
            error_color = self.app.theme_variables.get("error", "red")
            muted_color = self.app.theme_variables.get("text-muted", "dim")
            accent_color = self.app.theme_variables.get("accent", "blue")

            # Helper to create table
            def create_table(df, table_id, label_text, is_call):
                label = Label(label_text, classes="options-table-header")
                table = DataTable(id=table_id, zebra_stripes=True)
                table.cursor_type = "row"

                if df.empty:
                    return Vertical(label, table, classes="options-table-section")

                table.add_columns(
                    "Strike",
                    "Last",
                    "Bid",
                    "Ask",
                    "Chg %",
                    "Vol",
                    "OI",
                    "IV",
                    "Δ",
                    "Γ",
                    "Θ",
                    "ν",
                    "Pos",
                    "P/L",  # New columns
                )

                for _, row in df.iterrows():
                    strike = row.get("strike", 0)
                    contract_symbol = row.get("contractSymbol")

                    # Position check
                    position = self.app.option_positions.get(contract_symbol)
                    has_position = position is not None

                    is_itm = False
                    if is_call:
                        is_itm = (
                            strike < underlying_price if underlying_price else False
                        )
                    else:
                        is_itm = (
                            strike > underlying_price if underlying_price else False
                        )

                    # Format strike
                    strike_text = Text(f"{sym}{strike:.2f}")
                    if is_itm:
                        strike_text.stylize(f"bold {success_color}")

                    # Position styling
                    if has_position:
                        strike_text.stylize(f"bold {accent_color} reverse")

                    last_price = row.get("lastPrice", 0)
                    last_text = (
                        f"{sym}{last_price:.2f}"
                        if isinstance(last_price, (int, float))
                        and not (isinstance(last_price, float) and math.isnan(last_price))
                        else "—"
                    )
                    bid = self._clean_number(row.get("bid", 0), f"{sym}{{:.2f}}")
                    ask = self._clean_number(row.get("ask", 0), f"{sym}{{:.2f}}")

                    # % Change
                    pct_change_val = row.get("percentChange", 0)
                    if (
                        isinstance(pct_change_val, (int, float))
                        and not (
                            isinstance(pct_change_val, float)
                            and math.isnan(pct_change_val)
                        )
                    ):
                        pct_change_text = Text(f"{pct_change_val:.1f}%")
                        if pct_change_val > 0:
                            pct_change_text.stylize(success_color)
                        elif pct_change_val < 0:
                            pct_change_text.stylize(error_color)
                        else:
                            pct_change_text.stylize(muted_color)
                    else:
                        pct_change_text = Text("—", style=muted_color)

                    # Handle NaN values for volume and openInterest
                    volume_val = row.get("volume", 0)
                    oi_val = row.get("openInterest", 0)

                    volume = (
                        f"{int(volume_val):,}"
                        if not (
                            isinstance(volume_val, float) and math.isnan(volume_val)
                        )
                        else "—"
                    )
                    open_interest = (
                        f"{int(oi_val):,}"
                        if not (isinstance(oi_val, float) and math.isnan(oi_val))
                        else "—"
                    )
                    iv = f"{row.get('impliedVolatility', 0):.2%}"

                    # Greeks
                    delta = f"{row.get('delta', 0):.2f}"
                    gamma = f"{row.get('gamma', 0):.3f}"
                    theta = f"{row.get('theta', 0):.3f}"
                    vega = f"{row.get('vega', 0):.3f}"

                    # Position Data
                    pos_text = ""
                    pl_text = Text("")
                    if has_position:
                        qty = position.get("quantity", 0)
                        avg_cost = position.get("avg_cost", 0)
                        pos_text = f"{qty:g}"

                        # Calculate P/L
                        # P/L = (Current Price - Avg Cost) * Qty * 100
                        if isinstance(last_price, (int, float)) and not (
                            isinstance(last_price, float) and math.isnan(last_price)
                        ):
                            pl_val = (last_price - avg_cost) * qty * 100
                            pl_text = Text(f"{sym}{pl_val:,.0f}")
                            if pl_val > 0:
                                pl_text.stylize(success_color)
                            elif pl_val < 0:
                                pl_text.stylize(error_color)
                        else:
                            pl_text = Text("—", style=muted_color)

                    table.add_row(
                        strike_text,
                        last_text,
                        bid,
                        ask,
                        pct_change_text,
                        volume,
                        open_interest,
                        iv,
                        delta,
                        gamma,
                        theta,
                        vega,
                        pos_text,
                        pl_text,
                        key=contract_symbol,  # Store symbol as row key
                    )

                return Vertical(label, table, classes="options-table-section")

            # Create calls and puts tables
            calls_section = create_table(
                calls_df, "options-calls-table", "Calls", True
            )
            puts_section = create_table(
                puts_df, "options-puts-table", "Puts", False
            )

            # Create Horizontal tables_container with sections as children
            tables_container = Horizontal(
                calls_section, puts_section,
                classes="options-tables-container", id="options-tables-view"
            )

            # --- CHART VIEW ---
            chart = OIChart(
                calls_df, puts_df, underlying_price, ticker=self.app.options_ticker, currency_symbol=sym
            )
            chart_container = Container(chart, id="options-chart-view")

            # Create ContentSwitcher switcher with containers as children
            switcher = ContentSwitcher(
                tables_container, chart_container,
                initial="options-tables-view", id="options-content-switcher"
            )

            # Single mount call attaches the entire tree at once
            await display_container.mount(switcher)

            # Analytics summary (#11): implied move for the selected expiry,
            # put/call ratios, and max pain for the current chain.
            await self._mount_analytics_summary(
                display_container, calls_df, puts_df, underlying_price, last_data
            )

        except NoMatches:
            pass

    async def _mount_analytics_summary(
        self, display_container, calls_df, puts_df, underlying_price, last_data
    ) -> None:
        """Renders the analytics summary strip beneath the chain/chart."""
        try:
            expiration = last_data.get("expiration", "")
            days = analytics.days_until(expiration) if expiration else None
            move = analytics.implied_move(
                calls_df, puts_df, underlying_price, days
            )
            ratios = analytics.put_call_ratios(calls_df, puts_df)
            pain = analytics.max_pain(calls_df, puts_df)
            # Currency of the chain's own ticker, as in the chain render.
            chain_ticker = last_data.get("ticker") or self.app.options_ticker
            info = (
                market_provider.get_ticker_info(chain_ticker) if chain_ticker else None
            )
            sym = get_currency_symbol(info.get("currency", "USD") if info else "USD")

            def fmt(value, suffix=""):
                if value is None or (
                    isinstance(value, float) and math.isnan(value)
                ):
                    return "N/A"
                return f"{value:,.2f}{suffix}"

            parts: list[str] = []
            if move:
                parts.append(
                    f"Implied move: ±{sym}{move['move']:,.2f}"
                    f" (±{move['move_pct']:.1f}%)"
                )
            parts.append(f"P/C Vol: {fmt(ratios.get('put_call_volume'), 'x')}")
            parts.append(f"P/C OI: {fmt(ratios.get('put_call_open_interest'), 'x')}")
            if pain is not None:
                parts.append(f"Max pain: {sym}{pain:,.0f}")
            if days is not None:
                parts.append(f"{days}d to expiry")

            await display_container.mount(
                Static("  |  ".join(parts), classes="options-analytics-header")
            )
        except Exception as e:  # noqa: BLE001 - analytics must not break render
            self.app.notify(f"Analytics unavailable: {e}", severity="warning")

    def action_toggle_chart(self):
        """Toggles between the table view and the chart view."""
        try:
            switcher = self.query_one("#options-content-switcher", ContentSwitcher)
            if switcher.current == "options-tables-view":
                switcher.current = "options-chart-view"
            else:
                switcher.current = "options-tables-view"
        except NoMatches:
            pass

    def update_expirations(self, expirations: list[str]):
        """Updates the expiration selector with new dates."""
        self.expirations = expirations
        try:
            select = self.query_one("#options-expiration-select", Select)
            if expirations:
                options = [(exp, exp) for exp in expirations]
                # Highlight the expiry spanning the cached next earnings
                # date (#11): an [E] mark when earnings falls on/before the
                # expiry but after the previous one.
                next_earnings = self._next_earnings_date()
                if next_earnings is not None:
                    options = [
                        (
                            (
                                f"{exp}  [E]"
                                if self._spans_earnings(exp, expirations, next_earnings)
                                else exp
                            ),
                            exp,
                        )
                        for exp in expirations
                    ]
                select.set_options(options)
                select.disabled = False
                if options:
                    select.value = options[0][1]
            else:
                select.set_options([("No options available", "")])
                select.disabled = True
        except NoMatches:
            pass

    def _next_earnings_date(self) -> str | None:
        """The cached next-earnings ISO date string for the current ticker."""
        cached = market_provider.get_cached_price(self.app.options_ticker or "")
        if not cached:
            return None
        return cached.get("next_earnings")

    @staticmethod
    def _spans_earnings(expiry: str, expirations: list[str], earnings_iso: str) -> bool:
        """True when earnings falls between this expiry and the previous one."""
        try:
            position = expirations.index(expiry)
            return earnings_iso <= expiry and (
                position == 0 or earnings_iso > expirations[position - 1]
            )
        except (ValueError, TypeError):
            return False

    def action_prev_expiration(self):
        """Selects the previous expiration date."""
        try:
            select = self.query_one("#options-expiration-select", Select)
            if not self.expirations or select.disabled:
                return

            current_value = select.value

            if not current_value or current_value not in self.expirations:
                return

            idx = self.expirations.index(current_value)
            if idx > 0:
                select.value = self.expirations[idx - 1]
        except NoMatches:
            pass

    def action_next_expiration(self):
        """Selects the next expiration date."""
        try:
            select = self.query_one("#options-expiration-select", Select)
            if not self.expirations or select.disabled:
                return

            current_value = select.value

            if not current_value or current_value not in self.expirations:
                return

            idx = self.expirations.index(current_value)
            if idx < len(self.expirations) - 1:
                select.value = self.expirations[idx + 1]
        except NoMatches:
            pass

    @on(Button.Pressed, "#options-view-toggle")
    def on_toggle_chart_pressed(self):
        self.action_toggle_chart()

    def action_manage_position(self):
        """Opens modal to manage position for the selected option."""
        # Determine which table has focus
        focused = self.app.focused
        if not focused or not isinstance(focused, DataTable):
            return

        if focused.id not in ["options-calls-table", "options-puts-table"]:
            return

        # Get selected row
        row_key = focused.coordinate_to_cell_key(focused.cursor_coordinate).row_key
        if not row_key:
            return

        contract_symbol = row_key.value
        if not contract_symbol:
            return

        # Get current position if exists
        current_pos = self.app.option_positions.get(contract_symbol)

        def handle_position_result(result: tuple[float, float] | None):
            if result is None:
                return

            qty, cost = result
            if qty == 0:
                self.app.remove_option_position(contract_symbol)
            else:
                self.app.add_option_position(
                    contract_symbol, self.app.options_ticker, qty, cost
                )

            # Refresh view to show new position
            self.call_after_refresh(self._render_options_data)

        self.app.push_screen(
            PositionModal(contract_symbol, current_pos), handle_position_result
        )

    def _request_expirations(self):
        """Requests available expiration dates for the current ticker."""
        if not self.app.options_ticker:
            return
        self._mark_shown()

        # Disable expiration selector while loading
        try:
            self.query_one("#options-expiration-select", Select).disabled = True
        except NoMatches:
            pass

        self.app.fetch_options_expirations(self.app.options_ticker)

    def _request_options_chain(self):
        """Requests the options chain for the current ticker and selected expiration."""
        if not self.app.options_ticker:
            return

        try:
            expiration_select = self.query_one("#options-expiration-select", Select)
            if expiration_select.value and expiration_select.value != "":
                display_container = self.query_one("#options-display-container")
                display_container.loading = True
                self.app.fetch_options_chain(
                    self.app.options_ticker, expiration_select.value
                )
        except NoMatches:
            pass

    def _parse_ticker_from_input(self, value: str) -> str:
        """Extracts the ticker symbol from a suggestion string ('TICKER - Desc') or raw input."""
        if " - " in value:
            return value.strip().split(" - ")[0].upper()
        return value.strip().upper()

    @on(Input.Submitted, "#options-ticker-input")
    def on_options_ticker_submitted(self, event: Input.Submitted):
        """Handles submission of the ticker input, triggering expiration fetch."""
        if event.value:
            self._submit_ticker(self._parse_ticker_from_input(event.value))

    def _load_ticker(self) -> None:
        self._request_expirations()

    @on(Select.Changed, "#options-expiration-select")
    def on_expiration_changed(self, event: Select.Changed):
        """Handles changes in the expiration selector, triggering options chain fetch."""
        if event.value and event.value != "":
            self._request_options_chain()
