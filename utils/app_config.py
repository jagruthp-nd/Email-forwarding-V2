"""
app_config.py
-------------
Centralised, env-driven settings that operators change without code edits.

Email recipients (all configurable except managers — those come from Graph):
  SENDER_EMAIL         From address (Graph Mail.Send as this mailbox)
  IT_EMAIL             CC on manager notices (empty = no CC)
  IT_APPROVAL_EMAIL    To for Approve/Decline extension emails
  ADMIN_EMAILS         Soft-delete / admin technical notices (comma-separated)
  REPORT_EMAILS        Weekly/monthly report recipients (falls back to ADMIN_EMAILS)
  EF_TEST_RECIPIENT    Used when EF_TEST_MODE=true (all mail redirected here)
"""

from __future__ import annotations

import os
from typing import Dict, List
from urllib.parse import quote


def _csv_emails(raw: str) -> List[str]:
    return [e.strip() for e in (raw or "").split(",") if e.strip()]


def get_sender_email() -> str:
    return (
        os.environ.get("SENDER_EMAIL", "").strip()
        or "it-automation-service@netradyne.com"
    )


def get_it_email() -> str:
    """CC mailbox for manager-facing notices. Empty string = no CC."""
    return os.environ.get("IT_EMAIL", "").strip()


def get_it_approval_email() -> str:
    return os.environ.get("IT_APPROVAL_EMAIL", "").strip()


def get_admin_emails() -> List[str]:
    return _csv_emails(os.environ.get("ADMIN_EMAILS", ""))


def get_report_emails() -> List[str]:
    """Weekly/monthly offboard reports. Prefer REPORT_EMAILS; else ADMIN_EMAILS."""
    report = _csv_emails(os.environ.get("REPORT_EMAILS", ""))
    return report or get_admin_emails()


def get_servicedesk_ticket_url() -> str:
    """URL for the 'Raise extension request' button in manager alert emails."""
    return (
        os.environ.get("SERVICEDESK_TICKET_URL", "").strip()
        or os.environ.get("SDP_TICKET_URL", "").strip()
    )


def get_func_base_url() -> str:
    return os.environ.get("FUNC_BASE_URL", "").strip().rstrip("/")


def get_sharepoint_report_url() -> str:
    """
    Hyperlink to the EF Automation reports folder in SharePoint.

    Prefer explicit SHAREPOINT_REPORT_URL, otherwise build from:
      SHAREPOINT_SITE_URL / SHAREPOINT_LIBRARY / SHAREPOINT_FOLDER
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

    parts = [quote(p) for p in library.split("/") if p]
    if folder:
        parts.extend(quote(p) for p in folder.split("/") if p)
    return f"{site}/{'/'.join(parts)}"


def get_email_config_summary() -> Dict[str, str]:
    """For demo / ops pages — shows configured mailboxes (not managers)."""
    return {
        "SENDER_EMAIL": get_sender_email(),
        "IT_EMAIL": get_it_email() or "(empty – no CC)",
        "IT_APPROVAL_EMAIL": get_it_approval_email() or "(not set)",
        "ADMIN_EMAILS": ", ".join(get_admin_emails()) or "(not set)",
        "REPORT_EMAILS": ", ".join(get_report_emails()) or "(not set)",
        "EF_TEST_RECIPIENT": os.environ.get("EF_TEST_RECIPIENT", "").strip() or "(default prem_testing)",
        "EF_TEST_MODE": os.environ.get("EF_TEST_MODE", "false"),
        "managers": "(fetched automatically from Microsoft Graph / Entra)",
    }


def get_deletion_exempt_user_ids() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_USER_IDS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def get_deletion_exempt_emails() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_EMAILS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())
