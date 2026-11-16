"""Analyst and ownership research panel data (#9).

Fetches analyst price targets, recommendation breakdown, EPS/revenue
estimates, earnings history (estimate vs actual, surprise %), recent
insider transactions, and major/institutional holders from yfinance.

Every block tolerates missing data per ticker: failures yield None and
the view renders an empty state for that block only. Results are cached
with the slow info data cadence (daily TTL) in SQLite.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

def _get_yf():
    """Imports yfinance lazily so tests can patch a module-level shim."""
    import yfinance

    return yfinance


# Module-level indirection so tests can patch yfinance in one place.
_yf_module = None


def _ticker(ticker: str):
    global _yf_module
    if _yf_module is None:
        _yf_module = _get_yf()
    return _yf_module.Ticker(ticker)


_CACHE_TTL_SECONDS = 86400  # daily, like the slow info data
_CACHE_TTL_SECONDS = 86400  # daily, like the slow info data


def _safe(callable_, *names: str):
    """
    Runs a yfinance accessor, returning its value or None on any error.
    Used per block so one missing dataset never breaks the panel.
    """
    try:
        value = callable_()
    except Exception as e:  # noqa: BLE001 - tolerate missing data per block
        logger.debug(f"Research fetch failed for {'/'.join(names)}: {e}")
        return None
    return value


def fetch_research(ticker: str, force_refresh: bool = False) -> dict[str, Any]:
    """
    Gathers all research blocks for a ticker. Returns a dict with keys:
    price_targets, recommendations, eps_estimate, revenue_estimate,
    earnings_history, insider_transactions, institutional_holders,
    major_holders. Each value is a JSON-safe structure or None.
    """
    normalized = ticker.upper()
    t = _ticker(normalized)

    out: dict[str, Any] = {}

    targets = _safe(lambda: t.analyst_price_targets, "price_targets")
    if isinstance(targets, dict) and targets:
        out["price_targets"] = {
            k: (float(v) if isinstance(v, (int, float)) else None)
            for k, v in targets.items()
        }
    else:
        out["price_targets"] = None

    rec = _safe(lambda: t.recommendations_summary, "recommendations")
    if rec is not None and not getattr(rec, "empty", True):
        current = rec.iloc[0]
        out["recommendations"] = {
            "strong_buy": int(current.get("strongBuy", 0) or 0),
            "buy": int(current.get("buy", 0) or 0),
            "hold": int(current.get("hold", 0) or 0),
            "sell": int(current.get("sell", 0) or 0),
            "strong_sell": int(current.get("strongSell", 0) or 0),
            "previous": {
                "strong_buy": int(rec.iloc[1].get("strongBuy", 0) or 0) if len(rec) > 1 else None,
                "buy": int(rec.iloc[1].get("buy", 0) or 0) if len(rec) > 1 else None,
            },
        }
    else:
        out["recommendations"] = None

    eps_est = _safe(lambda: t.earnings_estimate, "eps_estimate")
    out["eps_estimate"] = _frame_to_records(eps_est, max_rows=4)
    rev_est = _safe(lambda: t.revenue_estimate, "revenue_estimate")
    out["revenue_estimate"] = _frame_to_records(rev_est, max_rows=4)

    history = _safe(lambda: t.earnings_history, "earnings_history")
    out["earnings_history"] = _frame_to_records(history, max_rows=8)

    insiders = _safe(lambda: t.insider_transactions, "insiders")
    out["insider_transactions"] = _frame_to_records(insiders, max_rows=15)

    institutional = _safe(lambda: t.institutional_holders, "holders")
    out["institutional_holders"] = _frame_to_records(institutional, max_rows=10)

    major = _safe(lambda: t.major_holders, "major_holders")
    if major is not None and not getattr(major, "empty", True):
        out["major_holders"] = {
            str(idx): float(row.iloc[0]) if len(row) else None
            for idx, row in major.iterrows()
        }
    else:
        out["major_holders"] = None

    return out


def _frame_to_records(frame, max_rows: int) -> list[dict] | None:
    """JSON-safe records from a yfinance frame; None when missing/empty."""
    if frame is None or getattr(frame, "empty", True):
        return None
    records = []
    for index, row in frame.head(max_rows).iterrows():
        record = {"_period": str(index)}
        for column in frame.columns:
            value = row.get(column)
            if value is None or (isinstance(value, float) and value != value):
                record[str(column)] = None
            elif isinstance(value, (int, float)):
                record[str(column)] = float(value)
            else:
                record[str(column)] = str(value)
        records.append(record)
    return records


_CACHE_KEY_SUFFIX = "::research"


def get_research(
    ticker: str, db_manager, force_refresh: bool = False
) -> dict[str, Any]:
    """
    Research blocks with daily caching in the fundamentals_cache table
    (keyed '<TICKER>::research'), matching the slow-info cadence. Always
    returns a dict; individual blocks are None when missing.
    """
    import json

    normalized = ticker.upper()
    cache_key = normalized + _CACHE_KEY_SUFFIX
    if not force_refresh:
        raw = db_manager.load_fundamentals_cache(cache_key) if db_manager else None
        if raw:
            try:
                return json.loads(raw).get("research", {})
            except (json.JSONDecodeError, TypeError):
                pass
    research = fetch_research(normalized, force_refresh=force_refresh)
    if research and db_manager is not None:
        try:
            db_manager.save_fundamentals_cache(
                cache_key, json.dumps({"research": research})
            )
        except TypeError as e:
            logger.warning(f"Failed to cache research for {normalized}: {e}")
    return research or {}
