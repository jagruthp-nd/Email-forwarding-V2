"""
app_config.py
-------------
Centralised, env-driven settings that operators change without code edits.

ServiceDesk link (change in Azure App Settings or local.settings.json):
  SERVICEDESK_TICKET_URL  – preferred name
  SDP_TICKET_URL            – legacy alias (still supported)
"""

from __future__ import annotations

import os
from typing import List


def get_servicedesk_ticket_url() -> str:
    """URL for the 'Raise extension request' button in manager alert emails."""
    return (
        os.environ.get("SERVICEDESK_TICKET_URL", "").strip()
        or os.environ.get("SDP_TICKET_URL", "").strip()
    )


def get_admin_emails() -> List[str]:
    return [
        e.strip()
        for e in os.environ.get("ADMIN_EMAILS", "").split(",")
        if e.strip()
    ]


def get_report_emails() -> List[str]:
    """
    Recipients for the daily consolidated offboard report.

    Prefer REPORT_EMAILS (team mailbox). Falls back to ADMIN_EMAILS.
    Change REPORT_EMAILS in App Settings without code changes.
    """
    report = [
        e.strip()
        for e in os.environ.get("REPORT_EMAILS", "").split(",")
        if e.strip()
    ]
    return report or get_admin_emails()


def get_sharepoint_report_url() -> str:
    """Optional SharePoint folder/file link shown in the consolidated report email."""
    return (
        os.environ.get("SHAREPOINT_REPORT_URL", "").strip()
        or os.environ.get("SHAREPOINT_REPORT_FOLDER_URL", "").strip()
    )


def get_deletion_exempt_user_ids() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_USER_IDS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def get_deletion_exempt_emails() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_EMAILS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())
