"""Database layer for the Link2Lead B2B LinkedIn Lead Generator.

This module provides all SQLite persistence for the application: lead storage,
pipeline status tracking, search pagination state, and daily query usage
accounting. It also exposes a pure URL canonicalization helper used to
normalize LinkedIn profile URLs before they are stored.

Phase 2 scope: database layer only. No search engine or UI logic lives here.
"""

from __future__ import annotations

import sqlite3

from config import (
    LINKEDIN_PROFILE_BASE_URL,
    LINKEDIN_PROFILE_RE,
    STATUS_DM_SENT,
)

# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------


def canonicalize_linkedin_url(url: str) -> str:
    """Canonicalize a LinkedIn profile URL to a standard form.

    Accepts raw LinkedIn URLs from search results (e.g.
    ``https://uk.linkedin.com/in/john-doe?ref=123/`` or
    ``linkedin.com/in/john-doe``), strips query parameters, removes regional
    subdomains and trailing slashes, and returns a uniform format:
    ``https://www.linkedin.com/in/<handle>/``.

    Args:
        url: The raw LinkedIn profile URL to canonicalize.

    Returns:
        The canonicalized profile URL.

    Raises:
        ValueError: If the input is empty or the profile handle cannot be
            parsed from the URL.
    """
    if not url:
        raise ValueError("LinkedIn URL cannot be empty.")

    stripped = url.strip()

    # The config regex requires a scheme. Accept scheme-less inputs such as
    # "linkedin.com/in/john-doe" by normalizing them to an https URL first.
    if not stripped.startswith(("http://", "https://")):
        stripped = f"https://{stripped}"

    match = LINKEDIN_PROFILE_RE.match(stripped)
    if not match:
        raise ValueError(f"Could not parse LinkedIn profile handle from: {url!r}")

    handle = match.group(1)
    return f"{LINKEDIN_PROFILE_BASE_URL}{handle}/"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# DDL executed on every init_db call. All statements are idempotent via
