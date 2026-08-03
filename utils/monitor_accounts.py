"""
monitor_accounts.py
-------------------
Daily monitoring logic – runs via the Azure Functions timer trigger at 9 AM UTC.

Decision matrix (days elapsed from offboard date):

  EF Required = NO
  ┌───────────────────┬─────────────────────────────────────────────────────────────┐
  │ Any day           │ NO action – IT deletes manually during offboarding.         │
  │                   │ Monthly cleanup scan (cleanup_scan.py) is the safety net.   │
  │                   │ Set AUTO_DELETE_NO_EF=true to revert to auto-delete on Day  │
  │                   │ EF_REMOVE_1 (legacy behaviour).                             │
  └───────────────────┴─────────────────────────────────────────────────────────────┘

  EF Required = YES  (Workflow B – attribute-based approval)
  ┌────────────────────┬────────────────────────────────────────────────────────────┐
  │ Any day            │ If CSA ExtStatus ∈ approved values → extend +30 days,     │
  │                    │   re-enable EF if it was disabled, clear CSA, send confirm │
  │ Day ALERT_DAY_1(†) │ ALERT – set CSA = EF_ALERT_SENT (ext_count=0)             │
  │ Day EF_REMOVE_1=30 │ DISABLE EF – grace starts; account kept alive             │
  │ Day DELETE_DAY_1(‡)│ DELETE account (reason: NO_EXTENSION_DAY30)               │
  │ Day ALERT_DAY_2(†) │ ALERT – set CSA = EF_ALERT_SENT (ext_count=1)             │
  │ Day EF_REMOVE_2=60 │ DISABLE EF – grace starts; account kept alive             │
  │ Day DELETE_DAY_2(‡)│ DELETE account (reason: NO_EXTENSION_DAY60)               │
  │ Day ALERT_DAY_3(†) │ ALERT – FINAL (ext_count=2)                               │
  │ Day ≥90            │ DELETE account (reason: MAX_POLICY_DAY90) – no grace      │
  └────────────────────┴────────────────────────────────────────────────────────────┘

  (†) Configurable via ALERT_DAY_1 / ALERT_DAY_2 / ALERT_DAY_3 env vars.
  (‡) Configurable via DELETE_DAY_1 (default 46) / DELETE_DAY_2 (default 76).

Grace-period behaviour:
  EF forwarding is disabled on Day 30 / 60 but the account is NOT deleted.
  IT still has until Day DELETE_DAY_1 / DELETE_DAY_2 to process late approvals.
  If a CSA extension is approved in this window, EF is automatically re-enabled.

Idempotency:
  `lastAlertDate` prevents duplicate alerts within the same window.
  The CSA attribute is cleared immediately after processing so it cannot
  trigger a second extension on the next daily run.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from .table_store  import TableStore
from .graph_api    import (
    get_terminated_users,
    extract_offboard_date,
    has_email_forwarding,
    get_forwarding_address,
    disable_email_forwarding,
    enable_email_forwarding,
    delete_user,
    get_manager,
    is_extension_approved,
    set_extension_attribute,
    clear_extension_attribute,
    is_auto_reply_enabled,
    set_auto_reply,
    disable_auto_reply,
    is_litigation_hold_active,
    remove_all_licenses,
    get_ticket_ref_attribute,
    set_ticket_ref_attribute,
)
from .email_sender import (
    send_ef_alert,
    send_ef_removed_notice,
    send_extension_confirm,
    send_deletion_notice,
    send_final_deletion_notice,
    send_it_approval_notification,
    send_no_ef_admin_notice,
)
from .app_config import get_admin_emails
from .approval_webhook import generate_approval_urls
from .automation_flags import is_dry_run, log_active_gates
from .deletion_exempt import is_automated_deletion_exempt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (days from offboard date)
#
# Alert days: configurable – tune without re-deploying.
# EF removal days: fixed policy (Day 30 & 60).
# Account deletion days: configurable grace after EF removal
#   (default: +16 days = Day 46 / Day 76).
# ---------------------------------------------------------------------------

# ── Alert days (configurable) ─────────────────────────────────────────────
_ALERT_DAY_1  = int(os.environ.get("ALERT_DAY_1",  "23"))   # 1st alert (ext=0)
_ALERT_DAY_2  = int(os.environ.get("ALERT_DAY_2",  "53"))   # 2nd alert (ext=1)
_ALERT_DAY_3  = int(os.environ.get("ALERT_DAY_3",  "83"))   # final alert (ext=2)

# ── EF removal days (fixed Day-30 / Day-60 policy) ────────────────────────
_EF_REMOVE_1  = 30    # EF disabled when ext_count=0 and no extension approved
_EF_REMOVE_2  = 60    # EF disabled when ext_count=1 and no second extension

# ── Account deletion days (configurable) ──────────────────────────────────
_DELETE_DAY_1 = int(os.environ.get("DELETE_DAY_1", "46"))   # was hardcoded 30
_DELETE_DAY_2 = int(os.environ.get("DELETE_DAY_2", "76"))   # was hardcoded 60
_DELETE_DAY_3 = 90                                            # max-policy, no grace

# ── Alert windows (ALERT_DAY → EF_REMOVE - 1) ─────────────────────────────
_ALERT_WINDOW_1 = (_ALERT_DAY_1, _EF_REMOVE_1 - 1)          # e.g. (23, 29)
_ALERT_WINDOW_2 = (_ALERT_DAY_2, _EF_REMOVE_2 - 1)          # e.g. (53, 59)
_ALERT_WINDOW_3 = (_ALERT_DAY_3, _DELETE_DAY_3 - 1)         # e.g. (83, 89)

# ── NO_EF auto-delete (default: off – India teams delete manually) ─────────
_AUTO_DELETE_NO_EF = os.environ.get("AUTO_DELETE_NO_EF", "false").lower() == "true"

# ── Region gate for account deletion ──────────────────────────────────────
# Only Azure AD accounts whose usageLocation matches this code will be deleted.
# Government / compliance rule: accounts in non-India regions must NOT be
# deleted by automation; IT must handle those manually.
# Set DELETE_REGION="" to disable the gate and allow deletion for all regions.
_DELETE_REGION = os.environ.get("DELETE_REGION", "IN").strip().upper()

# Maximum number of extensions (Workflow B hard cap = Day 90 policy)
_MAX_EXTENSIONS  = 2

# Written to CSA ExtStatus when the alert email is sent.
# IT Engineer changes this to EXTEND_TO_30 / EXTEND_TO_60 / EXTENDED_MAX.
_ATTR_ALERT_SENT = "EF_ALERT_SENT"

# OOO max active days (policy: 30 days)
_OOO_MAX_DAYS = int(os.environ.get("OOO_MAX_DAYS", "30"))
# IT approval mailbox for Approve/Decline notifications
_IT_APPROVAL_EMAIL = os.environ.get("IT_APPROVAL_EMAIL", "")

# Days after offboard to send a one-time NO_EF reminder to admins (0 = disabled)
_NO_EF_ADMIN_REMINDER_DAY = int(os.environ.get("NO_EF_ADMIN_REMINDER_DAY", "28"))


# ---------------------------------------------------------------------------
# Entry point (called by function_app.py timer trigger)
# ---------------------------------------------------------------------------

def run_monitor() -> Dict[str, int]:
    """
    Main entry point for the daily monitoring run.

    Returns a summary dict with counts of each action taken, which the
    timer function logs for visibility.
    """
    store = TableStore()
    store.ensure_tables()

    log_active_gates("monitor_accounts")

    today = datetime.now(timezone.utc).date()
    logger.info("=== EF Monitor starting – %s ===", today.isoformat())

    # Fetch all terminated+disabled accounts from Azure AD
    ad_users = get_terminated_users()

    # Also fetch currently monitored users from Table Storage so we can
    # skip users whose accounts were already deleted (statusCode=DELETED)
    # and carry forward their existing records.
    tracked = {r["userId"]: r for r in store.list_active_users()}

    summary = {"checked": 0, "alerted": 0, "ef_removed": 0, "deleted": 0, "errors": 0, "skipped": 0, "dry_run": 0}

    for user in ad_users:
        user_id = user.get("id", "")
        if not user_id:
            continue

        try:
            summary["checked"] += 1
            action = _process_user(user, today, store, tracked.get(user_id))
            if action == "alerted":
                summary["alerted"] += 1
            elif action == "ef_removed":
                summary["ef_removed"] += 1
            elif action == "deleted":
                summary["deleted"] += 1
            elif action == "skipped":
                summary["skipped"] += 1
            elif action == "dry_run":
                summary["dry_run"] += 1
        except Exception as exc:
            summary["errors"] += 1
            logger.error("Unhandled error for userId=%s: %s", user_id, exc, exc_info=True)

    logger.info(
        "=== EF Monitor complete – checked=%d alerted=%d ef_removed=%d deleted=%d errors=%d skipped=%d ===",
        summary["checked"], summary["alerted"], summary["ef_removed"],
        summary["deleted"], summary["errors"],  summary["skipped"],
    )
    return summary


# ---------------------------------------------------------------------------
# Per-user processing
# ---------------------------------------------------------------------------

def _process_user(
    user: Dict[str, Any],
    today: date,
    store: TableStore,
    existing_record: Optional[Dict[str, Any]],
) -> str:
    """
    Evaluate and act on one terminated user.

    Returns one of: 'alerted', 'deleted', 'skipped', 'no_action'.
    """
    user_id = user["id"]

    # ── India-only processing gate ─────────────────────────────────────────────
    if _DELETE_REGION:
        user_region = (user.get("usageLocation") or "").strip().upper()
        if user_region != _DELETE_REGION:
            logger.debug(
                "userId=%s usageLocation='%s' not in DELETE_REGION='%s' – skipping",
                user_id, user_region, _DELETE_REGION,
            )
            return "skipped"

    # ── 1. Parse offboard date ──────────────────────────────────────────────
    raw_date = extract_offboard_date(user)
    if not raw_date:
        logger.debug("userId=%s has no extensionAttribute10 – skipping", user_id)
        return "skipped"

    try:
        # Normalise the 'Z' suffix (UTC) so Python 3.8 fromisoformat() accepts it.
        # Python 3.11+ accepts Z natively; earlier versions require '+00:00'.
        normalised = raw_date.replace("Z", "+00:00")
        offboard_dt = datetime.fromisoformat(normalised)
        offboard_date = offboard_dt.date()
    except ValueError:
        logger.warning("userId=%s invalid extensionAttribute10 value: %s", user_id, raw_date)
        return "skipped"

    days_elapsed = (today - offboard_date).days
    if days_elapsed < 0:
        logger.debug("userId=%s offboard date is in the future (%s) – skipping", user_id, offboard_date)
        return "skipped"

    # ── 2. Get or create tracking record ───────────────────────────────────
    record = existing_record or _create_record(user, offboard_date, store)
    if record is None:
        return "skipped"

    # ── 3. Skip already-deleted ─────────────────────────────────────────────
    if record.get("statusCode") == "DELETED":
        return "skipped"

    ef_required  = _bool(record.get("efRequired", False))
    ext_count    = int(record.get("extensionCount", 0))
    status       = record.get("statusCode", "ACTIVE")
    last_alert   = record.get("lastAlertDate", "")

    logger.debug(
        "userId=%s days=%d ef=%s ext=%d status=%s",
        user_id, days_elapsed, ef_required, ext_count, status,
    )

    # ── OOO duration check – disable after OOO_MAX_DAYS ────────────────────
    ooo_set_str = record.get("oooSetDate", "")
    if ooo_set_str and not record.get("oooDisabledDate"):
        try:
            ooo_days = (today - date.fromisoformat(ooo_set_str)).days
            if ooo_days >= _OOO_MAX_DAYS:
                _disable_ooo(user_id, record, store)
                record = store.get_user(user_id) or record  # refresh
        except ValueError:
            pass

    # ── 4. NO EF PATH ───────────────────────────────────────────────────────
    #   India policy: IT deletes NO_EF accounts manually during offboarding.
    #   The monthly cleanup scan (cleanup_scan.py) is the automated safety net.
    #   Set AUTO_DELETE_NO_EF=true to revert to immediate auto-delete behaviour.
    if not ef_required:
        _maybe_no_ef_admin_reminder(record, days_elapsed, store)
        if _AUTO_DELETE_NO_EF and days_elapsed >= _EF_REMOVE_1:
            return _do_delete(user_id, record, "NO_EF", store)
        return "no_action"

    # ── 5. HAS EF – Workflow B attribute check (runs before alert/EF-remove/delete) ──
    #   IT sets CSA ExtStatus to EXTEND_TO_30 / EXTEND_TO_60 / EXTENDED_MAX
    #   in Azure AD after HR + Infosec approve the ServiceDesk ticket.
    #   If EF was previously disabled (grace period), it is automatically re-enabled.
    record = _check_and_apply_extension_attribute(user, record, store)
    ext_count = int(record.get("extensionCount", 0))
    status    = record.get("statusCode", "ACTIVE")

    # ── 6. HAS EF – alert / EF-remove / delete decision ────────────────────

    if ext_count == 0:
        # ── 6a. Alert window (Day ALERT_DAY_1 … EF_REMOVE_1 - 1) ─────────
        if _ALERT_WINDOW_1[0] <= days_elapsed <= _ALERT_WINDOW_1[1]:
            if not _already_alerted(last_alert, offboard_date, _ALERT_DAY_1, _EF_REMOVE_1 - 1):
                return _do_alert(record, store, days_remaining=_EF_REMOVE_1 - days_elapsed, is_final=False)
            return "no_action"

        # ── 6b. EF grace window (Day EF_REMOVE_1 … DELETE_DAY_1 - 1) ─────
        if _EF_REMOVE_1 <= days_elapsed < _DELETE_DAY_1:
            if status != "EF_DISABLED":
                return _do_remove_ef(
                    user_id, record, store,
                    reason="NO_EXTENSION_DAY30",
                    days_until_delete=_DELETE_DAY_1 - days_elapsed,
                )
            return "no_action"

        # ── 6c. Delete (Day DELETE_DAY_1+) ────────────────────────────────
        if days_elapsed >= _DELETE_DAY_1:
            return _do_delete(user_id, record, "NO_EXTENSION_DAY30", store)

    elif ext_count == 1:
        # ── 6d. Alert window 2 (Day ALERT_DAY_2 … EF_REMOVE_2 - 1) ───────
        if _ALERT_WINDOW_2[0] <= days_elapsed <= _ALERT_WINDOW_2[1]:
            if not _already_alerted(last_alert, offboard_date, _ALERT_DAY_2, _EF_REMOVE_2 - 1):
                return _do_alert(record, store, days_remaining=_EF_REMOVE_2 - days_elapsed, is_final=False)
            return "no_action"

        # ── 6e. EF grace window (Day EF_REMOVE_2 … DELETE_DAY_2 - 1) ─────
        if _EF_REMOVE_2 <= days_elapsed < _DELETE_DAY_2:
            if status != "EF_DISABLED":
                return _do_remove_ef(
                    user_id, record, store,
                    reason="NO_EXTENSION_DAY60",
                    days_until_delete=_DELETE_DAY_2 - days_elapsed,
                )
            return "no_action"

        # ── 6f. Delete (Day DELETE_DAY_2+) ────────────────────────────────
        if days_elapsed >= _DELETE_DAY_2:
            return _do_delete(user_id, record, "NO_EXTENSION_DAY60", store)

    elif ext_count == 2:
        # ── 6g. Final alert window (Day ALERT_DAY_3 … DELETE_DAY_3 - 1) ──
        if _ALERT_WINDOW_3[0] <= days_elapsed <= _ALERT_WINDOW_3[1]:
            if not _already_alerted(last_alert, offboard_date, _ALERT_DAY_3, _DELETE_DAY_3 - 1):
                return _do_alert(record, store, days_remaining=_DELETE_DAY_3 - days_elapsed, is_final=True)
            return "no_action"

        # ── 6h. Final delete (Day 90+) – no grace period at max policy ────
        if days_elapsed >= _DELETE_DAY_3:
            return _do_delete(user_id, record, "MAX_POLICY_DAY90", store, is_final=True)

    return "no_action"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _do_alert(
    record: Dict[str, Any],
    store: TableStore,
    days_remaining: int,
    is_final: bool,
) -> str:
    user_id = record["userId"]
    today_str = datetime.now(timezone.utc).date().isoformat()

    if is_dry_run():
        store.append_audit(
            user_id, "DRY_RUN_WOULD_ALERT",
            f"days_remaining={days_remaining} final={is_final}",
        )
        logger.info("DRY_RUN: would send EF alert for userId=%s", user_id)
        return "dry_run"

    ok = send_ef_alert(record, days_remaining=max(days_remaining, 1), is_final=is_final)

    # Mark the Azure AD user profile so IT can see the alert has been sent.
    # IT then changes this to one of the predefined approved values
    # (EXTEND_TO_30 / EXTEND_TO_60 / EXTENDED_MAX) after HR + Infosec approval.
    set_extension_attribute(user_id, _ATTR_ALERT_SENT)

    status = "ALERT_SENT"
    store.append_email_log(
        user_id=user_id,
        email_type="ALERT",
        recipient=record.get("managerEmail", ""),
        subject=f"EF Expiration Alert – {record.get('displayName', '')}",
        status="SENT" if ok else "FAILED",
    )

    record["statusCode"]    = status
    record["lastAlertDate"] = today_str
    store.upsert_user(record)

    action_label = "FINAL_ALERT" if is_final else "ALERTED"
    store.append_audit(user_id, action_label, f"Alert sent. days_remaining={days_remaining}. CSA ExtStatus set to {_ATTR_ALERT_SENT}")
    logger.info("Alert sent for userId=%s (days_remaining=%d final=%s)", user_id, days_remaining, is_final)

    # Generate Approve/Decline URLs and send IT notification
    if _IT_APPROVAL_EMAIL:
        try:
            ext_count = int(record.get("extensionCount", 0))
            _token, approve_url, decline_url = generate_approval_urls(user_id, ext_count)
            ext_type_map = {0: "EXTEND_TO_30", 1: "EXTEND_TO_60", 2: "EXTENDED_MAX"}
            ext_type = ext_type_map.get(ext_count, "EXTEND_TO_30")
            send_it_approval_notification(record, approve_url, decline_url, ext_type)
        except Exception as exc:
            logger.error("Failed to generate/send IT approval notification for userId=%s: %s", user_id, exc)

    return "alerted"


def _do_remove_ef(
    user_id: str,
    record: Dict[str, Any],
    store: TableStore,
    reason: str,
    days_until_delete: int,
) -> str:
    """
    Disable email forwarding for a user who has not had an extension approved.

    The account is NOT deleted here – it remains alive until _DELETE_DAY_1 / _DELETE_DAY_2.
    If IT approves a late extension within that window, _check_and_apply_extension_attribute
    will detect the CSA value and automatically re-enable EF.
    """
    today_str = datetime.now(timezone.utc).date().isoformat()

    if is_dry_run():
        store.append_audit(
            user_id, "DRY_RUN_WOULD_DISABLE_EF",
            f"reason={reason} days_until_delete={days_until_delete}",
        )
        logger.info("DRY_RUN: would disable EF for userId=%s", user_id)
        return "dry_run"

    # Disable forwarding via Graph API
    disable_email_forwarding(user_id)

    # Notify manager + IT
    send_ef_removed_notice(record, days_until_delete=days_until_delete, reason=reason)
    store.append_email_log(
        user_id=user_id,
        email_type="EF_REMOVED",
        recipient=record.get("managerEmail", ""),
        subject=f"EF Disabled – {record.get('displayName', '')} – Account deletion in {days_until_delete}d",
        status="SENT",
    )

    record["statusCode"]    = "EF_DISABLED"
    record["efDisabledDate"] = today_str
    store.upsert_user(record)
    store.append_audit(
        user_id,
        "EF_DISABLED",
        f"reason={reason}. EF removed. Account deletion in {days_until_delete} days "
        f"(DELETE_DAY={today_str}+{days_until_delete}) unless late extension approved via CSA.",
    )
    logger.info("EF removed for userId=%s reason=%s days_until_delete=%d", user_id, reason, days_until_delete)
    return "ef_removed"


def _do_delete(
    user_id: str,
    record: Dict[str, Any],
    reason: str,
    store: TableStore,
    is_final: bool = False,
) -> str:
    today_str = datetime.now(timezone.utc).date().isoformat()

    exempt, exempt_reason = is_automated_deletion_exempt(user_id, record)
    if exempt:
        logger.info(
            "userId=%s automated deletion SKIPPED (exempt=%s) reason=%s",
            user_id, exempt_reason, reason,
        )
        store.append_audit(
            user_id,
            "DELETE_SKIPPED_EXEMPT",
            f"reason={reason}. exempt={exempt_reason}. Manual deletion or remove exemption when ready.",
        )
        return "no_action"

    # ── Region gate ──────────────────────────────────────────────────────────
    # Government compliance: only delete accounts in the permitted region.
    if _DELETE_REGION:
        user_region = (record.get("usageLocation") or "").strip().upper()
        if user_region != _DELETE_REGION:
            logger.warning(
                "REGION GATE: userId=%s usageLocation='%s' does not match "
                "DELETE_REGION='%s' – deletion SKIPPED (government compliance). "
                "IT must delete this account manually.",
                user_id, user_region, _DELETE_REGION,
            )
            store.append_audit(
                user_id,
                "DELETE_SKIPPED_REGION",
                f"reason={reason}. usageLocation='{user_region}' not in allowed "
                f"DELETE_REGION='{_DELETE_REGION}'. Manual deletion required.",
            )
            return "no_action"

    # ── Litigation hold check ───────────────────────────────────────────────
    try:
        if is_litigation_hold_active(user_id):
            logger.warning(
                "userId=%s has active litigation hold – deletion SKIPPED. IT must manage this manually.",
                user_id,
            )
            store.append_audit(
                user_id,
                "DELETE_SKIPPED_LEGAL_HOLD",
                f"reason={reason}. Active litigation/in-place hold detected via Graph beta. Manual deletion required.",
            )
            _update_compliance_from_record(record, store, legalHoldActive=True, legalHoldChecked=today_str)
            return "no_action"
    except Exception as exc:
        logger.error("userId=%s litigation hold check failed: %s – proceeding with caution", user_id, exc)

    if is_dry_run():
        store.append_audit(user_id, "DRY_RUN_WOULD_DELETE", f"reason={reason}")
        logger.info("DRY_RUN: would delete userId=%s reason=%s", user_id, reason)
        return "dry_run"

    # Perform the Azure AD soft-delete
    ok = delete_user(user_id)
    if not ok:
        store.append_audit(user_id, "DELETE_FAILED", f"reason={reason} – Graph API delete call failed")
        logger.error("Delete failed for userId=%s reason=%s", user_id, reason)
        return "errors"

    # Send appropriate notification email
    if is_final or reason == "MAX_POLICY_DAY90":
        send_final_deletion_notice(record)
        email_type = "FINAL_DELETION"
    else:
        send_deletion_notice(record, reason=reason)
        email_type = "DELETION_NOTICE"

    store.append_email_log(
        user_id=user_id,
        email_type=email_type,
        recipient=record.get("managerEmail", ""),
        subject=f"Account Deleted – {record.get('displayName', '')}",
        status="SENT",
    )

    # Update tracking record
    record["statusCode"]  = "DELETED"
    record["deletedDate"] = today_str
    store.upsert_user(record)

    # Remove M365 licenses after successful deletion
    try:
        remove_all_licenses(user_id)
        record["licensesRemovedDate"] = today_str
    except Exception as exc:
        logger.error("License removal failed for userId=%s: %s", user_id, exc)

    store.append_audit(user_id, "DELETED", f"reason={reason}")
    logger.info("Deleted userId=%s reason=%s", user_id, reason)

    _update_compliance_from_record(
        record, store,
        deletedDate=today_str,
        deleteReason=reason,
        licensesRemovedDate=record.get("licensesRemovedDate", ""),
    )

    return "deleted"


# ---------------------------------------------------------------------------
# Workflow B – extension attribute check
# ---------------------------------------------------------------------------

def _check_and_apply_extension_attribute(
    user: Dict[str, Any],
    record: Dict[str, Any],
    store: TableStore,
) -> Dict[str, Any]:
    """
    Inspect the CSA ExtStatus on the live Azure AD user object.

    If IT has set it to an approved value (EXTEND_TO_30 / EXTEND_TO_60 / EXTENDED_MAX):
      - Increment extensionCount
      - Recalculate deleteDate = offboardDate + extensionCount × 30 days
      - Clear the attribute in Azure AD so it cannot trigger again tomorrow
      - Send a confirmation email to the manager
      - Write audit log

    If the attribute is set but extensionCount is already at the maximum,
    the attribute is still cleared (to avoid confusion) but no extension is granted.

    Returns the (possibly updated) record dict.
    """
    if not is_extension_approved(user):
        return record

    user_id   = record["userId"]
    ext_count = int(record.get("extensionCount", 0))

    if ext_count >= _MAX_EXTENSIONS:
        logger.warning(
            "userId=%s CSA ExtStatus is set but max extensions (%d) already reached – "
            "clearing attribute without applying extension",
            user_id, _MAX_EXTENSIONS,
        )
        clear_extension_attribute(user_id)
        store.append_audit(
            user_id,
            "ATTR_IGNORED",
            f"CSA ExtStatus set but max extensions ({_MAX_EXTENSIONS}) already reached – ignored",
        )
        return record

    # Read and store SD+ ticket reference if IT set it alongside the extension CSA
    ticket_ref = get_ticket_ref_attribute(user)
    if ticket_ref:
        record["ticketRef"] = ticket_ref
        logger.info("Stored ticketRef=%s for userId=%s", ticket_ref, user_id)

    # Apply the extension
    new_ext_count = ext_count + 1
    try:
        offboard_date = date.fromisoformat(record["offboardDate"])
    except (ValueError, KeyError) as exc:
        logger.error(
            "userId=%s cannot apply attribute extension – invalid offboardDate in record: %s",
            user_id, exc,
        )
        return record

    new_delete_date = offboard_date + timedelta(days=new_ext_count * 30)

    # If EF was disabled during the grace period, re-enable it now
    was_ef_disabled = record.get("statusCode") == "EF_DISABLED"
    if was_ef_disabled:
        fwd_addr = record.get("forwardingAddress", "")
        if fwd_addr:
            enable_email_forwarding(user_id, fwd_addr)
            logger.info("Re-enabled EF for userId=%s (late extension after grace period)", user_id)
        else:
            logger.warning(
                "userId=%s EF was disabled but forwardingAddress not stored – "
                "EF cannot be re-enabled automatically; IT must re-enable manually",
                user_id,
            )

    record["extensionCount"] = new_ext_count
    record["deleteDate"]     = new_delete_date.isoformat()
    record["statusCode"]     = "EXTENDED" if new_ext_count < _MAX_EXTENSIONS else "EXTENDED_MAX"
    record["lastAlertDate"]  = ""  # reset so the next alert window fires normally

    store.upsert_user(record)
    store.append_audit(
        user_id,
        "EXTENDED",
        f"Extension {new_ext_count}/{_MAX_EXTENSIONS} applied via CSA "
        f"(Workflow B – IT action after HR+Infosec approval). "
        f"New deleteDate={new_delete_date.isoformat()}."
        + (" EF re-enabled." if was_ef_disabled else ""),
    )

    # Clear the attribute immediately so it does not re-trigger tomorrow.
    clear_extension_attribute(user_id)

    # Send confirmation email to manager + IT CC
    ok = send_extension_confirm(record)
    store.append_email_log(
        user_id=user_id,
        email_type="EXTENSION_CONFIRM",
        recipient=record.get("managerEmail", ""),
        subject=f"EF Extended – {record.get('displayName', '')}",
        status="SENT" if ok else "FAILED",
    )

    logger.info(
        "Attribute-based extension %d/%d applied for userId=%s – new deleteDate=%s",
        new_ext_count, _MAX_EXTENSIONS, user_id, new_delete_date.isoformat(),
    )

    _update_compliance_from_record(
        record, store,
        extensionCount=new_ext_count,
        latestTicketRef=record.get("ticketRef", ""),
        latestExtensionDate=datetime.now(timezone.utc).date().isoformat(),
    )

    return record


# ---------------------------------------------------------------------------
# Record initialisation
# ---------------------------------------------------------------------------

def _create_record(
    user: Dict[str, Any],
    offboard_date: date,
    store: TableStore,
) -> Optional[Dict[str, Any]]:
    """
    Build and persist a new UserTracking record for a user seen for the first time.
    """
    user_id    = user["id"]
    user_email = user.get("mail") or user.get("userPrincipalName", "")

    # Resolve manager – may already be in the $expand response
    manager_obj = user.get("manager") or {}
    if not manager_obj.get("mail"):
        # Fallback: dedicated manager call
        manager_obj = get_manager(user_id) or {}

    manager_email = manager_obj.get("mail", "")
    manager_id    = manager_obj.get("id", "")

    if not manager_email:
        logger.warning("userId=%s has no manager email – will be tracked but alerts may not send", user_id)

    # Check email forwarding (live Graph API call)
    ef_required       = has_email_forwarding(user_id)
    forwarding_address = get_forwarding_address(user_id) if ef_required else ""

    delete_date = (offboard_date + timedelta(days=30)).isoformat()

    usage_location = (user.get("usageLocation") or "").strip().upper()

    record: Dict[str, Any] = {
        "userId":              user_id,
        "userEmail":           user_email,
        "displayName":         user.get("displayName", ""),
        "managerId":           manager_id,
        "managerEmail":        manager_email,
        "country":             user.get("country", ""),
        "usageLocation":       usage_location,
        "offboardDate":        offboard_date.isoformat(),
        "efRequired":          ef_required,
        "forwardingAddress":   forwarding_address or "",  # stored for re-enablement after grace removal
        "statusCode":          "ACTIVE",
        "extensionCount":      0,
        "deleteDate":          delete_date,
        "deletedDate":         "",
        "efDisabledDate":      "",
        "lastAlertDate":       "",
        "oooSetDate":          "",
        "oooDisabledDate":     "",
        "ticketRef":           "",
        "legalHoldChecked":    "",
        "legalHoldActive":     False,
        "licensesRemovedDate": "",
        "deletionExempt":      False,
        "noEfAdminNotifiedDate": "",
        "noEfAdminReminderDate": "",
    }

    store.upsert_user(record)
    store.append_audit(
        user_id,
        "REGISTERED",
        f"First seen. ef={ef_required} offboard={offboard_date} deleteDate={delete_date}",
    )
    logger.info(
        "Registered new user userId=%s ef=%s offboard=%s", user_id, ef_required, offboard_date
    )

    # Set OOO on first registration (only if not already active)
    try:
        if not is_auto_reply_enabled(user_id):
            manager_name = manager_obj.get("displayName", "") or manager_email
            ooo_ok = set_auto_reply(user_id, manager_name=manager_name, manager_email=manager_email)
            if ooo_ok:
                record["oooSetDate"] = offboard_date.isoformat()
                store.upsert_user(record)
                logger.info("OOO set for userId=%s (manager: %s)", user_id, manager_email)
        else:
            record["oooSetDate"] = offboard_date.isoformat()
            store.upsert_user(record)
            logger.info("OOO already active for userId=%s – skipping set", user_id)
    except Exception as exc:
        logger.error("OOO setup failed for userId=%s: %s", user_id, exc)

    # Create initial ComplianceExport record
    _update_compliance_from_record(record, store, firstSeenDate=offboard_date.isoformat())

    if not ef_required:
        _notify_no_ef_admins_on_register(record, store)

    return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notify_no_ef_admins_on_register(record: Dict[str, Any], store: TableStore) -> None:
    """One-time admin notice when a NO_EF account enters tracking."""
    if not get_admin_emails():
        return
    user_id = record.get("userId", "")
    if record.get("noEfAdminNotifiedDate"):
        return
    sent = send_no_ef_admin_notice(record, event="registered")
    today_str = datetime.now(timezone.utc).date().isoformat()
    record["noEfAdminNotifiedDate"] = today_str
    store.upsert_user(record)
    store.append_email_log(
        user_id=user_id,
        email_type="NO_EF_ADMIN",
        recipient="ADMIN_EMAILS",
        subject=f"NO EF registered – {record.get('displayName', '')}",
        status="SENT" if sent else "SUPPRESSED",
    )
    store.append_audit(user_id, "NO_EF_ADMIN_NOTICE", "Admin notified: account has no email forwarding.")


def _maybe_no_ef_admin_reminder(
    record: Dict[str, Any],
    days_elapsed: int,
    store: TableStore,
) -> None:
    """Optional one-time reminder before monthly cleanup may delete the account."""
    if _NO_EF_ADMIN_REMINDER_DAY <= 0:
        return
    if record.get("noEfAdminReminderDate"):
        return
    if days_elapsed < _NO_EF_ADMIN_REMINDER_DAY:
        return
    if record.get("statusCode") == "DELETED":
        return

    cleanup_min = int(os.environ.get("CLEANUP_MIN_OFFBOARD_DAYS", "30"))
    days_until = max(cleanup_min - days_elapsed, 0)
    user_id = record.get("userId", "")
    sent = send_no_ef_admin_notice(
        record,
        event="reminder",
        days_elapsed=days_elapsed,
        days_until_cleanup=days_until,
    )
    today_str = datetime.now(timezone.utc).date().isoformat()
    record["noEfAdminReminderDate"] = today_str
    store.upsert_user(record)
    store.append_audit(
        user_id,
        "NO_EF_ADMIN_REMINDER",
        f"Admin reminder at day {days_elapsed}. sent={sent}",
    )


def _bool(value: Any) -> bool:
    """Coerce various truthy representations from Table Storage to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _already_alerted(last_alert_date: str, offboard_date: date, window_start_day: int, window_end_day: int) -> bool:
    """
    Return True if an alert was already sent during the current alert window.

    window_start_day / window_end_day are both expressed as days-from-offboard.
    The window covers the full range from the configured alert day to the day
    before the deletion deadline, so a catch-up run within the same window
    does not re-send the alert.
    """
    if not last_alert_date:
        return False
    try:
        alerted = date.fromisoformat(last_alert_date)
        window_start = offboard_date + timedelta(days=window_start_day)
        window_end   = offboard_date + timedelta(days=window_end_day)
        return window_start <= alerted <= window_end
    except ValueError:
        return False


