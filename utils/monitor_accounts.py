"""
monitor_accounts.py
-------------------
Daily monitoring logic – runs via the Azure Functions timer trigger at 9 AM UTC.

Decision matrix (days elapsed from offboard date):

  EF Required = NO
  ┌─────────┬──────────────────────────────────────────┐
  │ Day ≥30 │ DELETE account (reason: NO_EF)           │
  └─────────┴──────────────────────────────────────────┘

  EF Required = YES  (Workflow B – attribute-based approval)
  ┌────────────────────┬────────────────────────────────────────────────────────────┐
  │ Any day            │ If extensionAttribute11 = EXTEND_30 → extend +30 days,     │
  │                    │ clear attribute, send confirm (checked before alert/delete) │
  │ Day ALERT_DAY_1    │ ALERT if not already alerted (extension 0)                 │
  │ (default: Day 23)  │ Set extensionAttribute11=EF_ALERT_SENT on user profile     │
  │                    │ Email instructs manager to raise a ServiceDesk ticket       │
  │ Day ≥30, ext=0     │ DELETE (reason: NO_EXTENSION_DAY30)                        │
  │ Day ALERT_DAY_2    │ ALERT if not already alerted (before Day 60)               │
  │ (default: Day 53)  │ Set extensionAttribute11=EF_ALERT_SENT on user profile     │
  │ Day ≥60, ext=1     │ DELETE (reason: NO_EXTENSION_DAY60)                        │
  │ Day ALERT_DAY_3    │ ALERT if not already alerted (FINAL, before D90)           │
  │ (default: Day 83)  │                                                            │
  │ Day ≥90            │ DELETE (reason: MAX_POLICY_DAY90)                          │
  └────────────────────┴────────────────────────────────────────────────────────────┘

Alert windows (25–29, 55–59, 85–89) catch up on missed runs:
  If the daily function was down on Day 25, the alert fires on Day 26, 27 …
  as long as the deletion hasn't happened yet.

Idempotency:
  Each user record carries a `lastAlertDate` field.  The monitor will not
  send a second alert for the same period even if re-run within the window.
  The attribute check is also idempotent: the attribute is cleared immediately
  after processing so it cannot trigger a second extension on the next run.
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
    delete_user,
    get_manager,
    is_extension_approved,
    set_extension_attribute,
    clear_extension_attribute,
)
from .email_sender import (
    send_ef_alert,
    send_extension_confirm,
    send_deletion_notice,
    send_final_deletion_notice,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alert / delete thresholds (in days from offboard date)
#
# Alert days are configurable via environment variables so the window before
# the deletion deadline can be tuned without a code deployment.
# Defaults to Day 23 / 53 / 83 (7 days before each deletion) to give enough
# time for the ServiceDesk ticket to be raised, HR + Infosec to approve, and
# IT to set extensionAttribute11.
#
# Delete thresholds are fixed policy limits and are NOT configurable.
# ---------------------------------------------------------------------------
_ALERT_DAY_1     = int(os.environ.get("ALERT_DAY_1", "23"))  # default: Day 23
_DELETE_1        = 30
_ALERT_WINDOW_1  = (_ALERT_DAY_1, _DELETE_1 - 1)             # e.g., (23, 29)

_ALERT_DAY_2     = int(os.environ.get("ALERT_DAY_2", "53"))  # default: Day 53
_DELETE_2        = 60
_ALERT_WINDOW_2  = (_ALERT_DAY_2, _DELETE_2 - 1)             # e.g., (53, 59)

_ALERT_DAY_3     = int(os.environ.get("ALERT_DAY_3", "83"))  # default: Day 83
_DELETE_3        = 90
_ALERT_WINDOW_3  = (_ALERT_DAY_3, _DELETE_3 - 1)             # e.g., (83, 89)

# Maximum number of extensions (Workflow B hard cap = Day 90 policy)
_MAX_EXTENSIONS  = 2

# Value written to extensionAttribute11 when the alert email is sent.
# IT changes this to "EXTEND_30" / "APPROVED" after HR + Infosec approval.
_ATTR_ALERT_SENT = "EF_ALERT_SENT"


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

    today = datetime.now(timezone.utc).date()
    logger.info("=== EF Monitor starting – %s ===", today.isoformat())

    # Fetch all terminated+disabled accounts from Azure AD
    ad_users = get_terminated_users()

    # Also fetch currently monitored users from Table Storage so we can
    # skip users whose accounts were already deleted (statusCode=DELETED)
    # and carry forward their existing records.
    tracked = {r["userId"]: r for r in store.list_active_users()}

    summary = {"checked": 0, "alerted": 0, "deleted": 0, "errors": 0, "skipped": 0}

    for user in ad_users:
        user_id = user.get("id", "")
        if not user_id:
            continue

        try:
            summary["checked"] += 1
            action = _process_user(user, today, store, tracked.get(user_id))
            if action == "alerted":
                summary["alerted"] += 1
            elif action == "deleted":
                summary["deleted"] += 1
            elif action == "skipped":
                summary["skipped"] += 1
        except Exception as exc:
            summary["errors"] += 1
            logger.error("Unhandled error for userId=%s: %s", user_id, exc, exc_info=True)

    logger.info(
        "=== EF Monitor complete – checked=%d alerted=%d deleted=%d errors=%d skipped=%d ===",
        summary["checked"], summary["alerted"], summary["deleted"],
        summary["errors"],  summary["skipped"],
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

    # ── 4. NO EF PATH ───────────────────────────────────────────────────────
    if not ef_required:
        if days_elapsed >= _DELETE_1:
            return _do_delete(user_id, record, "NO_EF", store)
        return "no_action"

    # ── 5. HAS EF – Workflow B attribute check (runs before alert/delete) ───
    #   IT Engineer sets extensionAttribute11 = "EXTEND_30" in Azure AD after
    #   HR + Infosec approve the manager's ServiceDesk ticket.  The monitor
    #   detects it here, applies the extension, and clears the attribute so it
    #   cannot trigger a second extension on the next daily run.
    record = _check_and_apply_extension_attribute(user, record, store)
    # Refresh locals in case an extension was just applied.
    ext_count  = int(record.get("extensionCount", 0))

    # ── 6. HAS EF – alert / delete decision ────────────────────────────────

    # ---- 6a. WINDOW 1: Day _ALERT_DAY_1 to Day 29, extension 0 → first alert ---
    if _ALERT_WINDOW_1[0] <= days_elapsed <= _ALERT_WINDOW_1[1]:
        if ext_count == 0 and not _already_alerted(last_alert, offboard_date, _ALERT_DAY_1, _DELETE_1 - 1):
            return _do_alert(record, store, days_remaining=_DELETE_1 - days_elapsed, is_final=False)
        return "no_action"

    # ---- 6b. Day ≥ 30, no extension → delete ------------------------------
    if days_elapsed >= _DELETE_1 and ext_count == 0:
        return _do_delete(user_id, record, "NO_EXTENSION_DAY30", store)

    # ---- 6c. WINDOW 2: Day _ALERT_DAY_2 to Day 59, extension 1 → second alert -
    if _ALERT_WINDOW_2[0] <= days_elapsed <= _ALERT_WINDOW_2[1]:
        if ext_count == 1 and not _already_alerted(last_alert, offboard_date, _ALERT_DAY_2, _DELETE_2 - 1):
            return _do_alert(record, store, days_remaining=_DELETE_2 - days_elapsed, is_final=False)
        return "no_action"

    # ---- 6d. Day ≥ 60, only 1 extension used → delete ---------------------
    if days_elapsed >= _DELETE_2 and ext_count == 1:
        return _do_delete(user_id, record, "NO_EXTENSION_DAY60", store)

    # ---- 6e. WINDOW 3: Day _ALERT_DAY_3 to Day 89, extension 2 → final alert -
    if _ALERT_WINDOW_3[0] <= days_elapsed <= _ALERT_WINDOW_3[1]:
        if ext_count == 2 and not _already_alerted(last_alert, offboard_date, _ALERT_DAY_3, _DELETE_3 - 1):
            return _do_alert(record, store, days_remaining=_DELETE_3 - days_elapsed, is_final=True)
        return "no_action"

    # ---- 6f. Day ≥ 90 → final delete -------------------------------------
    if days_elapsed >= _DELETE_3:
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

    ok = send_ef_alert(record, days_remaining=max(days_remaining, 1), is_final=is_final)

    # Mark the Azure AD user profile so IT can see the alert has been sent.
    # IT changes this value to "EXTEND_30" after HR + Infosec approval.
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
    store.append_audit(user_id, action_label, f"Alert sent. days_remaining={days_remaining}. extensionAttribute11 set to {_ATTR_ALERT_SENT}")
    logger.info("Alert sent for userId=%s (days_remaining=%d final=%s)", user_id, days_remaining, is_final)
    return "alerted"


def _do_delete(
    user_id: str,
    record: Dict[str, Any],
    reason: str,
    store: TableStore,
    is_final: bool = False,
) -> str:
    today_str = datetime.now(timezone.utc).date().isoformat()

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

    store.append_audit(user_id, "DELETED", f"reason={reason}")
    logger.info("Deleted userId=%s reason=%s", user_id, reason)
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
    Inspect extensionAttribute11 on the live Azure AD user object.

    If IT has set it to an approved value (EXTEND_30 / APPROVED / YES):
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
            "userId=%s extensionAttribute11 is set but max extensions (%d) already reached – "
            "clearing attribute without applying extension",
            user_id, _MAX_EXTENSIONS,
        )
        clear_extension_attribute(user_id)
        store.append_audit(
            user_id,
            "ATTR_IGNORED",
            f"extensionAttribute11 set but max extensions ({_MAX_EXTENSIONS}) already reached",
        )
        return record

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

    record["extensionCount"] = new_ext_count
    record["deleteDate"]     = new_delete_date.isoformat()
    record["statusCode"]     = "EXTENDED" if new_ext_count < _MAX_EXTENSIONS else "EXTENDED_MAX"
    record["lastAlertDate"]  = ""  # reset so the next alert window fires normally

    store.upsert_user(record)
    store.append_audit(
        user_id,
        "EXTENDED",
        f"Extension {new_ext_count}/{_MAX_EXTENSIONS} applied via extensionAttribute11 "
        f"(Workflow B – IT action after HR+Infosec approval). "
        f"New deleteDate={new_delete_date.isoformat()}",
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
    ef_required = has_email_forwarding(user_id)

    delete_date = (offboard_date + timedelta(days=30)).isoformat()

    record: Dict[str, Any] = {
        "userId":          user_id,
        "userEmail":       user_email,
        "displayName":     user.get("displayName", ""),
        "managerId":       manager_id,
        "managerEmail":    manager_email,
        "offboardDate":    offboard_date.isoformat(),
        "efRequired":      ef_required,
        "statusCode":      "ACTIVE",
        "extensionCount":  0,
        "deleteDate":      delete_date,
        "deletedDate":     "",
        "lastAlertDate":   "",
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
    return record


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
