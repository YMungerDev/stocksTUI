"""Computed watchlist metrics from the local daily-bar store (#2).

Pure pandas calculations over adjusted daily closes, sourced from the
daily_bars table so no extra network requests are needed. Each metric
returns None when there is insufficient history rather than a wrong value.
"""

import logging
import math
from datetime import date
from typing import Any

import pandas as pd

from stockstui.database.db_manager import DbManager

logger = logging.getLogger(__name__)

# Metric definitions surfaced as toggleable watchlist columns.
METRIC_COLUMNS: dict[str, str] = {
    "1W Return": "Return over ~5 trading days",
    "1M Return": "Return over ~21 trading days",
    "YTD Return": "Return since January 1st",
    "1Y Return": "Return over ~252 trading days",
    "vs 50d MA": "Percent distance from the 50-day moving average",
    "vs 200d MA": "Percent distance from the 200-day moving average",
    "RSI(14)": "Relative strength index over 14 days",
    "30d Vol": "30-day realised volatility, annualised",
    "Rel Volume": "Today's volume vs the 20-day average",
    "Sparkline": "Last 30 closes as an inline sparkline",
}

TRADING_DAYS_PER_YEAR = 252
_RSI_PERIOD = 14
_VOL_WINDOW = 30
_REL_VOLUME_WINDOW = 20
_SPARKLINE_POINTS = 30

# Unicode block glyphs for the inline sparkline, low to high.
_SPARK_GLYPHS = "▁▂▃▄▅▆▇█"


def adjusted_close_series(bars: pd.DataFrame) -> pd.Series | None:
    """Returns the adjusted-close series for a stored-bar frame, or None."""
    adj = bars.attrs.get("adjclose") or []
    values = [a for a in adj if a is not None]
    if len(values) < 2:
        return None
    return pd.Series(values, index=bars.index, dtype=float)


def _safe_ratio_or_none(numerator: float, denominator: float) -> float | None:
    if denominator is None or denominator == 0:
        return None
    value = numerator / denominator - 1
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def return_over_days(closes: pd.Series, days: int) -> float | None:
    """Simple return between the latest close and `days` trading days ago."""
    if len(closes) <= days:
        return None
    return _safe_ratio_or_none(closes.iloc[-1], closes.iloc[-days - 1])


def ytd_return(closes: pd.Series, today: date | None = None) -> float | None:
    """Return since the last close of the previous calendar year."""
    today = today or date.today()
    # The stored-bar index is tz-aware UTC, but tests and future callers may
    # hold naive indexes: normalize both to naive UTC calendar dates, which
    # matches how bars are dated (midnight stamps in their exchange zone).
    if isinstance(closes.index, pd.DatetimeIndex) and closes.index.tz is not None:
        index = closes.index.tz_localize(None)
    else:
        index = closes.index
    year_start = pd.Timestamp(today.year, 1, 1)
    closes = pd.Series(closes.to_numpy(), index=index)
    # Take the last bar strictly before January 1st of the current year.
    idx = closes.index.searchsorted(year_start, side="left")
    if idx == 0:
        return None
    return _safe_ratio_or_none(closes.iloc[-1], closes.iloc[idx - 1])


def distance_from_moving_average(closes: pd.Series, window: int) -> float | None:
    """Percent distance of the latest close from its simple moving average."""
    if len(closes) < window:
        return None
    sma = closes.tail(window).mean()
    return _safe_ratio_or_none(closes.iloc[-1], sma)


def rsi(closes: pd.Series, period: int = _RSI_PERIOD) -> float | None:
    """
    Wilder's RSI over `period` bars of the adjusted close series.

    Implemented with exponential moving averages (alpha=1/period,
    adjust=False), the standard smoothed formulation.
    """
    if len(closes) <= period:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    if loss.iloc[-1] == 0:
        return 100.0 if gain.iloc[-1] > 0 else None
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - 100 / (1 + rs)


def realised_volatility(closes: pd.Series, window: int = _VOL_WINDOW) -> float | None:
    """Annualised standard deviation of log returns over `window` days."""
    if len(closes) <= window:
        return None
    log_returns = (closes / closes.shift(1)).map(
        lambda r: math.log(r) if r > 0 else math.nan
    )
    sample = log_returns.dropna().tail(window)
    if len(sample) < 2:
        return None
    return float(sample.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))


def relative_volume(volumes: pd.Series, window: int = _REL_VOLUME_WINDOW) -> float | None:
    """Latest volume divided by the mean of the previous `window` days."""
    if len(volumes) <= window:
        return None
    average = volumes.tail(window + 1).head(window).mean()
    if not average or average <= 0:
        return None
    value = volumes.iloc[-1] / average
    return value if not (isinstance(value, float) and math.isnan(value)) else None


def sparkline(closes: pd.Series, points: int = _SPARKLINE_POINTS) -> str:
    """
    Renders the last `points` adjusted closes as Unicode block glyphs.
    Returns an empty string when there is no data.
    """
    if closes.empty:
        return ""
    sample = closes.tail(points)
    low, high = sample.min(), sample.max()
    if high == low:
        return _SPARK_GLYPHS[3] * len(sample)
    span = high - low
    return "".join(
        _SPARK_GLYPHS[min(int((v - low) / span * (len(_SPARK_GLYPHS) - 1) + 0.5), len(_SPARK_GLYPHS) - 1)]
        for v in sample
    )


def compute_metrics_for_ticker(db_manager: DbManager, ticker: str) -> dict[str, Any]:
    """
    Computes every METRIC_COLUMNS value for one ticker from the bar store.

    Tickers without stored bars yield empty strings — computed columns are
    blank, never errors, matching the events-columns convention.
    """
    empty = {key: "" for key in METRIC_COLUMNS}
    if db_manager is None:
        return empty
    try:
        bars = db_manager.load_daily_bars(ticker)
    except Exception as e:  # noqa: BLE001 - metrics must never break a render
        logger.warning(f"Failed to load bars for {ticker}: {e}")
        return empty
    if bars is None or bars.empty:
        return empty

    closes = adjusted_close_series(bars)
    if closes is None:
        return empty

    values: dict[str, Any] = {
        "1W Return": return_over_days(closes, 5),
        "1M Return": return_over_days(closes, 21),
        "YTD Return": ytd_return(closes),
        "1Y Return": return_over_days(closes, 252),
        "vs 50d MA": distance_from_moving_average(closes, 50),
        "vs 200d MA": distance_from_moving_average(closes, 200),
        "RSI(14)": rsi(closes),
        "30d Vol": realised_volatility(closes),
        "Rel Volume": relative_volume(bars["Volume"]),
        "Sparkline": sparkline(closes),
    }
    for key, value in values.items():
        if isinstance(value, float) and math.isnan(value):
            values[key] = None
    return values
