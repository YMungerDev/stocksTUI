"""Filings view: recent SEC filings for a ticker, with form-type filter."""

import logging
import webbrowser
from typing import Any

from rich.text import Text
from textual.app import ComposeResult, on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Select

from stockstui.data_providers import edgar_provider
from stockstui.ui.suggesters import TickerSuggester
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

logger = logging.getLogger(__name__)


def format_filing_rows(filings: list[dict[str, Any]]) -> list[tuple[Text, ...]]:
    """Formats EDGAR filings into DataTable rows: date, form, description."""
    rows: list[tuple[Text, ...]] = []
    for filing in filings:
        report_date = filing.get("report_date") or filing.get("filing_date") or ""
        form = filing.get("form") or "?"
        description = filing.get("description") or ""
        rows.append(
            (
                Text(report_date),
                Text(form, style="bold"),
                Text(description if description else "—"),
            )
        )
    return rows


class FilingsView(Vertical):
    """A view for displaying recent SEC filings for a selected ticker."""

    BINDINGS = [
        Binding("enter", "open_filing", "Open Filing", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._filings: list[dict[str, Any]] = []
        self._category: str | None = None

    def compose(self) -> ComposeResult:
        all_tickers_data = [s for lst in self.app.config.lists.values() for s in lst]
        suggester_data = [
            (s["ticker"], s.get("note") or s.get("alias", s["ticker"]))
            for s in all_tickers_data
        ]
        unique_suggester_data = list({t[0]: t for t in suggester_data}.values())
        suggester = TickerSuggester(unique_suggester_data, case_sensitive=False)

        with Horizontal(classes="news-controls"):
            yield Input(
                placeholder="Enter a ticker...",
                suggester=suggester,
                id="filings-ticker-input",
                value=self.app.filings_ticker or "",
            )
            yield Select(
                [("All", "All")]
                + [
                    (label, label) for label in edgar_provider.FILING_FORM_CATEGORIES
                ],
                prompt="Form type",
                id="filings-form-select",
                allow_blank=True,
            )
        yield NavigableDataTable(id="filings-table", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#filings-table", NavigableDataTable)
        table.add_columns("Date", "Form", "Description")
        if self.app.filings_ticker:
            self._request_filings()

    @on(Input.Submitted, "#filings-ticker-input")
    def on_filings_ticker_submitted(self, event: Input.Submitted) -> None:
        """Handles ticker submission, triggering an EDGAR fetch."""
        ticker = event.value.strip().upper()
        if not ticker:
            return
        self.app.filings_ticker = ticker
        self._request_filings()

    @on(Select.Changed, "#filings-form-select")
    def on_form_filter_changed(self, event: Select.Changed) -> None:
        """Applies the chosen form-type filter to the current filings."""
        self._category = event.value if event.value else None
        self._render_table()

    def _request_filings(self) -> None:
        """Dispatches the EDGAR fetch on a worker thread."""
        if not self.app.filings_ticker:
            return
        table = self.query_one("#filings-table", NavigableDataTable)
        table.loading = True
        self.app.fetch_filings(self.app.filings_ticker)

    def update_filings(self, filings: list[dict[str, Any]] | None) -> None:
        """Receives fetched filings and renders the table."""
        self._filings = filings or []
        self._render_table()

    def _render_table(self) -> None:
        try:
            table = self.query_one("#filings-table", NavigableDataTable)
            table.loading = False
            table.clear()
            visible = edgar_provider.filter_filings_by_form(
                self._filings, self._category
            )
            if not visible:
                table.add_row(
                    "[dim]No SEC filings found for this ticker."
                    " Non-US listings are not covered by EDGAR.[/dim]"
                )
                return
            for row in format_filing_rows(visible):
                table.add_row(*row)
        except Exception as e:  # noqa: BLE001 - a view render must never crash the app
            logger.error(f"Failed to render filings table: {e}")

    def action_open_filing(self) -> None:
        """Opens the selected filing's primary document in the browser."""
        try:
            table = self.query_one("#filings-table", NavigableDataTable)
            if table.cursor_row < 0:
                return
            visible = edgar_provider.filter_filings_by_form(
                self._filings, self._category
            )
            if table.cursor_row >= len(visible):
                return
            url = visible[table.cursor_row].get("url")
            if not url:
                self.notify("No document URL for this filing.", severity="warning")
                return
            webbrowser.open(url)
        except webbrowser.Error:
            self.notify(
                "No web browser found. Please configure your system's default browser.",
                severity="error",
                timeout=8,
            )
