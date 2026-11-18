"""SEC EDGAR fundamentals: as-filed statements from XBRL companyfacts.

One request per company (``companyfacts``) returns every XBRL fact the
company has filed. This module turns those facts into the same payload
shape as the yfinance statements (``{"income": {"columns", "rows"}, ...}``)
and uses yfinance's row names, so ratios, quality scores and the view work
unchanged on either source.

How facts become statement cells:

- Each line item lists XBRL concepts in priority order. Companies change
  concepts over time (Apple reported ``Revenues`` until 2018 and
  ``RevenueFromContractWithCustomerExcludingAssessedTax`` after), so the
  first concept with a value is chosen per period, not per company.
- The same period is reported again in later filings. The latest filing
  wins (it carries restatements); cells whose value changed between filings
  are flagged as restated.
- Duration facts of 350-380 days are fiscal years; 80-100 days are quarters.
  10-Qs report cash flows year-to-date and 10-Ks report no Q4 at all, so a
  missing quarter is derived from cumulative periods sharing a start date
  (9M - 6M, FY - 9M). Per-share and average-share items are not additive
  and are never derived.
- Only 10-K/10-Q/20-F/40-F family forms are used, in the currency the
  company reports most facts in.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any

from stockstui.data_providers import edgar_provider
from stockstui.data_providers.base import record_outcome

logger = logging.getLogger(__name__)

COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

_FORMS = frozenset(
    {
        "10-K", "10-K/A", "10-KT", "10-KT/A", "10-Q", "10-Q/A",
        "20-F", "20-F/A", "40-F", "40-F/A",
    }
)
ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)

# Columns kept in the payload. Quarterly keeps enough for ten years of
# trailing-twelve-month valuation history; the view shows fewer.
MAX_ANNUAL_COLUMNS = 12
MAX_QUARTERLY_COLUMNS = 44

# Line items: (statement, row name, kind, concepts in priority order).
# kind: "flow" = additive duration, "nonadditive" = per-share or average
# shares (duration, never derived), "instant" = balance sheet.
# sign=-1 stores an outflow as a negative number, matching yfinance.
_G = "us-gaap:"
_I = "ifrs-full:"


@dataclass(frozen=True)
class LineItem:
    statement: str
    name: str
    kind: str
    concepts: tuple[str, ...]
    sign: int = 1


LINE_ITEMS: tuple[LineItem, ...] = (
    # Income statement
    LineItem("income", "Total Revenue", "flow", (
        _G + "Revenues",
        _G + "RevenueFromContractWithCustomerExcludingAssessedTax",
        _G + "RevenueFromContractWithCustomerIncludingAssessedTax",
        _G + "SalesRevenueNet",
        _G + "SalesRevenueGoodsNet",
        _G + "RevenuesNetOfInterestExpense",
        _I + "Revenue",
        _I + "RevenueFromContractsWithCustomers",
    )),
    LineItem("income", "Cost Of Revenue", "flow", (
        _G + "CostOfRevenue",
        _G + "CostOfGoodsAndServicesSold",
        _G + "CostOfGoodsSold",
        _I + "CostOfSales",
    )),
    LineItem("income", "Gross Profit", "flow", (_G + "GrossProfit", _I + "GrossProfit")),
    LineItem("income", "Research And Development", "flow", (
        _G + "ResearchAndDevelopmentExpense",
        _G + "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        _I + "ResearchAndDevelopmentExpense",
    )),
    LineItem("income", "Selling General And Administration", "flow", (
        _G + "SellingGeneralAndAdministrativeExpense",
        _I + "SellingGeneralAndAdministrativeExpense",
    )),
    LineItem("income", "Operating Income", "flow", (
        _G + "OperatingIncomeLoss",
        _I + "ProfitLossFromOperatingActivities",
    )),
    LineItem("income", "Interest Expense", "flow", (
        _G + "InterestExpense",
        _G + "InterestExpenseNonoperating",
        _G + "InterestExpenseDebt",
    )),
    LineItem("income", "Pretax Income", "flow", (
        _G + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        _G + "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        _I + "ProfitLossBeforeTax",
    )),
    LineItem("income", "Tax Provision", "flow", (
        _G + "IncomeTaxExpenseBenefit",
        _I + "IncomeTaxExpenseContinuingOperations",
    )),
    LineItem("income", "Net Income", "flow", (
        _G + "NetIncomeLoss",
        _G + "NetIncomeLossAvailableToCommonStockholdersBasic",
        _G + "ProfitLoss",
        _I + "ProfitLossAttributableToOwnersOfParent",
        _I + "ProfitLoss",
    )),
    LineItem("income", "Reconciled Depreciation", "flow", (
        _G + "DepreciationDepletionAndAmortization",
        _G + "DepreciationAmortizationAndAccretionNet",
        _G + "DepreciationAndAmortization",
        _G + "Depreciation",
        _I + "DepreciationAndAmortisationExpense",
    )),
    LineItem("income", "Diluted EPS", "nonadditive", (
        _G + "EarningsPerShareDiluted",
        _G + "EarningsPerShareBasicAndDiluted",
        _I + "DilutedEarningsLossPerShare",
    )),
    LineItem("income", "Diluted Average Shares", "nonadditive", (
        _G + "WeightedAverageNumberOfDilutedSharesOutstanding",
        _I + "AdjustedWeightedAverageShares",
    )),
    # Balance sheet
    LineItem("balance", "Total Assets", "instant", (_G + "Assets", _I + "Assets")),
    LineItem("balance", "Current Assets", "instant", (_G + "AssetsCurrent", _I + "CurrentAssets")),
    LineItem("balance", "Cash And Cash Equivalents", "instant", (
        _G + "CashAndCashEquivalentsAtCarryingValue",
        _G + "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        _G + "Cash",
        _I + "CashAndCashEquivalents",
    )),
    LineItem("balance", "Other Short Term Investments", "instant", (
        _G + "ShortTermInvestments",
        _G + "MarketableSecuritiesCurrent",
        _G + "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        _I + "ShorttermInvestments",
    )),
    LineItem("balance", "Accounts Receivable", "instant", (
        _G + "AccountsReceivableNetCurrent",
        _G + "ReceivablesNetCurrent",
        _I + "CurrentTradeReceivables",
        _I + "TradeAndOtherCurrentReceivables",
    )),
    LineItem("balance", "Inventory", "instant", (_G + "InventoryNet", _I + "Inventories")),
    LineItem("balance", "Net PPE", "instant", (
        _G + "PropertyPlantAndEquipmentNet",
        _I + "PropertyPlantAndEquipment",
    )),
    LineItem("balance", "Current Liabilities", "instant", (
        _G + "LiabilitiesCurrent",
        _I + "CurrentLiabilities",
    )),
    LineItem("balance", "Accounts Payable", "instant", (
        _G + "AccountsPayableCurrent",
        _G + "AccountsPayableAndAccruedLiabilitiesCurrent",
        _I + "TradeAndOtherCurrentPayables",
    )),
    LineItem("balance", "Long Term Debt", "instant", (
        _G + "LongTermDebtNoncurrent",
        _G + "LongTermDebtAndCapitalLeaseObligations",
        _I + "NoncurrentPortionOfNoncurrentBorrowings",
        _I + "LongtermBorrowings",
    )),
    LineItem("balance", "Total Liabilities Net Minority Interest", "instant", (
        _G + "Liabilities",
        _I + "Liabilities",
    )),
    LineItem("balance", "Stockholders Equity", "instant", (
        _G + "StockholdersEquity",
        _G + "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        _I + "EquityAttributableToOwnersOfParent",
        _I + "Equity",
    )),
    LineItem("balance", "Retained Earnings", "instant", (
        _G + "RetainedEarningsAccumulatedDeficit",
        _I + "RetainedEarnings",
    )),
    LineItem("balance", "Ordinary Shares Number", "instant", (
        _G + "CommonStockSharesOutstanding",
    )),
    # Cash flow statement
    LineItem("cash", "Operating Cash Flow", "flow", (
        _G + "NetCashProvidedByUsedInOperatingActivities",
        _G + "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        _I + "CashFlowsFromUsedInOperatingActivities",
    )),
    LineItem("cash", "Capital Expenditure", "flow", (
        _G + "PaymentsToAcquirePropertyPlantAndEquipment",
        _G + "PaymentsToAcquireProductiveAssets",
        _I + "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        _I + "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
    ), sign=-1),
    LineItem("cash", "Stock Based Compensation", "flow", (
        _G + "ShareBasedCompensation",
        _G + "AllocatedShareBasedCompensationExpense",
    )),
    LineItem("cash", "Depreciation And Amortization", "flow", (
        _G + "DepreciationDepletionAndAmortization",
        _G + "DepreciationAmortizationAndAccretionNet",
        _G + "DepreciationAndAmortization",
        _I + "AdjustmentsForDepreciationAndAmortisationExpense",
    )),
    LineItem("cash", "Repurchase Of Capital Stock", "flow", (
        _G + "PaymentsForRepurchaseOfCommonStock",
        _I + "PaymentsToAcquireOrRedeemEntitysShares",
    ), sign=-1),
    LineItem("cash", "Cash Dividends Paid", "flow", (
        _G + "PaymentsOfDividends",
        _G + "PaymentsOfDividendsCommonStock",
        _I + "DividendsPaidClassifiedAsFinancingActivities",
    ), sign=-1),
)

# Current Debt: the first reported total, else the sum of the parts.
_CURRENT_DEBT_TOTALS = (
    _G + "DebtCurrent",
    _I + "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
)
_CURRENT_DEBT_PARTS = (
    _G + "LongTermDebtCurrent",
    _G + "CommercialPaper",
    _G + "ShortTermBorrowings",
)
_LIABILITIES_AND_EQUITY = _G + "LiabilitiesAndStockholdersEquity"
_EQUITY_INCL_NCI = _G + "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
_COVER_SHARES = "dei:EntityCommonStockSharesOutstanding"

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass
class Cell:
    """One chosen value with where it came from."""

    val: float
    form: str
    filed: str
    accn: str
    restated: bool = False
    derived: bool = False

    def provenance(self) -> list:
        flags = ("R" if self.restated else "") + ("D" if self.derived else "")
        return [self.form, self.filed, self.accn, flags]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def fetch_company_facts(cik: int) -> dict[str, Any]:
    """Downloads the companyfacts document. Raises EdgarError on failure."""
    session = edgar_provider._get_session()
    try:
        response = session.get(COMPANY_FACTS_URL.format(cik=cik), timeout=60)
        edgar_provider._raise_for_status(response)
        return response.json()
    except edgar_provider.EdgarError:
        raise
    except Exception as e:  # network/JSON errors become EdgarError
        raise edgar_provider.EdgarError(f"Could not fetch SEC company facts: {e}") from e


def reporting_currency(facts: dict[str, Any]) -> str | None:
    """The ISO currency the company reports most monetary facts in."""
    counts: Counter[str] = Counter()
    for taxonomy in ("us-gaap", "ifrs-full"):
        for concept in facts.get("facts", {}).get(taxonomy, {}).values():
            for unit, rows in concept.get("units", {}).items():
                if _CURRENCY_RE.match(unit):
                    counts[unit] += len(rows)
    return counts.most_common(1)[0][0] if counts else None


def _unit_rows(facts: dict[str, Any], concept: str, unit: str) -> list[dict]:
    taxonomy, name = concept.split(":", 1)
    node = facts.get("facts", {}).get(taxonomy, {}).get(name)
    if not node:
        return []
    return node.get("units", {}).get(unit, [])


def _choose_per_period(rows: list[dict], instant: bool) -> dict[tuple, Cell]:
    """
    Picks one value per period: the latest filing wins; a value that
    changed between filings is flagged as restated.
    """
    by_period: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("form") not in _FORMS or not isinstance(row.get("val"), (int, float)):
            continue
        end = _parse_date(row.get("end"))
        if end is None:
            continue
        if instant:
            key: tuple = (end,)
        else:
            start = _parse_date(row.get("start"))
            if start is None:
                continue
            key = (start, end)
        by_period.setdefault(key, []).append(row)

    chosen: dict[tuple, Cell] = {}
    for key, candidates in by_period.items():
        candidates.sort(key=lambda r: (r.get("filed") or "", r.get("accn") or ""))
        latest = candidates[-1]
        distinct = {round(float(c["val"]), 6) for c in candidates}
        chosen[key] = Cell(
            val=float(latest["val"]),
            form=latest.get("form", ""),
            filed=latest.get("filed", ""),
            accn=latest.get("accn", ""),
            restated=len(distinct) > 1,
        )
    return chosen


def _days(start: date, end: date) -> int:
    # XBRL durations are inclusive of both ends.
    return (end - start).days + 1


def split_durations(
    periods: dict[tuple, Cell], derive_quarters: bool
) -> tuple[dict[date, Cell], dict[date, Cell]]:
    """
    Splits duration facts into fiscal years and quarters keyed by end date.

    With derive_quarters, a quarter missing as a direct 3-month fact is the
    difference of two cumulative periods sharing a start date whose lengths
    differ by one quarter (6M - 3M, 9M - 6M, FY - 9M).
    """
    annual: dict[date, Cell] = {}
    quarterly: dict[date, Cell] = {}
    by_start: dict[date, list[tuple[date, Cell]]] = {}
    for (start, end), cell in periods.items():
        length = _days(start, end)
        if ANNUAL_DAYS[0] <= length <= ANNUAL_DAYS[1]:
            annual[end] = cell
        if QUARTER_DAYS[0] <= length <= QUARTER_DAYS[1]:
            quarterly[end] = cell
        by_start.setdefault(start, []).append((end, cell))

    if derive_quarters:
        for start, entries in by_start.items():
            entries.sort(key=lambda item: item[0])
            for (prev_end, prev_cell), (end, cell) in pairwise(entries):
                if end in quarterly:
                    continue
                gap = (end - prev_end).days
                if not (QUARTER_DAYS[0] <= gap <= QUARTER_DAYS[1]):
                    continue
                if _days(start, prev_end) < QUARTER_DAYS[0]:
                    continue
                quarterly[end] = Cell(
                    val=cell.val - prev_cell.val,
                    form=cell.form,
                    filed=cell.filed,
                    accn=cell.accn,
                    restated=cell.restated or prev_cell.restated,
                    derived=True,
                )
    return annual, quarterly


def _merge_concepts(per_concept: list[dict[date, Cell]]) -> dict[date, Cell]:
    """Per period, the first concept (in priority order) with a value wins."""
    merged: dict[date, Cell] = {}
    for series in per_concept:
        for end, cell in series.items():
            merged.setdefault(end, cell)
    return merged


def _item_series(
    facts: dict[str, Any], item: LineItem, currency: str
) -> tuple[dict[date, Cell], dict[date, Cell]]:
    """Annual and quarterly series for one line item, sign applied."""
    if item.name == "Diluted Average Shares":
        unit = "shares"
    elif item.name == "Diluted EPS":
        unit = f"{currency}/shares"
    elif item.name == "Ordinary Shares Number":
        unit = "shares"
    else:
        unit = currency

    annual_parts: list[dict[date, Cell]] = []
    quarterly_parts: list[dict[date, Cell]] = []
    for concept in item.concepts:
        rows = _unit_rows(facts, concept, unit)
        if not rows:
            continue
        if item.kind == "instant":
            chosen = _choose_per_period(rows, instant=True)
            series = {key[0]: cell for key, cell in chosen.items()}
            annual_parts.append(series)
            quarterly_parts.append(series)
        else:
            chosen = _choose_per_period(rows, instant=False)
            annual, quarterly = split_durations(
                chosen, derive_quarters=item.kind == "flow"
            )
            annual_parts.append(annual)
            quarterly_parts.append(quarterly)

    annual = _merge_concepts(annual_parts)
    quarterly = _merge_concepts(quarterly_parts)
    if item.sign == -1:
        for series in (annual, quarterly):
            for cell in series.values():
                cell.val = -abs(cell.val)
    return annual, quarterly


def _instant_series(facts: dict[str, Any], concept: str, unit: str) -> dict[date, Cell]:
    chosen = _choose_per_period(_unit_rows(facts, concept, unit), instant=True)
    return {key[0]: cell for key, cell in chosen.items()}


def _sum_cells(cells: list[Cell | None]) -> Cell | None:
    present = [c for c in cells if c is not None]
    if not present:
        return None
    base = max(present, key=lambda c: c.filed)
    return Cell(
        val=sum(c.val for c in present),
        form=base.form,
        filed=base.filed,
        accn=base.accn,
        restated=any(c.restated for c in present),
        # A sum of reported values is not an estimate; only inherit the
        # flag from inputs that were themselves derived periods.
        derived=any(c.derived for c in present),
    )


def _diff_cells(a: Cell | None, b: Cell | None) -> Cell | None:
    if a is None or b is None:
        return None
    return Cell(
        val=a.val - b.val, form=a.form, filed=a.filed, accn=a.accn,
        restated=a.restated or b.restated, derived=a.derived or b.derived,
    )


def _derive(series: dict[str, dict[date, Cell]], ends: list[date], facts, currency) -> None:
    """Adds derived rows (gross profit, EBITDA, debt, FCF, ...) in place."""

    def get(name: str, end: date) -> Cell | None:
        return series.get(name, {}).get(end)

    current_debt_total = _merge_concepts(
        [_instant_series(facts, c, currency) for c in _CURRENT_DEBT_TOTALS]
    )
    debt_parts = [_instant_series(facts, c, currency) for c in _CURRENT_DEBT_PARTS]
    liabilities_and_equity = _instant_series(facts, _LIABILITIES_AND_EQUITY, currency)
    equity_incl_nci = _instant_series(facts, _EQUITY_INCL_NCI, currency)

    for end in ends:
        if get("Gross Profit", end) is None:
            gp = _diff_cells(get("Total Revenue", end), get("Cost Of Revenue", end))
            if gp is not None:
                series.setdefault("Gross Profit", {})[end] = gp
        op, da = get("Operating Income", end), get("Reconciled Depreciation", end)
        if da is None:
            da = get("Depreciation And Amortization", end)
        if op is not None and da is not None:
            series.setdefault("EBITDA", {})[end] = _sum_cells([op, da])

        current_debt = current_debt_total.get(end) or _sum_cells(
            [part.get(end) for part in debt_parts]
        )
        if current_debt is not None:
            series.setdefault("Current Debt", {})[end] = current_debt
        total_debt = _sum_cells([get("Long Term Debt", end), current_debt])
        if total_debt is not None:
            series.setdefault("Total Debt", {})[end] = total_debt

        cash = get("Cash And Cash Equivalents", end)
        if cash is not None:
            series.setdefault("Cash Cash Equivalents And Short Term Investments", {})[
                end
            ] = _sum_cells([cash, get("Other Short Term Investments", end)])

        if get("Total Liabilities Net Minority Interest", end) is None:
            total = _diff_cells(
                liabilities_and_equity.get(end),
                equity_incl_nci.get(end) or get("Stockholders Equity", end),
            )
            if total is not None:
                series.setdefault("Total Liabilities Net Minority Interest", {})[end] = total

        equity = get("Stockholders Equity", end)
        if total_debt is not None and equity is not None:
            series.setdefault("Invested Capital", {})[end] = _sum_cells([total_debt, equity])

        ocf, capex = get("Operating Cash Flow", end), get("Capital Expenditure", end)
        if ocf is not None and capex is not None:
            series.setdefault("Free Cash Flow", {})[end] = _sum_cells([ocf, capex])


_STATEMENT_OF: dict[str, str] = {item.name: item.statement for item in LINE_ITEMS}
_STATEMENT_OF.update(
    {
        "EBITDA": "income",
        "Current Debt": "balance",
        "Total Debt": "balance",
        "Cash Cash Equivalents And Short Term Investments": "balance",
        "Invested Capital": "balance",
        "Free Cash Flow": "cash",
    }
)
# Rows that define which period ends become columns.
_SPINE = ("Total Revenue", "Net Income", "Operating Cash Flow", "Total Assets")


def _build_payloads(
    series: dict[str, dict[date, Cell]], max_columns: int, suffix: str
) -> dict[str, Any]:
    ends: set[date] = set()
    for name in _SPINE[:3]:
        ends.update(series.get(name, {}))
    columns = sorted(ends, reverse=True)[:max_columns]
    payloads: dict[str, Any] = {}
    for statement in ("income", "balance", "cash"):
        rows: dict[str, list] = {}
        provenance: dict[str, list] = {}
        for name, by_end in series.items():
            if _STATEMENT_OF.get(name) != statement:
                continue
            cells = [by_end.get(end) for end in columns]
            if all(c is None for c in cells):
                continue
            rows[name] = [c.val if c else None for c in cells]
            provenance[name] = [c.provenance() if c else None for c in cells]
        if rows:
            payloads[statement + suffix] = {
                "columns": [end.isoformat() for end in columns],
                "rows": rows,
                "provenance": provenance,
            }
    return payloads


def cover_shares(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Shares outstanding from each filing's cover page, oldest first.

    Each value is as originally filed, at the cover date shortly after the
    period end, so it is in that date's share units (pre any later split).
    """
    out = []
    for row in _unit_rows(facts, _COVER_SHARES, "shares"):
        if row.get("form") not in _FORMS or not isinstance(row.get("val"), (int, float)):
            continue
        out.append({"end": row.get("end"), "val": float(row["val"]), "filed": row.get("filed")})
    out.sort(key=lambda r: (r["end"] or "", r["filed"] or ""))
    return out