# IF NOT EXISTS, so repeated initialization is safe.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        linkedin_url TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        headline TEXT,
        target_category TEXT NOT NULL,
        location TEXT,
        status TEXT DEFAULT 'NEW',
        date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        date_contacted TIMESTAMP,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_state (
        query_key TEXT PRIMARY KEY,
        last_offset INTEGER DEFAULT 1,
        last_run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_usage (
        usage_date DATE PRIMARY KEY,
        query_count INTEGER DEFAULT 0
    )
    """,
)


def init_db(db_path: str = "leads.db") -> None:
    """Initialize the SQLite database and create tables if they do not exist.

    Creates the ``leads``, ``search_state``, and ``daily_usage`` tables. This
    function is idempotent and safe to call on every application startup.

    Args:
        db_path: Filesystem path to the SQLite database file.
    """
    with sqlite3.connect(db_path) as conn:
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)


# ---------------------------------------------------------------------------
# Lead queries
# ---------------------------------------------------------------------------


def check_lead_status(canonical_url: str, db_path: str = "leads.db") -> dict | None:
    """Look up the pipeline status of a lead by its canonical LinkedIn URL.

    Args:
        canonical_url: The canonicalized LinkedIn profile URL to look up.
        db_path: Filesystem path to the SQLite database file.

    Returns:
        A dictionary with keys ``status``, ``date_contacted``, and
        ``date_added`` if the lead exists, otherwise ``None``.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT status, date_contacted, date_added
            FROM leads
            WHERE linkedin_url = ?
            """,
            (canonical_url,),
        ).fetchone()

    if row is None:
        return None

    return {
        "status": row[0],
        "date_contacted": row[1],
        "date_added": row[2],
    }


def save_lead(lead_data: dict, db_path: str = "leads.db") -> bool:
    """Insert a new lead into the database, ignoring duplicates.

    The ``linkedin_url`` is canonicalized before insertion. If a lead with the
    same canonical URL already exists, the insert is ignored.

    Args:
        lead_data: Dictionary containing ``linkedin_url``, ``name``,
            ``headline``, ``target_category``, and ``location``.
        db_path: Filesystem path to the SQLite database file.

    Returns:
        ``True`` if a net-new record was inserted, ``False`` if it was ignored
        as a duplicate.
    """
    canonical_url = canonicalize_linkedin_url(lead_data["linkedin_url"])

    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO leads
                (linkedin_url, name, headline, target_category, location)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                canonical_url,
                lead_data["name"],
                lead_data.get("headline"),
                lead_data["target_category"],
                lead_data.get("location"),
            ),
        )

    return cursor.rowcount > 0


def update_lead_status(
    lead_id: int, new_status: str, notes: str = None, db_path: str = "leads.db"
) -> None:
    """Update the status of a lead by its database ID.

    If ``new_status`` equals ``STATUS_DM_SENT``, the ``date_contacted`` column
    is set to the current timestamp. The ``notes`` column is updated only when
    a non-``None`` value is provided.

    Args:
        lead_id: The database ID of the lead to update.
        new_status: The new pipeline status to assign.
        notes: Optional notes to store on the lead.
        db_path: Filesystem path to the SQLite database file.
    """
    with sqlite3.connect(db_path) as conn:
        if new_status == STATUS_DM_SENT:
            conn.execute(
                """
                UPDATE leads
                SET status = ?, date_contacted = CURRENT_TIMESTAMP, notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (new_status, notes, lead_id),
            )
        else:
            conn.execute(
                """
                UPDATE leads
                SET status = ?, notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (new_status, notes, lead_id),
            )


def get_leads_by_status(
    status_filter: str = None, db_path: str = "leads.db"
) -> list[dict]:
    """Fetch leads, optionally filtered by pipeline status.

    Args:
        status_filter: The status to filter by. If ``None`` or ``"ALL"``, all
            leads are returned.
        db_path: Filesystem path to the SQLite database file.

    Returns:
        A list of dictionaries, one per lead row, keyed by column name.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if status_filter is None or status_filter == "ALL":
            rows = conn.execute("SELECT * FROM leads ORDER BY date_added DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY date_added DESC",
                (status_filter,),
            ).fetchall()

    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Daily usage accounting
# ---------------------------------------------------------------------------


def get_daily_usage(usage_date: str, db_path: str = "leads.db") -> int:
    """Return the number of queries recorded for a given calendar date.

    Args:
        usage_date: The date to look up, in ``YYYY-MM-DD`` format.
        db_path: Filesystem path to the SQLite database file.

    Returns:
        The recorded query count for the date, or ``0`` if no record exists.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT query_count FROM daily_usage WHERE usage_date = ?",
            (usage_date,),
        ).fetchone()

    return row[0] if row is not None else 0


def increment_daily_usage(usage_date: str, db_path: str = "leads.db") -> int:
    """Increment the query count for a given calendar date.

    Creates the date row if it does not exist, then increments its
    ``query_count`` by one.

    Args:
        usage_date: The date to increment, in ``YYYY-MM-DD`` format.
        db_path: Filesystem path to the SQLite database file.

    Returns:
        The updated query count for the date.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO daily_usage (usage_date, query_count)
            VALUES (?, 1)
            ON CONFLICT(usage_date) DO UPDATE SET
                query_count = query_count + 1
            """,
            (usage_date,),
        )
        row = conn.execute(
            "SELECT query_count FROM daily_usage WHERE usage_date = ?",
            (usage_date,),
        ).fetchone()

    return row[0]


# ---------------------------------------------------------------------------
# Search pagination state
# ---------------------------------------------------------------------------


def get_search_offset(query_key: str, db_path: str = "leads.db") -> int:
    """Return the last search offset for a given query key.

    Args:
        query_key: The unique key identifying a search query.
        db_path: Filesystem path to the SQLite database file.

    Returns:
        The last recorded offset, or ``1`` if the key does not exist.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_offset FROM search_state WHERE query_key = ?",
            (query_key,),
        ).fetchone()

    return row[0] if row is not None else 1


def update_search_offset(
    query_key: str, new_offset: int, db_path: str = "leads.db"
) -> None:
    """Persist the search offset for a given query key.

    Creates the key row if it does not exist, then sets its ``last_offset`` to
    ``new_offset`` and refreshes ``last_run_timestamp``.

    Args:
        query_key: The unique key identifying a search query.
        new_offset: The new pagination offset to store.
        db_path: Filesystem path to the SQLite database file.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO search_state (query_key, last_offset, last_run_timestamp)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(query_key) DO UPDATE SET
                last_offset = excluded.last_offset,
                last_run_timestamp = CURRENT_TIMESTAMP
            """,
            (query_key, new_offset),
        )