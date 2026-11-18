"""SEC EDGAR provider: recent filings per ticker.

The first non-yfinance implementation wired through the capability registry
(see data_providers.base): the filings capability is served by EDGAR while
yfinance keeps serving quotes, info, history and news.

EDGAR requires an identifying User-Agent and expects modest request rates;
both the tickers file and submissions documents are cached with a TTL, and
unknown or non-US tickers return an empty list rather than an error.
"""

import logging
import re
import time
from typing import Any

import requests

from stockstui.data_providers.base import record_outcome

EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik:010d}/{accession}/{document}"

# EDGAR fair-use: requests must carry a descriptive UA declaring the
# software and an administrative contact email, and stay well below 10 req/s.
# The SEC answers 403 ("Undeclared Automated Tool") to a UA without an email,
# so no request is sent until a contact is configured (settings.json
# "sec_contact" or the STOCKSTUI_SEC_CONTACT environment variable).
# A shared session plus TTL caches keep this to a handful of calls per
# ticker per hour in normal use.
_contact: str = ""

_SUBMISSIONS_CACHE_TTL_SECONDS = 3600  # 1 hour; filings update slowly
_TICKERS_CACHE_TTL_SECONDS = 86400  # 24 hours; the mapping rarely changes

# Forms surfaced by default in the Filings view, in display order.
FILING_FORM_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Annual (10-K)": ("10-K", "10-K/A"),
    "Quarterly (10-Q)": ("10-Q", "10-Q/A"),
    "Current (8-K)": ("8-K", "8-K/A"),
    "Insider (4/144)": ("4", "4/A", "144"),
    "Proxy (DEF 14A)": ("DEF 14A", "DEFA14A"),
    "Registration": ("S-1", "S-1/A", "S-3", "S-3/A", "424B2", "FWP", "S-8"),
}

_submissions_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_tickers_cache: dict[str, tuple[float, dict[str, int]]] = {}
_session: requests.Session | None = None

# Filter list used by the view when no category is selected: the common
# filing types investors track. Other forms are still fetched and can be
# shown by picking a category.
DEFAULT_VISIBLE_FORMS: frozenset[str] = frozenset(
    {
        form
        for forms in FILING_FORM_CATEGORIES.values()
        for form in forms
    }
)


class EdgarError(Exception):
    """An EDGAR request could not be made or failed; the message is user-facing."""


class EdgarContactMissing(EdgarError):
    """No SEC contact email is configured, so no request is sent."""


CONTACT_MISSING_MESSAGE = (
    "The SEC requires a contact email in every request. Set it under"
    " Configs > General > SEC Contact (or STOCKSTUI_SEC_CONTACT)."
)


def _user_agent() -> str:
    return f"stocksTUI terminal app ({_contact})"


def _get_session() -> requests.Session:
    global _session
    if not _contact:
        raise EdgarContactMissing(CONTACT_MISSING_MESSAGE)
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": _user_agent()})
    return _session


def is_valid_contact(contact: str) -> bool:
    """True when the string contains something shaped like an email address."""
    return bool(re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact or ""))


def set_contact(contact: str) -> None:
    """Sets the SEC contact (name and email) used in the User-Agent header."""
    global _contact
    contact = (contact or "").strip()
    _contact = contact if is_valid_contact(contact) else ""
    if _session is not None:
        _session.headers.update({"User-Agent": _user_agent()})


def has_contact() -> bool:
    return bool(_contact)


# Backwards-compatible name.
set_user_agent = set_contact


def _raise_for_status(response: requests.Response) -> None:
    if response.status_code == 403:
        raise EdgarError(
            "SEC rejected the request (403). Check that the SEC contact"
            " setting holds a real email address."
        )
    response.raise_for_status()


def reset_caches() -> None:
    """Clears module caches (used by tests and forced refreshes)."""
    global _session
    _submissions_cache.clear()
    _tickers_cache.clear()
    _session = None


