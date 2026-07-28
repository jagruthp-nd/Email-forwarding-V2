"""
weekly_report.py
----------------
Weekly Friday summary report for IT admins.

Queries Azure Table Storage and generates a digest covering:
  - Active EF accounts by status
  - Extensions applied this week
  - Accounts deleted this week
  - Alert emails sent this week
  - Upcoming deadlines within the next 14 days

Admin recipients are controlled by the ADMIN_EMAILS environment variable
(comma-separated).  Add new admins there without any code changes.

Called from the weekly timer trigger in function_app.py.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from .table_store import TableStore
from .email_sender import send_weekly_report

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Admin email list (comma-separated env var – add new admins here)
# ---------------------------------------------------------------------------
_ADMIN_EMAILS: List[str] = [
    e.strip()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
]


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _build_report_data(store: TableStore) -> Dict[str, Any]:
    """Pull all data needed for the weekly report from Table Storage."""
    today       = date.today()
    week_ago    = datetime.now(timezone.utc) - timedelta(days=7)
    since_iso   = week_ago.isoformat()
    in_14_days  = today + timedelta(days=14)

    all_users = store.list_all_users()

    # ── Active accounts by status ────────────────────────────────────────────
    status_counts: Dict[str, int] = {}
    active_users: List[Dict[str, Any]] = []
    for u in all_users:
        code = u.get("statusCode", "UNKNOWN")
        status_counts[code] = status_counts.get(code, 0) + 1
        if code != "DELETED":
            active_users.append(u)

    # ── Upcoming deadlines (next 14 days) ────────────────────────────────────
    upcoming: List[Dict[str, Any]] = []
    for u in active_users:
        delete_date_str = u.get("deleteDate", "")
        if not delete_date_str:
            continue
        try:
            delete_date = date.fromisoformat(delete_date_str)
        except ValueError:
            continue
        days_left = (delete_date - today).days
        if 0 <= days_left <= 14:
            upcoming.append({
                "name":       u.get("displayName", "Unknown"),
                "email":      u.get("userEmail", ""),
                "deleteDate": delete_date_str,
                "daysLeft":   days_left,
                "extCount":   int(u.get("extensionCount", 0)),
                "status":     u.get("statusCode", ""),
            })
    upcoming.sort(key=lambda x: x["daysLeft"])

    # ── Recent audit events (past 7 days) ────────────────────────────────────
    recent_audits = store.list_recent_audits(since_iso)

    extensions_this_week = [
        a for a in recent_audits if a.get("action", "").startswith("EXTENDED")
    ]
    deletions_this_week = [
        a for a in recent_audits if a.get("action") == "DELETED"
    ]
    alerts_this_week = [
        a for a in recent_audits if a.get("action") == "ALERT_SENT"
    ]

    return {
        "report_date":          today.isoformat(),
        "report_period_start":  week_ago.strftime("%Y-%m-%d"),
        "total_active":         len(active_users),
        "total_deleted_ever":   status_counts.get("DELETED", 0),
        "status_counts":        status_counts,
        "upcoming_deadlines":   upcoming,
        "extensions_this_week": extensions_this_week,
        "deletions_this_week":  deletions_this_week,
        "alerts_this_week":     alerts_this_week,
    }


# ---------------------------------------------------------------------------
# Entry point (called from function_app.py timer trigger)
# ---------------------------------------------------------------------------

def run_weekly_report() -> Dict[str, Any]:
    """
    Generate and send the weekly EF Automation summary report.

    Returns a summary dict for logging.
    """
    if not _ADMIN_EMAILS:
        logger.warning(
            "ADMIN_EMAILS env var is not set – weekly report skipped. "
            "Set it to a comma-separated list of admin email addresses."
        )
        return {"status": "skipped", "reason": "ADMIN_EMAILS not configured"}

    store = TableStore()
    store.ensure_tables()

    data = _build_report_data(store)

    sent_count = send_weekly_report(data, _ADMIN_EMAILS)

    summary = {
        "status":             "sent",
        "recipients":         _ADMIN_EMAILS,
        "recipients_count":   sent_count,
        "active_accounts":    data["total_active"],
        "upcoming_deadlines": len(data["upcoming_deadlines"]),
        "extensions_week":    len(data["extensions_this_week"]),
        "deletions_week":     len(data["deletions_this_week"]),
    }
    logger.info("Weekly report dispatched: %s", summary)
    return summary