def statements_from_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """
    Builds yfinance-shaped statements (annual and quarterly) from companyfacts.

    Returns {} when the facts hold no usable income statement.
    """
    currency = reporting_currency(facts)
    if currency is None:
        return {}

    annual_series: dict[str, dict[date, Cell]] = {}
    quarterly_series: dict[str, dict[date, Cell]] = {}
    for item in LINE_ITEMS:
        annual, quarterly = _item_series(facts, item, currency)
        if annual:
            annual_series[item.name] = annual
        if quarterly:
            quarterly_series[item.name] = quarterly

    if not (annual_series.get("Total Revenue") or annual_series.get("Net Income")):
        return {}

    for series in (annual_series, quarterly_series):
        all_ends = sorted({end for by_end in series.values() for end in by_end})
        _derive(series, all_ends, facts, currency)

    statements: dict[str, Any] = {}
    statements.update(_build_payloads(annual_series, MAX_ANNUAL_COLUMNS, ""))
    statements.update(_build_payloads(quarterly_series, MAX_QUARTERLY_COLUMNS, "_q"))
    statements["meta"] = {
        "source": "edgar",
        "currency": currency,
        "entity": facts.get("entityName"),
        "cik": facts.get("cik"),
        "cover_shares": cover_shares(facts),
    }
    return statements


def fetch_statements(ticker: str) -> dict[str, Any]:
    """
    EDGAR statements for a ticker; {} for tickers EDGAR does not cover.
    Raises EdgarError when EDGAR cannot be reached or no contact is set.
    """
    cik = edgar_provider.get_cik_for_ticker(ticker)
    if cik is None:
        return {}
    statements = statements_from_facts(fetch_company_facts(cik))
    if statements:
        record_outcome(
            "fundamentals", ok=True, provider="edgar", detail=f"{ticker.upper()}: companyfacts"
        )
    return statements
