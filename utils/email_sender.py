"""
email_sender.py
---------------
SMTP email sender using it-automation-service@netradyne.com via Office 365.

The SMTP password is fetched from Azure Key Vault on first use and cached
for the lifetime of the function instance.  This avoids redundant KV calls
on every email while still keeping the secret out of code and config files.

Four email types (Workflow B):
  ALERT              – Day 25 / 55 / 85 warning sent to manager + IT CC.
                       Instructs manager to raise a ServiceDesk ticket with
                       business justification; no email-reply extension.
  EXTENSION_CONFIRM  – Confirmation sent after IT applies extensionAttribute11
                       (i.e., after HR + Infosec approval).
  DELETION_NOTICE    – Account has been deleted (Day 30 / 60 no-extension path)
  FINAL_DELETION     – Day 90 max-policy deletion with recovery instructions
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, Optional

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

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

    it_email      = os.environ.get("IT_EMAIL", "it-operations@netradyne.com")
    sdp_ticket_url = os.environ.get("SDP_TICKET_URL", "")
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
