"""Chart overlay computations for the History view (#8).

Pure pandas over a close/volume frame: moving averages, drawdown from
peak, and earnings-date lookups. Kept separate from the widget so every
value is unit-testable against hand-checked numbers.
"""

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def moving_average(closes: pd.Series, window: int) -> pd.Series | None:
    """
    Simple moving average over `window` bars, min_periods=window: the result
    is None until `window` closes exist, so callers never draw a truncated
    average (per #8's acceptance criteria).
    """
    if len(closes) < window:
        return None
    return closes.rolling(window, min_periods=window).mean()


def drawdown_from_peak(closes: pd.Series) -> pd.Series:
    """Running percent drawdown from the cumulative maximum."""
    peak = closes.cummax()
    return closes / peak - 1


def relative_volume_series(volumes: pd.Series, window: int = 20) -> pd.Series | None:
    """Volume divided by the rolling mean of the previous `window` bars."""
    if len(volumes) <= window:
        return None
    return volumes / volumes.rolling(window + 1, min_periods=window + 1).mean().shift(1)


def earnings_dates_in_range(
    bars_frame: pd.DataFrame, start: date, end: date
) -> list[Any]:
    """
    Earnings dates within [start, end] from the price cache's calendar data.

    The provider stores one upcoming earnings date per ticker; historical
    earnings dates are not persisted, so markers only appear when the
    cached date falls inside the visible range (typically the next report).
    """
    cached = bars_frame.attrs.get("earnings_date")
    if cached is None:
        return []
    if isinstance(cached, (datetime, date, str)):
        cached = [cached]
    out = []
    for value in cached:
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                continue
        if value is not None and start <= value <= end:
            out.append(value)
    return out


def align_series(series_by_ticker: dict[str, pd.Series]) -> pd.DataFrame:
    """
    Aligns named close series on their common dates (inner join) and drops
    rows with missing values, per #7's acceptance criteria.
    """
    if not series_by_ticker:
        return pd.DataFrame()
    frame = pd.DataFrame(series_by_ticker)
    return frame.dropna()


def rebase_to_100(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Rebases every column to start at 100 so relative performance is
    directly comparable across tickers with different price scales.
    """
    if frame.empty:
        return frame
    first = frame.iloc[0]
    return frame / first * 100


def pairwise_correlations(frame: pd.DataFrame) -> pd.DataFrame:
    """Daily-return correlation matrix for the aligned frame."""
    if frame.empty:
        return pd.DataFrame()
    returns = frame.pct_change().dropna()
    return returns.corr()


def beta_against(series: pd.Series, benchmark: pd.Series) -> float | None:
    """
    Beta of `series` versus `benchmark` over their common dates:
    cov(r_s, r_b) / var(r_b).
    """
    aligned = pd.DataFrame({"s": series, "b": benchmark}).dropna()
    if len(aligned) < 3:
        return None
    returns = aligned.pct_change().dropna()
    benchmark_var = returns["b"].var()
    if not benchmark_var or benchmark_var == 0:
        return None
    return float(returns["s"].cov(returns["b"]) / benchmark_var)
