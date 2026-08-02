"""Search engine layer for the Link2Lead B2B LinkedIn Lead Generator.

This module executes Google X-Ray searches via the Serper.dev API, parses the
raw profile results into normalized lead records, and cross-references every
discovered URL against the local SQLite database to report any existing
pipeline status.

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


# ---------------------------------------------------------------------------
# Core search execution
# ---------------------------------------------------------------------------


def execute_xray_search(
    query_key: str,
    raw_query: str,
    target_category: str,
    num_pages: int = 1,
) -> dict[str, object]:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
    """Execute a batched Google X-Ray search and return parsed leads.

    The function validates the Serper.dev API key and the daily query budget,
    resumes from the persisted pagination page, fetches up to ``num_pages``
    pages of ten results each, canonicalizes every discovered LinkedIn URL,
    parses the profile metadata, and cross-references each URL against the
    local lead database.

    Args:
        query_key: Unique identifier used to track the pagination page in
            ``search_state``.
        raw_query: The Google X-Ray search query string.
        target_category: Category label applied to every parsed result.
        num_pages: Number of 10-result pages to fetch. Coerced to an int and
            clamped to the inclusive range 1..5.

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

    # Page check ------------------------------------------------------------
    start_page = db.get_search_offset(query_key)
    parsed_items: list[dict[str, object]] = []
    current_page = start_page

    headers = {
        "X-API-KEY": api_key.strip(),
        "Content-Type": "application/json",
    }

    for _ in range(page_count):
        if daily_usage >= MAX_DAILY_QUERIES:
            break

        payload = {
            "q": raw_query,
            "num": RESULTS_PER_PAGE,
            "page": current_page,
        }

        # API execution -----------------------------------------------------
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

        # Telemetry and state ----------------------------------------------
        daily_usage = db.increment_daily_usage(today)
        current_page += 1
        db.update_search_offset(query_key, current_page)

        # Parse and cross-reference results --------------------------------
        organic_results = data.get("organic", [])
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
