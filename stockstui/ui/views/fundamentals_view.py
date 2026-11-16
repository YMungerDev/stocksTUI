"""Fundamentals view: financial statements and derived ratios (#6)."""

import logging
from typing import Any

from rich.text import Text
from textual.app import ComposeResult, on
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Select, Static

from stockstui.data_providers import fundamentals_provider
from stockstui.ui.suggesters import TickerSuggester
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

logger = logging.getLogger(__name__)

# Rows surfaced per statement, in display order, with yfinance aliases.
_INCOME_ROWS = [
    ("Total Revenue", ("Total Revenue", "Revenues")),
    ("Gross Profit", ("Gross Profit",)),
    ("Operating Income", ("Operating Income",)),
    ("Net Income", ("Net Income", "Net Income Common Stockholders")),
    ("Diluted EPS", ("Diluted EPS", "Basic EPS")),
    ("EBITDA", ("EBITDA", "Normalized EBITDA")),
]
_BALANCE_ROWS = [
    ("Total Debt", ("Total Debt",)),
    ("Cash & ST Investments", ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")),
    ("Stockholders Equity", ("Stockholders Equity", "Total Equity Gross Minority Interest")),
    ("Invested Capital", ("Invested Capital",)),
]
_CASH_ROWS = [
    ("Operating Cash Flow", ("Operating Cash Flow",)),
    ("Capital Expenditure", ("Capital Expenditure",)),
    ("Free Cash Flow", ("Free Cash Flow",)),
]

_RATIOS_BY_KEY = [
    ("revenue_growth", "Revenue YoY", True),
    ("net_income_growth", "Net Income YoY", True),
    ("eps_growth", "EPS YoY", True),
    ("gross_margin", "Gross Margin", True),
    ("operating_margin", "Op Margin", True),
    ("net_margin", "Net Margin", True),
    ("roic", "ROIC (21% tax)", True),
    ("fcf", "FCF", False),
    ("net_debt_to_ebitda", "Net Debt/EBITDA", False),
]



def format_statement_rows(
    payload: dict[str, Any] | None,
    row_specs: list[tuple[str, tuple[str, ...]]],
    max_columns: int = 5,
) -> list[tuple]:
    """Builds DataTable rows for one statement; blank when payload missing."""
    if not payload:
        return [(Text(name, style="dim"), *(Text("—") for _ in range(max_columns))) for name, _ in row_specs]
    columns = payload.get("columns", [])[:max_columns]
    rows = []
    for label, aliases in row_specs:
        values = None
        for alias in aliases:
            data = payload.get("rows", {}).get(alias)
            if data is not None:
                values = data
                break
        cells = [Text(label, style="bold")]
        for pos in range(len(columns)):
            if values is None or pos >= len(values) or values[pos] is None:
                cells.append(Text("—", style="dim"))
            else:
                value = values[pos]
                if isinstance(value, (int, float)):
                    if "EPS" in label:
                        cells.append(Text(f"{value:,.2f}"))
                    else:
                        cells.append(Text(f"{value:,.0f}"))
                else:
                    cells.append(Text(str(value)))
        rows.append(tuple(cells))
    return rows


class FundamentalsView(Vertical):
    """A view for displaying financial statements and derived ratios."""

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
                id="fundamentals-ticker-input",
                value=self.app.fundamentals_ticker or "",
            )
            yield Select(
                [("Annual", "annual"), ("Quarterly", "quarterly")],
                prompt="Period",
                id="fundamentals-period-select",
                allow_blank=False,
                value="annual",
            )
        yield NavigableDataTable(id="fundamentals-ratio-table", zebra_stripes=True)
        yield NavigableDataTable(id="fundamentals-table", zebra_stripes=True)
        yield Static("Analyst & Ownership Research", classes="content-header")
        yield NavigableDataTable(id="research-table", zebra_stripes=True)

    def on_mount(self) -> None:
        ratio_table = self.query_one("#fundamentals-ratio-table", NavigableDataTable)
        ratio_table.add_columns("Metric", "Value")
        table = self.query_one("#fundamentals-table", NavigableDataTable)
        table.add_columns("Line Item", "Latest", "Yr -1", "Yr -2", "Yr -3", "Yr -4")
        research_table = self.query_one("#research-table", NavigableDataTable)
        research_table.add_columns("Block", "Detail")
        if self.app.fundamentals_ticker:
            self.call_after_refresh(self._request_statements)

    @on(Input.Submitted, "#fundamentals-ticker-input")
    def on_ticker_submitted(self, event: Input.Submitted) -> None:
        ticker = event.value.strip().split(" - ")[0].strip().upper()
        if not ticker:
            return
        self.app.fundamentals_ticker = ticker
        self._request_statements()

    @on(Select.Changed, "#fundamentals-period-select")
    def on_period_changed(self, event: Select.Changed) -> None:
        if event.value:
            self._request_statements()

    def _request_statements(self) -> None:
        if not self.app.fundamentals_ticker:
            return
        table = self.query_one("#fundamentals-table", NavigableDataTable)
        table.loading = True
        self.app.fetch_fundamentals(self.app.fundamentals_ticker)
        self.app.fetch_research(self.app.fundamentals_ticker)

    def update_statements(self, statements: dict[str, Any]) -> None:
        """Receives fetched statements and renders ratios + statements."""
        try:
            self._render_statements(statements)
            ratios = fundamentals_provider.compute_ratios(statements)
            ratio_table = self.query_one("#fundamentals-ratio-table", NavigableDataTable)
            ratio_table.clear()
            for key, label, is_percent in _RATIOS_BY_KEY:
                value = ratios.get(key)
                if value is None:
                    ratio_table.add_row(Text(label), Text("—", style="dim"))
                elif is_percent:
                    sign = "+" if value > 0 else ""
                    ratio_table.add_row(Text(label), Text(f"{sign}{value:.1%}"))
                else:
                    ratio_table.add_row(Text(label), Text(f"{value:,.2f}"))
        except Exception as e:  # noqa: BLE001 - render must never crash the app
            logger.error(f"Failed to render fundamentals: {e}")

    def _render_statements(self, statements: dict[str, Any]) -> None:
        try:
            table = self.query_one("#fundamentals-table", NavigableDataTable)
            table.loading = False
            table.clear(columns=True)

            period = "quarterly" if self._is_quarterly() else "annual"
            key_prefix = "income_q" if period == "quarterly" else "income"
            payload = statements.get(key_prefix)
            if not payload:
                key_prefix = "income"
                payload = statements.get("income")
            if not payload:
                table.add_row(
                    "[dim]No statement data for this ticker."
                    " Non-US listings may be limited.[/dim]"
                )
                return
            columns = payload.get("columns", [])[:5]
            table.add_columns("Line Item", *[str(c) for c in columns])
            unit = " (Q)" if period == "quarterly" else ""
            for row in format_statement_rows(
                statements.get(key_prefix), _INCOME_ROWS
            ):
                table.add_row(*row)
            for row in format_statement_rows(
                statements.get("balance_q" if period == "quarterly" else "balance"),
                _BALANCE_ROWS,
            ):
                table.add_row(*row)
            for row in format_statement_rows(
                statements.get("cash_q" if period == "quarterly" else "cash"),
                _CASH_ROWS,
            ):
                table.add_row(*row)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to render statement table: {e}")

    def _is_quarterly(self) -> bool:
        try:
            select = self.query_one("#fundamentals-period-select", Select)
            return select.value == "quarterly"
        except Exception:  # noqa: BLE001
            return False

    def update_research(self, research: dict[str, Any]) -> None:
        """Receives the research payload and renders the panel table."""
        try:
            table = self.query_one("#research-table", NavigableDataTable)
            table.loading = False
            table.clear()
            rows = format_research_rows(research)
            if not rows:
                table.add_row(
                    Text("No analyst/ownership data for this ticker.", style="dim")
                )
                return
            for block, detail in rows:
                table.add_row(Text(block, style="bold"), Text(detail))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to render research panel: {e}")


