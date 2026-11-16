"""Provider interface and capability registry for market data.

Market data is delivered through small capabilities (quotes, info, history,
news, options). yfinance — implemented by the `market_provider` and
`options_provider` modules — serves every capability today. Alternative
implementations (EDGAR for statements and filings, FRED for macro series,
the local bar store for history) can take over individual capabilities as
they are built; when the primary provider fails, callers keep the data
already held in the in-memory caches.

Implementations record the outcome of each network fetch via `record_outcome`;
the Debug tab's "Data Sources" panel reads `get_provider_report` to show which
provider serves each capability and when a fallback was used.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol


class MarketDataProvider(Protocol):
    """Contract for one market data implementation.

    Satisfied function-for-function by
    `stockstui.data_providers.market_provider` today. Future implementations
    should fulfil the same methods so they can replace yfinance per capability
    without touching callers.
    """

    def get_market_price_data(
        self,
        tickers: list[str],
        force_refresh: bool = False,
        enable_pre_post_market: bool = False,
    ) -> list[dict]: ...

    def get_ticker_info(self, ticker: str) -> dict | None: ...

    def get_historical_data(self, ticker: str, period: str, interval: str = "1d"): ...

    def get_news_for_tickers(self, tickers: list[str]) -> list[dict] | None: ...

    def get_market_status(self, calendar_name: str = "NYSE") -> dict: ...


# Display names for the Debug tab's "Data Sources" panel, in panel order.
CAPABILITIES: dict[str, str] = {
    "quotes": "Quotes & metadata (slow fetch)",
    "quotes_live": "Live quotes (batch download)",
    "info": "Ticker info lookups",
    "history": "Historical bars",
    "news": "News feed",
    "options": "Options chains & expirations",
    "filings": "SEC filings (EDGAR)",
}

# The implementation currently serving each capability. A future provider
# takes over a capability by replacing its entry here; EDGAR already serves
# filings alongside yfinance.
SERVING_PROVIDER: dict[str, str] = {
    key: ("edgar" if key == "filings" else "yfinance") for key in CAPABILITIES
}

# What serves the data when the primary provider fails, per capability.
FALLBACKS: dict[str, str] = {
    "quotes": "in-memory price cache",
    "quotes_live": "in-memory price cache",
    "info": "none",
    "history": "local daily bar store",
    "news": "stale cache",
    "options": "TTL cache",
    "filings": "TTL cache",
}


@dataclass
class ProviderOutcome:
    provider: str
    ok: bool
    detail: str
    fallback_used: bool
    recorded_at: float


_outcomes: dict[str, ProviderOutcome] = {}
_lock = threading.Lock()


def record_outcome(
    capability: str,
    *,
    ok: bool,
    provider: str = "yfinance",
    detail: str = "",
    fallback_used: bool = False,
) -> None:
    """Records the result of one provider fetch for the Debug tab."""
    if capability not in CAPABILITIES:
        logging.warning(f"Outcome recorded for unknown capability '{capability}'")
        return
    with _lock:
        _outcomes[capability] = ProviderOutcome(
            provider=provider,
            ok=ok,
            detail=detail[:160],
            fallback_used=fallback_used,
            recorded_at=time.time(),
        )


def get_provider_report() -> list[dict]:
    """Builds the Debug tab rows: one per capability with its last outcome."""
    now = time.time()
    with _lock:
        snapshot = dict(_outcomes)

    rows = []
    for key, description in CAPABILITIES.items():
        outcome = snapshot.get(key)
        if outcome is None:
            status, detail, age = "idle", "", None
        else:
            status = (
                "ok" if outcome.ok else ("fallback" if outcome.fallback_used else "error")
            )
            detail = outcome.detail
            age = now - outcome.recorded_at
        rows.append(
            {
                "capability": key,
                "description": description,
                "provider": SERVING_PROVIDER[key],
                "status": status,
                "detail": detail,
                "age_seconds": age,
                "fallback": FALLBACKS[key],
            }
        )
    return rows
