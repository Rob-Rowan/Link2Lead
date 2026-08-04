"""Search engine layer for the Link2Lead B2B LinkedIn Lead Generator.

This module executes Google X-Ray searches via the Serper.dev API, parses the
raw profile results into normalized lead records, and cross-references every
discovered URL against the local SQLite database to report any existing
pipeline status.

In addition to person-first profile searches, this module implements Account
Discovery Mode (Company-First Prospecting): company X-Ray searches via
``execute_company_xray_search`` and decision-maker sub-searches via
``find_company_founder``.

Phase 3 scope: search engine layer only. No UI logic lives here.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import requests

import db
from config import MAX_DAILY_QUERIES, RESULTS_PER_PAGE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Serper.dev Google Search API endpoint.
_SERPER_ENDPOINT: str = "https://google.serper.dev/search"

# Request timeout in seconds for every outbound API call.
_REQUEST_TIMEOUT_SECONDS: int = 15

# Maximum length of the truncated snippet excerpt used as a location
# fallback when no clean location clue can be inferred.
_SNIPPET_EXCERPT_LIMIT: int = 120

# Matches a trailing "| LinkedIn" or "- LinkedIn" suffix (case-insensitive)
# so it can be stripped from raw profile titles.
_LINKEDIN_SUFFIX_RE: re.Pattern[str] = re.compile(
    r"\s*(?:\||-)\s*LinkedIn\s*$", flags=re.IGNORECASE
)

_HTML_TAG_RE: re.Pattern[str] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")

# Matches a "City, ST" location pair, e.g. "San Francisco, CA".
_CITY_STATE_RE: re.Pattern[str] = re.compile(
    r"\b((?:[A-Z][A-Za-z]+)(?: [A-Z][A-Za-z]+){0,3}),\s*([A-Z]{2})\b"
)

# Matches a "Greater ... Area" phrase, e.g. "Greater London Area".
_GREATER_AREA_RE: re.Pattern[str] = re.compile(
    r"\bGreater\s+([A-Z][A-Za-z]+)(?:\s+Area)?\b"
)

# Known country / region keywords that may appear in snippets.
_KNOWN_REGIONS: tuple[str, ...] = (
    "United States",
    "United Kingdom",
    "USA",
    "U.S.",
    "UK",
    "Canada",
    "Australia",
    "Germany",
    "France",
    "Spain",
    "India",
    "Singapore",
    "Netherlands",
    "Remote",
)

# Precompiled word-boundary matcher for the known region keywords.
_KNOWN_REGION_RE: re.Pattern[str] = re.compile(
    r"\b(?:" + "|".join(re.escape(region) for region in _KNOWN_REGIONS)
    + r")\b"
)

# Matches a LinkedIn company page URL and captures the company handle.
_LINKEDIN_COMPANY_RE: re.Pattern[str] = re.compile(
    r"^https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/company/([A-Za-z0-9\-_]+)"
)

# The canonical base URL used to rebuild a standardized company page URL.
_LINKEDIN_COMPANY_BASE_URL: str = "https://www.linkedin.com/company/"

# Matches an employee headcount marker such as "2-10 employees",
# "51-200 employees", or "10,001+ employees".
_HEADCOUNT_RE: re.Pattern[str] = re.compile(
    r"\b(\d[\d,]*(?:\s*[-+]\s*\d[\d,]*)?)\s+employees?\b",
    flags=re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SearchEngineError(Exception):
    """Raised when the Serper.dev API fails or is misconfigured."""


# Backward-compatible alias so existing callers referencing GoogleSearchError
# continue to work.
GoogleSearchError = SearchEngineError


class DailyLimitExceededError(Exception):
    """Raised when the daily query budget has been fully consumed."""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_profile_title(raw_title: str) -> tuple[str, str]:
    """Split a dirty Google X-Ray result title into name and headline.

    Google X-Ray titles look like ``John Doe - Co-Founder & CTO at TechCorp |
    LinkedIn``. The trailing ``| LinkedIn`` (or ``- LinkedIn``) suffix is
    removed first, then the title is split on the first hyphen or pipe. The
    left segment becomes the profile name and the remaining segment becomes
    the headline. If no delimiter exists, the entire string is treated as the
    name and the headline defaults to an empty string.

    Args:
        raw_title: The raw title string pulled from a search result.

    Returns:
        A ``(name, headline)`` tuple with both values stripped of
        surrounding whitespace.
    """
    if not raw_title:
        return "", ""

    cleaned = _LINKEDIN_SUFFIX_RE.sub("", raw_title.strip())

    first_hyphen = cleaned.find("-")
    first_pipe = cleaned.find("|")
    positions = [
        position
        for position in (first_hyphen, first_pipe)
        if position != -1
    ]
    if not positions:
        return cleaned, ""

    split_at = min(positions)
    name = cleaned[:split_at].strip()
    headline = cleaned[split_at + 1:].strip()
    return name, headline


def parse_location_from_snippet(snippet: str) -> str:
    """Extract a best-effort location string from a search result snippet.

    The snippet is scrubbed of HTML tags and collapsed whitespace before a
    series of heuristic patterns are applied: ``City, ST`` pairs, known
    region keywords, and ``Greater ... Area`` phrases. If no clean location
    can be inferred, a truncated excerpt of the snippet is returned. An
    empty or unusable snippet falls back to ``"Unknown / See Profile"``.

    Args:
        snippet: The raw snippet text from a search result.

    Returns:
        A human-readable location string, a truncated snippet excerpt, or
        ``"Unknown / See Profile"``.
    """
    if not snippet:
        return "Unknown / See Profile"

    text = _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", snippet)).strip()
    if not text:
        return "Unknown / See Profile"

    city_state = _CITY_STATE_RE.search(text)
    if city_state:
        return f"{city_state.group(1)}, {city_state.group(2)}"

    greater_area = _GREATER_AREA_RE.search(text)
    if greater_area:
        return f"Greater {greater_area.group(1)} Area"

    # A concrete location keyword (e.g. "Singapore" or "United States")
    # outranks the generic "Remote" marker, which is used only as a
    # last-resort location clue when nothing more specific appears.
    region_matches = _KNOWN_REGION_RE.findall(text)
    concrete_regions = [match for match in region_matches if match != "Remote"]
    if concrete_regions:
        return concrete_regions[0]

    if "Remote" in region_matches:
        return "Remote"

    if len(text) > _SNIPPET_EXCERPT_LIMIT:
        return text[:_SNIPPET_EXCERPT_LIMIT].rsplit(" ", 1)[0] + "..."

    return text


def _canonicalize_company_url(url: str) -> str | None:
    """Canonicalize a LinkedIn company page URL to a standard form.

    Accepts raw LinkedIn company URLs (e.g.
    ``https://uk.linkedin.com/company/acme-corp?trk=...`` or
    ``linkedin.com/company/acme-corp``), strips query parameters, removes
    regional subdomains and trailing slashes, and returns a uniform format:
    ``https://www.linkedin.com/company/<handle>/``.

    Args:
        url: The raw LinkedIn company page URL to canonicalize.

    Returns:
        The canonicalized company page URL, or ``None`` if the input is not a
        recognizable LinkedIn company page URL.
    """
    if not url:
        return None

    stripped = url.strip()

    # The company regex requires a scheme. Accept scheme-less inputs such as
    # "linkedin.com/company/acme-corp" by normalizing them to https first.
    if not stripped.startswith(("http://", "https://")):
        stripped = f"https://{stripped}"

    match = _LINKEDIN_COMPANY_RE.match(stripped)
    if not match:
        return None

    handle = match.group(1)
    return f"{_LINKEDIN_COMPANY_BASE_URL}{handle}/"


def _parse_headcount(text: str) -> str:
    """Extract a best-effort employee headcount string from a snippet.

    Searches the scrubbed snippet text for a headcount marker such as
    ``2-10 employees``, ``11-50 employees``, or ``10,001+ employees`` and
    returns the matched span. If no recognizable marker exists, returns
    ``"Unknown"``.

    Args:
        text: Raw snippet text, which is HTML-scrubbed and whitespace-
            collapsed before matching.

    Returns:
        A human-readable headcount string such as ``"2-10 employees"``, or
        ``"Unknown"``.
    """
    if not text:
        return "Unknown"

    cleaned = _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()
    match = _HEADCOUNT_RE.search(cleaned)
    if not match:
        return "Unknown"

    return f"{match.group(1).strip()} employees"


def _parse_company_title(raw_title: str) -> str:
    """Extract a clean company name from a LinkedIn company page title.

    Google X-Ray company titles look like ``Acme Corp | LinkedIn`` or
    ``Acme Corp - Automation Agency | LinkedIn``. The trailing ``| LinkedIn``
    (or ``- LinkedIn``) suffix is removed and the remaining string is taken as
    the company name.

    Args:
        raw_title: The raw title string pulled from a search result.

    Returns:
        The cleaned company name, or an empty string if the input was empty.
    """
    if not raw_title:
        return ""

    cleaned = _LINKEDIN_SUFFIX_RE.sub("", raw_title.strip())
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Core search execution
# ---------------------------------------------------------------------------


def execute_xray_search(
    query_key: str,
    raw_query: str,
    target_category: str,
    num_pages: int = 1,
    country_code: str = "us",
) -> dict[str, object]:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    """Execute a batched Google X-Ray search and return parsed leads.

    The function validates the Serper.dev API key and the daily query budget,
    resumes from the persisted pagination page, fetches up to ``num_pages``
    pages of ten results each, canonicalizes every discovered LinkedIn URL,
    parses the profile metadata, and cross-references each URL against the
    local lead database. The ``gl`` geolocation parameter biases results
    toward the selected target country.

    Args:
        query_key: Unique identifier used to track the pagination page in
            ``search_state``.
        raw_query: The Google X-Ray search query string.
        target_category: Category label applied to every parsed result.
        num_pages: Number of 10-result pages to fetch. Coerced to an int and
            clamped to the inclusive range 1..5.
        country_code: Serper.dev ``gl`` geolocation code used to bias results
            toward a target country. Defaults to ``"us"``.

    Returns:
        A dictionary with the parsed results list and telemetry:

        * ``results``: list of lead dicts, each containing ``linkedin_url``,
          ``name``, ``headline``, ``target_category``, ``location``, and
          ``existing_status``.
        * ``raw_hits``: number of successfully parsed results.
        * ``daily_usage_count``: updated daily query count.
        * ``exhausted``: ``True`` only when the maximum page cap has been
          reached, in which case ``results`` is empty and a ``message``
          key explains the state.

    Raises:
        SearchEngineError: If the API key is missing or a request to the
            Serper.dev API fails.
        DailyLimitExceededError: If the daily query count already meets or
            exceeds ``config.MAX_DAILY_QUERIES``.
    """
    try:
        page_count = min(max(int(num_pages), 1), 5)
    except (TypeError, ValueError):
        page_count = 1

    # Environment check -----------------------------------------------------
    api_key = os.getenv("SERPER_API_KEY")
    if (
        not api_key
        or api_key.strip().lower() in {"your_serper_api_key_here", ""}
    ):
        raise SearchEngineError("Missing SERPER_API_KEY in .env file.")

    # Daily usage check -----------------------------------------------------
    today = datetime.now().strftime("%Y-%m-%d")
    daily_usage = db.get_daily_usage(today)
    if daily_usage >= MAX_DAILY_QUERIES:
        raise DailyLimitExceededError(
            f"Daily query limit of {MAX_DAILY_QUERIES} reached."
        )

    start_page = db.get_search_offset(query_key)
    parsed_items: list[dict[str, object]] = []

    headers = {
        "X-API-KEY": api_key.strip(),
        "Content-Type": "application/json",
    }

    # Request up to 50 results (5 pages worth) in 1 HTTP call = 1 Serper credit
    requested_num_results = page_count * 10

    payload = {
        "q": raw_query,
        "gl": country_code,
        "num": requested_num_results,
        "page": start_page,
    }

    # API execution (Single HTTP Request) -----------------------------------
    try:
        response = requests.post(
            _SERPER_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        raise SearchEngineError(
            f"Request to Serper API failed: {exc}"
        ) from exc

    if response.status_code != 200:
        raise SearchEngineError(
            f"Serper API returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    # Telemetry and state (Increment ONCE per search action) ----------------
    daily_usage = db.increment_daily_usage(today)

    # Parse and cross-reference results ------------------------------------
    organic_results = data.get("organic", [])

    # IF Serper returns 0 results on an advanced offset page, reset state to Page 1
    if not organic_results:
        if start_page > 1:
            db.update_search_offset(query_key, 1)
            return {
                "results": [],
                "raw_hits": 0,
                "daily_usage_count": daily_usage,
                "exhausted": True,
                "message": "Reached the end of Google's index for this query. Search offset has been automatically reset to Page 1 for your next run.",
            }
        else:
            return {
                "results": [],
                "raw_hits": 0,
                "daily_usage_count": daily_usage,
                "exhausted": True,
                "message": "No organic LinkedIn profiles found matching this query.",
            }

    # If results were successfully retrieved, advance offset for the next run
    db.update_search_offset(query_key, start_page + 1)

    for item in organic_results:
        raw_link = str(item.get("link", "") or "")
        raw_title = str(item.get("title", "") or "")
        snippet = str(item.get("snippet", "") or "")

        if not raw_link:
            continue

        try:
            canonical_url = db.canonicalize_linkedin_url(raw_link)
        except Exception:
            canonical_url = None

        if not canonical_url:
            continue

        name, headline = parse_profile_title(raw_title)
        location = parse_location_from_snippet(snippet)
        existing_status = db.check_lead_status(canonical_url)

        parsed_items.append(
            {
                "linkedin_url": canonical_url,
                "name": name,
                "headline": headline,
                "target_category": target_category,
                "location": location,
                "existing_status": existing_status,
            }
        )

    return {
        "results": parsed_items,
        "raw_hits": len(parsed_items),
        "daily_usage_count": daily_usage,
        "exhausted": False,
    }


# ---------------------------------------------------------------------------
# Account Discovery Mode (Company-First Prospecting)
# ---------------------------------------------------------------------------


def execute_company_xray_search(
    query_key: str,
    raw_query: str,
    num_pages: int = 1,
    country_code: str = "us",
) -> dict[str, object]:
    """Execute a company-first Google X-Ray search and return parsed companies.

    Runs a Serper.dev search using a company preset from
    ``config.TARGET_COMPANY_PRESETS`` (e.g. ``site:linkedin.com/company/ ...``),
    filters the organic results to LinkedIn company page URLs, and parses each
    company name, description, location, and headcount from the title and
    snippet. Daily usage accounting and the persisted ``search_state``
    pagination resume/advance logic mirror ``execute_xray_search``.

    Args:
        query_key: Unique identifier used to track the pagination page in
            ``search_state``.
        raw_query: The Google X-Ray company search query string.
        num_pages: Number of 10-result pages to fetch. Coerced to an int and
            clamped to the inclusive range 1..5.
        country_code: Serper.dev ``gl`` geolocation code used to bias results
            toward a target country. Defaults to ``"us"``.

    Returns:
        A dictionary with the parsed results list and telemetry:

        * ``results``: list of company dicts, each containing
          ``company_name``, ``company_url``, ``description``, ``location``,
          and ``headcount``.
        * ``raw_hits``: number of successfully parsed company results.
        * ``daily_usage_count``: updated daily query count.
        * ``exhausted``: ``True`` only when the maximum page cap has been
          reached, in which case ``results`` is empty and a ``message``
          key explains the state.

    Raises:
        SearchEngineError: If the API key is missing or a request to the
            Serper.dev API fails.
        DailyLimitExceededError: If the daily query count already meets or
            exceeds ``config.MAX_DAILY_QUERIES``.
    """
    try:
        page_count = min(max(int(num_pages), 1), 5)
    except (TypeError, ValueError):
        page_count = 1

    # Environment check -----------------------------------------------------
    api_key = os.getenv("SERPER_API_KEY")
    if (
        not api_key
        or api_key.strip().lower() in {"your_serper_api_key_here", ""}
    ):
        raise SearchEngineError("Missing SERPER_API_KEY in .env file.")

    # Daily usage check -----------------------------------------------------
    today = datetime.now().strftime("%Y-%m-%d")
    daily_usage = db.get_daily_usage(today)
    if daily_usage >= MAX_DAILY_QUERIES:
        raise DailyLimitExceededError(
            f"Daily query limit of {MAX_DAILY_QUERIES} reached."
        )

    start_page = db.get_search_offset(query_key)
    parsed_items: list[dict[str, object]] = []

    headers = {
        "X-API-KEY": api_key.strip(),
        "Content-Type": "application/json",
    }

    # Request up to 50 results (5 pages worth) in 1 HTTP call = 1 Serper credit
    requested_num_results = page_count * 10

    payload = {
        "q": raw_query,
        "gl": country_code,
        "num": requested_num_results,
        "page": start_page,
    }

    # API execution (Single HTTP Request) -----------------------------------
    try:
        response = requests.post(
            _SERPER_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        raise SearchEngineError(
            f"Request to Serper API failed: {exc}"
        ) from exc

    if response.status_code != 200:
        raise SearchEngineError(
            f"Serper API returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    # Telemetry and state (Increment ONCE per search action) ----------------
    daily_usage = db.increment_daily_usage(today)

    # Parse results ---------------------------------------------------------
    organic_results = data.get("organic", [])

    # IF Serper returns 0 results on an advanced offset page, reset state to Page 1
    if not organic_results:
        if start_page > 1:
            db.update_search_offset(query_key, 1)
            return {
                "results": [],
                "raw_hits": 0,
                "daily_usage_count": daily_usage,
                "exhausted": True,
                "message": "Reached the end of Google's index for this query. Search offset has been automatically reset to Page 1 for your next run.",
            }
        else:
            return {
                "results": [],
                "raw_hits": 0,
                "daily_usage_count": daily_usage,
                "exhausted": True,
                "message": "No LinkedIn company pages found matching this query.",
            }

    # If results were successfully retrieved, advance offset for the next run
    db.update_search_offset(query_key, start_page + 1)

    for item in organic_results:
        raw_link = str(item.get("link", "") or "")
        raw_title = str(item.get("title", "") or "")
        snippet = str(item.get("snippet", "") or "")

        if not raw_link:
            continue

        canonical_url = _canonicalize_company_url(raw_link)
        if not canonical_url:
            continue

        company_name = _parse_company_title(raw_title)

        # The scrubbed snippet is the company's description text from
        # LinkedIn (tagline / about blurb). Location is parsed separately.
        snippet_clean = (
            _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", snippet)).strip()
            if snippet
            else ""
        )
        description = snippet_clean
        location = parse_location_from_snippet(snippet)
        if location in ("Unknown / See Profile", "Remote"):
            location = "Unknown"
        headcount = _parse_headcount(snippet)

        parsed_items.append(
            {
                "company_name": company_name or raw_title or "Unknown Company",
                "company_url": canonical_url,
                "description": description,
                "location": location,
                "headcount": headcount,
            }
        )

    return {
        "results": parsed_items,
        "raw_hits": len(parsed_items),
        "daily_usage_count": daily_usage,
        "exhausted": False,
    }


def find_company_founder(
    company_name: str,
    country_code: str = "us",
) -> dict[str, object]:
    """Execute a targeted founder sub-search for a discovered company.

    Runs a single-page Serper.dev search using the query
    ``site:linkedin.com/in/ "{company_name}" ("Founder" OR "CEO" OR
    "Co-Founder" OR "CTO" OR "Owner")``, parses the resulting LinkedIn profile
    records, and cross-references each URL against the local lead database to
    surface any existing pipeline status.

    Args:
        company_name: The company name to target in the founder sub-search.
        country_code: Serper.dev ``gl`` geolocation code used to bias results
            toward a target country. Defaults to ``"us"``.

    Returns:
        A dictionary with the parsed results list and telemetry:

        * ``results``: list of founder profile dicts, each containing
          ``linkedin_url``, ``name``, ``headline``, ``location``, and
          ``existing_status``.
        * ``raw_hits``: number of successfully parsed founder results.
        * ``daily_usage_count``: updated daily query count.
        * ``exhausted``: ``True`` only when no organic LinkedIn profiles were
          returned, in which case ``results`` is empty and a ``message``
          key explains the state.

    Raises:
        SearchEngineError: If the API key is missing or a request to the
            Serper.dev API fails.
        DailyLimitExceededError: If the daily query count already meets or
            exceeds ``config.MAX_DAILY_QUERIES``.
    """
    # Environment check -----------------------------------------------------
    api_key = os.getenv("SERPER_API_KEY")
    if (
        not api_key
        or api_key.strip().lower() in {"your_serper_api_key_here", ""}
    ):
        raise SearchEngineError("Missing SERPER_API_KEY in .env file.")

    # Daily usage check -----------------------------------------------------
    today = datetime.now().strftime("%Y-%m-%d")
    daily_usage = db.get_daily_usage(today)
    if daily_usage >= MAX_DAILY_QUERIES:
        raise DailyLimitExceededError(
            f"Daily query limit of {MAX_DAILY_QUERIES} reached."
        )

    # Query construction ----------------------------------------------------
    escaped_name = company_name.replace('"', '\\"')
    raw_query = (
        f'site:linkedin.com/in/ "{escaped_name}" '
        '("Founder" OR "CEO" OR "Co-Founder" OR "CTO" OR "Owner")'
    )

    headers = {
        "X-API-KEY": api_key.strip(),
        "Content-Type": "application/json",
    }

    payload = {
        "q": raw_query,
        "gl": country_code,
        "num": 10,
        "page": 1,
    }

    # API execution (Single HTTP Request) -----------------------------------
    try:
        response = requests.post(
            _SERPER_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        raise SearchEngineError(
            f"Request to Serper API failed: {exc}"
        ) from exc

    if response.status_code != 200:
        raise SearchEngineError(
            f"Serper API returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    data = response.json()

    # Telemetry and state (Increment ONCE per search action) ----------------
    daily_usage = db.increment_daily_usage(today)

    organic_results = data.get("organic", [])

    if not organic_results:
        return {
            "results": [],
            "raw_hits": 0,
            "daily_usage_count": daily_usage,
            "exhausted": True,
            "message": "No founder profiles found for this company.",
        }

    parsed_items: list[dict[str, object]] = []

    for item in organic_results:
        raw_link = str(item.get("link", "") or "")
        raw_title = str(item.get("title", "") or "")
        snippet = str(item.get("snippet", "") or "")

        if not raw_link:
            continue

        try:
            canonical_url = db.canonicalize_linkedin_url(raw_link)
        except Exception:
            canonical_url = None

        if not canonical_url:
            continue

        name, headline = parse_profile_title(raw_title)
        location = parse_location_from_snippet(snippet)
        existing_status = db.check_lead_status(canonical_url)

        parsed_items.append(
            {
                "linkedin_url": canonical_url,
                "name": name,
                "headline": headline,
                "location": location,
                "existing_status": existing_status,
            }
        )

    return {
        "results": parsed_items,
        "raw_hits": len(parsed_items),
        "daily_usage_count": daily_usage,
        "exhausted": len(parsed_items) == 0,
    }
