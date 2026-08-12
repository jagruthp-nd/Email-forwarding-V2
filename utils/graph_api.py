"""
graph_api.py
------------
Microsoft Graph API wrapper.

All calls use the Function App's System-assigned Managed Identity
(DefaultAzureCredential) – no client secrets stored in code.

Required Graph API application permissions (granted via assign_permissions.ps1):
  - Directory.Read.All                       : list terminated/disabled users
  - User.ReadWrite.All                       : delete accounts
  - MailboxSettings.ReadWrite                : read & clear mailbox forwarding
  - CustomSecAttributeAssignment.ReadWrite.All : read/write Custom Security Attributes
  - Mail.Send                                : send mail as SENDER_EMAIL (no SMTP password)

Workflow B note:
  The extension approval signal is stored as a Custom Security Attribute (CSA):
    Attribute set : CSA_ATTRIBUTE_SET  (default "EFAutomation")
    Attribute name: CSA_ATTRIBUTE_NAME (default "ExtStatus")

  CSA are cloud-native and writable directly in Azure AD / Entra portal —
  unlike onPremisesExtensionAttributes which are read-only from the cloud
  for on-premises synced users.

  Lifecycle of CSA value:
    (absent)        → no alert sent yet
    "EF_ALERT_SENT"  → alert email was sent; IT should raise ticket → get approval → update
    "EXTEND_TO_30"   → IT sets after first approval  (extension 1, deadline Day 30→60)
    "EXTEND_TO_60"   → IT sets after second approval (extension 2, deadline Day 60→90)
    "EXTENDED_MAX"   → IT sets when granting final/max extension (deadline Day 90)
    (cleared)       → extension has been processed
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests
from azure.identity import DefaultAzureCredential
from azure.core.credentials import TokenCredential

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_BETA = "https://graph.microsoft.com/beta"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_credential: Optional[TokenCredential] = None


def _get_credential() -> TokenCredential:
    """Return (and cache) the DefaultAzureCredential."""
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _auth_headers() -> Dict[str, str]:
    """Return Authorization + Content-Type headers for Graph API calls."""
    token = _get_credential().get_token(_GRAPH_SCOPE)
    return {
        "Authorization": f"Bearer {token.token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _get(url: str, *, advanced: bool = False) -> Optional[Dict]:
    """HTTP GET with error handling.

    advanced=True adds ConsistencyLevel: eventual (required for filters like
    employeeType and some directory properties).
    """
    try:
        headers = _auth_headers()
        if advanced:
            headers["ConsistencyLevel"] = "eventual"
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("GET %s → %s %s", url, resp.status_code, (resp.text or "")[:300])
        return None
    except Exception as exc:
        logger.error("GET %s failed: %s", url, exc)
        return None


def _delete(url: str) -> bool:
    """HTTP DELETE; returns True on 204 No Content."""
    from .automation_flags import is_graph_write_disabled

    if is_graph_write_disabled():
        logger.info("DISABLE_GRAPH_WRITES/EF_DRY_RUN: skipped DELETE %s", url)
        return True
    try:
        resp = requests.delete(url, headers=_auth_headers(), timeout=30)
        return resp.status_code == 204
    except Exception as exc:
        logger.error("DELETE %s failed: %s", url, exc)
        return False


def _patch(url: str, payload: Dict) -> bool:
    """HTTP PATCH; returns True on 200 or 204."""
    from .automation_flags import is_graph_write_disabled

    if is_graph_write_disabled():
        logger.info("DISABLE_GRAPH_WRITES/EF_DRY_RUN: skipped PATCH %s", url)
        return True
    try:
        resp = requests.patch(
            url, headers=_auth_headers(), json=payload, timeout=30
        )
        return resp.status_code in (200, 204)
    except Exception as exc:
        logger.error("PATCH %s failed: %s", url, exc)
        return False


def _post(url: str, payload: Optional[Dict] = None) -> Optional[Dict]:
    """HTTP POST; returns JSON body or None."""
    from .automation_flags import is_graph_write_disabled

    if is_graph_write_disabled():
        logger.info("DISABLE_GRAPH_WRITES/EF_DRY_RUN: skipped POST %s", url)
        return {}
    try:
        resp = requests.post(
            url, headers=_auth_headers(), json=payload or {}, timeout=30
        )
        if resp.status_code in (200, 201):
            return resp.json()
        logger.warning("POST %s → %s", url, resp.status_code)
        return None
    except Exception as exc:
        logger.error("POST %s failed: %s", url, exc)
        return None


def send_mail(
    sender: str,
    to_address: str,
    subject: str,
    html_body: str,
    cc_address: Optional[str] = None,
    *,
    save_to_sent_items: bool = True,
) -> bool:
    """
    Send HTML mail via Microsoft Graph application Mail.Send.

    POST /users/{sender}/sendMail

    Not gated by DISABLE_GRAPH_WRITES – outbound mail uses DISABLE_OUTBOUND_EMAIL /
    EF_DRY_RUN / EF_TEST_MODE in email_sender instead.
    Requires application permission Mail.Send (+ admin consent) and that the
    app is allowed to send as *sender* (mailbox / Application Access Policy).
    """
    to_recipients = [{"emailAddress": {"address": to_address}}]
    message: Dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": to_recipients,
    }
    if cc_address:
        message["ccRecipients"] = [{"emailAddress": {"address": cc_address}}]

    payload = {
        "message": message,
        "saveToSentItems": save_to_sent_items,
    }
    url = f"{_GRAPH_BASE}/users/{sender}/sendMail"
    try:
        resp = requests.post(url, headers=_auth_headers(), json=payload, timeout=60)
        # Graph sendMail returns 202 Accepted with empty body on success
        if resp.status_code in (202, 200):
            logger.info("Graph sendMail → %s (cc: %s) | %s", to_address, cc_address, subject)
            return True
        logger.error(
            "Graph sendMail failed → %s status=%s body=%s",
            to_address,
            resp.status_code,
            (resp.text or "")[:500],
        )
        return False
    except Exception as exc:
        logger.error("Graph sendMail failed → %s: %s", to_address, exc)
        return False


# ---------------------------------------------------------------------------
# User queries
# ---------------------------------------------------------------------------

def get_terminated_users() -> List[Dict[str, Any]]:
    """
    Return all Azure AD users where:
      - employeeType eq 'Terminated'
      - accountEnabled eq false
      - onPremisesExtensionAttributes/extensionAttribute10 is not null
        (the offboard date is stored there)

    Handles pagination via @odata.nextLink.

    Note: $expand=manager cannot be combined with the employeeType advanced
    filter (Graph returns Request_UnsupportedQuery).  Manager is resolved
    later via get_manager() when a tracking record is created.
    """
    select_fields = ",".join([
        "id",
        "mail",
        "userPrincipalName",
        "displayName",
        "accountEnabled",
        "employeeType",
        "usageLocation",                  # 2-letter ISO country code used for region-gated deletion
        "country",                        # full country name (fallback display)
        "onPremisesExtensionAttributes",
        "customSecurityAttributes",       # EFAutomation.ExtStatus (Workflow B)
    ])
    # employeeType filter requires Graph advanced query:
    # ConsistencyLevel: eventual + $count=true
    filter_expr = (
        "employeeType eq 'Terminated'"
        " and accountEnabled eq false"
    )

    url = (
        f"{_GRAPH_BASE}/users"
        f"?$count=true"
        f"&$filter={requests.utils.quote(filter_expr)}"
        f"&$select={select_fields}"
        f"&$top=999"
    )

    users: List[Dict] = []
    while url:
        data = _get(url, advanced=True)
        if data is None:
            break
        users.extend(data.get("value", []))
        url = data.get("@odata.nextLink")  # None when last page reached

    logger.info("Graph: found %d terminated+disabled users", len(users))
    return users


# ---------------------------------------------------------------------------
# Workflow B – Custom Security Attribute (CSA) config
#
# One-time setup in Azure AD admin center before first use:
#   1. Create attribute set:  name = CSA_ATTRIBUTE_SET  (default "EFAutomation")
#   2. Create ExtStatus:      CSA_ATTRIBUTE_NAME (default "ExtStatus")
#   3. Create TicketRef:      CSA_TICKET_REF_NAME (default "TicketRef")
#   4. Create DeleteExempt:   CSA_DELETE_EXEMPT_NAME (default "DeleteExempt")
#                             Boolean preferred, or String (true / EXEMPT)
#   5. Grant app CustomSecAttributeAssignment.ReadWrite.All (see assign_permissions.ps1)
#      IT Engineers need Attribute Assignment Administrator to set CSA in portal.
# ---------------------------------------------------------------------------

#: Azure AD Custom Security Attribute set name (configure via env var).
CSA_ATTRIBUTE_SET  = os.environ.get("CSA_ATTRIBUTE_SET",  "EFAutomation")
#: Azure AD Custom Security Attribute name within the set (configure via env var).
CSA_ATTRIBUTE_NAME = os.environ.get("CSA_ATTRIBUTE_NAME", "ExtStatus")
#: Azure AD Custom Security Attribute name for the SD+ ticket reference.
CSA_TICKET_REF_NAME = os.environ.get("CSA_TICKET_REF_NAME", "TicketRef")
#: CSA name for operational delete-exempt (Boolean or String True/EXEMPT).
CSA_DELETE_EXEMPT_NAME = os.environ.get("CSA_DELETE_EXEMPT_NAME", "DeleteExempt")

#: Predefined CSA values IT Engineers select to signal an approved extension.
#: These must exactly match the predefined values configured on the CSA attribute
#: in Azure AD (attribute set: EFAutomation, attribute: ExtStatus,
#: "Allow only predefined values" = Yes).  EF_ALERT_SENT is also in the predefined
#: list so the automation can write it via the Graph API.
_APPROVED_VALUES: frozenset = frozenset({"EXTEND_TO_30", "EXTEND_TO_60", "EXTENDED_MAX"})
_DELETE_EXEMPT_TRUTHY = frozenset({"true", "1", "yes", "exempt", "y"})


def _csa_attr_set(user: Dict[str, Any]) -> Dict[str, Any]:
    csa = user.get("customSecurityAttributes") or {}
    return csa.get(CSA_ATTRIBUTE_SET) or {}


def get_extension_attribute(user: Dict[str, Any]) -> Optional[str]:
    """
    Return the current value of the CSA ExtensionStatus attribute from the
    already-fetched Azure AD user dict, or None if not set.

    The user dict must have been fetched with $select=customSecurityAttributes
    (handled by get_terminated_users).
    """
    value = (_csa_attr_set(user).get(CSA_ATTRIBUTE_NAME) or "")
    if isinstance(value, str):
        value = value.strip()
    return value or None


def get_delete_exempt_csa_value(user: Dict[str, Any]) -> Any:
    """Raw CSA DeleteExempt value from a user dict (bool or str), or None."""
    return _csa_attr_set(user).get(CSA_DELETE_EXEMPT_NAME)


def is_csa_delete_exempt(
    user: Optional[Dict[str, Any]] = None,
    user_id: str = "",
) -> bool:
    """
    True when Entra CSA EFAutomation.DeleteExempt (or CSA_DELETE_EXEMPT_NAME) is set.

    Accepts Boolean true or string values: true / yes / 1 / EXEMPT.
    If *user* is omitted, fetches customSecurityAttributes for *user_id*.
    """
    ad_user = user
    if ad_user is None and user_id:
        ad_user = _get(
            f"{_GRAPH_BASE}/users/{user_id}?$select=id,customSecurityAttributes"
        )
    if not ad_user:
        return False

    raw = get_delete_exempt_csa_value(ad_user)
    if raw is None or raw is False:
        return False
    if raw is True:
        return True
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in _DELETE_EXEMPT_TRUTHY
    return bool(raw)


def is_extension_approved(user: Dict[str, Any]) -> bool:
    """Return True if the CSA ExtensionStatus signals an approved extension."""
    value = get_extension_attribute(user)
    return value is not None and value.upper() in _APPROVED_VALUES


def set_extension_attribute(user_id: str, value: str) -> bool:
    """
    Set the CSA ExtensionStatus attribute on the Azure AD user.

    Because Custom Security Attributes are cloud-native (not synced from
    on-prem AD), this PATCH works for both cloud-only and on-prem synced
    users as long as the managed identity has
    CustomSecAttributeAssignment.ReadWrite.All.
    """
    ok = _patch(
        f"{_GRAPH_BASE}/users/{user_id}",
        {
            "customSecurityAttributes": {
                CSA_ATTRIBUTE_SET: {
                    "@odata.type": "#Microsoft.DirectoryServices.CustomSecurityAttributeValue",
                    CSA_ATTRIBUTE_NAME: value,
                }
            }
        },
    )
    if ok:
        logger.info("Set CSA %s.%s=%s for userId=%s", CSA_ATTRIBUTE_SET, CSA_ATTRIBUTE_NAME, value, user_id)
    else:
        logger.error(
            "Failed to set CSA %s.%s=%s for userId=%s – check CustomSecAttributeAssignment.ReadWrite.All permission",
            CSA_ATTRIBUTE_SET, CSA_ATTRIBUTE_NAME, value, user_id,
        )
    return ok


def clear_extension_attribute(user_id: str) -> bool:
    """
    Clear the CSA ExtensionStatus attribute after the extension has been processed.
    """
    ok = _patch(
        f"{_GRAPH_BASE}/users/{user_id}",
        {
            "customSecurityAttributes": {
                CSA_ATTRIBUTE_SET: {
                    "@odata.type": "#Microsoft.DirectoryServices.CustomSecurityAttributeValue",
                    CSA_ATTRIBUTE_NAME: None,
                }
            }
        },
    )
    if ok:
        logger.info("Cleared CSA %s.%s for userId=%s", CSA_ATTRIBUTE_SET, CSA_ATTRIBUTE_NAME, user_id)
    else:
        logger.error(
            "Failed to clear CSA %s.%s for userId=%s – check CustomSecAttributeAssignment.ReadWrite.All permission",
            CSA_ATTRIBUTE_SET, CSA_ATTRIBUTE_NAME, user_id,
        )
    return ok


def extract_offboard_date(user: Dict[str, Any]) -> Optional[str]:
    """
    Return the ISO 8601 offboard date string from extensionAttribute10,
    or None if not set.

    Azure AD stores this in onPremisesExtensionAttributes for synced users.
    Example value: '2026-04-23T12:28:34.084-07:00'
    """
    attrs = user.get("onPremisesExtensionAttributes") or {}
    return attrs.get("extensionAttribute10")


def get_manager(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch manager details for a user. Used as fallback if $expand fails."""
    data = _get(f"{_GRAPH_BASE}/users/{user_id}/manager?$select=id,mail,displayName,userPrincipalName")
    return data


