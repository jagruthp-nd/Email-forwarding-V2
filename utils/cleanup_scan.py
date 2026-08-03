"""
cleanup_scan.py
---------------
Monthly safety-net scan for stale accounts that should have been deleted.

Two populations are targeted:

  1. UserTracking-tracked accounts with no EF (efRequired=False) that IT should
     have manually deleted during offboarding but missed.  These are identified
     from Table Storage directly (no extra Graph API call needed).

  2. Azure AD accounts that are sign-in-blocked, have an offboard date
     (extensionAttribute10), match the configured CLEANUP_REGION, and are NOT
     already tracked as DELETED in UserTracking.  These completely slipped past
     the daily monitor (e.g. employeeType was never set to 'Terminated').

Configurable env vars:
  CLEANUP_SCHEDULE          NCronTab for the timer trigger (default: monthly on 1st at 06:00 UTC)
  CLEANUP_REGION            Azure AD usageLocation code to filter by (default "IN")
  CLEANUP_MIN_OFFBOARD_DAYS Minimum days since offboard before cleanup deletes (default 30)

Account safety rules:
  - Already marked DELETED in UserTracking → SKIP
  - deletionExempt / skipCleanupDeletion on UserTracking → SKIP
  - DELETION_EXEMPT_USER_IDS / DELETION_EXEMPT_EMAILS app settings → SKIP
  - Account still has active email forwarding → SKIP (log warning, let daily monitor handle)
  - Account already deleted from Azure AD (soft-deleted / 404) → mark DELETED in tracking, SKIP
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from .table_store import TableStore
from .graph_api   import (
    get_terminated_users,
    extract_offboard_date,
    has_email_forwarding,
    delete_user,
    get_deleted_user,
)
from .automation_flags import is_dry_run, log_active_gates
from .deletion_exempt import is_automated_deletion_exempt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CLEANUP_REGION            = os.environ.get("CLEANUP_REGION",            "IN")
_CLEANUP_MIN_OFFBOARD_DAYS = int(os.environ.get("CLEANUP_MIN_OFFBOARD_DAYS", "30"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_cleanup_scan() -> Dict[str, int]:
    """
    Main entry point – called from the monthly timer trigger in function_app.py.

    Returns a summary dict logged by the caller.
    """
    store = TableStore()
    store.ensure_tables()

    log_active_gates("monthly_cleanup")

    today = datetime.now(timezone.utc).date()
    logger.info("=== Monthly Cleanup Scan starting – %s (region=%s) ===", today.isoformat(), _CLEANUP_REGION)

    summary = {"checked": 0, "deleted": 0, "skipped_already_deleted": 0,
               "skipped_has_ef": 0, "skipped_too_recent": 0, "skipped_exempt": 0,
               "dry_run": 0, "errors": 0}

    # Build a fast lookup of what's already tracked
    all_tracked = {r["userId"]: r for r in store.list_all_users()}

    # ── Population 1: tracked NO_EF accounts that IT missed ──────────────
    for record in all_tracked.values():
        if _bool(record.get("efRequired", False)):
            continue   # has EF – daily monitor owns this
        if record.get("statusCode") == "DELETED":
            continue   # already done

        user_id = record.get("userId", "")
        if not user_id:
            continue

        offboard_str = record.get("offboardDate", "")
        try:
            offboard = date.fromisoformat(offboard_str)
        except ValueError:
            continue

        days_elapsed = (today - offboard).days
        if days_elapsed < _CLEANUP_MIN_OFFBOARD_DAYS:
            summary["skipped_too_recent"] += 1
            continue

        summary["checked"] += 1
        result = _delete_stale(user_id, record, store, source="tracked_no_ef")
        _tally(summary, result)

    # ── Population 2: AD accounts not in tracking (slipped past daily monitor) ─
    ad_users = get_terminated_users()   # employeeType=Terminated + accountEnabled=false

    for user in ad_users:
        user_id = user.get("id", "")
        if not user_id:
            continue

        # Skip if already tracked (daily monitor handles these)
        if user_id in all_tracked:
            continue

        # Filter by region (usageLocation)
        if _CLEANUP_REGION and user.get("usageLocation", "") != _CLEANUP_REGION:
            continue

        raw_date = extract_offboard_date(user)
        if not raw_date:
            continue

        try:
            normalised = raw_date.replace("Z", "+00:00")
            offboard = datetime.fromisoformat(normalised).date()
        except ValueError:
            continue

        days_elapsed = (today - offboard).days
        if days_elapsed < _CLEANUP_MIN_OFFBOARD_DAYS:
            summary["skipped_too_recent"] += 1
            continue

        summary["checked"] += 1

        # Build a minimal stub record for logging/auditing
        stub: Dict[str, Any] = {
            "userId":       user_id,
            "displayName":  user.get("displayName", ""),
            "userEmail":    user.get("mail") or user.get("userPrincipalName", ""),
            "offboardDate": offboard.isoformat(),
            "statusCode":   "ACTIVE",
            "managerEmail": "",
        }
        result = _delete_stale(user_id, stub, store, source="untracked_ad_account")
        _tally(summary, result)

    logger.info(
        "=== Cleanup Scan complete – checked=%d deleted=%d "
        "skip_deleted=%d skip_ef=%d skip_recent=%d skip_exempt=%d dry_run=%d errors=%d ===",
        summary["checked"],   summary["deleted"],
        summary["skipped_already_deleted"], summary["skipped_has_ef"],
        summary["skipped_too_recent"], summary["skipped_exempt"],
        summary["dry_run"], summary["errors"],
    )
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _delete_stale(
    user_id: str,
    record: Dict[str, Any],
    store: TableStore,
    source: str,
) -> str:
    """
    Delete one stale account if safe to do so.

    Returns: 'deleted' | 'skipped_already_deleted' | 'skipped_has_ef' | 'skipped_exempt' | 'dry_run' | 'error'
    """
    exempt, exempt_reason = is_automated_deletion_exempt(
        user_id, record, user_email=record.get("userEmail", ""),
    )
    if exempt:
        logger.info(
            "Cleanup skip (exempt=%s) userId=%s source=%s",
            exempt_reason, user_id, source,
        )
        store.append_audit(
            user_id,
            "CLEANUP_SKIPPED_EXEMPT",
            f"source={source} exempt={exempt_reason}",
        )
        return "skipped_exempt"

    if is_dry_run():
        logger.info("DRY_RUN: would cleanup-delete userId=%s source=%s", user_id, source)
        store.append_audit(user_id, "DRY_RUN_WOULD_CLEANUP_DELETE", f"source={source}")
        return "dry_run"

    # Check if account is already soft-deleted in Azure AD
    if get_deleted_user(user_id) is not None:
        logger.info("userId=%s already in recycle bin – marking DELETED in tracking", user_id)
        record["statusCode"] = "DELETED"
        record["deletedDate"] = datetime.now(timezone.utc).date().isoformat()
        store.upsert_user(record)
        store.append_audit(user_id, "DELETED", f"cleanup_scan: already soft-deleted in AD (source={source})")
        return "skipped_already_deleted"

    # Safety check: skip if active EF forwarding found
    try:
        if has_email_forwarding(user_id):
            logger.warning(
                "userId=%s has active EF – skipping cleanup deletion. "
                "Daily monitor should handle this account.",
                user_id,
            )
            return "skipped_has_ef"
    except Exception as exc:
        logger.error("userId=%s EF check failed: %s – skipping", user_id, exc)
        return "error"

    # Safe to delete
    ok = delete_user(user_id)
    if not ok:
        store.append_audit(user_id, "DELETE_FAILED", f"cleanup_scan: Graph API delete failed (source={source})")
        logger.error("Cleanup delete failed for userId=%s (source=%s)", user_id, source)
        return "error"

    today_str = datetime.now(timezone.utc).date().isoformat()
    record["statusCode"]  = "DELETED"
    record["deletedDate"] = today_str
    store.upsert_user(record)
    store.append_audit(
        user_id, "DELETED",
        f"cleanup_scan: stale NO_EF account deleted (source={source}, region={_CLEANUP_REGION})",
    )
    logger.info("Cleanup: deleted stale account userId=%s (source=%s)", user_id, source)
    return "deleted"


def _tally(summary: Dict[str, int], result: str) -> None:
    if result == "deleted":
        summary["deleted"] += 1
    elif result == "skipped_already_deleted":
        summary["skipped_already_deleted"] += 1
    elif result == "skipped_has_ef":
        summary["skipped_has_ef"] += 1
    elif result == "skipped_exempt":
        summary["skipped_exempt"] += 1
    elif result == "dry_run":
        summary["dry_run"] += 1
    elif result == "error":
        summary["errors"] += 1


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)
