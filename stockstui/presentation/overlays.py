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
    if isinstance(cached, (datetime, date)):
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
