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

Workflow B note:
  The extension approval signal is stored as a Custom Security Attribute (CSA):
    Attribute set : CSA_ATTRIBUTE_SET  (default "EFAutomation")
    Attribute name: CSA_ATTRIBUTE_NAME (default "ExtensionStatus")

  CSA are cloud-native and writable directly in Azure AD / Entra portal —
  unlike onPremisesExtensionAttributes which are read-only from the cloud
  for on-premises synced users.

  Lifecycle of CSA value:
    (absent)        → no alert sent yet
    "EF_ALERT_SENT" → alert email was sent; IT should raise ticket → approve → update
    "EXTEND_30"     → approved; daily monitor will extend +30 days and clear
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


def _get(url: str) -> Optional[Dict]:
    """HTTP GET with error handling."""
    try:
        resp = requests.get(url, headers=_auth_headers(), timeout=30)
        if resp.status_code == 200:
            return resp.json()
        logger.warning("GET %s → %s", url, resp.status_code)
        return None
    except Exception as exc:
        logger.error("GET %s failed: %s", url, exc)
        return None


def _delete(url: str) -> bool:
    """HTTP DELETE; returns True on 204 No Content."""
    try:
        resp = requests.delete(url, headers=_auth_headers(), timeout=30)
        return resp.status_code == 204
    except Exception as exc:
        logger.error("DELETE %s failed: %s", url, exc)
        return False


def _patch(url: str, payload: Dict) -> bool:
    """HTTP PATCH; returns True on 200 or 204."""
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

    Manager is expanded inline so we avoid a second round-trip per user.
    Handles pagination automatically via @odata.nextLink.
    """
    select_fields = ",".join([
        "id",
        "mail",
        "userPrincipalName",
        "displayName",
        "accountEnabled",
        "employeeType",
        "onPremisesExtensionAttributes",
        "customSecurityAttributes",       # EFAutomation.ExtensionStatus (Workflow B)
    ])
    filter_expr = (
        "employeeType eq 'Terminated'"
        " and accountEnabled eq false"
    )
    expand_expr = "manager($select=id,mail,displayName,userPrincipalName)"

    url = (
        f"{_GRAPH_BASE}/users"
        f"?$filter={requests.utils.quote(filter_expr)}"
        f"&$select={select_fields}"
        f"&$expand={expand_expr}"
        f"&$top=999"
    )

    users: List[Dict] = []
    while url:
        data = _get(url)
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
#   2. Create attribute:      name = CSA_ATTRIBUTE_NAME (default "ExtensionStatus")
#                             type = String, single-value, not predefined values
#   3. Grant managed identity the "Attribute Assignment Administrator" role
#      (or the CustomSecAttributeAssignment.ReadWrite.All app permission –
#       see infra/assign_permissions.ps1)
# ---------------------------------------------------------------------------

#: Azure AD Custom Security Attribute set name (configure via env var).
CSA_ATTRIBUTE_SET  = os.environ.get("CSA_ATTRIBUTE_SET",  "EFAutomation")
#: Azure AD Custom Security Attribute name within the set (configure via env var).
CSA_ATTRIBUTE_NAME = os.environ.get("CSA_ATTRIBUTE_NAME", "ExtensionStatus")

#: Values the daily monitor treats as "extension approved – extend 30 days".
_APPROVED_VALUES: frozenset = frozenset({"EXTEND_30", "APPROVED", "YES"})


def get_extension_attribute(user: Dict[str, Any]) -> Optional[str]:
    """
    Return the current value of the CSA ExtensionStatus attribute from the
    already-fetched Azure AD user dict, or None if not set.

    The user dict must have been fetched with $select=customSecurityAttributes
    (handled by get_terminated_users).
    """
    csa = user.get("customSecurityAttributes") or {}
    attr_set = csa.get(CSA_ATTRIBUTE_SET) or {}
    value = (attr_set.get(CSA_ATTRIBUTE_NAME) or "").strip()
    return value or None


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

def has_email_forwarding(user_id: str) -> bool:
    """
    Detect whether email forwarding is active on this user's mailbox.

    Checks two places:
      1. Mailbox-level forwardingSmtpAddress (set via IT / Exchange admin)
      2. Inbox rules with forward/redirect actions (set via rules)

    Returns True if any forwarding is found.
    """
    # Check 1: mailbox settings forwarding address
    settings = _get(f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings")
    if settings:
        if settings.get("forwardingSmtpAddress") or settings.get("forwardingAddress"):
            logger.info("User %s has mailbox-level forwarding", user_id)
            return True

    # Check 2: inbox rules
    rules_data = _get(
        f"{_GRAPH_BASE}/users/{user_id}/mailFolders/inbox/messageRules"
    )
    if rules_data:
        for rule in rules_data.get("value", []):
            actions = rule.get("actions", {})
            has_fwd = (
                actions.get("forwardTo")
                or actions.get("redirectTo")
                or actions.get("forwardAsAttachmentTo")
            )
            if has_fwd and rule.get("isEnabled", True):
                logger.info("User %s has forwarding inbox rule: %s", user_id, rule.get("displayName"))
                return True

    return False


def disable_email_forwarding(user_id: str) -> bool:
    """
    Clear mailbox-level email forwarding for a user.
    Called during account recovery (EF is permanently disabled on recovery).
    """
    ok = _patch(
        f"{_GRAPH_BASE}/users/{user_id}/mailboxSettings",
        {"forwardingSmtpAddress": None},
    )
    if ok:
        logger.info("Cleared mailbox forwarding for user %s", user_id)
    else:
        logger.warning("Could not clear mailbox forwarding for user %s", user_id)
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
