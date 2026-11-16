"""Watchlist screening expressions and output formats (#12).

A filter expression over normalised quote fields, evaluated with
DataFrame.query — usable in the TUI and as --filter in -o output mode.
Output formats: table (default), json, csv. JSON and CSV emit raw
numbers, not formatted strings (acceptance criteria).

Normalised columns (snake_case) with computed helpers:
  symbol, description, price, previous_close, open, day_low, day_high,
  volume, market_cap, pe_ratio, eps, beta, dividend_yield,
  fifty_two_week_low/high, all_time_high, currency
  plus computed: off_ath (price/all_time_high - 1), pct_change
  (price/previous_close - 1)
"""

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class FilterError(ValueError):
    """Raised for expressions that fail to compile or evaluate."""


def normalise_quotes(quotes: list[dict[str, Any]]) -> pd.DataFrame:
    """
    Builds the query DataFrame from price-cache entries: normalised
    snake_case columns plus computed off_ath and pct_change.
    """
    rows = []
    for quote in quotes:
        if not quote:
            continue
        row = {
            "symbol": quote.get("symbol"),
            "description": quote.get("description"),
            "currency": quote.get("currency"),
            "price": quote.get("price"),
            "previous_close": quote.get("previous_close"),
            "open": quote.get("open"),
            "day_low": quote.get("day_low"),
            "day_high": quote.get("day_high"),
            "volume": quote.get("volume"),
            "market_cap": quote.get("market_cap"),
            "pe_ratio": quote.get("pe_ratio"),
            "eps": quote.get("eps"),
            "beta": quote.get("beta"),
            "dividend_yield": quote.get("dividend_yield"),
            "fifty_two_week_low": quote.get("fifty_two_week_low"),
            "fifty_two_week_high": quote.get("fifty_two_week_high"),
            "all_time_high": quote.get("all_time_high"),
        }
        price = row["price"]
        if isinstance(price, (int, float)):
            if row["all_time_high"]:
                row["off_ath"] = price / row["all_time_high"] - 1
            else:
                row["off_ath"] = None
            if row["previous_close"]:
                row["pct_change"] = price / row["previous_close"] - 1
            else:
                row["pct_change"] = None
        else:
            row["off_ath"] = None
            row["pct_change"] = None
        rows.append(row)
    return pd.DataFrame(rows)


def apply_filter(quotes: list[dict[str, Any]], expression: str) -> list[dict[str, Any]]:
    """
    Filters price-cache entries with a DataFrame.query expression over the
    normalised columns. Raises FilterError with a clear message for invalid
    expressions (never a traceback, per the acceptance criteria).
    """
    if not expression or not expression.strip():
        return quotes
    frame = normalise_quotes(quotes)
    try:
        filtered = frame.query(expression, engine="python")
    except (SyntaxError, pd.errors.UndefinedVariableError, NameError, ValueError, KeyError, TypeError) as e:
        raise FilterError(
            f"Invalid filter expression: {e!s}. "
            "Use snake_case fields like price, pe_ratio, off_ath, pct_change."
        ) from e
    keep = set(filtered["symbol"].tolist()) if not filtered.empty else set()
    return [q for q in quotes if q.get("symbol") in keep]


def to_json(quotes: list[dict[str, Any]]) -> str:
    """Raw-number JSON array of quotes (formatted strings never emitted)."""
    import json

    frame = normalise_quotes(quotes)
    payload = []
    for _, row in frame.iterrows():
        record = {}
        for column in frame.columns:
            value = row.get(column)
            if value is None or (isinstance(value, float) and value != value):
                record[column] = None
            elif isinstance(value, (int, float)):
                record[column] = value
            else:
                record[column] = str(value)
        payload.append(record)
    return json.dumps(payload, indent=2, default=str)


CSV_COLUMNS: list[str] = [
    "symbol",
    "description",
    "currency",
    "price",
    "previous_close",
    "open",
    "day_low",
    "day_high",
    "volume",
    "market_cap",
    "pe_ratio",
    "eps",
    "beta",
    "dividend_yield",
    "fifty_two_week_low",
    "fifty_two_week_high",
    "all_time_high",
    "off_ath",
    "pct_change",
]


def to_csv(quotes: list[dict[str, Any]]) -> str:
    """CSV of raw numbers over the fixed column set."""
    import csv
    import io

    frame = normalise_quotes(quotes)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for _, row in frame.iterrows():
        values = []
        for column in CSV_COLUMNS:
            value = row.get(column)
            if value is None or (isinstance(value, float) and value != value):
                values.append("")
            else:
                values.append(value)
        writer.writerow(values)
    return buffer.getvalue()
