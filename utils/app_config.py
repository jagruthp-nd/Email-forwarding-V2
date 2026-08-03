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


def get_deletion_exempt_user_ids() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_USER_IDS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def get_deletion_exempt_emails() -> frozenset:
    raw = os.environ.get("DELETION_EXEMPT_EMAILS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())
