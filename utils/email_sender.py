"""
email_sender.py
---------------
Outbound email via Microsoft Graph Mail.Send (application permission).

Sends as SENDER_EMAIL (e.g. it-automation-service@netradyne.com) using the
Function App managed identity or local app-registration credentials
(AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET).  No SMTP password
or Key Vault secret is required.

Email types (Workflow B):
  ALERT              – Day ALERT_DAY_1/2/3 warning sent to manager + IT CC.
  EF_REMOVED         – Day 30 / 60: forwarding disabled; account still alive.
  EXTENSION_CONFIRM  – Confirmation after CSA ExtStatus extension approval.
  DELETION_NOTICE    – Account deleted (Day DELETE_DAY_1/DELETE_DAY_2)
  FINAL_DELETION     – Day 90 max-policy deletion
  WEEKLY_REPORT      – Friday digest to ADMIN_EMAILS
  NO_EF_ADMIN        – Admin notice for accounts without forwarding
  IT_APPROVAL        – Approve/Decline links for IT
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .app_config import (
    get_admin_emails,
    get_it_approval_email,
    get_it_email,
    get_report_emails,
    get_sender_email,
    get_servicedesk_ticket_url,
    get_sharepoint_report_url,
)
from .automation_flags import apply_test_email_routing, is_outbound_email_disabled
from .graph_api import send_mail

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core send helper
# ---------------------------------------------------------------------------

def _send(
    to_address: str,
    subject: str,
    html_body: str,
    cc_address: Optional[str] = None,
) -> bool:
    """
    Send one HTML email via Microsoft Graph (Mail.Send).

    Returns True on success, False on failure (caller logs the reason).
    """
    to_address, cc_address = apply_test_email_routing(to_address, cc_address)

    if is_outbound_email_disabled():
        logger.info(
            "Outbound email suppressed (EF_DRY_RUN / DISABLE_OUTBOUND_EMAIL): "
            "would send to %s (cc: %s) | %s",
            to_address,
            cc_address or "",
            subject,
        )
        return True

    return send_mail(
        sender=get_sender_email(),
        to_address=to_address,
        subject=subject,
        html_body=html_body,
        cc_address=cc_address,
    )


# ---------------------------------------------------------------------------
# Public email functions (one per email type)
# ---------------------------------------------------------------------------

def send_ef_alert(record: Dict[str, Any], days_remaining: int, is_final: bool = False) -> bool:
    """
    Day ALERT_DAY_1 / ALERT_DAY_2 / ALERT_DAY_3 alert to manager with CC to IT.

    Parameters
    ----------
    record         : UserTracking dict
    days_remaining : How many days until the account is deleted
    is_final       : True when this is the Day-ALERT_DAY_3 final warning (max policy)
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        logger.warning("No manager email for userId=%s – skipping alert", record.get("userId"))
        return False

    it_email       = get_it_email()
    sdp_ticket_url = get_servicedesk_ticket_url()
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    offboard_date = record.get("offboardDate", "")
    delete_date   = record.get("deleteDate", "")
    it_contact    = it_email or "IT Operations"

    if is_final:
        urgency_banner = (
            '<div style="background:#dc3545;color:#fff;padding:12px 20px;'
            'border-radius:4px;margin:20px 0;font-weight:bold;">'
            '&#9888; FINAL NOTICE – Maximum extension policy (90 days) '
            'will be reached. No further extensions are possible after this.</div>'
        )
        subject = f"[FINAL] Email Forwarding – {employee_name} – Account Deletion in {days_remaining} days"
    else:
        urgency_banner = (
            '<div style="background:#fd7e14;color:#fff;padding:12px 20px;'
            'border-radius:4px;margin:20px 0;font-weight:bold;">'
            f'&#9888; Action Required – Email forwarding expires in <u>{days_remaining} days</u>.</div>'
        )
        subject = f"Email Forwarding Expiration – {employee_name} – Action Required"

    # Build the SDP button only when the URL is configured
    if sdp_ticket_url:
        sdp_button = (
            f'<a href="{sdp_ticket_url}" target="_blank" '
            'style="display:inline-block;margin-top:12px;padding:10px 20px;'
            'background:#0078d4;color:#fff;text-decoration:none;'
            'border-radius:4px;font-weight:bold;">&#128196; Raise Extension Request in ServiceDesk</a>'
        )
    else:
        sdp_button = (
            f'<p style="margin:8px 0 0;">Contact {it_contact} '
            'to get the ServiceDesk ticket template link.</p>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">

  <div style="background:#0078d4;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">Email Forwarding Expiration Notice</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">

    <p>Dear Manager,</p>

    {urgency_banner}

    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr style="background:#e9ecef;">
        <td style="padding:10px 14px;font-weight:bold;width:180px;border:1px solid #dee2e6;">Employee</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;">{employee_name}</td>
      </tr>
      <tr>
        <td style="padding:10px 14px;font-weight:bold;border:1px solid #dee2e6;">Email</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;">{employee_mail}</td>
      </tr>
      <tr style="background:#e9ecef;">
        <td style="padding:10px 14px;font-weight:bold;border:1px solid #dee2e6;">Offboarding Date</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;">{offboard_date}</td>
      </tr>
      <tr>
        <td style="padding:10px 14px;font-weight:bold;border:1px solid #dee2e6;color:#dc3545;">Forwarding Expires</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;font-weight:bold;color:#dc3545;">{delete_date}</td>
      </tr>
    </table>

    {'<p><strong>No further extensions are available. The account will be deleted on the date shown above.</strong></p>' if is_final else f'''
    <div style="background:#d1ecf1;border:1px solid #bee5eb;padding:16px;border-radius:4px;margin:20px 0;">
      <h3 style="margin:0 0 10px;color:#0c5460;">&#x2192; To request a 30-day extension:</h3>
      <ol style="margin:0;padding-left:20px;line-height:1.7;">
        <li>Raise a <strong>ServiceDesk ticket</strong> with your business justification and the
            duration requested (maximum 30 days per extension).</li>
        <li>HR and Infosec will review and approve the request
            <em>(SLA: 2 business days each)</em>.</li>
        <li>Upon approval, IT will process the extension in Azure AD.
            You will receive a <strong>confirmation email</strong> once the new expiry is active.</li>
      </ol>
      {sdp_button}
      <p style="margin:12px 0 0;font-weight:bold;color:#721c24;">
        &#9888; Your request must be raised and fully approved before
        <u>{delete_date}</u> to prevent account deletion.
      </p>
    </div>
    <p style="font-size:13px;color:#666;">
      Company policy: Maximum 90 days of email forwarding from offboarding date (2 x 30-day extensions).
      Extensions require HR and Infosec approval per compliance requirements (SDP).
    </p>
    '''}

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated message from IT Operations.<br>
      Questions? Contact us at {it_contact}
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email or None)


