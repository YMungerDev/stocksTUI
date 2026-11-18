"""Valuation multiples now and against the company's own history.

Multiples at each fiscal quarter end use trailing-twelve-month (TTM) flows
from four consecutive quarters, the balance sheet at that quarter end, and
the market value of equity at the quarter-end close. Without quarterly data
(e.g. 20-F filers), fiscal-year ends and annual flows are used instead.

Share counts: Yahoo closes are split-adjusted, so a historical market cap
uses shares in today's split units. Cover-page share counts are as filed at
the time, so each is multiplied by every split dated after its cover date.

Multiples with a non-positive denominator (losses, negative EBITDA or
equity) are None rather than a negative or huge number.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from itertools import pairwise
from math import isnan
from statistics import median
from typing import Any

METRICS: tuple[tuple[str, str, bool], ...] = (
    # (key, label, is_percent)
    ("pe", "P/E (TTM)", False),
    ("ev_ebitda", "EV/EBITDA (TTM)", False),
    ("ev_sales", "EV/Sales (TTM)", False),
    ("fcf_yield", "FCF Yield (TTM)", True),
    ("pb", "P/B", False),
)

_FLOW_ROWS = {
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "revenue": ("Total Revenue", "Revenues"),
    "fcf": ("Free Cash Flow",),
}
_BALANCE_ROWS = {
    "debt": ("Total Debt",),
    "cash": (
        "Cash Cash Equivalents And Short Term Investments",
        "Cash And Cash Equivalents",
    ),
    "equity": ("Stockholders Equity",),
}


def _row(payload: dict[str, Any] | None, aliases: tuple[str, ...]) -> list | None:
    if not payload:
        return None
    rows = payload.get("rows", {})
    for alias in aliases:
        if alias in rows:
            return rows[alias]
    return None


def _by_date(payload: dict[str, Any] | None, aliases: tuple[str, ...]) -> dict[date, float]:
    values = _row(payload, aliases)
    if not payload or values is None:
        return {}
    out = {}
    for column, value in zip(payload.get("columns", []), values):
        if isinstance(value, (int, float)) and not isnan(value):
            try:
                out[date.fromisoformat(str(column)[:10])] = float(value)
            except ValueError:
                continue
    return out


def ttm_series(payload_q: dict[str, Any] | None, aliases: tuple[str, ...]) -> dict[date, float]:
    """
    TTM sums keyed by quarter end, only where four consecutive quarters
    (each 80-100 days apart) are all present.
    """
    quarters = _by_date(payload_q, aliases)
    ends = sorted(quarters)
    out: dict[date, float] = {}
    for i in range(3, len(ends)):
        window = ends[i - 3 : i + 1]
        gaps = [(b - a).days for a, b in pairwise(window)]
        if all(80 <= g <= 100 for g in gaps):
            out[ends[i]] = sum(quarters[e] for e in window)
    return out


def split_factor_after(when: date, splits: list[tuple[date, float]]) -> float:
    """Product of split ratios strictly after `when` (2.0 for a 2-for-1)."""
    factor = 1.0
    for split_date, ratio in splits:
        if split_date > when and ratio and ratio > 0:
            factor *= ratio
    return factor


def shares_at(
    when: date,
    cover_shares: list[dict[str, Any]],
    splits: list[tuple[date, float]],
    max_lag_days: int = 120,
) -> float | None:
    """
    Split-adjusted shares outstanding for a period ending `when`: the first
    cover-page count dated on or after `when` (within max_lag_days).
    """
    candidates = []
    for entry in cover_shares:
        try:
            cover_date = date.fromisoformat(str(entry["end"])[:10])
        except (KeyError, ValueError, TypeError):
            continue
        if when <= cover_date <= when + timedelta(days=max_lag_days):
            candidates.append((cover_date, float(entry["val"])))
    if not candidates:
        return None
    cover_date, value = min(candidates)
    return value * split_factor_after(cover_date, splits)


def latest_shares(
    cover_shares: list[dict[str, Any]], splits: list[tuple[date, float]]
) -> float | None:
    """Most recent cover-page share count in today's split units."""
    best = None
    for entry in cover_shares:
        try:
            cover_date = date.fromisoformat(str(entry["end"])[:10])
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or cover_date > best[0]:
            best = (cover_date, float(entry["val"]))
    if best is None:
        return None
    return best[1] * split_factor_after(best[0], splits)


def close_on_or_before(closes: list[tuple[date, float]], when: date, max_gap_days: int = 7) -> float | None:
    """The last close on or before `when`, if within max_gap_days. `closes` sorted by date."""
    dates = [d for d, _ in closes]
    pos = bisect_right(dates, when) - 1
    if pos < 0:
        return None
    close_date, value = closes[pos]
    if (when - close_date).days > max_gap_days:
        return None
    return value


