"""
app_config.py
-------------
Centralised, env-driven settings that operators change without code edits.
"""

from __future__ import annotations

import os
from typing import List
from urllib.parse import quote


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
    Recipients for weekly/monthly offboard reports.

    Prefer REPORT_EMAILS (team mailbox). Falls back to ADMIN_EMAILS.
    """
    report = [
        e.strip()
        for e in os.environ.get("REPORT_EMAILS", "").split(",")
        if e.strip()
    ]
    return report or get_admin_emails()


def get_sharepoint_report_url() -> str:
    """
    Hyperlink to the EF Automation reports folder in SharePoint.

    Prefer explicit SHAREPOINT_REPORT_URL, otherwise build from:
      SHAREPOINT_SITE_URL   e.g. https://netorg726775.sharepoint.com/sites/ITTEAM259
      SHAREPOINT_LIBRARY    e.g. Shared Documents
      SHAREPOINT_FOLDER     e.g. General/Jagruth/Automation_Reports/EF
    """
    explicit = (
        os.environ.get("SHAREPOINT_REPORT_URL", "").strip()
        or os.environ.get("SHAREPOINT_REPORT_FOLDER_URL", "").strip()
    )
    if explicit:
        return explicit

    site = os.environ.get("SHAREPOINT_SITE_URL", "").strip().rstrip("/")
    library = os.environ.get("SHAREPOINT_LIBRARY", "Shared Documents").strip().strip("/")
    folder = os.environ.get("SHAREPOINT_FOLDER", "").strip().strip("/")
    if not site:
        return ""

    # Path-style deep link into the document library folder
    parts = [quote(p) for p in library.split("/") if p]
    if folder:
        parts.extend(quote(p) for p in folder.split("/") if p)
    return f"{site}/{'/'.join(parts)}"


def get_deletion_exempt_user_ids() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_USER_IDS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def get_deletion_exempt_emails() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_EMAILS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())