def send_ef_removed_notice(record: Dict[str, Any], days_until_delete: int, reason: str) -> bool:
    """
    Notification sent when email forwarding is disabled at the Day-30 or
    Day-60 grace point because no extension was approved in time.

    The account is NOT deleted yet.  There are still *days_until_delete* days
    for IT to process a late approval (DELETE_DAY_1 / DELETE_DAY_2 thresholds).
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email       = get_it_email()
    it_contact     = it_email or "IT Operations"
    sdp_ticket_url = get_servicedesk_ticket_url()
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    delete_date   = record.get("deleteDate", "")

    reason_text = {
        "NO_EXTENSION_DAY30": "no approved extension request was received before the 30-day forwarding deadline",
        "NO_EXTENSION_DAY60": "no approved second extension was received before the 60-day deadline",
    }.get(reason, "the forwarding period has expired with no approved extension")

    if sdp_ticket_url:
        sdp_button = (
            f'<a href="{sdp_ticket_url}" target="_blank" '
            'style="display:inline-block;margin-top:12px;padding:10px 20px;'
            'background:#0078d4;color:#fff;text-decoration:none;'
            'border-radius:4px;font-weight:bold;">&#128196; Raise Extension Request in ServiceDesk</a>'
        )
    else:
        sdp_button = (
            f'<p style="margin:8px 0 0;">Contact {it_contact} '
            'to raise a ServiceDesk extension ticket.</p>'
        )

    subject = f"[Action Required] Email Forwarding Disabled – {employee_name} – Account Deletion in {days_until_delete} days"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">

  <div style="background:#dc3545;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">&#9888; Email Forwarding Disabled – Account Deletion Pending</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>Dear Manager,</p>
    <p>Email forwarding for <strong>{employee_name}</strong> ({employee_mail}) has been
    <strong>disabled</strong> because {reason_text}.</p>

    <div style="background:#fff3cd;border:1px solid #ffc107;padding:16px;border-radius:4px;margin:20px 0;">
      <strong>&#9200; Account Deletion in {days_until_delete} day{"s" if days_until_delete != 1 else ""}
      (on {delete_date})</strong><br>
      The Azure AD account will be deleted on this date unless an extension is approved.
    </div>

    <table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">
      <tr style="background:#e9ecef;">
        <td style="padding:8px 14px;font-weight:bold;width:180px;border:1px solid #dee2e6;">Employee</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{employee_name}</td>
      </tr>
      <tr>
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Email</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{employee_mail}</td>
      </tr>
      <tr style="background:#e9ecef;">
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;color:#dc3545;">Account Deleted On</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;font-weight:bold;color:#dc3545;">{delete_date}</td>
      </tr>
    </table>

    <div style="background:#d1ecf1;border:1px solid #bee5eb;padding:16px;border-radius:4px;margin:20px 0;">
      <h3 style="margin:0 0 8px;color:#0c5460;">&#x2192; To still request an extension (late approval):</h3>
      <ol style="margin:0;padding-left:20px;line-height:1.8;">
        <li>Raise a <strong>ServiceDesk ticket</strong> immediately with your business justification.</li>
        <li>HR and Infosec must approve before the deletion date shown above.</li>
        <li>IT will update the Azure AD attribute. Forwarding will be re-enabled automatically.</li>
      </ol>
      {sdp_button}
    </div>

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated message from IT Operations.<br>
      Questions? Contact us at {it_contact}
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email or None)


def send_extension_confirm(record: Dict[str, Any]) -> bool:
    """
    Confirmation email sent to manager after an extension is approved.
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email      = get_it_email()
    it_contact    = it_email or "IT Operations"
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    new_delete    = record.get("deleteDate", "")
    ext_count     = int(record.get("extensionCount", 0))
    max_ext       = 2

    subject = f"Email Forwarding Extended – {employee_name} – Confirmed"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">

  <div style="background:#28a745;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">&#10003; Email Forwarding Extended</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>Dear Manager,</p>
    <p>Your ServiceDesk extension request for <strong>{employee_name}</strong> has been
    approved by HR and Infosec, and the extension has been applied by IT.</p>

    <table style="width:100%;border-collapse:collapse;margin:16px 0;">
      <tr style="background:#e9ecef;">
        <td style="padding:10px 14px;font-weight:bold;width:180px;border:1px solid #dee2e6;">Employee</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;">{employee_name} ({employee_mail})</td>
      </tr>
      <tr>
        <td style="padding:10px 14px;font-weight:bold;border:1px solid #dee2e6;color:#28a745;">New Expiry Date</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;font-weight:bold;color:#28a745;">{new_delete}</td>
      </tr>
    </table>

    {'<p style="color:#dc3545;font-weight:bold;">This was the final extension. The account will be deleted on the new expiry date. No further extensions can be granted.</p>' if ext_count >= max_ext else ''}

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated confirmation from IT Operations.<br>
      Questions? Contact us at {it_contact}
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email or None)


