"""Fundamentals provider: financial statements and derived ratios (#6).

Statements come from yfinance (annual and quarterly); non-US tickers
degrade naturally to whatever yfinance provides. Statements are cached in
SQLite as JSON keyed by ticker with a fetch timestamp and refreshed at
most daily (after new filings).
"""

import json
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 86400

_STATEMENT_KEYS: dict[str, str] = {
    "income": "income_stmt",
    "income_q": "quarterly_income_stmt",
    "balance": "balance_sheet",
    "balance_q": "quarterly_balance_sheet",
    "cash": "cash_flow",
    "cash_q": "quarterly_cash_flow",
}


def fetch_statements(ticker: str, force_refresh: bool = False) -> dict[str, Any]:
    """
    Fetches all six statements for a ticker, returning:
    {statement_key: {"columns": [ISO dates], "rows": {line_item: [values]}}}

    Missing statements are absent keys; the caller tolerates that.
    """
    import yfinance as yf

    normalized = ticker.upper()
    statements: dict[str, Any] = {}
    ticker_obj = yf.Ticker(normalized)
    for key, attr in _STATEMENT_KEYS.items():
        try:
            frame = getattr(ticker_obj, attr)
        except Exception as e:  # noqa: BLE001 - one missing statement must not kill all
            logger.debug(f"{normalized}.{attr} failed: {e}")
            continue
        if frame is None or frame.empty:
            continue
        try:
            statements[key] = _frame_to_payload(frame)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to serialise {normalized}.{attr}: {e}")
    return statements


def _frame_to_payload(frame: pd.DataFrame) -> dict[str, Any]:
    """Serialises a statement DataFrame to JSON-safe columns/rows."""
    columns = [str(col.date() if hasattr(col, "date") else col) for col in frame.columns]
    rows: dict[str, list] = {}
    for line_item, values in frame.iterrows():
        row = []
        for value in values:
            if value is None or (isinstance(value, float) and value != value):
                row.append(None)
            elif isinstance(value, (int, float)):
                row.append(float(value))
            else:
                row.append(str(value))
        rows[str(line_item)] = row
    return {"columns": columns, "rows": rows}


def load_cached_statements(db_manager, ticker: str) -> dict[str, Any] | None:
    """Loads statements from the SQLite cache if fresh, else None."""
    if db_manager is None:
        return None
    raw = db_manager.load_fundamentals_cache(ticker)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload.get("statements")


def get_statements(
    ticker: str, db_manager, force_refresh: bool = False
) -> dict[str, Any]:
    """
    Statements with caching: fresh cache first, yfinance otherwise, and the
    cache is written back when fetched. Always returns a dict (possibly
    empty for tickers with no statement data at all).
    """
    normalized = ticker.upper()
    if not force_refresh:
        cached = load_cached_statements(db_manager, normalized)
        if cached:
            return cached
    statements = fetch_statements(normalized, force_refresh=force_refresh)
    if statements and db_manager is not None:
        db_manager.save_fundamentals_cache(
            normalized, json.dumps({"statements": statements})
        )
    return statements or {}


def _row(payload: dict[str, Any], *names: str) -> list | None:
    """The first matching row (values in column order) for any alias."""
    rows = payload.get("rows", {})
    for name in names:
        if name in rows:
            return rows[name]
    return None


def growth_rate(latest: float, prior: float) -> float | None:
    """Year-over-year growth; None when prior is zero/missing."""
    if prior is None or prior == 0:
        return None
    return latest / prior - 1


def _latest_value(payload: dict[str, Any] | None, *names: str) -> float | None:
    if not payload:
        return None
    row = _row(payload, *names)
    if row is None:
        return None
    for value in row:
        if value is not None and isinstance(value, (int, float)):
            return float(value)
    return None


def _growth_for(payload: dict[str, Any] | None, *names: str) -> float | None:
    """Growth between the two most recent non-None values of a row."""
    if not payload:
        return None
    row = _row(payload, *names)
    if row is None:
        return None
    values = [v for v in row if v is not None and isinstance(v, (int, float))]
    if len(values) >= 2:
        return growth_rate(float(values[0]), float(values[1]))
    return None


def compute_ratios(statements: dict[str, Any]) -> dict[str, float | None]:
    """
    Derived metrics over the annual statements (#6): growths, margins, ROIC,
    FCF, net debt / EBITDA. Values are None when inputs are missing.
    """
    out: dict[str, float | None] = {
        "revenue_growth": None,
        "net_income_growth": None,
        "eps_growth": None,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
        "roic": None,
        "fcf": None,
        "net_debt_to_ebitda": None,
    }
    income = statements.get("income")
    balance = statements.get("balance")
    cash = statements.get("cash")
    if not income:
        return out

    out["revenue_growth"] = _growth_for(income, "Total Revenue", "Revenues")
    out["net_income_growth"] = _growth_for(
        income, "Net Income", "Net Income Common Stockholders"
    )
    out["eps_growth"] = _growth_for(income, "Diluted EPS", "Basic EPS")

    revenue = _latest_value(income, "Total Revenue", "Revenues")
    gross = _latest_value(income, "Gross Profit")
    operating = _latest_value(income, "Operating Income")
    net_income = _latest_value(income, "Net Income", "Net Income Common Stockholders")
    ebitda = _latest_value(income, "EBITDA", "Normalized EBITDA")

    if revenue:
        out["gross_margin"] = gross / revenue if gross is not None else None
        out["operating_margin"] = operating / revenue if operating is not None else None
        out["net_margin"] = net_income / revenue if net_income is not None else None

    out["fcf"] = _latest_value(cash, "Free Cash Flow")

    if balance:
        invested = _latest_value(balance, "Invested Capital")
        total_debt = _latest_value(balance, "Total Debt")
        cash_bal = _latest_value(
            balance,
            "Cash And Cash Equivalents",
            "Cash Cash Equivalents And Short Term Investments",
        )
        # NOPAT approximation: operating income taxed at a flat 21%.
        nopat = operating * 0.79 if operating is not None else None
        if nopat is not None and invested:
            out["roic"] = nopat / invested
        if total_debt is not None and cash_bal is not None:
            net_debt = total_debt - cash_bal
            out["net_debt_to_ebitda"] = (
                net_debt / ebitda if ebitda and ebitda != 0 else None
            )
    return out
