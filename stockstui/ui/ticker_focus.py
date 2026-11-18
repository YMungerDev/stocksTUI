"""
One selected ticker shared by every ticker view (#23).

The app keeps a single ``focus_ticker``. Setting it (Enter on a watchlist
row, ``:`` go-to-ticker, or a single symbol typed into any ticker view)
copies it into each view's own ``<view>_ticker`` attribute, so every view
opens on the same company. A view's attribute can still be set on its own
for input only that view understands (History comparisons like
``NVDA,AMD`` or ``NVDA/SMH``, multi-ticker News); the next focus change
replaces it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from stockstui.data_providers import market_provider
from stockstui.presentation.formatter import get_currency_symbol

# Ticker views and the app attribute each one reads its ticker from.
TICKER_VIEW_ATTRS: dict[str, str] = {
    "history": "history_ticker",
    "news": "news_ticker",
    "filings": "filings_ticker",
    "fundamentals": "fundamentals_ticker",
    "options": "options_ticker",
}


def normalize_ticker(value: str | None) -> str | None:
    """Uppercases a symbol and strips a suggester suffix ('AAPL - Apple')."""
    if not value:
        return None
    symbol = str(value).strip().split(" - ")[0].strip().upper()
    return symbol or None


def is_single_ticker(value: str | None) -> bool:
    """True for one plain symbol; False for comparisons, ratios or lists."""
    return bool(value) and "," not in str(value) and "/" not in str(value)


class TickerHistory:
    """Back/forward list of recently viewed tickers, like a browser."""

    def __init__(self, limit: int = 50) -> None:
        self._items: list[str] = []
        self._index = -1
        self._limit = limit

    @property
    def current(self) -> str | None:
        return self._items[self._index] if self._index >= 0 else None

    def visit(self, ticker: str) -> None:
        """Records a newly viewed ticker, dropping any forward entries."""
        if ticker == self.current:
            return
        del self._items[self._index + 1 :]
        self._items.append(ticker)
        if len(self._items) > self._limit:
            del self._items[: len(self._items) - self._limit]
        self._index = len(self._items) - 1

    def can_go_back(self) -> bool:
        return self._index > 0

    def can_go_forward(self) -> bool:
        return 0 <= self._index < len(self._items) - 1

    def back(self) -> str | None:
        if not self.can_go_back():
            return None
        self._index -= 1
        return self.current

    def forward(self) -> str | None:
        if not self.can_go_forward():
            return None
        self._index += 1
        return self.current


def _display_name(app: Any, ticker: str, cached: dict | None) -> str | None:
    """Name for the header: the user's alias/note, else the quote's name."""
    config = getattr(app, "config", None)
    lists = getattr(config, "lists", None)
    if isinstance(lists, dict):
        for entries in lists.values():
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and str(entry.get("ticker", "")).upper() == ticker:
                    alias = entry.get("alias") or entry.get("note")
                    if alias and str(alias).upper() != ticker:
                        return str(alias)
    if cached and cached.get("description") and cached["description"] != ticker:
        return str(cached["description"])
    info = market_provider.get_info_cache_state().get(ticker) or {}
    return info.get("longName") or info.get("shortName")


def header_text(app: Any, value: str | None) -> Text:
    """
    The one-line header on ticker views, e.g. ``AAPL · Apple · $353.78 +0.4%``.

    Price and change come from the quote cache, so they show once the ticker
    has been quoted (it is on a watchlist); otherwise just symbol and name.
    """
    text = Text()
    if not value:
        text.append(
            "No ticker selected — Enter on a watchlist row or press : to pick one",
            style="dim",
        )
        return text
    if not is_single_ticker(value):
        text.append("Comparing ", style="dim")
        text.append(", ".join(p for p in str(value).split(",") if p), style="bold")
        return text

    ticker = str(value).upper()
    cached = market_provider.get_cached_price(ticker)
    text.append(ticker, style="bold")
    name = _display_name(app, ticker, cached)
    if name:
        text.append(f" · {name}")
    price = cached.get("price") if cached else None
    if isinstance(price, (int, float)):
        symbol = get_currency_symbol(cached.get("currency") or "USD") if cached else "$"
        text.append(f" · {symbol}{price:,.2f}")
        prev = cached.get("previous_close") if cached else None
        if isinstance(prev, (int, float)) and prev:
            pct = (price - prev) / prev * 100
            variables = getattr(app, "theme_variables", None) or {}
            colour = (
                variables.get("green", "green")
                if pct >= 0
                else variables.get("red", "red")
            )
            text.append(f" {pct:+.1f}%", style=colour)
    return text


class TickerHeader(Static):
    """Shows which ticker a view is on (see :func:`header_text`)."""

    DEFAULT_CLASSES = "ticker-header"

    def show(self, value: str | None) -> None:
        self.update(header_text(self.app, value))


class TickerViewMixin:
    """
    Shared ticker plumbing for the five ticker views.

    A view sets ``ticker_attr`` (its app attribute) and ``ticker_input_id``,
    implements ``_load_ticker()`` to request data for the current ticker,
    and calls ``_mark_shown()`` whenever it starts showing a ticker. The app
    calls ``show_ticker()`` when the shared ticker changed since then.
    """

    ticker_attr: ClassVar[str]
    ticker_input_id: ClassVar[str]
    _shown_ticker: str | None = None
    app: Any

    def current_ticker(self) -> str | None:
        return getattr(self.app, self.ticker_attr, None)

    def _mark_shown(self) -> None:
        self._shown_ticker = self.current_ticker()
        self.refresh_ticker_header()

    def refresh_ticker_header(self) -> None:
        try:
            self.query_one(TickerHeader).show(self.current_ticker())  # type: ignore[attr-defined]
        except NoMatches:
            pass

    def needs_ticker_sync(self) -> bool:
        ticker = self.current_ticker()
        return bool(ticker) and ticker != self._shown_ticker

    def show_ticker(self) -> None:
        """Loads the app's current ticker for this view."""
        try:
            self.query_one(f"#{self.ticker_input_id}", Input).value = (  # type: ignore[attr-defined]
                self.current_ticker() or ""
            )
        except NoMatches:
            pass
        self._load_ticker()

    def _submit_ticker(self, value: str) -> None:
        """
        Applies a ticker typed into this view. A single symbol becomes the
        shared ticker for every view; anything else stays local to this view.
        """
        setattr(self.app, self.ticker_attr, value)
        setter = getattr(self.app, "set_focus_ticker", None)
        if is_single_ticker(value) and callable(setter):
            setter(value, sync=False)
        self._load_ticker()

    def _load_ticker(self) -> None:  # pragma: no cover - each view overrides
        raise NotImplementedError
