"""
automation_flags.py
-------------------
Environment gates for safe local, UAT, and production runs.

  EF_DRY_RUN=true
      Skip outbound email and all Microsoft Graph / EXO write operations.
      Table Storage updates still run.

  DISABLE_OUTBOUND_EMAIL=true
      Skip Graph Mail.Send only.

  DISABLE_GRAPH_WRITES=true
      Skip Graph/EXO mutations (delete, mailbox/CSA, EF revoke, licenses).
      Mail.Send still works. Ignored when EF_DRY_RUN=true.

  EF_UAT_MODE=true
      UAT profile: send emails + allow EF revoke; NEVER delete accounts.
      Implies DISABLE_ACCOUNT_DELETION=true.
      Set DISABLE_GRAPH_WRITES=false so EF revoke / CSA can run.

  DISABLE_ACCOUNT_DELETION=true
      Skip soft-delete of Entra users (also implied by EF_UAT_MODE).

  EF_TEST_MODE=true
      Redirect every outbound email To → EF_TEST_RECIPIENT and strip CC.
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


def is_uat_mode() -> bool:
    return _truthy("EF_UAT_MODE")


def is_outbound_email_disabled() -> bool:
    if is_dry_run():
        return True
    return _truthy("DISABLE_OUTBOUND_EMAIL")


def is_graph_write_disabled() -> bool:
    if is_dry_run():
        return True
    return _truthy("DISABLE_GRAPH_WRITES")


def is_account_deletion_disabled() -> bool:
    """True when soft-delete of accounts must not run."""
    if is_dry_run() or is_graph_write_disabled():
        return True
    if is_uat_mode():
        return True
    return _truthy("DISABLE_ACCOUNT_DELETION")


def is_ef_revoke_disabled() -> bool:
    """True when mailbox forwarding must not be cleared."""
    if is_dry_run():
        return True
    # UAT explicitly allows EF revoke even though deletes are blocked
    if is_uat_mode() and not _truthy("DISABLE_GRAPH_WRITES"):
        return False
    return is_graph_write_disabled()


def is_test_mode() -> bool:
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
    if is_test_mode():
        test_to = get_test_recipient()
        return "IT Testing (prem_testing)", test_to
    return manager_name or manager_email, manager_email


def log_active_gates(context: str) -> None:
    if not (
        is_dry_run()
        or is_outbound_email_disabled()
        or is_graph_write_disabled()
        or is_account_deletion_disabled()
        or is_test_mode()
        or is_uat_mode()
    ):
        return
    logger.warning(
        "%s gates: EF_DRY_RUN=%s DISABLE_OUTBOUND_EMAIL=%s DISABLE_GRAPH_WRITES=%s "
        "EF_UAT_MODE=%s DISABLE_ACCOUNT_DELETION=%s EF_TEST_MODE=%s EF_TEST_RECIPIENT=%s "
        "ef_revoke_disabled=%s",
        context,
        is_dry_run(),
        _truthy("DISABLE_OUTBOUND_EMAIL") if not is_dry_run() else False,
        _truthy("DISABLE_GRAPH_WRITES") if not is_dry_run() else False,
        is_uat_mode(),
        is_account_deletion_disabled(),
        is_test_mode(),
        get_test_recipient() if is_test_mode() else "",
        is_ef_revoke_disabled(),
    )
