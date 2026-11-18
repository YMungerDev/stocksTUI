"""
Per-view help (#25) and the ``:`` go-to-ticker prompt (#23).

``?`` opens :class:`HelpScreen` for the current tab: what the view shows,
what its tables mean, and its keys. The key list combines the curated keys
below with whatever bindings are live in the footer when help opens, so a
new binding still appears even before its help text is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from stockstui.ui.suggesters import TickerSuggester

# Keys that work on every tab; listed once at the bottom of each help page.
GLOBAL_KEYS: tuple[tuple[str, str], ...] = (
    ("1-9, 0", "Switch tab"),
    (":", "Go to ticker (sets the shared ticker)"),
    ("/", "Search the current table"),
    ("i", "Focus the view's main input"),
    ("Esc", "Unfocus input / cancel mode / close"),
    ("?", "This help"),
    ("q", "Quit"),
)

TICKER_KEYS: tuple[tuple[str, str], ...] = (
    ("<  /  alt+left", "Previous ticker viewed"),
    (">  /  alt+right", "Next ticker viewed"),
)

SHARED_TICKER_NOTE = (
    "Every ticker view (History, News, Filings, Fundamentals, Options) follows "
    "one shared ticker. Enter on a watchlist row, ':' or typing a single symbol "
    "here changes it for all of them."
)


@dataclass(frozen=True)
class ViewHelp:
    title: str
    summary: str
    sections: tuple[tuple[str, str], ...] = ()
    keys: tuple[tuple[str, str], ...] = ()
    ticker_view: bool = False
    extra: tuple[str, ...] = field(default_factory=tuple)


VIEW_HELP: dict[str, ViewHelp] = {
    "watchlist": ViewHelp(
        title="Watchlist",
        summary="Live quotes for the tickers in this list, refreshed on r or on a timer.",
        sections=(
            ("Change / % Change", "Versus the previous close."),
            ("Computed columns", "Returns, moving averages, RSI and volatility from stored daily bars."),
        ),
        keys=(
            ("Enter", "Set the shared ticker to this row"),
            ("o", "Open the row in News / History / Options / Yahoo"),
            ("s", "Sort mode, then a column letter"),
            ("a", "Price alerts for the row"),
            ("t", "Screen with an expression, e.g. pe_ratio < 20"),
            ("f", "Filter by tag"),
            ("e", "Quick-edit the row"),
            ("r / R", "Refresh / force refresh"),
        ),
    ),
    "history": ViewHelp(
        title="History",
        summary="Price history as a table or chart for the shared ticker.",
        sections=(
            (
                "Comparison",
                (
                    "Type NVDA,AMD to overlay rebased series, or NVDA/SMH for a ratio. "
                    "This stays local to History until the shared ticker changes."
                ),
            ),
            ("Range", "1D to All; the switch toggles table and chart."),
        ),
        keys=(
            ("ctrl+m / ctrl+n", "50-day / 200-day moving average"),
            ("ctrl+t", "Volume"),
            ("ctrl+o", "Drawdown"),
            ("ctrl+y", "Earnings markers"),
            ("s", "Sort the table"),
        ),
        ticker_view=True,
    ),
    "news": ViewHelp(
        title="News",
        summary="Recent headlines for the shared ticker. A comma-separated list stays local to News.",
        keys=(
            ("Tab / shift+Tab", "Move between links"),
            ("Enter", "Open the highlighted link"),
        ),
        ticker_view=True,
    ),
    "filings": ViewHelp(
        title="Filings",
        summary="Recent SEC filings for the shared ticker, from EDGAR.",
        sections=(("Form type", "Filter to 10-K, 10-Q, 8-K, insider forms and so on."),),
        keys=(("o / Enter", "Open the highlighted filing"),),
        ticker_view=True,
    ),
    "fundamentals": ViewHelp(
        title="Fundamentals",
        summary="Financial statements from filing XBRL, with derived ratios.",
        sections=(
            ("Ratios", "Margins, returns and leverage from the latest period."),
            ("Statements", "* restated by a later filing; italics are derived (e.g. Q4 = FY - 9M)."),
            ("Valuation vs Own History", "Current multiple against its 5y/10y range and percentile."),
            ("Earnings Quality", "Accruals and cash conversion, latest vs prior year."),
            ("Analyst & Ownership", "Estimates, surprises, insiders and holders."),
        ),
        keys=(("o", "Open the filing behind the highlighted row"),),
        ticker_view=True,
    ),
    "options": ViewHelp(
        title="Options",
        summary="Option chain for the shared ticker and the selected expiry.",
        sections=(("Analytics", "Implied move, skew, term structure, put/call ratios and max pain."),),
        keys=(
            ("[ / ]", "Previous / next expiry"),
            ("c", "Toggle the open-interest chart"),
            ("p / Enter", "Add or edit a position for the highlighted contract"),
        ),
        ticker_view=True,
    ),
    "fred": ViewHelp(
        title="FRED",
        summary="Economic series from FRED, with recent values and changes.",
        keys=(("e", "Edit the series alias"), ("o", "Open the series in a browser")),
    ),
    "calendar": ViewHelp(
        title="Calendar",
        summary="Upcoming earnings and ex-dividend dates for watchlist tickers.",
        keys=(("f", "Filter by tag"),),
    ),
    "debug": ViewHelp(
        title="Debug",
        summary="Data-provider tests and timings.",
    ),
    "configs": ViewHelp(
        title="Configs",
        summary="Settings, watchlists, portfolios and FRED series.",
        keys=(("Backspace", "Back to the settings menu"),),
    ),
}


def help_for(category: str | None, is_watchlist: bool) -> ViewHelp:
    if is_watchlist:
        return VIEW_HELP["watchlist"]
    return VIEW_HELP.get(category or "", ViewHelp(title=(category or "Help").title(), summary=""))


def _key_table(rows: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    for key, desc in rows:
        table.add_row(key, desc)
    return table


def _key_tokens(rows) -> set[str]:
    """Individual keys in curated rows: 'ctrl+m / ctrl+n' -> {'ctrl+m', 'ctrl+n'}."""
    return {tok for key, _ in rows for tok in re.split(r"[\s/,]+", key.lower()) if tok}


def build_help(help_: ViewHelp, live_keys: list[tuple[str, str, str]]) -> Group:
    """
    Renders a help page. ``live_keys`` are (key, display, description) for
    the bindings in the footer; any not already covered are listed too.
    """
    parts: list = []
    if help_.summary:
        parts.append(Text(help_.summary))
    if help_.ticker_view:
        parts += [Text(""), Text(SHARED_TICKER_NOTE, style="dim")]
    if help_.sections:
        parts += [Text(""), Text("What you see", style="bold underline")]
        parts.append(_key_table(list(help_.sections)))

    view_keys = list(help_.keys)
    if help_.ticker_view:
        view_keys += list(TICKER_KEYS)
    known = _key_tokens(view_keys) | _key_tokens(GLOBAL_KEYS)
    for key, display, desc in live_keys:
        if key.lower() not in known and display.lower() not in known:
            view_keys.append((display, desc))
            known.add(key.lower())
    if view_keys:
        parts += [Text(""), Text("Keys", style="bold underline"), _key_table(view_keys)]
    parts += [Text(""), Text("Everywhere", style="bold underline"), _key_table(list(GLOBAL_KEYS))]
    return Group(*parts)


class HelpScreen(ModalScreen[None]):
    """Help for the current view; ? or Esc closes it."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,question_mark,q", "close", "Close", show=False),
    ]

    def __init__(self, help_: ViewHelp, live_keys: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._help = help_
        self._live_keys = live_keys

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label(f"{self._help.title} — help", id="help-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(build_help(self._help, self._live_keys), id="help-body")
            yield Label("? or Esc to close", id="help-hint")

    def action_close(self) -> None:
        self.dismiss(None)


class GoToTickerModal(ModalScreen[str | None]):
    """``:`` prompt that sets the shared ticker."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, suggestions: list[tuple[str, str]], current: str | None = None) -> None:
        super().__init__()
        self._suggestions = suggestions
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Go to ticker:")
            yield Input(
                placeholder=self._current or "e.g. AAPL",
                suggester=TickerSuggester(self._suggestions, case_sensitive=False),
                id="goto-ticker-input",
            )

    def on_mount(self) -> None:
        self.query_one("#goto-ticker-input", Input).focus()

    @on(Input.Submitted, "#goto-ticker-input")
    def on_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
