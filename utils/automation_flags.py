"""
automation_flags.py
-------------------
Environment gates for safe local and staging tests without side effects.

Set in local.settings.json (local) or Function App Configuration (Azure):

  EF_DRY_RUN=true
      Skip outbound email and all Microsoft Graph write operations (delete,
      mailbox/CSA changes, licenses, auto-reply). Table Storage updates still
      run so you can trace logic in AuditLog.

  DISABLE_OUTBOUND_EMAIL=true
      Skip Graph Mail.Send only; other Graph writes still run.
      Ignored when EF_DRY_RUN is true.

  DISABLE_GRAPH_WRITES=true
      Skip Graph mutations (delete/mailbox/CSA/licenses) only; Mail.Send still works.
      Ignored when EF_DRY_RUN is true.

  EF_TEST_MODE=true
      Redirect every outbound email To → EF_TEST_RECIPIENT and strip all CC.
      Use for template checks against a single mailbox
      (default: prem_testing@netradyne.com).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TEST_RECIPIENT = "prem_testing@netradyne.com"


def _truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def is_dry_run() -> bool:
    return _truthy("EF_DRY_RUN")


def is_outbound_email_disabled() -> bool:
    if is_dry_run():
        return True
    return _truthy("DISABLE_OUTBOUND_EMAIL")


def is_graph_write_disabled() -> bool:
    if is_dry_run():
        return True
    return _truthy("DISABLE_GRAPH_WRITES")


def is_test_mode() -> bool:
    """When true, all mail goes only to EF_TEST_RECIPIENT with no CC."""
    return _truthy("EF_TEST_MODE")


def get_test_recipient() -> str:
    return (
        os.environ.get("EF_TEST_RECIPIENT", "").strip()
        or _DEFAULT_TEST_RECIPIENT
    )


def apply_test_email_routing(
    to_address: str,
    cc_address: Optional[str] = None,
) -> tuple:
    """
    In EF_TEST_MODE: rewrite To to the test mailbox and drop CC.
    Returns (to_address, cc_address).
    """
    if not is_test_mode():
        return to_address, cc_address

    test_to = get_test_recipient()
    logger.warning(
        "EF_TEST_MODE: redirecting email intended for to=%s cc=%s → to=%s (no CC)",
        to_address,
        cc_address or "",
        test_to,
    )
    return test_to, None


def resolve_ooo_contact(manager_name: str, manager_email: str) -> tuple:
    """
    Point-of-contact shown in the OOO message.

    In EF_TEST_MODE always use the test recipient so no real manager is named.
    """
    if is_test_mode():
        test_to = get_test_recipient()
        return "IT Testing (prem_testing)", test_to
    return manager_name or manager_email, manager_email


def log_active_gates(context: str) -> None:
    """Log which safety gates are on (call at start of timer jobs)."""
    if not (
        is_dry_run()
        or is_outbound_email_disabled()
        or is_graph_write_disabled()
        or is_test_mode()
    ):
        return
    logger.warning(
        "%s running with safety gates: EF_DRY_RUN=%s DISABLE_OUTBOUND_EMAIL=%s "
        "DISABLE_GRAPH_WRITES=%s EF_TEST_MODE=%s EF_TEST_RECIPIENT=%s",
        context,
        is_dry_run(),
        _truthy("DISABLE_OUTBOUND_EMAIL") if not is_dry_run() else False,
        _truthy("DISABLE_GRAPH_WRITES") if not is_dry_run() else False,
        is_test_mode(),
        get_test_recipient() if is_test_mode() else "",
    )
