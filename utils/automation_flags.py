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
      Skip SMTP only; Graph writes still run. Ignored when EF_DRY_RUN is true.

  DISABLE_GRAPH_WRITES=true
      Skip Graph mutations only; emails still send. Ignored when EF_DRY_RUN is true.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


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


def log_active_gates(context: str) -> None:
    """Log which safety gates are on (call at start of timer jobs)."""
    if not (is_dry_run() or is_outbound_email_disabled() or is_graph_write_disabled()):
        return
    logger.warning(
        "%s running with safety gates: EF_DRY_RUN=%s DISABLE_OUTBOUND_EMAIL=%s DISABLE_GRAPH_WRITES=%s",
        context,
        is_dry_run(),
        _truthy("DISABLE_OUTBOUND_EMAIL") if not is_dry_run() else False,
        _truthy("DISABLE_GRAPH_WRITES") if not is_dry_run() else False,
    )
