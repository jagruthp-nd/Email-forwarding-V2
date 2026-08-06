"""
approval_webhook.py
-------------------
Business logic for the EF Extension Approve / Decline HTTP trigger.

When an EF alert fires the daily monitor generates a time-limited one-use
token and emails the IT approval mailbox with two links:
  - Approve → opens a form to enter the SD+ ticket number, then sets CSA
  - Decline → logs the refusal; normal deletion proceeds at deadline

Token validity: APPROVAL_TOKEN_DAYS env var (default 7 days).
Token stored in ApprovalTokens table in Azure Table Storage.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Mapping from extension_count → CSA value offered in the approval button
_EXT_TYPE_MAP = {
    0: "EXTEND_TO_30",
    1: "EXTEND_TO_60",
    2: "EXTENDED_MAX",
}


def generate_approval_urls(user_id: str, ext_count: int) -> Tuple[str, str, str]:
    """
    Generate and store an approval token.
    Returns (token, approve_url, decline_url).
    Called from _do_alert in monitor_accounts.py.
    """
    from .table_store import TableStore

    token_days = int(os.environ.get("APPROVAL_TOKEN_DAYS", "7"))
    ext_type = _EXT_TYPE_MAP.get(ext_count, "EXTEND_TO_30")
    token = uuid.uuid4().hex  # 32-char hex token

    expires_at = (datetime.now(timezone.utc) + timedelta(days=token_days)).isoformat()

    store = TableStore()
    store.store_approval_token({
        "token":     token,
        "userId":    user_id,
        "extType":   ext_type,
        "expiresAt": expires_at,
        "used":      False,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    })

    base = os.environ.get("FUNC_BASE_URL", "").rstrip("/")
    approve_url = f"{base}/api/ef_approval?token={token}&action=approve"
    decline_url = f"{base}/api/ef_approval?token={token}&action=decline"
    logger.info("Generated approval token for userId=%s extType=%s expires=%s", user_id, ext_type, expires_at)
    return token, approve_url, decline_url


def handle_get(token: str, action: str) -> Tuple[str, int]:
    """
    Handle GET request (user clicks email button).
    Returns (html_body, http_status_code).
    """
    from .table_store import TableStore
    store = TableStore()
    rec = store.get_approval_token(token)

    if not rec:
        return _error_page("This approval link is invalid or has expired."), 400

    try:
        expires = datetime.fromisoformat(rec["expiresAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires:
            return _error_page("This approval link has expired (7-day limit). Check the latest alert email."), 410
    except (ValueError, KeyError):
        return _error_page("Invalid token data."), 400

    if _coerce_bool(rec.get("used", False)):
        return _error_page("This approval link has already been used."), 409

    user_id  = rec.get("userId", "")
    ext_type = rec.get("extType", "")
    user_rec = store.get_user(user_id) or {}

    display_name  = user_rec.get("displayName", user_id)
    user_email    = user_rec.get("userEmail", "")
    offboard_date = user_rec.get("offboardDate", "")

    ext_label = {
        "EXTEND_TO_30": "1st extension (+30 days, total 30 from offboard)",
        "EXTEND_TO_60": "2nd extension (+30 days, total 60 from offboard)",
        "EXTENDED_MAX": "Final extension (+30 days, total 90 – max policy)",
    }.get(ext_type, ext_type)

    if action == "decline":
        store.mark_token_used(token)
        store.append_audit(
            user_id, "EXTENSION_DECLINED",
            f"IT declined extension via email button. ext_type={ext_type}. "
            "Normal deletion will proceed at scheduled deadline.",
        )
        _update_compliance(store, user_id, extensionDeclinedDate=datetime.now(timezone.utc).date().isoformat())
        logger.info("Extension DECLINED via token for userId=%s ext_type=%s", user_id, ext_type)
        return _decline_page(display_name), 200

    if action == "approve":
        return _approval_form(token, display_name, user_email, offboard_date, ext_label), 200

    return _error_page("Unknown action."), 400


def handle_post(token: str, ticket_ref: str) -> Tuple[str, int]:
    """
    Handle POST (form submitted with ticket number).
    Returns (html_body, http_status_code).
    """
    from .table_store import TableStore
    from .graph_api import set_extension_attribute, set_ticket_ref_attribute

    store = TableStore()
    rec = store.get_approval_token(token)

    if not rec:
        return _error_page("Invalid or expired token."), 400

    try:
        expires = datetime.fromisoformat(rec["expiresAt"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires:
            return _error_page("This approval link has expired."), 410
    except (ValueError, KeyError):
        return _error_page("Invalid token."), 400

    if _coerce_bool(rec.get("used", False)):
        return _error_page("This link has already been used."), 409

    user_id  = rec.get("userId", "")
    ext_type = rec.get("extType", "")
    ticket   = (ticket_ref or "").strip()

    ok_ext = set_extension_attribute(user_id, ext_type)
    if ticket:
        set_ticket_ref_attribute(user_id, ticket)

    if not ok_ext:
        return _error_page(
            "Failed to set the extension in Azure AD. "
            "Please set the CSA manually in the Azure portal, or contact IT administration."
        ), 500

    store.mark_token_used(token)
    store.append_audit(
        user_id, "EXTENSION_APPROVED_EMAIL",
        f"IT approved extension via email button. ext_type={ext_type} ticketRef={ticket}. "
        "CSA ExtStatus set – daily monitor will apply extension in next run.",
    )
    _update_compliance(store, user_id, latestTicketRef=ticket,
                       extensionApprovedViaEmailDate=datetime.now(timezone.utc).date().isoformat())

    user_rec = store.get_user(user_id) or {}
    display_name = user_rec.get("displayName", user_id)
    logger.info("Extension APPROVED via token for userId=%s ext_type=%s ticket=%s", user_id, ext_type, ticket)
    return _success_page(display_name, ext_type, ticket), 200


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _error_page(msg: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<title>EF Automation</title></head>'
        '<body style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;margin:40px auto;padding:20px;">'
        '<div style="background:#dc3545;color:#fff;padding:24px;border-radius:6px;">'
        f'<h2>&#9888; Action Failed</h2><p>{msg}</p>'
        '</div></body></html>'
    )


def _decline_page(name: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<title>Declined</title></head>'
        '<body style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;margin:40px auto;padding:20px;">'
        '<div style="background:#6c757d;color:#fff;padding:24px;border-radius:6px;">'
        f'<h2>Extension Declined</h2>'
        f'<p>The extension request for <strong>{name}</strong> has been declined.</p>'
        '<p>The account will proceed with the normal deletion schedule.</p>'
        '<p style="font-size:13px;opacity:.8;margin-top:16px;">You can close this window.</p>'
        '</div></body></html>'
    )


def _approval_form(token: str, name: str, email: str, offboard: str, ext_label: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Approve Extension</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;margin:40px auto;padding:20px;">
<div style="background:#0078d4;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
  <h2 style="margin:0;">&#9989; Confirm EF Extension Approval</h2>
  <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
</div>
<div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px;">
    <tr style="background:#e9ecef;">
      <td style="padding:8px 14px;font-weight:bold;width:160px;border:1px solid #dee2e6;">Employee</td>
      <td style="padding:8px 14px;border:1px solid #dee2e6;">{name}</td></tr>
    <tr><td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Email</td>
      <td style="padding:8px 14px;border:1px solid #dee2e6;">{email}</td></tr>
    <tr style="background:#e9ecef;">
      <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Offboard Date</td>
      <td style="padding:8px 14px;border:1px solid #dee2e6;">{offboard}</td></tr>
    <tr><td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Extension</td>
      <td style="padding:8px 14px;border:1px solid #dee2e6;">{ext_label}</td></tr>
  </table>
  <form method="post" action="">
    <input type="hidden" name="token" value="{token}">
    <label for="tref" style="font-weight:bold;display:block;margin-bottom:6px;">
      SD+ Ticket Number <span style="color:#888;font-weight:normal;">(optional but recommended)</span>
    </label>
    <input type="text" id="tref" name="ticket_ref" placeholder="e.g. 12345"
      style="width:100%;padding:10px;font-size:14px;border:1px solid #ced4da;border-radius:4px;
      box-sizing:border-box;margin-bottom:16px;">
    <button type="submit"
      style="background:#28a745;color:#fff;padding:12px 28px;border:none;border-radius:4px;
      font-size:15px;cursor:pointer;width:100%;">
      &#9989; Approve Extension
    </button>
  </form>
  <p style="font-size:12px;color:#888;margin-top:14px;">
    By approving, you confirm HR and InfoSec have approved via the SD+ ticket.
    The extension is applied in the next daily automation run (9:00 AM UTC).
  </p>
</div></body></html>"""


def _success_page(name: str, ext_type: str, ticket: str) -> str:
    ticket_info = f" (Ticket: {ticket})" if ticket else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<title>Approved</title></head>'
        '<body style="font-family:Segoe UI,Arial,sans-serif;max-width:600px;margin:40px auto;padding:20px;">'
        '<div style="background:#28a745;color:#fff;padding:24px;border-radius:6px;">'
        f'<h2>&#9989; Extension Approved</h2>'
        f'<p>Email forwarding for <strong>{name}</strong> has been approved{ticket_info}.</p>'
        f'<p>Extension ({ext_type}) will be applied in the next daily run (9:00 AM UTC).</p>'
        '<p style="font-size:13px;opacity:.85;margin-top:16px;">You can close this window.</p>'
        '</div></body></html>'
    )


# ---------------------------------------------------------------------------
# Compliance record helper
# ---------------------------------------------------------------------------

def _update_compliance(store: Any, user_id: str, **updates: Any) -> None:
    """Merge updates into the ComplianceExport record for user_id."""
    existing = store.get_compliance_record(user_id) or {}
    existing["userId"] = user_id
    existing.update(updates)
    store.upsert_compliance_record(existing)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)