def _disable_ooo(user_id: str, record: Dict[str, Any], store: TableStore) -> None:
    """Disable OOO after the max active period."""
    today_str = datetime.now(timezone.utc).date().isoformat()
    try:
        disable_auto_reply(user_id)
    except Exception as exc:
        logger.error("Failed to disable OOO for userId=%s: %s", user_id, exc)
    record["oooDisabledDate"] = today_str
    store.upsert_user(record)
    store.append_audit(user_id, "OOO_DISABLED", f"OOO disabled after {_OOO_MAX_DAYS} days (policy limit).")
    _update_compliance_from_record(record, store, oooDisabledDate=today_str)
    logger.info("OOO disabled for userId=%s after %d days", user_id, _OOO_MAX_DAYS)


def _update_compliance_from_record(record: Dict[str, Any], store: TableStore, **extra: Any) -> None:
    """
    Build / update the ComplianceExport row for this user from the tracking record.
    Merges in any extra keyword-argument overrides.
    """
    user_id = record.get("userId", "")
    existing = store.get_compliance_record(user_id) or {}

    snapshot = {
        "userId":               user_id,
        "displayName":          record.get("displayName", ""),
        "userEmail":            record.get("userEmail", ""),
        "usageLocation":        record.get("usageLocation", ""),
        "country":              record.get("country", ""),
        "offboardDate":         record.get("offboardDate", ""),
        "efRequired":           record.get("efRequired", False),
        "forwardingAddress":    record.get("forwardingAddress", ""),
        "ticketRef":            record.get("ticketRef", ""),
        "extensionCount":       record.get("extensionCount", 0),
        "statusCode":           record.get("statusCode", ""),
        "lastAlertDate":        record.get("lastAlertDate", ""),
        "oooSetDate":           record.get("oooSetDate", ""),
        "oooDisabledDate":      record.get("oooDisabledDate", ""),
        "efDisabledDate":       record.get("efDisabledDate", ""),
        "deletedDate":          record.get("deletedDate", ""),
        "licensesRemovedDate":  record.get("licensesRemovedDate", ""),
        "managerEmail":         record.get("managerEmail", ""),
    }
    # Apply existing values then overrides
    if isinstance(existing, dict):
        existing.update(snapshot)
        existing.update(extra)
    else:
        existing = snapshot
        existing.update(extra)
    store.upsert_compliance_record(existing)