def format_research_rows(research: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Flattens the research payload into (block, detail) rows. Each block
    tolerates missing data: absent keys are skipped entirely.
    """
    rows: list[tuple[str, str]] = []
    if not research:
        return rows

    targets = research.get("price_targets")
    if targets:
        parts = []
        for label, key in (("Mean", "mean"), ("High", "high"), ("Low", "low")):
            if targets.get(key) is not None:
                parts.append(f"{label} {targets[key]:,.2f}")
        if parts:
            rows.append(("Price Targets", " | ".join(parts)))

    rec = research.get("recommendations")
    if rec:
        total = sum(
            rec.get(k) or 0
            for k in ("strong_buy", "buy", "hold", "sell", "strong_sell")
        )
        rows.append(
            (
                "Recommendations",
                f"{rec.get('strong_buy', 0)} strong buy / {rec.get('buy', 0)} buy / "
                f"{rec.get('hold', 0)} hold / {rec.get('sell', 0)} sell / "
                f"{rec.get('strong_sell', 0)} strong sell ({total} analysts)",
            )
        )

    eps_est = research.get("eps_estimate")
    if eps_est:
        first = eps_est[0]
        avg = first.get("avg")
        growth = first.get("growth")
        if avg is not None:
            detail = f"EPS est (0q): {avg:.2f}"
            if growth is not None:
                detail += f", growth {growth:.1%}"
            analysts = first.get("numberOfAnalysts")
            if analysts:
                detail += f", {int(analysts)} analysts"
            rows.append(("EPS Estimates", detail))

    rev_est = research.get("revenue_estimate")
    if rev_est:
        first = rev_est[0]
        avg = first.get("avg")
        if avg is not None:
            detail = f"Revenue est (0q): {avg:,.0f}"
            growth = first.get("growth")
            if growth is not None:
                detail += f", growth {growth:.1%}"
            rows.append(("Revenue Estimates", detail))

    history = research.get("earnings_history")
    if history:
        surprises = [
            h.get("surprisePercent")
            for h in history
            if h.get("surprisePercent") is not None
        ]
        if surprises:
            rows.append(
                (
                    "Earnings Surprises",
                    f"last {len(surprises)} quarters: "
                    + ", ".join(f"{s:+.1%}" for s in surprises),
                )
            )

    insiders = research.get("insider_transactions")
    if insiders:
        latest = insiders[0]
        detail = f"{latest.get('Insider', '?')}: {latest.get('Text', '')}"
        rows.append(("Insider Transactions", detail))
        rows.append(
            (
                "  recent",
                " | ".join(
                    f"{t.get('Insider', '?')} {t.get('Start Date', '')}"
                    for t in insiders[1:4]
                )
                or "—",
            )
        )

    institutional = research.get("institutional_holders")
    if institutional:
        top = institutional[:3]
        rows.append(
            (
                "Top Institutional",
                " | ".join(
                    f"{h.get('Holder', '?')} {h.get('pctHeld', 0):.1%}"
                    for h in top
                ),
            )
        )

    major = research.get("major_holders")
    if major:
        parts = []
        if major.get("insidersPercentHeld") is not None:
            parts.append(f"insiders {major['insidersPercentHeld']:.1%}")
        if major.get("institutionsPercentHeld") is not None:
            parts.append(f"institutions {major['institutionsPercentHeld']:.1%}")
        if major.get("institutionsCount") is not None:
            parts.append(f"{int(major['institutionsCount'])} institutions")
        if parts:
            rows.append(("Ownership", " | ".join(parts)))
    return rows
