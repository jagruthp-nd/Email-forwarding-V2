"""
exchange_admin.py
-----------------
Read/clear Exchange Online mailbox forwarding via the EXO Admin API.

M365 Admin Center "Email forwarding" is NOT exposed on Graph mailboxSettings
in this tenant. Use:

  GET https://outlook.office365.com/adminapi/beta/{tenant}/Mailbox('upn')
      ?$select=ForwardingSmtpAddress,ForwardingAddress,DeliverToMailboxAndForward

Requires:
  - Application permission Exchange.ManageAsApp (Office 365 Exchange Online)
  - Directory role Exchange Administrator (or equivalent) on the app's SP
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

_EXO_SCOPE = "https://outlook.office365.com/.default"
_EXO_BASE = "https://outlook.office365.com/adminapi/beta"


def _exo_headers() -> Dict[str, str]:
    token = DefaultAzureCredential().get_token(_EXO_SCOPE).token
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _tenant() -> str:
    return os.environ.get("AZURE_TENANT_ID", "").strip()


def _normalize_smtp(addr: Optional[str]) -> str:
    if not addr:
        return ""
    a = str(addr).strip()
    if a.lower().startswith("smtp:"):
        a = a[5:]
    return a.strip()


def get_mailbox_forwarding(user_principal_name: str) -> Dict[str, Any]:
    """
    Return admin-center mailbox forwarding for a UPN.

    Keys: enabled (bool), forwardingAddress (str), rawSmtp (str), source (str)
    """
    result = {
        "enabled": False,
        "forwardingAddress": "",
        "rawSmtp": "",
        "source": "exo_admin_api",
        "error": "",
    }
    tenant = _tenant()
    upn = (user_principal_name or "").strip()
    if not tenant or not upn:
        result["error"] = "missing tenant or UPN"
        return result

    url = (
        f"{_EXO_BASE}/{tenant}/Mailbox('{upn}')"
        f"?$select=ForwardingSmtpAddress,ForwardingAddress,DeliverToMailboxAndForward,"
        f"PrimarySmtpAddress"
    )
    try:
        resp = requests.get(url, headers=_exo_headers(), timeout=60)
        if resp.status_code != 200:
            result["error"] = f"EXO HTTP {resp.status_code}: {(resp.text or '')[:300]}"
            logger.warning("EXO get mailbox forwarding failed for %s: %s", upn, result["error"])
            return result
        data = resp.json()
        raw = data.get("ForwardingSmtpAddress") or ""
        # ForwardingAddress can be a recipient DN / object — prefer SMTP
        fwd_obj = data.get("ForwardingAddress")
        deliver = bool(data.get("DeliverToMailboxAndForward"))
        addr = _normalize_smtp(raw)
        if not addr and isinstance(fwd_obj, str):
            addr = _normalize_smtp(fwd_obj)
        elif not addr and isinstance(fwd_obj, dict):
            addr = _normalize_smtp(fwd_obj.get("PrimarySmtpAddress") or fwd_obj.get("Name"))

        result["rawSmtp"] = raw or ""
        result["forwardingAddress"] = addr
        result["enabled"] = bool(addr) or deliver
        if addr:
            logger.info("EXO forwarding for %s → %s (deliverAndForward=%s)", upn, addr, deliver)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        logger.error("EXO get mailbox forwarding exception for %s: %s", upn, exc)
        return result


def set_mailbox_forwarding(user_principal_name: str, forwarding_address: str) -> bool:
    """Set Admin Center SMTP forwarding (Keep a copy = DeliverToMailboxAndForward)."""
    from .automation_flags import is_ef_revoke_disabled

    if is_ef_revoke_disabled():
        logger.info("EF writes disabled – skipped set_mailbox_forwarding for %s", user_principal_name)
        return True

    tenant = _tenant()
    upn = (user_principal_name or "").strip()
    addr = _normalize_smtp(forwarding_address)
    if not tenant or not upn or not addr:
        return False
    smtp = f"smtp:{addr}"
    url = f"{_EXO_BASE}/{tenant}/InvokeCommand"
    body = {
        "CmdletInput": {
            "CmdletName": "Set-Mailbox",
            "Parameters": {
                "Identity": upn,
                "ForwardingSmtpAddress": smtp,
                "DeliverToMailboxAndForward": True,
            },
        }
    }
    try:
        resp = requests.post(url, headers=_exo_headers(), json=body, timeout=90)
        if resp.status_code in (200, 204):
            logger.info("Set EXO forwarding for %s → %s", upn, addr)
            return True
        logger.error("EXO set forwarding failed for %s: %s %s", upn, resp.status_code, (resp.text or "")[:400])
        return False
    except Exception as exc:
        logger.error("EXO set forwarding exception for %s: %s", upn, exc)
        return False


def clear_mailbox_forwarding(user_principal_name: str) -> bool:
    """
    Clear M365 Admin Center email forwarding via Set-Mailbox (InvokeCommand).
    """
    from .automation_flags import is_ef_revoke_disabled

    if is_ef_revoke_disabled():
        logger.info("EF revoke disabled – skipped clear_mailbox_forwarding for %s", user_principal_name)
        return True

    tenant = _tenant()
    upn = (user_principal_name or "").strip()
    if not tenant or not upn:
        return False

    url = f"{_EXO_BASE}/{tenant}/InvokeCommand"
    body = {
        "CmdletInput": {
            "CmdletName": "Set-Mailbox",
            "Parameters": {
                "Identity": upn,
                "ForwardingSmtpAddress": None,
                "ForwardingAddress": None,
                "DeliverToMailboxAndForward": False,
            },
        }
    }
    try:
        resp = requests.post(url, headers=_exo_headers(), json=body, timeout=90)
        if resp.status_code in (200, 204):
            logger.info("Cleared EXO mailbox forwarding for %s", upn)
            return True
        logger.error(
            "EXO clear forwarding failed for %s: %s %s",
            upn, resp.status_code, (resp.text or "")[:400],
        )
        return False
    except Exception as exc:
        logger.error("EXO clear forwarding exception for %s: %s", upn, exc)
        return False