def multiples(
    market_cap: float,
    net_income: float | None,
    ebitda: float | None,
    revenue: float | None,
    fcf: float | None,
    debt: float | None,
    cash: float | None,
    equity: float | None,
) -> dict[str, float | None]:
    ev = market_cap + (debt or 0.0) - (cash or 0.0) if debt is not None or cash is not None else None

    def positive_ratio(numerator, denominator):
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator

    return {
        "pe": positive_ratio(market_cap, net_income),
        "ev_ebitda": positive_ratio(ev, ebitda),
        "ev_sales": positive_ratio(ev, revenue),
        "fcf_yield": fcf / market_cap if fcf is not None and market_cap > 0 else None,
        "pb": positive_ratio(market_cap, equity),
    }


def _flows(statements: dict[str, Any]) -> tuple[dict[str, dict[date, float]], dict[str, dict[date, float]], bool]:
    """TTM flows (or annual flows) and matching balances keyed by period end."""
    quarterly = {
        key: ttm_series(statements.get(stmt + "_q"), aliases)
        for key, (stmt, aliases) in {
            "net_income": ("income", _FLOW_ROWS["net_income"]),
            "ebitda": ("income", _FLOW_ROWS["ebitda"]),
            "revenue": ("income", _FLOW_ROWS["revenue"]),
            "fcf": ("cash", _FLOW_ROWS["fcf"]),
        }.items()
    }
    if quarterly["net_income"] or quarterly["revenue"]:
        balances = {
            key: _by_date(statements.get("balance_q"), aliases)
            for key, aliases in _BALANCE_ROWS.items()
        }
        return quarterly, balances, True
    annual = {
        "net_income": _by_date(statements.get("income"), _FLOW_ROWS["net_income"]),
        "ebitda": _by_date(statements.get("income"), _FLOW_ROWS["ebitda"]),
        "revenue": _by_date(statements.get("income"), _FLOW_ROWS["revenue"]),
        "fcf": _by_date(statements.get("cash"), _FLOW_ROWS["fcf"]),
    }
    balances = {
        key: _by_date(statements.get("balance"), aliases)
        for key, aliases in _BALANCE_ROWS.items()
    }
    return annual, balances, False


def history_points(
    statements: dict[str, Any],
    closes: list[tuple[date, float]],
    splits: list[tuple[date, float]],
) -> list[dict[str, Any]]:
    """Multiples at each period end that has a price and share count, oldest first."""
    cover = (statements.get("meta") or {}).get("cover_shares") or []
    flows, balances, _ = _flows(statements)
    ends = sorted(set(flows["net_income"]) | set(flows["revenue"]))
    points = []
    for end in ends:
        price = close_on_or_before(closes, end)
        shares = shares_at(end, cover, splits)
        if price is None or shares is None:
            continue
        market_cap = price * shares
        values = multiples(
            market_cap,
            flows["net_income"].get(end),
            flows["ebitda"].get(end),
            flows["revenue"].get(end),
            flows["fcf"].get(end),
            balances["debt"].get(end),
            balances["cash"].get(end),
            balances["equity"].get(end),
        )
        points.append({"date": end, "market_cap": market_cap, **values})
    return points


def current_multiples(
    statements: dict[str, Any],
    price: float,
    splits: list[tuple[date, float]],
) -> dict[str, Any] | None:
    """Multiples at `price` using the latest TTM flows and latest balance sheet."""
    cover = (statements.get("meta") or {}).get("cover_shares") or []
    shares = latest_shares(cover, splits)
    if shares is None or price is None:
        return None
    flows, balances, _ = _flows(statements)

    def latest(series: dict[date, float]) -> float | None:
        return series[max(series)] if series else None

    market_cap = price * shares
    values = multiples(
        market_cap,
        latest(flows["net_income"]),
        latest(flows["ebitda"]),
        latest(flows["revenue"]),
        latest(flows["fcf"]),
        latest(balances["debt"]),
        latest(balances["cash"]),
        latest(balances["equity"]),
    )
    return {"market_cap": market_cap, "shares": shares, **values}


def percentile_rank(history: list[float], value: float) -> float | None:
    """Share of historical values at or below `value` (0..1)."""
    if not history:
        return None
    return sum(1 for h in history if h <= value) / len(history)


def summarize(
    points: list[dict[str, Any]], current: dict[str, Any] | None, today: date
) -> dict[str, dict[str, float | None]]:
    """Per metric: current, 5y and 10y medians, 10y min/max and percentile."""
    out: dict[str, dict[str, float | None]] = {}
    ten_years = today - timedelta(days=3653)
    five_years = today - timedelta(days=1826)
    for key, _, _ in METRICS:
        ten = [p[key] for p in points if p[key] is not None and p["date"] >= ten_years]
        five = [p[key] for p in points if p[key] is not None and p["date"] >= five_years]
        now = (current or {}).get(key)
        out[key] = {
            "current": now,
            "median_5y": median(five) if five else None,
            "median_10y": median(ten) if ten else None,
            "min_10y": min(ten) if ten else None,
            "max_10y": max(ten) if ten else None,
            "percentile_10y": percentile_rank(ten, now) if now is not None else None,
            "points": len(ten),
        }
    return out
