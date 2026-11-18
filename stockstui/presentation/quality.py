"""Earnings-quality metrics over annual statements.

Works on the shared statement payload (yfinance row names), whichever
provider filled it. Every metric returns None when an input is missing,
never a guess.

Definitions (fiscal year t, prior year t-1):

- Sloan accruals: (NI - CFO) / average total assets. High positive
  accruals have historically predicted weaker future returns.
- Cash conversion: CFO / NI.
- DSO / DIO / DPO: receivables / revenue, inventory / COGS and payables /
  COGS, times 365, on year-end balances. Cash conversion cycle is
  DSO + DIO - DPO.
- Capex / D&A: reinvestment relative to depreciation.
- Piotroski F-score: nine binary signals (Piotroski 2000). Signals whose
  inputs are missing are skipped and the score is reported out of the
  number evaluated.
- Altman Z (original public-manufacturer model): 1.2 WC/TA + 1.4 RE/TA +
  3.3 EBIT/TA + 0.6 MVE/TL + 1.0 Sales/TA. Needs market value of equity.
  Below 1.81 is the distress zone, above 2.99 the safe zone. Not meaningful
  for banks and insurers.
- Beneish M-score: the 8-variable model (threshold -1.78), or the
  5-variable model (threshold -2.22) when SG&A, leverage or accrual inputs
  are missing. AQI omits long-term securities (not in the payload).
"""

from __future__ import annotations

from math import isnan
from typing import Any

_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Revenues", "Operating Revenue"),
    "cogs": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "gross": ("Gross Profit",),
    "sga": ("Selling General And Administration",),
    "ebit": ("Operating Income", "EBIT"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "dep": ("Reconciled Depreciation", "Depreciation And Amortization"),
    "total_assets": ("Total Assets",),
    "current_assets": ("Current Assets",),
    "current_liabilities": ("Current Liabilities",),
    "total_liabilities": ("Total Liabilities Net Minority Interest", "Total Liabilities"),
    "retained": ("Retained Earnings",),
    "receivables": ("Accounts Receivable", "Receivables"),
    "inventory": ("Inventory",),
    "payables": ("Accounts Payable",),
    "ppe": ("Net PPE",),
    "ltd": ("Long Term Debt",),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "cfo": ("Operating Cash Flow",),
    "capex": ("Capital Expenditure",),
    "cash_dep": ("Depreciation And Amortization", "Reconciled Depreciation"),
}

# Which statement each input comes from.
_SOURCE: dict[str, str] = {
    **{k: "income" for k in ("revenue", "cogs", "gross", "sga", "ebit", "net_income", "dep")},
    **{
        k: "balance"
        for k in (
            "total_assets", "current_assets", "current_liabilities", "total_liabilities",
            "retained", "receivables", "inventory", "payables", "ppe", "ltd", "shares",
        )
    },
    **{k: "cash" for k in ("cfo", "capex", "cash_dep")},
}


def _value(statements: dict[str, Any], key: str, offset: int) -> float | None:
    """The value of an input `offset` fiscal years before the latest column."""
    payload = statements.get(_SOURCE[key])
    if not payload:
        return None
    rows = payload.get("rows", {})
    for alias in _ALIASES[key]:
        values = rows.get(alias)
        if values is not None:
            if offset < len(values):
                value = values[offset]
                if isinstance(value, (int, float)) and not isnan(value):
                    return float(value)
            return None
    return None


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


class _Year:
    """Inputs of one fiscal year, by offset from the latest."""

    def __init__(self, statements: dict[str, Any], offset: int):
        self._statements = statements
        self._offset = offset

    def __getattr__(self, key: str) -> float | None:
        value = _value(self._statements, key, self._offset)
        if key == "cogs" and value is None:
            revenue = _value(self._statements, "revenue", self._offset)
            gross = _value(self._statements, "gross", self._offset)
            if revenue is not None and gross is not None:
                return revenue - gross
        return value


def sloan_accruals(statements: dict[str, Any], offset: int = 0) -> float | None:
    y, p = _Year(statements, offset), _Year(statements, offset + 1)
    if y.net_income is None or y.cfo is None or y.total_assets is None:
        return None
    avg_assets = (
        (y.total_assets + p.total_assets) / 2 if p.total_assets is not None else y.total_assets
    )
    return _div(y.net_income - y.cfo, avg_assets)


def cash_conversion(statements: dict[str, Any], offset: int = 0) -> float | None:
    y = _Year(statements, offset)
    if y.net_income is None or y.net_income <= 0:
        return None
    return _div(y.cfo, y.net_income)


def working_capital_days(statements: dict[str, Any], offset: int = 0) -> dict[str, float | None]:
    y = _Year(statements, offset)
    dso = _div(y.receivables, y.revenue)
    dio = _div(y.inventory, y.cogs)
    dpo = _div(y.payables, y.cogs)
    dso = dso * 365 if dso is not None else None
    dio = dio * 365 if dio is not None else None
    dpo = dpo * 365 if dpo is not None else None
    ccc = None
    if dso is not None and dpo is not None:
        ccc = dso + (dio or 0.0) - dpo
    return {"dso": dso, "dio": dio, "dpo": dpo, "ccc": ccc}


def capex_to_depreciation(statements: dict[str, Any], offset: int = 0) -> float | None:
    y = _Year(statements, offset)
    if y.capex is None:
        return None
    dep = y.cash_dep if y.cash_dep is not None else y.dep
    return _div(abs(y.capex), dep)