def _send_admin_soft_delete_notice(
    record: Dict[str, Any],
    *,
    reason: str,
    max_policy: bool = False,
) -> int:
    """Admin-only notice: deletion is Entra soft-delete (recycle bin ~30 days)."""
    recipients = list(get_admin_emails())
    it_email = get_it_email()
    if it_email and it_email not in recipients:
        recipients.append(it_email)
    if not recipients:
        return 0

    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    user_id = record.get("userId", "")
    deleted_date = datetime.now(timezone.utc).strftime("%B %d, %Y")
    recovery_days = int(os.environ.get("RECOVERY_GRACE_DAYS", "30"))
    policy = "max policy (90 days)" if max_policy else reason

    subject = f"[EF Admin] Soft-delete completed – {employee_name}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">
  <div style="background:#243a5e;color:#fff;padding:20px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:18px;">Admin notice – Soft delete (not permanent)</h2>
  </div>
  <div style="background:#f8f9fa;padding:20px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p><strong>{employee_name}</strong> ({employee_mail}) was <strong>soft-deleted</strong>
    on {deleted_date}.</p>
    <ul>
      <li>Graph action: Entra recycle bin (soft delete) — <em>not</em> a hard/permanent delete</li>
      <li>Microsoft auto-purges the recycle bin after ~{recovery_days} days</li>
      <li>Reason / path: {policy}</li>
      <li>User ID: {user_id or '—'}</li>
    </ul>
    <p style="font-size:13px;color:#666;">Managers receive a plain “account deleted” notice without this detail.</p>
  </div>
