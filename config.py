"""Configuration module for the Link2Lead B2B LinkedIn Lead Generator.

This module centralizes all application configuration: environment variable
loading, safety and pagination limits, pipeline status constants, target buyer
preset search queries, and LinkedIn profile URL canonicalization rules.

Phase 1 scope: configuration only. No database, search engine, or UI logic
lives here.
"""

from __future__ import annotations

import re
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------

# Load environment variables from a .env file located in the project root.
# Uses override=True so environment variables dynamically refresh in process memory.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

# ---------------------------------------------------------------------------
# Safety and pagination constants
# ---------------------------------------------------------------------------

# Hard tripwire limit: the maximum number of Serper.dev API queries permitted
# per rolling day. This is a safety ceiling, not a soft target.
MAX_DAILY_QUERIES: int = 95

# Maximum number of result pages to request per search run. Serper.dev
# paginates via a 1-based `page` parameter, so we never request a page
# beyond this cap.
MAX_PAGES: int = 5

# Number of results returned per Serper.dev API request.
RESULTS_PER_PAGE: int = 10

# ---------------------------------------------------------------------------
# Pipeline status constants
# ---------------------------------------------------------------------------

# A lead has been discovered but no outreach has been performed yet.
STATUS_NEW: str = "NEW"

# A direct message has been sent to the lead.
STATUS_DM_SENT: str = "DM_SENT"

# The lead has accepted a connection request.
STATUS_CONNECTED: str = "CONNECTED"

# The lead was intentionally skipped (e.g. out of scope or duplicate).
STATUS_SKIPPED: str = "SKIPPED"

# The lead has been archived and is no longer part of the active pipeline.
STATUS_ARCHIVED: str = "ARCHIVED"

# All valid pipeline statuses, used for validation and UI filtering.
ALL_STATUSES: list[str] = [
    STATUS_NEW,
    STATUS_DM_SENT,
    STATUS_CONNECTED,
    STATUS_SKIPPED,
    STATUS_ARCHIVED,
]

# ---------------------------------------------------------------------------
# Target buyer preset queries (Hardened Boolean X-Ray)
# ---------------------------------------------------------------------------

# Maps a readable buyer persona name to a hardened Google X-Ray search query.
# Each string combines explicit decision-maker titles with business entity anchors
# to filter out W-2 employees, individual contributors, and general job seekers.
TARGET_BUYER_PRESETS: dict[str, str] = {
    "Automation Agencies & No-Code Shops": (
        'site:linkedin.com/in/ ("Founder" OR "CEO" OR "Owner" OR "Agency Owner") '
        '("Automation Agency" OR "AI Automation" OR "No-Code Agency")'
    ),
    "Boutique Software & App Dev Shops": (
        'site:linkedin.com/in/ ("Founder" OR "CEO" OR "Co-Founder" OR "Owner") '
        '("Software Agency" OR "Dev Shop" OR "App Development Agency")'
    ),
    "Fractional CTOs & Tech Advisors": (
        'site:linkedin.com/in/ ("Fractional CTO" OR "Interim CTO" OR '
        '"Fractional Chief Technology Officer")'
    ),
    "Law Firm Partners & Owners": (
        'site:linkedin.com/in/ ("Managing Partner" OR "Founding Partner" OR '
        '"Law Firm Owner") ("Law Firm" OR "Legal Group")'
    ),
    "Medical Practices & Specialty Clinics": (
        'site:linkedin.com/in/ ("Practice Owner" OR "Medical Director" OR '
        '"Clinic Owner") ("Medical Practice" OR "Specialty Clinic")'
    ),
    "Dental Groups & Specialty Dentistry": (
        'site:linkedin.com/in/ ("Practice Owner" OR "Dental Practice Owner" OR '
        '"Owner Dentist") ("Dental" OR "Orthodontics")'
    ),
    "Wealth Management & RIAs": (
        'site:linkedin.com/in/ ("Managing Director" OR "Managing Partner" OR '
        '"Principal" OR "Founder") ("Wealth Management" OR "RIA")'
    ),
    "Logistics & Commercial Engineering": (
        'site:linkedin.com/in/ ("Owner" OR "President" OR "VP of Logistics" OR '
        '"Director of Supply Chain") ("Logistics" OR "Supply Chain")'
    ),
}

# ---------------------------------------------------------------------------
# LinkedIn profile URL canonicalization
# ---------------------------------------------------------------------------

# Matches a LinkedIn profile URL and captures the profile handle.
LINKEDIN_PROFILE_RE: re.Pattern[str] = re.compile(
    r"^https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/([A-Za-z0-9\-_]+)"
)

# The canonical base URL used to rebuild a standardized profile URL.
LINKEDIN_PROFILE_BASE_URL: str = "https://www.linkedin.com/in/"


def canonicalize_linkedin_url(url: str) -> str | None:
    """Canonicalize a LinkedIn profile URL to a standard form.

    Strips tracking parameters (e.g. ``?query=...``), regional subdomains
    (e.g. ``uk.linkedin.com``), and trailing slashes, standardizing the URL
    to ``https://www.linkedin.com/in/<handle>/``.

    Args:
        url: The raw LinkedIn profile URL to canonicalize.

    Returns:
        The canonicalized profile URL, or ``None`` if the input is not a
        recognizable LinkedIn profile URL.
    """
    if not url:
        return None

    match = LINKEDIN_PROFILE_RE.match(url.strip())
    if not match:
        return None

    handle = match.group(1)
    return f"{LINKEDIN_PROFILE_BASE_URL}{handle}/"