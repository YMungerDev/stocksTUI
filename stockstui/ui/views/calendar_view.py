"""Calendar view: upcoming earnings and ex-dividend dates across watchlists."""

import logging
from datetime import date, datetime
from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from stockstui.data_providers import market_provider
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

logger = logging.getLogger(__name__)


def collect_upcoming_events(
    lists: dict[str, list[dict[str, Any]]], hidden_tabs: set[str]
) -> list[dict[str, Any]]:
    """
    Gathers cached earnings/ex-dividend dates for every visible watchlist
    ticker and returns rows sorted by date, past dates dropped.

    Only the in-memory calendar cache is read: this runs on the UI thread, so
    it must not touch the network. Tickers whose calendar has not been fetched
    yet simply do not appear until the next slow refresh populates it.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    today = date.today()
    for list_name, list_data in lists.items():
        if list_name in hidden_tabs:
            continue
        for entry in list_data:
            ticker = entry.get("ticker", "").upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            cached = market_provider.get_cached_price(ticker)
            if cached is None:
                continue
            alias = entry.get("alias") or cached.get("description") or ticker
            events: list[tuple[date, str, str]] = []
            if earnings := cached.get("next_earnings"):
                events.append((earnings, "Earnings", _format_date(earnings)))
            if ex_div := cached.get("ex_dividend"):
                events.append((ex_div, "Ex-Dividend", _format_date(ex_div)))
            for event_date, event_type, date_str in events:
                if event_date < today:
                    continue
                rows.append(
                    {
                        "date": event_date,
                        "Date": date_str,
                        "Ticker": ticker,
                        "Alias": alias,
                        "Event": event_type,
                        "List": list_name,
                    }
                )
    rows.sort(key=lambda row: row["date"])
    return rows


def _format_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class CalendarView(Vertical):
    """View listing upcoming earnings and ex-dividend dates for watchlists."""

    app: Any

    def compose(self) -> ComposeResult:
        yield Label("Upcoming Earnings & Ex-Dividend Dates", classes="content-header")
        yield NavigableDataTable(id="calendar-table", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#calendar-table", NavigableDataTable)
        table.add_columns("Date", "Ticker", "Alias", "Event", "List")
        self.repopulate()

    def on_screen_resume(self) -> None:
        self.repopulate()

    def repopulate(self) -> None:
        """Renders the events table from the in-memory calendar cache."""
        try:
            table = self.query_one("#calendar-table", NavigableDataTable)
            table.clear()
            rows = collect_upcoming_events(
                self.app.config.lists, self.app._hidden_tabs
            )
            if not rows:
                table.add_row(
                    "[dim]No upcoming events cached yet. Refresh a watchlist (r) "
                    "to fetch earnings and dividend dates.[/dim]"
                )
                return
            for row in rows:
                table.add_row(
                    row["Date"], row["Ticker"], row["Alias"], row["Event"], row["List"]
                )
        except Exception as e:  # noqa: BLE001 - a view render must never crash the app
            logger.error(f"Failed to render calendar view: {e}")