</body>
</html>"""
    sent = 0
    for addr in recipients:
        if _send(addr, subject, html):
            sent += 1
    return sent


def send_deletion_notice(record: Dict[str, Any], reason: str) -> bool:
    """
    Manager notification after account deletion (Day 30 / 60 paths).
    Managers see plain “deleted” wording; admins get soft-delete detail separately.

    reason values: 'NO_EF', 'NO_EXTENSION_DAY30', 'NO_EXTENSION_DAY60'
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email      = get_it_email()
    it_contact    = it_email or "IT Operations"
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    deleted_date  = datetime.now(timezone.utc).strftime("%B %d, %Y")

    reason_text_map = {
        "NO_EF":              "No email forwarding was configured for this account.",
        "NO_EXTENSION_DAY30": (
            "No approved extension request was processed before the 30-day deadline. "
            "A ServiceDesk ticket with HR and Infosec approval was required."
        ),
        "NO_EXTENSION_DAY60": (
            "No approved second extension was processed before the 60-day deadline. "
            "A ServiceDesk ticket with HR and Infosec approval was required."
        ),
    }
    reason_text = reason_text_map.get(reason, "As per company offboarding policy.")

    subject = f"Account Deleted – {employee_name} – {deleted_date}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">

  <div style="background:#6c757d;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">Account Deleted</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>Dear Manager,</p>
    <p>The Azure AD account for <strong>{employee_name}</strong> ({employee_mail}) has been
    <strong>deleted</strong> on <strong>{deleted_date}</strong>.</p>

    <p>Reason: {reason_text}</p>

    <p>If this account is still required for business purposes, contact
    {('<a href="mailto:' + it_email + '">' + it_email + '</a>') if it_email else it_contact}
    with subject: <em>Account Recovery – {employee_name}</em>.</p>

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated message from IT Operations.
    </p>
  </div>
