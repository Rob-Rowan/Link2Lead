"""Streamlit application entry point for the Link2Lead B2B LinkedIn Lead Generator.

This module is the Phase 4 UI layer. It wires together the configuration
presets (``config.py``), the SQLite persistence layer (``db.py``), and the
Google X-Ray search engine (``search_engine.py``) into a four-tab Streamlit
dashboard:

* Tab 1 - Configuration & Presets
* Tab 2 - Prospect Discovery Engine
* Tab 3 - Pipeline Tracker
* Tab 4 - Analytics & CSV Export

No business logic lives here beyond UI orchestration; all persistence and
search behavior is delegated to the underlying modules.
"""

from __future__ import annotations
import db
import search_engine
from config import (
    ALL_STATUSES,
    MAX_DAILY_QUERIES,
    STATUS_ARCHIVED,
    STATUS_CONNECTED,
    STATUS_DM_SENT,
    STATUS_NEW,
    STATUS_SKIPPED,
    TARGET_BUYER_PRESETS,
)
import csv
import io
import os
from datetime import date
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
import streamlit as st
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


# ---------------------------------------------------------------------------
# Application initialization
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="LinkedIn Lead Generator & Pipeline Tracker",
    layout="wide",
)

# Ensure all SQLite tables exist before any query runs.
db.init_db()

# Initialize session state variables if not already present.
if "search_results" not in st.session_state:
    st.session_state.search_results = []

if "last_yield_stats" not in st.session_state:
    st.session_state.last_yield_stats = None


# ---------------------------------------------------------------------------
# Small UI helpers
# ---------------------------------------------------------------------------


def _status_badge(existing_status: dict) -> None:
    """Render a colored status pill for an already-known lead.

    Args:
        existing_status: The ``check_lead_status`` dict containing at least a
            ``status`` key.
    """
    status = existing_status.get("status", "")

    if status in (STATUS_DM_SENT, STATUS_CONNECTED):
        st.markdown(
            f'<span style="background-color:#ff4b4b;color:white;'
            f'padding:2px 10px;border-radius:12px;font-size:0.85em">'
            f"🚨 ALREADY CONTACTED (Status: {status})</span>",
            unsafe_allow_html=True,
        )
    elif status == STATUS_NEW:
        st.markdown(
            f'<span style="background-color:#ffa726;color:white;'
            f'padding:2px 10px;border-radius:12px;font-size:0.85em">'
            f"📌 IN PIPELINE (Status: NEW)</span>",
            unsafe_allow_html=True,
        )
    elif status in (STATUS_SKIPPED, STATUS_ARCHIVED):
        st.markdown(
            f'<span style="background-color:#9e9e9e;color:white;'
            f'padding:2px 10px;border-radius:12px;font-size:0.85em">'
            f"🚫 PREVIOUSLY SKIPPED</span>",
            unsafe_allow_html=True,
        )


def _get_lead_id_by_url(url: str) -> int | None:
    """Return the database ID of a lead by its canonical LinkedIn URL.

    Args:
        url: The canonical LinkedIn profile URL.

    Returns:
        The lead's database ID, or ``None`` if it does not exist.
    """
    for lead in db.get_leads_by_status("ALL"):
        if lead["linkedin_url"] == url:
            return lead["id"]
    return None


def _save_notes(lead_id: int, current_status: str) -> None:
    """Persist the notes text area for a lead via an on_change callback.

    Args:
        lead_id: The database ID of the lead.
        current_status: The lead's current pipeline status, preserved so the
            notes update does not alter the status.
    """
    notes = st.session_state.get(f"notes_{lead_id}", "")
    db.update_lead_status(lead_id, current_status, notes=notes)


