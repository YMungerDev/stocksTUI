"""Fundamentals view: statements, valuation history and earnings quality (#6)."""

import logging
import webbrowser
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult, on
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Select, Static

from stockstui.data_providers import fundamentals_provider
from stockstui.presentation import quality as quality_metrics
from stockstui.presentation import valuation as valuation_metrics
from stockstui.ui.suggesters import TickerSuggester
from stockstui.ui.widgets.navigable_data_table import NavigableDataTable

logger = logging.getLogger(__name__)

# Rows surfaced per statement, in display order, with yfinance aliases
# (EDGAR statements use the same names).
_INCOME_ROWS = [
    ("Total Revenue", ("Total Revenue", "Revenues")),
    ("Cost Of Revenue", ("Cost Of Revenue", "Reconciled Cost Of Revenue")),
    ("Gross Profit", ("Gross Profit",)),
    ("R&D", ("Research And Development",)),
    ("SG&A", ("Selling General And Administration",)),
    ("Operating Income", ("Operating Income",)),
    ("Net Income", ("Net Income", "Net Income Common Stockholders")),
    ("Diluted EPS", ("Diluted EPS", "Basic EPS")),
    ("Diluted Shares", ("Diluted Average Shares",)),
    ("EBITDA", ("EBITDA", "Normalized EBITDA")),
]
_BALANCE_ROWS = [
    ("Total Assets", ("Total Assets",)),
    ("Total Liabilities", ("Total Liabilities Net Minority Interest",)),
    ("Total Debt", ("Total Debt",)),
    ("Cash & ST Investments", ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")),
    ("Stockholders Equity", ("Stockholders Equity", "Total Equity Gross Minority Interest")),
    ("Invested Capital", ("Invested Capital",)),
]
_CASH_ROWS = [
    ("Operating Cash Flow", ("Operating Cash Flow",)),
    ("Capital Expenditure", ("Capital Expenditure",)),
    ("Free Cash Flow", ("Free Cash Flow",)),
    ("Stock Based Comp", ("Stock Based Compensation",)),
    ("Buybacks", ("Repurchase Of Capital Stock",)),
    ("Dividends Paid", ("Cash Dividends Paid",)),
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

# Columns shown: yfinance has ~4 years; EDGAR has more history than fits.
_COLUMNS_BY_SOURCE = {"edgar": {"annual": 10, "quarterly": 8}}
_DEFAULT_COLUMNS = 5

FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"


def _format_number(label: str, value: float) -> str:
    """Per-share values as-is; everything else in millions."""
    if "EPS" in label:
        return f"{value:,.2f}"
    return f"{value / 1e6:,.0f}"


def format_statement_rows(
    payload: dict[str, Any] | None,
    row_specs: list[tuple[str, tuple[str, ...]]],
    max_columns: int = 5,
    leading: dict[str, float | None] | None = None,
) -> list[tuple]:
    """
    Builds DataTable rows for one statement; blank when payload missing.

    `leading` adds a first value column (TTM) keyed by row alias. EDGAR
    provenance marks cells: a trailing '*' is a value restated by a later
    filing, italics a value derived (e.g. Q4 = FY - 9M).
    """
    width = max_columns + (1 if leading is not None else 0)
    if not payload:
        return [(Text(name, style="dim"), *(Text("—") for _ in range(width))) for name, _ in row_specs]
    columns = payload.get("columns", [])[:max_columns]
    provenance = payload.get("provenance", {})
    rows = []
    for label, aliases in row_specs:
        values = None
        marks = None
        for alias in aliases:
            data = payload.get("rows", {}).get(alias)
            if data is not None:
                values = data
                marks = provenance.get(alias)
                break
        cells = [Text(label, style="bold")]
        if leading is not None:
            lead = None
            for alias in aliases:
                if leading.get(alias) is not None:
                    lead = leading[alias]
                    break
            cells.append(
                Text(_format_number(label, lead), style="bold")
                if lead is not None
                else Text("—", style="dim")
            )
        for pos in range(len(columns)):
            if values is None or pos >= len(values) or values[pos] is None:
                cells.append(Text("—", style="dim"))
                continue
            value = values[pos]
            if not isinstance(value, (int, float)):
                cells.append(Text(str(value)))
                continue
            flags = ""
            if marks and pos < len(marks) and marks[pos]:
                flags = marks[pos][3] or ""
            text = Text(_format_number(label, value), style="italic" if "D" in flags else "")
            if "R" in flags:
                text.append("*", style="yellow")
            cells.append(text)
        rows.append(tuple(cells))
    return rows


def ttm_values(statements: dict[str, Any]) -> dict[str, float | None] | None:
    """
    TTM flows (sum of the last four quarters) and latest-quarter balances,
    keyed by row name. None when there is no quarterly data.
    """
    if not statements.get("income_q"):
        return None
    out: dict[str, float | None] = {}
    for key in ("income_q", "cash_q"):
        payload = statements.get(key) or {}
        for name in payload.get("rows", {}):
            if name in ("Diluted EPS", "Diluted Average Shares"):
                continue
            series = valuation_metrics.ttm_series(payload, (name,))
            out[name] = series[max(series)] if series else None
    balance = statements.get("balance_q") or {}
    for name, values in balance.get("rows", {}).items():
        out[name] = values[0] if values else None
    net_income = out.get("Net Income")
    shares_row = (statements.get("income_q") or {}).get("rows", {}).get("Diluted Average Shares")
    if net_income is not None and shares_row and shares_row[0]:
        out["Diluted EPS"] = net_income / shares_row[0]
    return out


def _fmt_multiple(value: float | None, is_percent: bool) -> Text:
    if value is None:
        return Text("—", style="dim")
    return Text(f"{value:.1%}" if is_percent else f"{value:,.1f}x")


def format_valuation_rows(valuation: dict[str, Any]) -> list[tuple[Text, ...]]:
    """Rows: metric, current, 5y median, 10y median, 10y min, 10y max, 10y percentile."""
    summary = (valuation or {}).get("summary") or {}
    rows = []
    for key, label, is_percent in valuation_metrics.METRICS:
        stats = summary.get(key) or {}
        pct = stats.get("percentile_10y")
        if pct is None:
            pct_text = Text("—", style="dim")
        else:
            # For yields, a high percentile is cheap; for multiples, expensive.
            expensive = pct <= 0.2 if is_percent else pct >= 0.8
            cheap = pct >= 0.8 if is_percent else pct <= 0.2
            style = "red" if expensive else ("green" if cheap else "")
            pct_text = Text(f"{pct:.0%} ({stats.get('points', 0)} pts)", style=style)
        rows.append(
            (
                Text(label, style="bold"),
                _fmt_multiple(stats.get("current"), is_percent),
                _fmt_multiple(stats.get("median_5y"), is_percent),
                _fmt_multiple(stats.get("median_10y"), is_percent),
                _fmt_multiple(stats.get("min_10y"), is_percent),
                _fmt_multiple(stats.get("max_10y"), is_percent),
                pct_text,
            )
        )
    return rows


def _quality_cell(key: str, report: dict[str, Any]) -> Text:
    value = report.get(key)
    if value is None:
        return Text("—", style="dim")
    if key == "piotroski":
        score, evaluated = value
        style = "green" if score >= 7 else ("red" if score <= 3 else "")
        return Text(f"{score}/{evaluated}", style=style)
    if key == "beneish":
        m, model = value
        flagged = m > quality_metrics.BENEISH_THRESHOLDS[model]
        return Text(f"{m:.2f} ({model})", style="red" if flagged else "green")
    if key == "altman_z":
        style = "red" if value < 1.81 else ("green" if value > 2.99 else "yellow")
        return Text(f"{value:.2f}", style=style)
    if key in ("sloan_accruals",):
        return Text(f"{value:+.1%}", style="red" if value > 0.1 else "")
    if key == "cash_conversion":
        return Text(f"{value:.2f}x", style="red" if value < 0.8 else "")
    if key == "capex_to_da":
        return Text(f"{value:.2f}x")
    return Text(f"{value:,.0f} days")


_QUALITY_ROWS = [
    ("piotroski", "Piotroski F-score", "7+ strong, 3- weak"),
    ("altman_z", "Altman Z", "<1.81 distress, >2.99 safe"),
    ("beneish", "Beneish M-score", "above threshold = manipulation risk"),
    ("sloan_accruals", "Sloan accruals", "(NI - CFO) / avg assets; high = low quality"),
    ("cash_conversion", "Cash conversion", "CFO / net income"),
    ("dso", "DSO", "receivables / revenue x 365"),
    ("dio", "DIO", "inventory / COGS x 365"),
    ("dpo", "DPO", "payables / COGS x 365"),
    ("ccc", "Cash conversion cycle", "DSO + DIO - DPO"),
    ("capex_to_da", "Capex / D&A", "<1 = under-reinvesting"),
]


def format_quality_rows(
    statements: dict[str, Any], market_cap: float | None
) -> list[tuple[Text, ...]]:
    latest = quality_metrics.quality_report(statements, market_cap, offset=0)
    prior = quality_metrics.quality_report(statements, None, offset=1)
    return [
        (
            Text(label, style="bold"),
            _quality_cell(key, latest),
            _quality_cell(key, prior),
            Text(hint, style="dim"),
        )
        for key, label, hint in _QUALITY_ROWS
    ]


def looks_like_financial(statements: dict[str, Any]) -> bool:
    """Banks and insurers report no current assets or gross profit."""
    income = (statements.get("income") or {}).get("rows", {})
    balance = (statements.get("balance") or {}).get("rows", {})
    return "Current Assets" not in balance and "Gross Profit" not in income


def source_line(statements: dict[str, Any]) -> Text:
    meta = statements.get("meta") or {}
    if meta.get("source") != "edgar":
        return Text("Source: Yahoo Finance (yfinance)  ·  values in millions", style="dim")
    text = Text("Source: SEC EDGAR XBRL", style="bold")
    entity = meta.get("entity")
    if entity:
        text.append(f" — {entity}")
    if meta.get("currency"):
        text.append(f", {meta['currency']} millions")
    text.append("  ·  * restated by a later filing, italics derived (e.g. Q4 = FY - 9M)", style="dim")
    text.append("  ·  o opens the filing behind the highlighted row", style="dim")
    return text


class FundamentalsView(Vertical):
    """Financial statements, derived ratios, valuation history and quality scores."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "open_source_filing", "Open Filing", show=True),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._statements: dict[str, Any] = {}
        self._valuation: dict[str, Any] = {}
        # (payload key, row alias) per statement-table row, for 'o'.
        self._row_sources: list[tuple[str, tuple[str, ...]] | None] = []

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
        yield Static("", id="fundamentals-source")
        yield NavigableDataTable(id="fundamentals-ratio-table", zebra_stripes=True)
        yield Static("", id="fundamentals-note", classes="table-note")
        yield NavigableDataTable(id="fundamentals-table", zebra_stripes=True)
        yield Static("Valuation vs Own History", classes="content-header")
        yield Static("", id="valuation-note", classes="table-note")
        yield NavigableDataTable(id="valuation-table", zebra_stripes=True)
        yield Static("Earnings Quality", classes="content-header")
        yield Static("", id="quality-note", classes="table-note")
        yield NavigableDataTable(id="quality-table", zebra_stripes=True)
        yield Static("Analyst & Ownership Research", classes="content-header")
        yield Static("", id="research-note", classes="table-note")
        yield NavigableDataTable(id="research-table", zebra_stripes=True)

    def on_mount(self) -> None:
        ratio_table = self.query_one("#fundamentals-ratio-table", NavigableDataTable)
        ratio_table.add_columns("Metric", "Value")
        table = self.query_one("#fundamentals-table", NavigableDataTable)
        table.add_columns("Line Item", "Latest", "Yr -1", "Yr -2", "Yr -3", "Yr -4")
        valuation_table = self.query_one("#valuation-table", NavigableDataTable)
        valuation_table.add_columns(
            "Multiple", "Current", "5y Median", "10y Median", "10y Min", "10y Max", "10y Percentile"
        )
        quality_table = self.query_one("#quality-table", NavigableDataTable)
        quality_table.add_columns("Metric", "Latest FY", "Prior FY", "Reading")
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
        if event.value and self._statements:
            # Both periods are already loaded; re-render without refetching.
            self._render_statements(self._statements)
        elif event.value:
            self._request_statements()

    def _set_note(self, note_id: str, message: str | None) -> None:
        """
        Shows a message above a table, or hides it. Messages never go into
        the table itself, where they would widen the first column (#22).
        """
        note = self.query_one(f"#{note_id}", Static)
        note.update(message or "")
        note.display = bool(message)

    def _request_statements(self) -> None:
        if not self.app.fundamentals_ticker:
            return
        table = self.query_one("#fundamentals-table", NavigableDataTable)
        table.loading = True
        self.query_one("#valuation-table", NavigableDataTable).loading = True
        self._valuation = {}
        self.app.fetch_fundamentals(self.app.fundamentals_ticker)
        self.app.fetch_research(self.app.fundamentals_ticker)

    def update_statements(self, statements: dict[str, Any]) -> None:
        """Receives fetched statements and renders ratios, statements and quality."""
        self._statements = statements or {}
        try:
            self.query_one("#fundamentals-source", Static).update(
                source_line(self._statements) if self._statements else ""
            )
            self._render_statements(self._statements)
            ratios = fundamentals_provider.compute_ratios(self._statements)
            ratio_table = self.query_one("#fundamentals-ratio-table", NavigableDataTable)
            ratio_table.clear()
            for key, label, is_percent in _RATIOS_BY_KEY:
                value = ratios.get(key)
                if value is None:
                    ratio_table.add_row(Text(label), Text("—", style="dim"))
                elif is_percent:
                    sign = "+" if value > 0 else ""
                    ratio_table.add_row(Text(label), Text(f"{sign}{value:.1%}"))
                elif key == "fcf":
                    ratio_table.add_row(Text(label), Text(f"{value / 1e6:,.0f}M"))
                else:
                    ratio_table.add_row(Text(label), Text(f"{value:,.2f}"))
            self._render_quality()
        except Exception as e:  # noqa: BLE001 - render must never crash the app
            logger.error(f"Failed to render fundamentals: {e}")

    def update_valuation(self, valuation: dict[str, Any]) -> None:
        """Receives valuation history; also re-renders quality (Altman Z needs market cap)."""
        self._valuation = valuation or {}
        try:
            table = self.query_one("#valuation-table", NavigableDataTable)
            table.loading = False
            table.clear()
            reason = self._valuation.get("unavailable")
            self._set_note(
                "valuation-note",
                f"Valuation history unavailable: {reason}" if reason else None,
            )
            if not reason:
                for row in format_valuation_rows(self._valuation):
                    table.add_row(*row)
            self._render_quality()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to render valuation: {e}")

    def _render_quality(self) -> None:
        table = self.query_one("#quality-table", NavigableDataTable)
        table.clear()
        self._set_note("quality-note", None)
        if not self._statements.get("income"):
            return
        if looks_like_financial(self._statements):
            self._set_note(
                "quality-note",
                "Quality scores are not meaningful for banks and insurers"
                " (no current assets or gross profit).",
            )
            return
        for row in format_quality_rows(self._statements, self._valuation.get("market_cap")):
            table.add_row(*row)

    def _render_statements(self, statements: dict[str, Any]) -> None:
        try:
            table = self.query_one("#fundamentals-table", NavigableDataTable)
            table.loading = False
            table.clear(columns=True)
            self._row_sources = []
            self._set_note("fundamentals-note", None)

            quarterly = self._is_quarterly()
            suffix = "_q" if quarterly and statements.get("income_q") else ""
            payload = statements.get("income" + suffix)
            if not payload:
                self._set_note(
                    "fundamentals-note",
                    "No statement data for this ticker. Non-US listings may be limited.",
                )
                return
            source = (statements.get("meta") or {}).get("source", "yfinance")
            limits = _COLUMNS_BY_SOURCE.get(source, {})
            max_columns = limits.get("quarterly" if suffix else "annual", _DEFAULT_COLUMNS)
            leading = ttm_values(statements) if source == "edgar" and not suffix else None
            columns = payload.get("columns", [])[:max_columns]
            headers = ["Line Item"] + (["TTM"] if leading is not None else [])
            table.add_columns(*headers, *[str(c) for c in columns])
            for key, specs in (
                ("income", _INCOME_ROWS),
                ("balance", _BALANCE_ROWS),
                ("cash", _CASH_ROWS),
            ):
                section = statements.get(key + suffix)
                section_columns = (section or {}).get("columns", [])[:max_columns]
                if section and section_columns != columns:
                    section = _align_columns(section, columns)
                for (label, aliases), row in zip(
                    specs, format_statement_rows(section, specs, max_columns=len(columns), leading=leading)
                ):
                    table.add_row(*row)
                    self._row_sources.append((key + suffix, aliases))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to render statement table: {e}")

    def _is_quarterly(self) -> bool:
        try:
            select = self.query_one("#fundamentals-period-select", Select)
            return select.value == "quarterly"
        except Exception:  # noqa: BLE001
            return False

    def action_open_source_filing(self) -> None:
        """Opens the SEC filing behind the highlighted row's latest value."""
        meta = self._statements.get("meta") or {}
        if meta.get("source") != "edgar":
            self.notify("Filing links need SEC EDGAR data.", severity="information")
            return
        table = self.query_one("#fundamentals-table", NavigableDataTable)
        row = table.cursor_row
        if row is None or row >= len(self._row_sources) or self._row_sources[row] is None:
            self.notify("Highlight a statement row first.", severity="information")
            return
        key, aliases = self._row_sources[row]
        payload = self._statements.get(key) or {}
        provenance = payload.get("provenance", {})
        marks = next((provenance[a] for a in aliases if a in provenance), None)
        mark = next((m for m in (marks or []) if m), None)
        if not mark or not meta.get("cik"):
            self.notify("No filing recorded for this row.", severity="warning")
            return
        url = FILING_INDEX_URL.format(cik=int(meta["cik"]), accession=mark[2].replace("-", ""))
        try:
            webbrowser.open(url)
            self.notify(f"Opening {mark[0]} filed {mark[1]}")
        except Exception as e:  # noqa: BLE001
            self.notify(f"Could not open browser: {e}", severity="error")

    def update_research(self, research: dict[str, Any]) -> None:
        """Receives the research payload and renders the panel table."""
        try:
            table = self.query_one("#research-table", NavigableDataTable)
            table.loading = False
            table.clear()
            rows = format_research_rows(research)
            self._set_note(
                "research-note",
                None if rows else "No analyst/ownership data for this ticker.",
            )
            if not rows:
                return
            for block, detail in rows:
                table.add_row(Text(block, style="bold"), Text(detail))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to render research panel: {e}")


def _align_columns(payload: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    """Re-indexes a statement payload onto the income statement's columns."""
    index = {c: i for i, c in enumerate(payload.get("columns", []))}
    rows, provenance = {}, {}
    for name, values in payload.get("rows", {}).items():
        rows[name] = [values[index[c]] if c in index and index[c] < len(values) else None for c in columns]
        marks = payload.get("provenance", {}).get(name)
        if marks:
            provenance[name] = [marks[index[c]] if c in index and index[c] < len(marks) else None for c in columns]
    return {"columns": list(columns), "rows": rows, "provenance": provenance}


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