</body>
</html>"""

    ok = _send(manager_email, subject, html)
    _send_admin_soft_delete_notice(record, reason=reason, max_policy=False)
    return ok


def send_weekly_report(data: Dict[str, Any], admin_emails: List[str]) -> int:
    """
    Send the weekly EF Automation summary digest to all admin recipients.

    Parameters
    ----------
    data         : Report data dict produced by weekly_report.generate_weekly_report()
    admin_emails : List of admin email addresses (from ADMIN_EMAILS env var)

    Returns the number of successfully delivered emails.
    """
    if not admin_emails:
        return 0

    report_date   = data.get("report_date", "")
    period_start  = data.get("report_period_start", "")
    total_active  = data.get("total_active", 0)
    total_deleted = data.get("total_deleted_ever", 0)
    upcoming      = data.get("upcoming_deadlines", [])
    extensions    = data.get("extensions_this_week", [])
    deletions     = data.get("deletions_this_week", [])
    alerts        = data.get("alerts_this_week", [])
    status_counts = data.get("status_counts", {})

    subject = f"[EF Automation] Weekly Report – {report_date}"

    # ── Status breakdown rows ─────────────────────────────────────────────
    status_label = {
        "MONITORING":    "Monitoring (no alert yet)",
        "ALERT_SENT":    "Alert sent – awaiting ticket",
        "EXTENDED":      "Extended (1st extension active)",
        "EXTENDED_MAX":  "Extended (max – final period)",
        "DELETED":       "Deleted",
    }
    _zebra = ' style="background:#f8f9fa"'  # pre-computed to avoid backslash-in-fstring (Py 3.8)
    status_rows_html = "".join(
        '<tr' + ('' if i % 2 else _zebra) + '>'
        '<td style="padding:8px 14px;border:1px solid #dee2e6;">'
        + status_label.get(code, code)
        + '</td><td style="padding:8px 14px;border:1px solid #dee2e6;text-align:center;">'
        '<strong>' + str(count) + '</strong></td></tr>'
        for i, (code, count) in enumerate(sorted(status_counts.items()))
    )

    # ── Upcoming deadline rows ────────────────────────────────────────────
    if upcoming:
        _urgent_style = 'color:#dc3545;font-weight:bold;'
        upcoming_row_parts = []
        for i, u in enumerate(upcoming):
            deadline_style = _urgent_style if u["daysLeft"] <= 3 else ""
            upcoming_row_parts.append(
                '<tr' + ('' if i % 2 else _zebra) + '>'
                '<td style="padding:8px 14px;border:1px solid #dee2e6;">' + u["name"] + '</td>'
                '<td style="padding:8px 14px;border:1px solid #dee2e6;">' + u["email"] + '</td>'
                '<td style="padding:8px 14px;border:1px solid #dee2e6;' + deadline_style + '">'
                + u["deleteDate"] + ' (' + str(u["daysLeft"]) + 'd)</td></tr>'
            )
        upcoming_rows = "".join(upcoming_row_parts)
        upcoming_section = (
            '<h3 style="color:#dc3545;margin:24px 0 8px;">&#9888; Upcoming Deadlines (next 14 days) &ndash; '
            + str(len(upcoming))
            + '</h3><table style="width:100%;border-collapse:collapse;font-size:13px;">'
            '<tr style="background:#dc3545;color:#fff;">'
            '<th style="padding:8px 14px;text-align:left;">Employee</th>'
            '<th style="padding:8px 14px;text-align:left;">Email</th>'
            '<th style="padding:8px 14px;text-align:left;">Deadline</th>'
            '</tr>' + upcoming_rows + '</table>'
        )
    else:
        upcoming_section = (
            '<p style="color:#28a745;">&#10003; No accounts expiring in the next 14 days.</p>'
        )

    # ── Activity rows helper ──────────────────────────────────────────────
    def _audit_rows(items: List[Dict[str, Any]]) -> str:
        if not items:
            return "<tr><td colspan='3' style='padding:8px 14px;color:#888;'>None this week</td></tr>"
        parts = []
        for i, r in enumerate(items):
            ts = (r.get("executedAt") or "")[:19].replace("T", " ")
            parts.append(
                '<tr' + ('' if i % 2 else _zebra) + '>'
                '<td style="padding:8px 14px;border:1px solid #dee2e6;">'
                + r.get("PartitionKey", "")
                + '</td><td style="padding:8px 14px;border:1px solid #dee2e6;">'
                + r.get("action", "")
                + '</td><td style="padding:8px 14px;border:1px solid #dee2e6;">'
                + ts + '</td></tr>'
            )
        return "".join(parts)

    activity_table_header = (
        '<tr style="background:#495057;color:#fff;">'
        '<th style="padding:8px 14px;text-align:left;">User ID</th>'
        '<th style="padding:8px 14px;text-align:left;">Action</th>'
        '<th style="padding:8px 14px;text-align:left;">Time (UTC)</th></tr>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:780px;margin:auto;padding:20px;">

  <div style="background:#0078d4;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">📊 EF Automation – Weekly Report</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">
      Period: {period_start} → {report_date} &nbsp;|&nbsp; Netradyne IT Operations
    </p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">

    <!-- Summary cards -->
    <table style="width:100%;border-collapse:separate;border-spacing:10px;margin-bottom:8px;">
      <tr>
        <td style="background:#0078d4;color:#fff;padding:16px;border-radius:6px;text-align:center;width:25%;">
          <div style="font-size:28px;font-weight:bold;">{total_active}</div>
          <div style="font-size:12px;opacity:.85;">Active EF Accounts</div>
        </td>
        <td style="background:#fd7e14;color:#fff;padding:16px;border-radius:6px;text-align:center;width:25%;">
          <div style="font-size:28px;font-weight:bold;">{len(upcoming)}</div>
          <div style="font-size:12px;opacity:.85;">Deadlines in 14 days</div>
        </td>
        <td style="background:#28a745;color:#fff;padding:16px;border-radius:6px;text-align:center;width:25%;">
          <div style="font-size:28px;font-weight:bold;">{len(extensions)}</div>
          <div style="font-size:12px;opacity:.85;">Extensions this week</div>
        </td>
        <td style="background:#6c757d;color:#fff;padding:16px;border-radius:6px;text-align:center;width:25%;">
          <div style="font-size:28px;font-weight:bold;">{len(deletions)}</div>
          <div style="font-size:12px;opacity:.85;">Deletions this week</div>
        </td>
      </tr>
    </table>

    <!-- Status breakdown -->
    <h3 style="margin:24px 0 8px;">Account Status Breakdown</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="background:#495057;color:#fff;">
        <th style="padding:8px 14px;text-align:left;">Status</th>
        <th style="padding:8px 14px;text-align:center;">Count</th>
      </tr>
      {status_rows_html}
    </table>

    <!-- Upcoming deadlines -->
    {upcoming_section}

    <!-- Extensions this week -->
    <h3 style="margin:24px 0 8px;">Extensions Applied This Week – {len(extensions)}</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      {activity_table_header}
      {_audit_rows(extensions)}
    </table>

    <!-- Deletions this week -->
    <h3 style="margin:24px 0 8px;">Accounts Deleted This Week – {len(deletions)}</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      {activity_table_header}
      {_audit_rows(deletions)}
    </table>

    <!-- Alerts this week -->
    <h3 style="margin:24px 0 8px;">Alert Emails Sent This Week – {len(alerts)}</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      {activity_table_header}
      {_audit_rows(alerts)}
    </table>

    <p style="font-size:12px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated weekly digest from EF Automation.<br>
      To add or remove report recipients, update the <strong>ADMIN_EMAILS</strong>
      application setting (comma-separated) in the Azure Function App configuration.
    </p>
  </div>
</body>
</html>"""

    sent = 0
    for admin in admin_emails:
        if _send(admin, subject, html):
            sent += 1
    logger.info("Weekly report sent to %d/%d admins", sent, len(admin_emails))
    return sent