def _load_ticker_to_cik() -> dict[str, int]:
    """Loads the SEC ticker->CIK mapping, cached for a day."""
    now = time.time()
    cached = _tickers_cache.get("map")
    if cached and now - cached[0] < _TICKERS_CACHE_TTL_SECONDS:
        return cached[1]
    response = _get_session().get(EDGAR_TICKERS_URL, timeout=15)
    _raise_for_status(response)
    mapping: dict[str, int] = {}
    for entry in response.json().values():
        mapping[entry["ticker"].upper()] = int(entry["cik_str"])
    _tickers_cache["map"] = (now, mapping)
    return mapping


def get_cik_for_ticker(ticker: str) -> int | None:
    """
    Resolves a ticker to its SEC CIK, or None for non-SEC tickers.

    Raises EdgarError when the mapping cannot be loaded, so an outage is not
    mistaken for (and cached as) a ticker EDGAR does not cover.
    """
    try:
        return _load_ticker_to_cik().get(ticker.upper())
    except EdgarError:
        raise
    except (requests.RequestException, KeyError, ValueError) as e:
        logging.warning(f"Failed to load EDGAR ticker mapping: {e}")
        raise EdgarError(f"Could not reach SEC EDGAR: {e}") from e


def fetch_filings(ticker: str, force_refresh: bool = False) -> list[dict[str, Any]]:
    """
    Returns recent filings for a ticker, newest first, capped at 100.

    Each row has form, filing_date, report_date, description, and url (the
    primary document on sec.gov). Non-SEC tickers yield []. A missing
    contact or an EDGAR failure with nothing cached raises EdgarError; with a
    cached list, the stale list is served instead.
    """
    normalized = ticker.upper()
    now = time.time()
    cached = _submissions_cache.get(normalized)
    if cached and not force_refresh and now - cached[0] < _SUBMISSIONS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        cik = get_cik_for_ticker(normalized)
        if cik is None:
            # Non-US / non-SEC ticker: an empty state, not an error (per #10).
            _submissions_cache[normalized] = (now, [])
            return []
        response = _get_session().get(EDGAR_SUBMISSIONS_URL.format(cik=cik), timeout=15)
        _raise_for_status(response)
        recent = response.json()["filings"]["recent"]
    except (EdgarError, requests.RequestException, KeyError, ValueError) as e:
        logging.warning(f"Failed to fetch EDGAR submissions for {normalized}: {e}")
        record_outcome(
            "filings", ok=False, provider="edgar", detail=f"{normalized}: {e}",
            fallback_used=bool(cached),
        )
        # Serve stale cache on failure rather than losing the list.
        if cached:
            return cached[1]
        if isinstance(e, EdgarError):
            raise
        raise EdgarError(f"Could not fetch filings from SEC EDGAR: {e}") from e

    filings: list[dict[str, Any]] = []
    for pos in range(len(recent.get("form", []))):
        form = str(recent["form"][pos])
        document = recent["primaryDocument"][pos] or ""
        accession = recent["accessionNumber"][pos] or ""
        accession_nodash = accession.replace("-", "")
        filings.append(
            {
                "form": form,
                "filing_date": recent["filingDate"][pos],
                "report_date": recent["reportDate"][pos] or "",
                "description": (
                    recent.get("primaryDocDescription", [""] * (pos + 1))[pos] or ""
                ),
                "url": (
                    FILING_INDEX_URL.format(
                        cik=cik,
                        accession=accession_nodash,
                        document=document,
                    )
                    if document and accession
                    else None
                ),
            }
        )
        if len(filings) >= 100:
            break
    _submissions_cache[normalized] = (now, filings)
    record_outcome("filings", ok=True, provider="edgar", detail=f"{normalized}: {len(filings)} filings")
    return filings


def filter_filings_by_form(
    filings: list[dict[str, Any]], category: str | None
) -> list[dict[str, Any]]:
    """
    Filters filings by a FILING_FORM_CATEGORIES key; None shows the default
    investor-facing forms, 'All' shows everything.
    """
    if category == "All":
        return filings
    if category is None:
        return [f for f in filings if f["form"] in DEFAULT_VISIBLE_FORMS]
    forms = FILING_FORM_CATEGORIES.get(category, ())
    return [f for f in filings if f["form"] in forms]