# ---------------------------------------------------------------------------
# Email Forwarding Detection
# ---------------------------------------------------------------------------

def _user_upn(user_id: str) -> str:
    data = _get(f"{_GRAPH_BASE}/users/{user_id}?$select=userPrincipalName,mail")
    if not data:
        return ""
    return (data.get("userPrincipalName") or data.get("mail") or "").strip()


def _addresses_from_inbox_rules(user_id: str) -> list:
    """Return enabled inbox-rule forward/redirect SMTP addresses."""
    addrs = []
    rules_data = _get(
        f"{_GRAPH_BASE}/users/{user_id}/mailFolders/inbox/messageRules"
    )
    if not rules_data:
        return addrs
    for rule in rules_data.get("value", []):
        if not rule.get("isEnabled", True):
            continue
        actions = rule.get("actions") or {}
        for key in ("forwardTo", "redirectTo", "forwardAsAttachmentTo"):
            for recip in actions.get(key) or []:
                addr = (recip.get("emailAddress") or {}).get("address") or ""
                if addr and addr not in addrs:
                    addrs.append(addr)
    return addrs


def has_email_forwarding(user_id: str) -> bool:
    """
    Detect whether email forwarding is active.

    Order:
      1. Exchange Online Admin API (M365 Admin Center "Email forwarding")
      2. Graph mailboxSettings (often unavailable in this tenant)
      3. Inbox rules with forward/redirect actions
    """
    upn = _user_upn(user_id)
    if upn:
        try:
            from .exchange_admin import get_mailbox_forwarding
            exo = get_mailbox_forwarding(upn)
            if exo.get("forwardingAddress") or exo.get("enabled"):
                logger.info("User %s has EXO admin-center forwarding", user_id)
                return True
        except Exception as exc:
            logger.warning("EXO forwarding check failed for %s: %s", user_id, exc)

    settings = _get(f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings")
    if settings:
        if settings.get("forwardingSmtpAddress") or settings.get("forwardingAddress"):
            logger.info("User %s has Graph mailboxSettings forwarding", user_id)
            return True

    if _addresses_from_inbox_rules(user_id):
        logger.info("User %s has forwarding inbox rule(s)", user_id)
        return True
    return False


def get_forwarding_address(user_id: str) -> Optional[str]:
    """
    Return the best-known SMTP forwarding target for *user_id*, or None.

    Prefers M365 Admin Center / EXO ForwardingSmtpAddress, then Graph
    mailboxSettings, then the first enabled inbox-rule forward target.
    """
    upn = _user_upn(user_id)
    if upn:
        try:
            from .exchange_admin import get_mailbox_forwarding
            exo = get_mailbox_forwarding(upn)
            if exo.get("forwardingAddress"):
                return exo["forwardingAddress"]
        except Exception as exc:
            logger.warning("EXO get_forwarding_address failed for %s: %s", user_id, exc)

    settings = _get(f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings")
    if settings:
        addr = settings.get("forwardingSmtpAddress") or settings.get("forwardingAddress")
        if addr:
            if isinstance(addr, str) and addr.lower().startswith("smtp:"):
                return addr[5:]
            return addr

    rule_addrs = _addresses_from_inbox_rules(user_id)
    return rule_addrs[0] if rule_addrs else None


def disable_email_forwarding(user_id: str) -> bool:
    """
    Clear email forwarding for a user (Admin Center SMTP + best-effort Graph).

    Called on Day-30/60 EF grace removal and on account recovery.
    """
    from .automation_flags import is_ef_revoke_disabled

    if is_ef_revoke_disabled():
        logger.info("EF revoke disabled – skipped disable_email_forwarding for %s", user_id)
        return True

    upn = _user_upn(user_id)
    ok = False
    if upn:
        try:
            from .exchange_admin import clear_mailbox_forwarding
            ok = clear_mailbox_forwarding(upn) or ok
        except Exception as exc:
            logger.warning("EXO clear forwarding failed for %s: %s", user_id, exc)

    # Best-effort Graph clear (often unsupported on this tenant)
    if _patch(
        f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings",
        {"forwardingSmtpAddress": None},
    ):
        ok = True

    if ok:
        logger.info("Cleared mailbox forwarding for userId=%s", user_id)
    else:
        logger.warning("Could not clear mailbox forwarding for userId=%s", user_id)
    return ok


def enable_email_forwarding(user_id: str, forwarding_address: str) -> bool:
    """
    (Re-)enable mailbox-level SMTP forwarding via EXO Set-Mailbox when possible.
    """
    if not forwarding_address:
        logger.warning("enable_email_forwarding called for userId=%s with empty address", user_id)
        return False

    from .automation_flags import is_ef_revoke_disabled
    if is_ef_revoke_disabled():
        logger.info("Graph/EF writes disabled – skipped enable_email_forwarding for %s", user_id)
        return True

    upn = _user_upn(user_id)
    smtp = forwarding_address if forwarding_address.lower().startswith("smtp:") else f"smtp:{forwarding_address}"
    if upn:
        try:
            from .exchange_admin import set_mailbox_forwarding
            if set_mailbox_forwarding(upn, forwarding_address):
                return True
        except Exception as exc:
            logger.warning("EXO enable forwarding exception: %s", exc)

    ok = _patch(
        f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings",
        {"forwardingSmtpAddress": smtp},
    )
    if ok:
        logger.info("Re-enabled Graph mailbox forwarding → %s for userId=%s", forwarding_address, user_id)
    else:
        logger.warning("Could not re-enable mailbox forwarding for userId=%s", user_id)
    return ok


# ---------------------------------------------------------------------------
# Account lifecycle
# ---------------------------------------------------------------------------

def delete_user(user_id: str) -> bool:
    """
    Soft-delete a user from Azure AD.

    The deleted user moves to Entra's built-in recycle bin for 30 days.
    Within that window the account can be restored via restore_user().
    After 30 days Entra permanently deletes it automatically.

    Returns True on success.
    """
    ok = _delete(f"{_GRAPH_BASE}/users/{user_id}")
    if ok:
        logger.info("User %s soft-deleted (moved to recycle bin)", user_id)
    else:
        logger.error("Failed to delete user %s", user_id)
    return ok


def restore_user(user_id: str) -> Tuple[bool, str]:
    """
    Restore a user from the Entra recycle bin.
    Must be called within 30 days of deletion.

    On success, caller should immediately call disable_email_forwarding().

    Returns (success: bool, message: str).
    """
    result = _post(f"{_GRAPH_BASE}/directory/deletedItems/{user_id}/restore")
    if result:
        logger.info("User %s restored from recycle bin", user_id)
        # Immediately disable any forwarding
        disable_email_forwarding(user_id)
        return True, "Account restored. Email forwarding has been disabled."
    return False, (
        "Restore failed. The account may already be permanently deleted "
        "(>30 days) or the user_id is incorrect."
    )


def get_deleted_user(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Return details of a soft-deleted user (in the recycle bin), or None.
    Useful to confirm a user is recoverable before attempting restore.
    """
    return _get(f"{_GRAPH_BASE}/directory/deletedItems/{user_id}")


# ---------------------------------------------------------------------------
# Auto-Reply (OOO) management
# ---------------------------------------------------------------------------

def set_auto_reply(user_id: str, manager_name: str, manager_email: str) -> bool:
    """
    Configure OOO for a terminated user (internal senders only).

    externalAudience=none → no auto-reply to external/internet senders
    (testing and production). Manager is named as the internal point of contact.
    """
    internal_msg = (
        f"Thank you for your email. This mailbox is no longer monitored as the individual "
        f"is no longer associated with Netradyne. For business-related matters, please contact "
        f"{manager_name} at {manager_email}."
    )
    return _patch(
        f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings",
        {
            "automaticRepliesSetting": {
                "status": "alwaysEnabled",
                "externalAudience": "none",
                "internalReplyMessage": internal_msg,
                "externalReplyMessage": "",
            }
        },
    )


def disable_auto_reply(user_id: str) -> bool:
    """Disable OOO for a user (called after OOO_MAX_DAYS or on deletion)."""
    return _patch(
        f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings",
        {"automaticRepliesSetting": {"status": "disabled"}},
    )


def is_auto_reply_enabled(user_id: str) -> bool:
    """Return True if OOO is currently active (alwaysEnabled or scheduled)."""
    settings = _get(f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings")
    if settings:
        status = (settings.get("automaticRepliesSetting") or {}).get("status", "disabled")
        return status in ("alwaysEnabled", "scheduled")
    return False


# ---------------------------------------------------------------------------
# TicketRef CSA attribute
# ---------------------------------------------------------------------------

def get_ticket_ref_attribute(user: Dict[str, Any]) -> Optional[str]:
    """Return the TicketRef CSA value from an already-fetched user dict, or None."""
    csa = user.get("customSecurityAttributes") or {}
    attr_set = csa.get(CSA_ATTRIBUTE_SET) or {}
    value = (attr_set.get(CSA_TICKET_REF_NAME) or "").strip()
    return value or None


def set_ticket_ref_attribute(user_id: str, ticket_ref: str) -> bool:
    """Set the TicketRef CSA on the Azure AD user."""
    ok = _patch(
        f"{_GRAPH_BASE}/users/{user_id}",
        {
            "customSecurityAttributes": {
                CSA_ATTRIBUTE_SET: {
                    "@odata.type": "#Microsoft.DirectoryServices.CustomSecurityAttributeValue",
                    CSA_TICKET_REF_NAME: ticket_ref,
                }
            }
        },
    )
    if ok:
        logger.info("Set CSA %s.%s=%s for userId=%s", CSA_ATTRIBUTE_SET, CSA_TICKET_REF_NAME, ticket_ref, user_id)
    else:
        logger.error("Failed to set CSA %s.%s for userId=%s", CSA_ATTRIBUTE_SET, CSA_TICKET_REF_NAME, user_id)
    return ok


# ---------------------------------------------------------------------------
# Litigation hold check
# ---------------------------------------------------------------------------

def is_litigation_hold_active(user_id: str) -> bool:
    """
    Check if a user has an active litigation / in-place hold via the beta endpoint.
    Returns True if any InPlaceHolds are set on the user mailbox.
    Requires User.Read.All (or AuditLog.Read.All) on the app registration.
    """
    data = _get(f"{_GRAPH_BETA}/users/{user_id}?$select=inPlaceHolds")
    if data:
        holds = data.get("inPlaceHolds") or []
        if holds:
            logger.info("userId=%s has %d in-place hold(s) – deletion blocked", user_id, len(holds))
            return True
    return False


# ---------------------------------------------------------------------------
# License removal
# ---------------------------------------------------------------------------

def get_assigned_license_skus(user_id: str) -> List[str]:
    """Return list of skuId strings for all assigned licenses."""
    data = _get(f"{_GRAPH_BASE}/users/{user_id}?$select=assignedLicenses")
    if data:
        return [lic.get("skuId", "") for lic in data.get("assignedLicenses", []) if lic.get("skuId")]
    return []


def remove_all_licenses(user_id: str) -> bool:
    """Remove all M365 licenses from the user. Call after account deletion."""
    skus = get_assigned_license_skus(user_id)
    if not skus:
        logger.info("userId=%s has no licenses to remove", user_id)
        return True
    result = _post(
        f"{_GRAPH_BASE}/users/{user_id}/assignLicense",
        {"addLicenses": [], "removeLicenses": skus},
    )
    if result is not None:
        logger.info("Removed %d license(s) from userId=%s: %s", len(skus), user_id, skus)
        return True
    logger.error("Failed to remove licenses from userId=%s", user_id)
    return False