def send_it_approval_notification(
    record: Dict[str, Any],
    approve_url: str,
    decline_url: str,
    ext_type: str,
) -> bool:
    """
    Notification sent to the IT approval mailbox when an EF alert fires.
    Contains Approve and Decline buttons (links to the HTTP trigger).
    """
    it_approval_email = get_it_approval_email()
    if not it_approval_email:
        logger.warning("IT_APPROVAL_EMAIL not set – IT approval notification not sent")
        return False

    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    manager_email = record.get("managerEmail", "")
    offboard_date = record.get("offboardDate", "")
    ext_count     = int(record.get("extensionCount", 0))
    delete_date   = record.get("deleteDate", "")

    ext_label = {
        "EXTEND_TO_30": "1st extension – 30 more days",
        "EXTEND_TO_60": "2nd extension – 30 more days",
        "EXTENDED_MAX": "Final extension – max policy (Day 90)",
    }.get(ext_type, ext_type)

    subject = f"[IT Action Required] EF Extension Approval – {employee_name}"
    ext_num = ext_count + 1

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:660px;margin:auto;padding:20px;">
  <div style="background:#0078d4;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">&#128276; EF Extension Approval Required</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>
  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>A manager has been notified that their direct report's email forwarding is expiring.
    If a valid SD+ ticket with HR and InfoSec approval exists, approve the extension below.</p>

    <table style="width:100%;border-collapse:collapse;font-size:13px;margin:16px 0;">
      <tr style="background:#e9ecef;">
        <td style="padding:8px 14px;font-weight:bold;width:180px;border:1px solid #dee2e6;">Employee</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{employee_name}</td>
      </tr>
      <tr>
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Email</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{employee_mail}</td>
      </tr>
      <tr style="background:#e9ecef;">
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Manager</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{manager_email}</td>
      </tr>
      <tr>
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Offboard Date</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{offboard_date}</td>
      </tr>
      <tr style="background:#e9ecef;">
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;">Extension Type</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;">{ext_label} (extension #{ext_num})</td>
      </tr>
      <tr>
        <td style="padding:8px 14px;font-weight:bold;border:1px solid #dee2e6;color:#dc3545;">Current Delete Date</td>
        <td style="padding:8px 14px;border:1px solid #dee2e6;font-weight:bold;color:#dc3545;">{delete_date}</td>
      </tr>
    </table>

    <div style="background:#fff3cd;border:1px solid #ffc107;padding:14px;border-radius:4px;margin:16px 0;font-size:13px;">
      &#9203; This approval link is valid for <strong>7 days</strong>.
      After that, the extension must be set manually via Azure AD portal.
    </div>

    <table style="width:100%;border-collapse:collapse;margin-top:20px;">
      <tr>
        <td style="padding-right:10px;width:50%;">
          <a href="{approve_url}" target="_blank"
            style="display:block;text-align:center;padding:14px;background:#28a745;color:#fff;
            text-decoration:none;border-radius:4px;font-weight:bold;font-size:15px;">
            &#9989; Approve Extension
          </a>
        </td>
        <td style="padding-left:10px;width:50%;">
          <a href="{decline_url}" target="_blank"
            style="display:block;text-align:center;padding:14px;background:#dc3545;color:#fff;
            text-decoration:none;border-radius:4px;font-weight:bold;font-size:15px;">
            &#10060; Decline
          </a>
        </td>
      </tr>
    </table>

    <p style="font-size:12px;color:#888;border-top:1px solid #dee2e6;padding-top:14px;margin-top:24px;">
      Clicking Approve opens a confirmation page where you can enter the SD+ ticket number.<br>
      Clicking Decline logs the refusal — the account will be <strong>soft-deleted</strong>
      on schedule (Entra recycle bin; Microsoft purges after ~30 days). This is not a hard delete.
    </p>
  </div>
</body>
</html>"""

    return _send(it_approval_email, subject, html, cc_address=None)


def send_offboard_consolidated_report(
    *,
    report_date: str,
    new_no_ef: List[Dict[str, Any]],
    new_with_ef: List[Dict[str, Any]],
    overdue_no_ef: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    period_label: str = "Weekly",
    period_start: str = "",
) -> int:
    """
    Consolidated weekly/monthly report for IT / team mailbox (REPORT_EMAILS).

    SharePoint folder link is hyperlinked when configured.
    Newly tracked EF rows include forwarding target + manager.
    """
    recipients = get_report_emails()
    if not recipients:
        logger.warning("Consolidated offboard report skipped – REPORT_EMAILS/ADMIN_EMAILS not set")
        return 0

    summary = summary or {}
    sp_marker = "<!--SP_ARCHIVE_BLOCK-->"

    def _rows_no_ef(records: List[Dict[str, Any]], with_days: bool = False) -> str:
        cols = 6 if with_days else 5
        if not records:
            return f'<tr><td colspan="{cols}" style="padding:10px;color:#666;">None</td></tr>'
        parts = []
        for r in records:
            parts.append(
                "<tr>"
                f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('displayName','')}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;font-size:12px;'>{r.get('userEmail','')}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('offboardDate','')}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('usageLocation','') or '—'}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;font-size:12px;'>{r.get('managerEmail','') or '—'}</td>"
                + (f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('daysElapsed','')}</td>" if with_days else "")
                + "</tr>"
            )
        return "".join(parts)

    def _rows_ef(records: List[Dict[str, Any]]) -> str:
        if not records:
            return '<tr><td colspan="6" style="padding:10px;color:#666;">None</td></tr>'
        parts = []
        for r in records:
            parts.append(
                "<tr>"
                f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('displayName','')}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;font-size:12px;'>{r.get('userEmail','')}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('offboardDate','')}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;font-size:12px;'>"
                f"{r.get('forwardingAddress','') or '—'}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;font-size:12px;'>"
                f"{r.get('managerEmail','') or '—'}</td>"
                f"<td style='padding:8px;border:1px solid #dee2e6;'>{r.get('usageLocation','') or '—'}</td>"
                "</tr>"
            )
        return "".join(parts)

    period_line = f"{period_start} → {report_date}" if period_start else report_date
    subject = (
        f"[EF Automation] {period_label} offboard report – {report_date} "
        f"(new NO_EF: {len(new_no_ef)}, new EF: {len(new_with_ef)}, overdue NO_EF: {len(overdue_no_ef)})"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:920px;margin:auto;padding:20px;">
  <div style="background:#243a5e;color:#fff;padding:22px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">{period_label} Offboard / EF Monitor Report</h2>
    <p style="margin:6px 0 0;opacity:.9;font-size:14px;">{period_line} · Netradyne IT Automation</p>
  </div>
  <div style="background:#f8f9fa;padding:22px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    {sp_marker}

    <h3 style="margin:8px 0 10px;font-size:15px;">Period activity summary</h3>
    <table style="border-collapse:collapse;font-size:13px;margin-bottom:20px;">
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Registrations (NO_EF)</td><td><strong>{len(new_no_ef)}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Registrations (EF)</td><td><strong>{len(new_with_ef)}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Alerts</td><td><strong>{summary.get('alerts', summary.get('alerted', '—'))}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Extensions</td><td><strong>{summary.get('extensions', '—')}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Deletions</td><td><strong>{summary.get('deletions', summary.get('deleted', '—'))}</strong></td></tr>
      <tr><td style="padding:4px 12px 4px 0;color:#666;">Active tracked</td><td><strong>{summary.get('total_active', '—')}</strong></td></tr>
    </table>

    <h3 style="margin:18px 0 8px;font-size:15px;">Newly tracked – No email forwarding ({len(new_no_ef)})</h3>
    <p style="font-size:13px;color:#666;margin-top:0;">IT should delete these during offboarding; monthly cleanup is the safety net.</p>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:18px;">
      <tr style="background:#e9ecef;">
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Name</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Email</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Offboard</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Region</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Manager</th>
      </tr>
      {_rows_no_ef(new_no_ef)}
    </table>

    <h3 style="margin:18px 0 8px;font-size:15px;">Newly tracked – Email forwarding active ({len(new_with_ef)})</h3>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:18px;">
      <tr style="background:#e9ecef;">
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Name</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Email</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Offboard</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Forwarding to</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Manager</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Region</th>
      </tr>
      {_rows_ef(new_with_ef)}
    </table>

    <h3 style="margin:18px 0 8px;font-size:15px;">NO_EF overdue (past reminder day) ({len(overdue_no_ef)})</h3>
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <tr style="background:#e9ecef;">
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Name</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Email</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Offboard</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Region</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Manager</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Days</th>
      </tr>
      {_rows_no_ef(overdue_no_ef, with_days=True)}
    </table>

    <p style="font-size:12px;color:#888;border-top:1px solid #dee2e6;padding-top:14px;margin-top:24px;">
      Recipients: <strong>REPORT_EMAILS</strong> (fallback ADMIN_EMAILS).<br>
      Deletions by this automation are <strong>soft-delete</strong> (Entra recycle bin; Microsoft
      purges after ~30 days) — not hard/permanent delete.<br>
      Operational state remains in Azure Table Storage.
    </p>
  </div>
</body>
</html>"""

    # Upload Excel/CSV archives to SharePoint (not HTML — easier long-term handling)
    from .report_export import build_report_archives
    from .sharepoint import upload_report_file

    folder_url = get_sharepoint_report_url()
    archives = build_report_archives(
        report_date=report_date,
        period_label=period_label,
        period_start=period_start or "",
        new_no_ef=new_no_ef,
        new_with_ef=new_with_ef,
        overdue_no_ef=overdue_no_ef,
        summary=summary,
    )
    uploaded: List[Dict[str, Any]] = []
    last_err = ""
    for filename, content, content_type in archives:
        result = upload_report_file(
            filename=filename,
            content=content,
            content_type=content_type,
        )
        if result.get("ok") and result.get("web_url"):
            uploaded.append(result)
        else:
            last_err = result.get("error") or "upload skipped"
            logger.warning("SharePoint archive failed for %s: %s", filename, last_err)

    if uploaded:
        links = "".join(
            f'<li style="margin:4px 0;"><a href="{u["web_url"]}" target="_blank">'
            f'{u["filename"]}</a></li>'
            for u in uploaded
        )
        primary = uploaded[0]
        sp_block = (
            f'<p style="margin:16px 0;">'
            f'<a href="{primary["web_url"]}" target="_blank" '
            f'style="display:inline-block;padding:10px 18px;background:#0078d4;color:#fff;'
            f'text-decoration:none;border-radius:4px;font-weight:bold;">'
            f'Open report data in SharePoint</a></p>'
            f'<ul style="font-size:13px;color:#666;margin:8px 0;padding-left:20px;">{links}</ul>'
            f'<p style="font-size:12px;color:#666;">Folder: '
            f'<a href="{folder_url or primary["web_url"]}">{folder_url or primary["web_url"]}</a></p>'
        )
    elif folder_url:
        sp_block = (
            f'<p style="margin:16px 0;">'
            f'<a href="{folder_url}" target="_blank" '
            f'style="display:inline-block;padding:10px 18px;background:#0078d4;color:#fff;'
            f'text-decoration:none;border-radius:4px;font-weight:bold;">'
            f'Open report folder in SharePoint</a></p>'
            f'<p style="font-size:12px;color:#856404;">Excel/CSV archive not uploaded '
            f'({last_err or "unknown"}). Folder link still available.</p>'
        )
    else:
        sp_block = (
            '<p style="font-size:12px;color:#888;margin:12px 0;">'
            'SharePoint archive not configured '
            '(set SHAREPOINT_SITE_URL / LIBRARY / FOLDER).</p>'
        )

    html = html.replace(sp_marker, sp_block, 1)

    sent = 0
    for addr in recipients:
        if _send(addr, subject, html):
            sent += 1
    return sent


def send_no_ef_admin_notice(*args: Any, **kwargs: Any) -> int:
    """Deprecated – use send_offboard_consolidated_report. Kept for import compatibility."""
    logger.warning("send_no_ef_admin_notice is deprecated; consolidated report is used instead")
    return 0


def send_final_deletion_notice(record: Dict[str, Any]) -> bool:
    """
    Manager notice for Day 90 max-policy deletion (plain “deleted” wording).
    Admins get soft-delete technical detail in a separate email.
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email      = get_it_email()
    it_contact    = it_email or "IT Operations"
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    deleted_date  = datetime.now(timezone.utc).strftime("%B %d, %Y")

    subject = f"Account Deleted (Max Policy) – {employee_name} – {deleted_date}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">

  <div style="background:#dc3545;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">Account Deleted – Max Policy Reached</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>Dear Manager,</p>

    <p>The Azure AD account for <strong>{employee_name}</strong> ({employee_mail}) has been
    <strong>deleted</strong> on <strong>{deleted_date}</strong> as per the company's
    maximum email forwarding policy of <strong>90 days</strong>.</p>

    <p>All email forwarding has ceased. No further extensions can be granted.</p>

    <p>If this account is still required for business purposes (other than email forwarding),
    contact {('<a href="mailto:' + it_email + '">' + it_email + '</a>') if it_email else it_contact}
    with subject: <em>Account Recovery Request – {employee_name}</em>.</p>

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated message from IT Operations.<br>
      Questions? Contact {it_contact}
    </p>
  </div>
</body>
</html>"""

    ok = _send(manager_email, subject, html)
    _send_admin_soft_delete_notice(record, reason="MAX_POLICY_DAY90", max_policy=True)
    return ok