def _leads_to_csv(leads: list[dict]) -> str:
    """Serialize a list of lead dicts to a CSV string.

    Args:
        leads: List of lead dictionaries keyed by column name.

    Returns:
        A CSV-formatted string with a header row.
    """
    fieldnames = [
        "id",
        "linkedin_url",
        "name",
        "headline",
        "target_category",
        "location",
        "status",
        "date_added",
        "date_contacted",
        "notes",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for lead in leads:
        writer.writerow({key: lead.get(key, "") for key in fieldnames})
    return output.getvalue()


# ---------------------------------------------------------------------------
# Top telemetry bar / header
# ---------------------------------------------------------------------------

st.title("🔗 LinkedIn Lead Generator & Pipeline Tracker")

today_iso = date.today().isoformat()
daily_usage = db.get_daily_usage(today_iso)

usage_col, warn_col = st.columns([3, 2])
with usage_col:
    st.metric(
        label="Daily API Quota",
        value=f"{daily_usage} / {MAX_DAILY_QUERIES} Queries Used",
    )
    st.progress(min(daily_usage / MAX_DAILY_QUERIES, 1.0))
with warn_col:
    if daily_usage >= 90:
        st.warning(
            f"⚠️ Approaching daily API limit ({daily_usage}/{MAX_DAILY_QUERIES}). "
            "Searches will be blocked once the limit is reached."
        )

st.divider()

# ---------------------------------------------------------------------------
# Tab layout
# ---------------------------------------------------------------------------

tab_config, tab_discovery, tab_pipeline, tab_analytics = st.tabs(
    [
        "⚙️ Configuration & Presets",
        "🔍 Prospect Discovery Engine",
        "📊 Pipeline Tracker",
        "📈 Analytics & CSV Export",
    ]
)

# ===========================================================================
# Tab 1: Configuration & Presets
# ===========================================================================

with tab_config:
    st.subheader("⚙️ Configuration & Presets")

    # Environment status ----------------------------------------------------
    api_key = os.getenv("SERPER_API_KEY")
    placeholder_values = {"your_serper_api_key_here"}

    key_configured = bool(api_key) and api_key.strip().lower() not in placeholder_values

    if key_configured:
        st.success("✅ SERPER_API_KEY is set")
    else:
        st.error("❌ SERPER_API_KEY is missing or unconfigured")

    st.divider()

    # Category selector -----------------------------------------------------
    st.markdown("**Target Buyer Preset**")
    selected_category = st.selectbox(
        "Select a buyer persona category",
        options=list(TARGET_BUYER_PRESETS.keys()),
        key="config_category",
    )

    # Underlying query string ----------------------------------------------
    st.markdown("**Google X-Ray Query String**")
    st.code(TARGET_BUYER_PRESETS[selected_category], language="text")

    # Optional location / keyword modifier ---------------------------------
    st.markdown("**Optional Modifier**")
    location_filter = st.text_input(
        "Location filter or custom keyword modifier (e.g. \"Austin, TX\")",
        key="config_location_filter",
        placeholder="Austin, TX",
    )

    if location_filter.strip():
        st.caption(
            "This modifier will be appended to the query string in the "
            "Prospect Discovery Engine."
        )

# ===========================================================================
# Tab 2: Prospect Discovery Engine
# ===========================================================================

with tab_discovery:
    st.subheader("🔍 Prospect Discovery Engine")

    # Category / query selector --------------------------------------------
    discovery_category = st.selectbox(
        "Category / Query",
        options=list(TARGET_BUYER_PRESETS.keys()),
        key="discovery_category",
    )

    # Build the effective query string, appending any modifier from Tab 1.
    base_query = TARGET_BUYER_PRESETS[discovery_category]
    modifier = st.session_state.get("config_location_filter", "").strip()
    effective_query = f"{base_query} {modifier}".strip() if modifier else base_query

    st.caption(f"Effective query: {effective_query}")

    # Number of pages slider ------------------------------------------------
    num_pages = st.slider(
        "Search Yield Batch (1 API Credit = 10 to 50 results)",
        min_value=1,
        max_value=5,
        value=5,  # Default to 5 to pull 50 results per search
        key="discovery_pages",
    )

    # Execute search button -------------------------------------------------
    if st.button("🚀 Execute Search", key="execute_search"):
        try:
            result = search_engine.execute_xray_search(
                query_key=discovery_category,
                raw_query=effective_query,
                target_category=discovery_category,
                num_pages=num_pages,
            )
        except search_engine.DailyLimitExceededError:
            st.error("Daily API query limit reached (95/95). Try again tomorrow.")
        except search_engine.GoogleSearchError as exc:
            st.error(str(exc))
        else:
            results = result.get("results", [])
            st.session_state.search_results = results

            total_raw = len(results)
            existing = sum(1 for r in results if r.get("existing_status") is not None)
            net_new = total_raw - existing

            st.session_state.last_yield_stats = {
                "total_raw": total_raw,
                "existing": existing,
                "net_new": net_new,
            }

            if result.get("exhausted"):
                st.info(result.get("message", "Search exhausted."))

    # Search yield banner ---------------------------------------------------
    if st.session_state.last_yield_stats is not None:
        stats = st.session_state.last_yield_stats
        st.markdown(
            f"**Fetched {stats['total_raw']} Profiles | "
            f"{stats['net_new']} Net-New | {stats['existing']} Existing Matches**"
        )

    st.divider()

    # Result cards grid -----------------------------------------------------
    if not st.session_state.search_results:
        st.info("No search results yet. Run a search above to populate this list.")
    else:
        for idx, prospect in enumerate(st.session_state.search_results):
            url = prospect.get("linkedin_url", "")
            name = prospect.get("name", "Unknown")
            headline = prospect.get("headline", "")
            location = prospect.get("location", "Unknown / See Profile")
            category = prospect.get("target_category", discovery_category)
            existing_status = prospect.get("existing_status")

            with st.container(border=True):
                st.markdown(f"### {name}")
                if headline:
                    st.markdown(f"*{headline}*")
                st.markdown(f"📍 {location}  ·  🏷️ {category}")

                st.markdown(
                    f"[Open Profile]({url})",
                    unsafe_allow_html=True,
                )

                if existing_status is not None:
                    _status_badge(existing_status)

                    if existing_status.get("status") in (
                        STATUS_DM_SENT,
                        STATUS_CONNECTED,
                        STATUS_SKIPPED,
                        STATUS_ARCHIVED,
                    ):
                        st.button(
                            "Already Saved",
                            key=f"already_saved_{idx}",
                            disabled=True,
                        )
                    else:
                        st.button(
                            "Already Saved",
                            key=f"already_saved_{idx}",
                            disabled=True,
                        )
                else:
                    col_save, col_skip = st.columns(2)
                    with col_save:
                        if st.button("+ Save to Pipeline", key=f"save_{idx}"):
                            lead_data = {
                                "linkedin_url": url,
                                "name": name,
                                "headline": headline,
                                "target_category": category,
                                "location": location,
                            }
                            db.save_lead(lead_data)
                            # Reflect the new state in the UI immediately.
                            for r in st.session_state.search_results:
                                if r["linkedin_url"] == url:
                                    r["existing_status"] = {
                                        "status": STATUS_NEW,
                                        "date_contacted": None,
                                        "date_added": None,
                                    }
                            st.toast("Lead saved to pipeline!")
                            st.rerun()
                    with col_skip:
                        if st.button("Skip", key=f"skip_{idx}"):
                            lead_data = {
                                "linkedin_url": url,
                                "name": name,
                                "headline": headline,
                                "target_category": category,
                                "location": location,
                            }
                            lead_id = _get_lead_id_by_url(url)
                            if lead_id is None:
                                db.save_lead(lead_data)
                                lead_id = _get_lead_id_by_url(url)
                            if lead_id is not None:
                                db.update_lead_status(lead_id, STATUS_SKIPPED)
                            # Reflect the new state in the UI immediately.
                            for r in st.session_state.search_results:
                                if r["linkedin_url"] == url:
                                    r["existing_status"] = {
                                        "status": STATUS_SKIPPED,
                                        "date_contacted": None,
                                        "date_added": None,
                                    }
                            st.toast("Lead skipped.")
                            st.rerun()

# ===========================================================================
# Tab 3: Pipeline Tracker
# ===========================================================================

with tab_pipeline:
    st.subheader("📊 Pipeline Tracker")

    # Filter controls -------------------------------------------------------
    status_filter = st.radio(
        "Filter by status",
        options=["ALL"] + ALL_STATUSES,
        horizontal=True,
        key="pipeline_filter",
    )

    leads = db.get_leads_by_status(status_filter)

    if not leads:
        st.info("No leads found for the selected filter.")
    else:
        for lead in leads:
            lead_id = lead["id"]
            with st.container(border=True):
                st.markdown(f"### {lead['name']}")
                if lead.get("headline"):
                    st.markdown(f"*{lead['headline']}*")
                st.markdown(
                    f"🏷️ {lead.get('target_category', '—')}  ·  "
                    f"📍 {lead.get('location', '—')}"
                )
                st.markdown(
                    f"**Status:** {lead.get('status', '—')}  ·  "
                    f"**Added:** {lead.get('date_added', '—')}  ·  "
                    f"**Contacted:** {lead.get('date_contacted', '—')}"
                )
                st.markdown(f"[Open Profile]({lead.get('linkedin_url', '')})")

                # Status action buttons -------------------------------------
                action_col1, action_col2, action_col3 = st.columns(3)
                with action_col1:
                    if st.button("Mark DM Sent", key=f"dm_{lead_id}"):
                        db.update_lead_status(lead_id, STATUS_DM_SENT)
                        st.toast("Marked as DM Sent.")
                        st.rerun()
                with action_col2:
                    if st.button("Mark Connected", key=f"conn_{lead_id}"):
                        db.update_lead_status(lead_id, STATUS_CONNECTED)
                        st.toast("Marked as Connected.")
                        st.rerun()
                with action_col3:
                    if st.button("Archive", key=f"arch_{lead_id}"):
                        db.update_lead_status(lead_id, STATUS_ARCHIVED)
                        st.toast("Lead archived.")
                        st.rerun()

                # Persistent notes field ------------------------------------
                st.text_area(
                    "Notes",
                    value=lead.get("notes") or "",
                    key=f"notes_{lead_id}",
                    on_change=partial(_save_notes, lead_id, lead.get("status", STATUS_NEW)),
                )

# ===========================================================================
# Tab 4: Analytics & CSV Export
# ===========================================================================

with tab_analytics:
    st.subheader("📈 Analytics & CSV Export")

    all_leads = db.get_leads_by_status("ALL")

    total_leads = len(all_leads)
    dms_sent = sum(1 for lead in all_leads if lead.get("status") == STATUS_DM_SENT)
    connections = sum(1 for lead in all_leads if lead.get("status") == STATUS_CONNECTED)
    conversion_rate = (connections / dms_sent * 100) if dms_sent > 0 else 0.0

    # Metric cards row ------------------------------------------------------
    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
    with metric_col1:
        st.metric("Total Leads Saved", total_leads)
    with metric_col2:
        st.metric("DMs Sent", dms_sent)
    with metric_col3:
        st.metric("Connections Made", connections)
    with metric_col4:
        st.metric("Conversion Rate", f"{conversion_rate:.1f}%")

    st.divider()

    # Export section --------------------------------------------------------
    st.markdown("**Export Full Pipeline**")
    st.caption("Download all leads from the SQLite database as a CSV file.")

    if all_leads:
        csv_data = _leads_to_csv(all_leads)
        st.download_button(
            label="⬇️ Download linkedin_leads_export.csv",
            data=csv_data,
            file_name="linkedin_leads_export.csv",
            mime="text/csv",
            key="download_csv",
        )
    else:
        st.info("No leads to export yet. Save leads from the Discovery tab first.")