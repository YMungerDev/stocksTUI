"""Options analytics over a fetched chain: implied move, skew, term
structure, put/call ratios, and max pain (#11).

Pure pandas over calls/puts DataFrames as returned by options_provider;
every function excludes illiquid strikes (zero/NaN bid and stale last)
per the acceptance criteria, and every value is unit-testable against
fixture chains.
"""

import logging
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _liquid(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filters a chain side to liquid rows: a positive bid and a last price
    that is not stale-zero, per #11's illiquidity criteria.
    """
    if df is None or df.empty:
        return df
    mask = (
        (df["bid"].fillna(0) > 0)
        & (df["lastPrice"].fillna(0) > 0)
    )
    return df[mask]


def implied_move(
    calls: pd.DataFrame,
    puts: pd.DataFrame,
    underlying_price: float,
    days_to_expiry: int | None = None,
) -> dict[str, float] | None:
    """
    ATM straddle implied move, as both an absolute price and a percent.

    Uses the mid of the ATM call and put straddle. Returns None when either
    side has no liquid ATM row.
    """
    if underlying_price is None or underlying_price <= 0:
        return None
    if calls is None or calls.empty or puts is None or puts.empty:
        return None

    liquid_calls = _liquid(calls)
    liquid_puts = _liquid(puts)
    if liquid_calls.empty or liquid_puts.empty:
        return None

    call_idx = (liquid_calls["strike"] - underlying_price).abs().idxmin()
    put_idx = (liquid_puts["strike"] - underlying_price).abs().idxmin()
    call_strike = liquid_calls.loc[call_idx, "strike"]
    put_strike = liquid_puts.loc[put_idx, "strike"]

    def _mid(row: pd.Series) -> float | None:
        bid, ask = row.get("bid"), row.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return float((bid + ask) / 2)
        last = row.get("lastPrice")
        if last is not None and last > 0:
            return float(last)
        return None

    call_mid = _mid(liquid_calls.loc[call_idx])
    put_mid = _mid(liquid_puts.loc[put_idx])
    if call_mid is None or put_mid is None:
        return None

    straddle = call_mid + put_mid
    move_abs = straddle
    move_pct = straddle / underlying_price * 100
    out: dict[str, float] = {"straddle": straddle, "move": move_abs, "move_pct": move_pct}
    if days_to_expiry is not None and days_to_expiry > 0:
        # Annualised-equivalent daily move: sqrt-scaling by calendar days.
        out["daily_move_pct"] = move_pct / (days_to_expiry ** 0.5)
    out["atm_strike"] = float(call_strike)
    return out


def iv_skew(calls: pd.DataFrame, puts: pd.DataFrame) -> dict[str, Any] | None:
    """
    IV skew for one expiry: put wing, call wing, and the ATM baseline.

    Returns put_25_delta IV minus call_25_delta IV approximated by OTM
    strikes near ±25% moneyness around the underlying, plus the full IV
    surface by strike for charting. Illiquid strikes excluded.
    """
    if calls is None or calls.empty or puts is None or puts.empty:
        return None
    liquid_calls = _liquid(calls).copy()
    liquid_puts = _liquid(puts).copy()
    if liquid_calls.empty or liquid_puts.empty:
        return None

    call_ivs = liquid_calls[["strike", "impliedVolatility"]].dropna()
    put_ivs = liquid_puts[["strike", "impliedVolatility"]].dropna()
    if call_ivs.empty or put_ivs.empty:
        return None

    # ATM IV: strike nearest the mid of the traded strike ranges.
    centre = (
        float(call_ivs["strike"].median()) + float(put_ivs["strike"].median())
    ) / 2
    atm_call = call_ivs.iloc[(call_ivs["strike"] - centre).abs().argmin()]
    atm_put = put_ivs.iloc[(put_ivs["strike"] - centre).abs().argmin()]
    atm_iv = float((atm_call["impliedVolatility"] + atm_put["impliedVolatility"]) / 2)

    # 25-delta approximation: OTM put ~ -25% moneyness, OTM call ~ +25%.
    wing_low = centre * 0.75
    wing_high = centre * 1.25
    put_wing = put_ivs.iloc[(put_ivs["strike"] - wing_low).abs().argmin()]
    call_wing = call_ivs.iloc[(call_ivs["strike"] - wing_high).abs().argmin()]

    return {
        "atm_iv": atm_iv,
        "put_wing_iv": float(put_wing["impliedVolatility"]),
        "call_wing_iv": float(call_wing["impliedVolatility"]),
        "skew": float(put_wing["impliedVolatility"] - call_wing["impliedVolatility"]),
        "call_iv_by_strike": dict(
            zip(call_ivs["strike"].tolist(), call_ivs["impliedVolatility"].tolist())
        ),
        "put_iv_by_strike": dict(
            zip(put_ivs["strike"].tolist(), put_ivs["impliedVolatility"].tolist())
        ),
    }


def term_structure(
    atm_ivs: dict[str, float | None],
    expiries: list[str],
) -> list[tuple[str, float]]:
    """ATM IV term structure across expiries, in chronological order."""
    out = []
    for expiry in sorted(expiries):
        iv = atm_ivs.get(expiry)
        if iv is not None and iv > 0 and not np.isnan(iv):
            out.append((expiry, float(iv)))
    return out


def put_call_ratios(calls: pd.DataFrame, puts: pd.DataFrame) -> dict[str, float]:
    """
    Put/call ratios by volume and open interest. Zero-denominator sides
    yield NaN so callers can show a blank rather than a wrong value.
    """
    out: dict[str, float] = {}
    for name, column in (
        ("volume", "volume"),
        ("open_interest", "openInterest"),
    ):
        call_total = float(
            calls[column].fillna(0).sum() if calls is not None and not calls.empty else 0
        )
        put_total = float(
            puts[column].fillna(0).sum() if puts is not None and not puts.empty else 0
        )
        out[f"put_call_{name}"] = (
            put_total / call_total if call_total > 0 else float("nan")
        )
    return out


def max_pain(calls: pd.DataFrame, puts: pd.DataFrame) -> float | None:
    """
    Strike where option writers' total payout is minimised.

    For each candidate settlement strike S: call writers pay max(S - K, 0)
    per call at strike K < S; put writers pay max(K - S, 0) per put at
    strike K > S. Weighted by open interest. Illiquid strikes (zero OI)
    contribute nothing naturally.
    """
    if calls is None or calls.empty or puts is None or puts.empty:
        return None
    call_oi = calls[["strike", "openInterest"]].fillna(0)
    put_oi = puts[["strike", "openInterest"]].fillna(0)
    strikes = sorted(set(call_oi["strike"]) | set(put_oi["strike"]))
    if not strikes:
        return None

    best_strike, best_pain = None, None
    for settlement in strikes:
        call_pain = float(
            ((settlement - call_oi["strike"]).clip(lower=0) * call_oi["openInterest"]).sum()
        )
        put_pain = float(
            ((put_oi["strike"] - settlement).clip(lower=0) * put_oi["openInterest"]).sum()
        )
        total = call_pain + put_pain
        if best_pain is None or total < best_pain:
            best_pain, best_strike = total, float(settlement)
    return best_strike


def days_until(expiry: str, today: date | None = None) -> int:
    """Calendar days from today until an ISO expiry string."""
    today = today or date.today()
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0
    return (expiry_date - today).days