def piotroski(statements: dict[str, Any], offset: int = 0) -> tuple[int, int] | None:
    """(score, signals evaluated) or None when fewer than five signals have inputs."""
    y, p, pp = (_Year(statements, offset + k) for k in range(3))
    signals: list[bool] = []

    def add(condition: bool | None) -> None:
        if condition is not None:
            signals.append(bool(condition))

    roa = _div(y.net_income, p.total_assets)
    roa_prior = _div(p.net_income, pp.total_assets)
    cfo_ratio = _div(y.cfo, p.total_assets)
    add(roa > 0 if roa is not None else None)
    add(cfo_ratio > 0 if cfo_ratio is not None else None)
    add(roa > roa_prior if roa is not None and roa_prior is not None else None)
    add(cfo_ratio > roa if cfo_ratio is not None and roa is not None else None)

    lev = _div(y.ltd, y.total_assets)
    lev_prior = _div(p.ltd, p.total_assets)
    add(lev < lev_prior if lev is not None and lev_prior is not None else None)
    cr = _div(y.current_assets, y.current_liabilities)
    cr_prior = _div(p.current_assets, p.current_liabilities)
    add(cr > cr_prior if cr is not None and cr_prior is not None else None)
    add(
        y.shares <= p.shares
        if y.shares is not None and p.shares is not None
        else None
    )

    gm = _div(y.revenue - y.cogs, y.revenue) if y.revenue is not None and y.cogs is not None else None
    gm_prior = (
        _div(p.revenue - p.cogs, p.revenue) if p.revenue is not None and p.cogs is not None else None
    )
    add(gm > gm_prior if gm is not None and gm_prior is not None else None)
    turnover = _div(y.revenue, p.total_assets)
    turnover_prior = _div(p.revenue, pp.total_assets)
    add(
        turnover > turnover_prior
        if turnover is not None and turnover_prior is not None
        else None
    )
    if len(signals) < 5:
        return None
    return sum(signals), len(signals)


def altman_z(
    statements: dict[str, Any], market_cap: float | None, offset: int = 0
) -> float | None:
    y = _Year(statements, offset)
    ta = y.total_assets
    if not ta or market_cap is None:
        return None
    parts = (y.current_assets, y.current_liabilities, y.retained, y.ebit, y.total_liabilities, y.revenue)
    if any(v is None for v in parts):
        return None
    wc = y.current_assets - y.current_liabilities
    mve_tl = _div(market_cap, y.total_liabilities)
    if mve_tl is None:
        return None
    return (
        1.2 * wc / ta
        + 1.4 * y.retained / ta
        + 3.3 * y.ebit / ta
        + 0.6 * mve_tl
        + 1.0 * y.revenue / ta
    )


def beneish(statements: dict[str, Any], offset: int = 0) -> tuple[float, str] | None:
    """(M-score, model) where model is '8-var' or '5-var'; None if inputs are missing."""
    y, p = _Year(statements, offset), _Year(statements, offset + 1)

    def ratio(a, b):
        return _div(a, b)

    dsri = _div(ratio(y.receivables, y.revenue), ratio(p.receivables, p.revenue))
    gm_y = _div(y.revenue - y.cogs, y.revenue) if y.revenue is not None and y.cogs is not None else None
    gm_p = _div(p.revenue - p.cogs, p.revenue) if p.revenue is not None and p.cogs is not None else None
    gmi = _div(gm_p, gm_y)

    def asset_quality(year: _Year) -> float | None:
        if year.current_assets is None or year.ppe is None or not year.total_assets:
            return None
        return 1 - (year.current_assets + year.ppe) / year.total_assets

    aqi = _div(asset_quality(y), asset_quality(p))
    sgi = _div(y.revenue, p.revenue)

    def dep_rate(year: _Year) -> float | None:
        dep = year.cash_dep if year.cash_dep is not None else year.dep
        if dep is None or year.ppe is None:
            return None
        return _div(dep, dep + year.ppe)

    depi = _div(dep_rate(p), dep_rate(y))
    five = (dsri, gmi, aqi, sgi, depi)
    if any(v is None for v in five):
        return None

    sgai = _div(ratio(y.sga, y.revenue), ratio(p.sga, p.revenue))

    def leverage(year: _Year) -> float | None:
        if year.current_liabilities is None or not year.total_assets:
            return None
        return (year.current_liabilities + (year.ltd or 0.0)) / year.total_assets

    lvgi = _div(leverage(y), leverage(p))
    tata = (
        _div(y.net_income - y.cfo, y.total_assets)
        if y.net_income is not None and y.cfo is not None
        else None
    )
    if sgai is not None and lvgi is not None and tata is not None:
        m = (
            -4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
            + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi
        )
        return m, "8-var"
    m = -6.065 + 0.823 * dsri + 0.906 * gmi + 0.593 * aqi + 0.717 * sgi + 0.107 * depi
    return m, "5-var"


BENEISH_THRESHOLDS = {"8-var": -1.78, "5-var": -2.22}


def quality_report(
    statements: dict[str, Any], market_cap: float | None = None, offset: int = 0
) -> dict[str, Any]:
    """All quality metrics for one fiscal year (offset 0 = latest)."""
    days = working_capital_days(statements, offset)
    return {
        "sloan_accruals": sloan_accruals(statements, offset),
        "cash_conversion": cash_conversion(statements, offset),
        **days,
        "capex_to_da": capex_to_depreciation(statements, offset),
        "piotroski": piotroski(statements, offset),
        # Market cap is only known today, so Z uses it for the latest year only.
        "altman_z": altman_z(statements, market_cap if offset == 0 else None, offset),
        "beneish": beneish(statements, offset),
    }
