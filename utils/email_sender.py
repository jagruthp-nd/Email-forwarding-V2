"""
email_sender.py
---------------
SMTP email sender using it-automation-service@netradyne.com via Office 365.

The SMTP password is fetched from Azure Key Vault on first use and cached
for the lifetime of the function instance.  This avoids redundant KV calls
on every email while still keeping the secret out of code and config files.

Six email types (Workflow B):
  ALERT              – Day ALERT_DAY_1/2/3 warning sent to manager + IT CC.
                       Instructs manager to raise a ServiceDesk ticket with
                       business justification; no email-reply extension.
  EF_REMOVED         – Day 30 / 60: forwarding disabled; account still alive.
                       IT has DELETE_DAY_1/2 grace days to process late approvals.
  EXTENSION_CONFIRM  – Confirmation sent after IT sets CSA ExtStatus to an
                       approved value (EXTEND_TO_30 / EXTEND_TO_60 / EXTENDED_MAX).
  DELETION_NOTICE    – Account deleted (Day DELETE_DAY_1/DELETE_DAY_2 no-extension)
  FINAL_DELETION     – Day 90 max-policy deletion with recovery instructions
  WEEKLY_REPORT      – Friday digest sent to admins (ADMIN_EMAILS env var)
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from .app_config import get_admin_emails, get_servicedesk_ticket_url
from .automation_flags import is_outbound_email_disabled

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Credential cache
# ---------------------------------------------------------------------------

_smtp_password: Optional[str] = None   # cached for instance lifetime


def _get_smtp_password() -> str:
    """Resolve SMTP password: env var first, Key Vault fallback."""
    global _smtp_password
    if _smtp_password:
        return _smtp_password

    # Prefer direct env var (handy for local testing)
    pwd = os.environ.get("SENDER_PASSWORD", "")
    if pwd:
        _smtp_password = pwd
        return _smtp_password

    # Fetch from Key Vault
    kv_name = os.environ.get("KEY_VAULT_NAME", "")
    if not kv_name:
        raise RuntimeError(
            "SMTP password unavailable: set SENDER_PASSWORD env var "
            "or KEY_VAULT_NAME pointing to a vault with secret 'smtp-password'."
        )

    credential = DefaultAzureCredential()
    kv_url = f"https://{kv_name}.vault.azure.net"
    client = SecretClient(vault_url=kv_url, credential=credential)
    secret = client.get_secret("smtp-password")
    _smtp_password = secret.value
    logger.info("SMTP password loaded from Key Vault '%s'", kv_name)
    return _smtp_password


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
    Send one HTML email via Office 365 SMTP (TLS on port 587).

    Returns True on success, False on failure (caller logs the reason).
    """
    if is_outbound_email_disabled():
        logger.info(
            "Outbound email suppressed (EF_DRY_RUN / DISABLE_OUTBOUND_EMAIL): "
            "would send to %s | %s",
            to_address,
            subject,
        )
        return True

    sender = os.environ.get("SENDER_EMAIL", "it-automation-service@netradyne.com")
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.office365.com")
    smtp_port   = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"IT Operations <{sender}>"
    msg["To"]      = to_address
    if cc_address:
        msg["Cc"] = cc_address

    # Plain-text fallback
    plain = (
        "Your email client does not support HTML. "
        "Please contact IT Operations for details."
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    recipients = [to_address]
    if cc_address:
        recipients.append(cc_address)

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, _get_smtp_password())
            server.sendmail(sender, recipients, msg.as_string())
        logger.info("Email sent → %s (cc: %s) | %s", to_address, cc_address, subject)
        return True
    except Exception as exc:
        logger.error("SMTP send failed → %s: %s", to_address, exc)
        return False


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

    it_email       = os.environ.get("IT_EMAIL", "it-operations@netradyne.com")
    sdp_ticket_url = get_servicedesk_ticket_url()
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    offboard_date = record.get("offboardDate", "")
    delete_date   = record.get("deleteDate", "")
    ext_count     = int(record.get("extensionCount", 0))
    max_ext       = 2
    exts_left     = max_ext - ext_count

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
            f'<p style="margin:8px 0 0;">Contact <a href="mailto:{it_email}">{it_email}</a> '
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
      <tr style="background:#e9ecef;">
        <td style="padding:10px 14px;font-weight:bold;border:1px solid #dee2e6;">Extensions Used</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;">{ext_count} of {max_ext} ({exts_left} remaining)</td>
      </tr>
    </table>

    {'<p><strong>No further extensions are available. The account will be permanently deleted on the date shown above.</strong></p>' if is_final else f'''
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
      Questions? Contact us at {it_email}
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email)


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

    it_email       = os.environ.get("IT_EMAIL", "it-operations@netradyne.com")
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
            f'<p style="margin:8px 0 0;">Contact <a href="mailto:{it_email}">{it_email}</a> '
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
      The Azure AD account will be permanently deleted on this date unless an extension is approved.
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
      Questions? Contact us at {it_email}
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email)


def send_extension_confirm(record: Dict[str, Any]) -> bool:
    """
    Confirmation email sent to manager after an extension is approved.
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email      = os.environ.get("IT_EMAIL", "it-operations@netradyne.com")
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
      <tr style="background:#e9ecef;">
        <td style="padding:10px 14px;font-weight:bold;border:1px solid #dee2e6;">Extensions Used</td>
        <td style="padding:10px 14px;border:1px solid #dee2e6;">{ext_count} of {max_ext}</td>
      </tr>
    </table>

    {'<p style="color:#dc3545;font-weight:bold;">This was the final extension. The account will be permanently deleted on the new expiry date. No further extensions can be granted.</p>' if ext_count >= max_ext else ''}

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated confirmation from IT Operations.<br>
      Questions? Contact us at {it_email}
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email)


def send_deletion_notice(record: Dict[str, Any], reason: str) -> bool:
    """
    Notification sent after an account is deleted (Day 30 / 60 paths).

    reason values: 'NO_EF', 'NO_EXTENSION_DAY30', 'NO_EXTENSION_DAY60'
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email      = os.environ.get("IT_EMAIL", "it@netradyne.com")
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

    <div style="background:#fff3cd;border:1px solid #ffeeba;padding:16px;border-radius:4px;margin:20px 0;">
      <h4 style="margin:0 0 8px;color:#856404;">&#128274; Account Recovery (if needed)</h4>
      <p style="margin:0;">If this account is required for business purposes, it <strong>can be recovered
      within 30 days</strong> of deletion. Note that email forwarding will be <u>permanently disabled</u>
      upon recovery.</p>
      <p style="margin:8px 0 0;">To request recovery, email <a href="mailto:{it_email}">{it_email}</a>
      with subject: <em>Account Recovery – {employee_name}</em></p>
    </div>

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated message from IT Operations.
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email)


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
                + u["deleteDate"] + ' (' + str(u["daysLeft"]) + 'd)</td>'
                '<td style="padding:8px 14px;border:1px solid #dee2e6;text-align:center;">'
                + str(u["extCount"]) + '/2</td></tr>'
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
            '<th style="padding:8px 14px;text-align:center;">Extensions</th>'
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
    it_approval_email = os.environ.get("IT_APPROVAL_EMAIL", "")
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
      Clicking Decline logs the refusal — the account will be deleted on schedule.
    </p>
  </div>
</body>
</html>"""

    return _send(it_approval_email, subject, html, cc_address=None)


def send_no_ef_admin_notice(
    record: Dict[str, Any],
    *,
    event: str = "registered",
    days_elapsed: int = 0,
    days_until_cleanup: Optional[int] = None,
) -> int:
    """
    Notify ADMIN_EMAILS that a terminated account has no email forwarding.

    event: 'registered' | 'reminder'
    Returns count of admin inboxes successfully sent (0 if none configured).
    """
    admins = get_admin_emails()
    if not admins:
        logger.warning(
            "NO_EF admin notice skipped for userId=%s – ADMIN_EMAILS not set",
            record.get("userId"),
        )
        return 0

    name = record.get("displayName", "Unknown user")
    email = record.get("userEmail", "")
    offboard = record.get("offboardDate", "")
    user_id = record.get("userId", "")
    region = record.get("usageLocation", "")

    if event == "reminder":
        subject = f"[NO EF] Manual deletion reminder – {name} (day {days_elapsed})"
        intro = (
            f"This account still has <strong>no email forwarding</strong> and has not been "
            f"deleted. It has been <strong>{days_elapsed} days</strong> since offboarding."
        )
        if days_until_cleanup is not None:
            intro += (
                f" The monthly cleanup job may delete it in approximately "
                f"<strong>{days_until_cleanup} days</strong> unless exempted or deleted manually."
            )
    else:
        subject = f"[NO EF] New terminated account – manual deletion required – {name}"
        intro = (
            "A newly tracked terminated account was registered with "
            "<strong>no active email forwarding</strong>. "
            "Per policy, IT should delete this account during offboarding; "
            "the monthly cleanup scan is the automated safety net."
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">
  <div style="background:#6c757d;color:#fff;padding:20px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:18px;">No Email Forwarding – Admin Notice</h2>
  </div>
  <div style="background:#f8f9fa;padding:20px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>{intro}</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr><td style="padding:6px 0;color:#666;">Employee</td><td><strong>{name}</strong> ({email})</td></tr>
      <tr><td style="padding:6px 0;color:#666;">Offboard date</td><td>{offboard}</td></tr>
      <tr><td style="padding:6px 0;color:#666;">Region</td><td>{region or '—'}</td></tr>
      <tr><td style="padding:6px 0;color:#666;">Object ID</td><td style="font-size:12px;">{user_id}</td></tr>
    </table>
    <p style="font-size:13px;color:#666;margin-top:16px;">
      To exclude an account from automated cleanup deletion, set
      <code>deletionExempt=true</code> on the UserTracking row or add the user to
      <strong>DELETION_EXEMPT_USER_IDS</strong> / <strong>DELETION_EXEMPT_EMAILS</strong>
      in Function App settings.
    </p>
  </div>
</body>
</html>"""

    sent = 0
    for admin in admins:
        if _send(admin, subject, html):
            sent += 1
    return sent


def send_final_deletion_notice(record: Dict[str, Any]) -> bool:
    """
    Final notification for Day 90 max-policy deletion.
    Explicitly states no further extensions are possible.
    """
    manager_email = record.get("managerEmail", "")
    if not manager_email:
        return False

    it_email      = os.environ.get("IT_EMAIL", "it-operations@netradyne.com")
    employee_name = record.get("displayName", "the terminated employee")
    employee_mail = record.get("userEmail", "")
    deleted_date  = datetime.now(timezone.utc).strftime("%B %d, %Y")
    recovery_days = int(os.environ.get("RECOVERY_GRACE_DAYS", "30"))

    subject = f"Account Deleted (Max Policy) – {employee_name} – {deleted_date}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:640px;margin:auto;padding:20px;">

  <div style="background:#dc3545;color:#fff;padding:24px;border-radius:6px 6px 0 0;">
    <h2 style="margin:0;font-size:20px;">Account Permanently Deleted – Max Policy Reached</h2>
    <p style="margin:6px 0 0;opacity:.85;font-size:14px;">Netradyne IT Operations</p>
  </div>

  <div style="background:#f8f9fa;padding:24px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    <p>Dear Manager,</p>

    <p>The Azure AD account for <strong>{employee_name}</strong> ({employee_mail}) has been
    <strong>permanently deleted</strong> on <strong>{deleted_date}</strong> as per the company's
    maximum email forwarding policy of <strong>90 days</strong>.</p>

    <p>All email forwarding for this account has ceased.  No further extensions can be granted.</p>

    <div style="background:#fff3cd;border:1px solid #ffeeba;padding:16px;border-radius:4px;margin:20px 0;">
      <h4 style="margin:0 0 8px;color:#856404;">&#128274; Account Recovery (for other business purposes only)</h4>
      <p style="margin:0;">
        If this account is needed for <u>reasons other than email forwarding</u> (e.g., accessing
        mailbox archive, SharePoint permissions), it can be restored within <strong>{recovery_days} days</strong>
        of this notice.
      </p>
      <ul style="margin:8px 0 0;padding-left:20px;">
        <li>Email forwarding will be <strong>permanently disabled</strong> on recovery</li>
        <li>Mailbox access may be limited to read-only archive</li>
        <li>Recovery must be approved by IT Director</li>
      </ul>
      <p style="margin:8px 0 0;">
        To request recovery, email <a href="mailto:{it_email}">{it_email}</a> with subject:<br>
        <em>Account Recovery Request – {employee_name}</em>
      </p>
    </div>

    <p style="font-size:13px;color:#888;border-top:1px solid #dee2e6;padding-top:16px;margin-top:24px;">
      This is an automated message from IT Operations. Reference: MAX_POLICY_DAY90<br>
      Questions? Contact <a href="mailto:{it_email}">{it_email}</a>
    </p>
  </div>
</body>
</html>"""

    return _send(manager_email, subject, html, cc_address=it_email)
