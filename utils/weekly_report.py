"""
weekly_report.py
----------------
Weekly (Monday) and monthly offboard / EF summary for REPORT_EMAILS.

Builds a consolidated report covering:
  - Newly tracked NO_EF accounts in the period
  - Newly tracked EF accounts (forwarding target + manager)
  - Overdue NO_EF accounts
  - Accounts deleted in the period (automation, monthly cleanup, or manually by IT)
  - Activity counts (alerts, extensions, deletions)

SharePoint folder link is included when SHAREPOINT_* settings are set.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from .app_config import get_report_emails
from .email_sender import send_offboard_consolidated_report
from .table_store import TableStore

logger = logging.getLogger(__name__)

_NO_EF_ADMIN_REMINDER_DAY = int(os.environ.get("NO_EF_ADMIN_REMINDER_DAY", "10"))
_RECOVERY_GRACE_DAYS = int(os.environ.get("RECOVERY_GRACE_DAYS", "30"))


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _user_row(u: Dict[str, Any], *, days_elapsed: Any = None) -> Dict[str, Any]:
    row = {
        "displayName": u.get("displayName", ""),
        "userEmail": u.get("userEmail", ""),
        "offboardDate": u.get("offboardDate", ""),
        "usageLocation": u.get("usageLocation", ""),
        "managerEmail": u.get("managerEmail", ""),
        "forwardingAddress": u.get("forwardingAddress", ""),
        "userId": u.get("userId", ""),
    }
    if days_elapsed is not None:
        row["daysElapsed"] = days_elapsed
    return row


def _deleted_row(u: Dict[str, Any]) -> Dict[str, Any]:
    row = _user_row(u)
    deleted = u.get("deletedDate", "")
    try:
        recoverable = (date.fromisoformat(deleted) + timedelta(days=_RECOVERY_GRACE_DAYS)).isoformat()
    except ValueError:
        recoverable = ""
    row.update({
        "deletedDate": deleted,
        "deleteReason": u.get("deleteReason", ""),
        "deletedBy": u.get("deletedBy", ""),
        "recoverableUntil": recoverable,
    })
    return row


def _collect_period_data(store: TableStore, lookback_days: int) -> Dict[str, Any]:
    today = date.today()
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    since_iso = since.isoformat()
    period_start = since.date().isoformat()

    all_users = {u.get("userId"): u for u in store.list_all_users() if u.get("userId")}
    recent = store.list_recent_audits(since_iso)

    registered_ids = {
        (a.get("PartitionKey") or "").strip()
        for a in recent
        if (a.get("action") or "") == "REGISTERED"
    }
    registered_ids.discard("")

    new_no_ef: List[Dict[str, Any]] = []
    new_with_ef: List[Dict[str, Any]] = []
    for uid in sorted(registered_ids):
        u = all_users.get(uid) or store.get_user(uid)
        if not u:
            continue
        row = _user_row(u)
        if _bool(u.get("efRequired", False)):
            new_with_ef.append(row)
        else:
            new_no_ef.append(row)

    overdue: List[Dict[str, Any]] = []
    if _NO_EF_ADMIN_REMINDER_DAY > 0:
        for u in all_users.values():
            if _bool(u.get("efRequired", False)):
                continue
            if u.get("statusCode") == "DELETED":
                continue
            try:
                offboard = date.fromisoformat(u.get("offboardDate", ""))
            except ValueError:
                continue
            days = (today - offboard).days
            if days >= _NO_EF_ADMIN_REMINDER_DAY:
                overdue.append(_user_row(u, days_elapsed=days))
        overdue.sort(key=lambda r: int(r.get("daysElapsed") or 0), reverse=True)

    deleted = sorted(
        (_deleted_row(u) for u in all_users.values()
         if u.get("statusCode") == "DELETED" and (u.get("deletedDate") or "") >= period_start),
        key=lambda r: r["deletedDate"],
        reverse=True,
    )

    alerts = sum(1 for a in recent if (a.get("action") or "") in ("ALERTED", "ALERT_SENT", "FINAL_ALERT"))
    extensions = sum(1 for a in recent if str(a.get("action", "")).startswith("EXTENDED"))
    active = sum(1 for u in all_users.values() if u.get("statusCode") != "DELETED")

    return {
        "report_date": today.isoformat(),
        "period_start": period_start,
        "new_no_ef": new_no_ef,
        "new_with_ef": new_with_ef,
        "overdue_no_ef": overdue,
        "deleted": deleted,
        "summary": {
            "alerts": alerts,
            "extensions": extensions,
            "deletions": len(deleted),
            "total_active": active,
        },
    }


def _refresh_ef_forwarding_targets(rows: List[Dict[str, Any]]) -> None:
    """Fill blank Forwarding-to from EXO Admin Center when possible."""
    from .graph_api import get_forwarding_address

    for row in rows:
        if row.get("forwardingAddress"):
            continue
        uid = row.get("userId") or ""
        if not uid:
            continue
        try:
            addr = get_forwarding_address(uid) or ""
            if addr:
                row["forwardingAddress"] = addr
        except Exception:
            continue


def archive_period_report(
    period_label: str = "OnDemand",
    lookback_days: int = 7,
) -> Dict[str, Any]:
    """
    Build Excel/CSV archives and upload to SharePoint — no email.

    Used by the HTTP `archive_report` function for immediate reports.
    """
    from .report_export import build_report_archives
    from .sharepoint import upload_report_file

    store = TableStore()
    store.ensure_tables()
    data = _collect_period_data(store, lookback_days)
    _refresh_ef_forwarding_targets(data["new_with_ef"])

    archives = build_report_archives(
        report_date=data["report_date"],
        period_label=period_label,
        period_start=data["period_start"],
        new_no_ef=data["new_no_ef"],
        new_with_ef=data["new_with_ef"],
        overdue_no_ef=data["overdue_no_ef"],
        deleted=data["deleted"],
        summary=data["summary"],
    )
    uploaded = []
    errors = []
    for filename, content, content_type in archives:
        result = upload_report_file(
            filename=filename, content=content, content_type=content_type,
        )
        if result.get("ok"):
            uploaded.append({"filename": filename, "web_url": result.get("web_url", "")})
        else:
            errors.append({"filename": filename, "error": result.get("error", "")})

    summary = {
        "status": "archived" if uploaded else "failed",
        "period": period_label,
        "email_sent": False,
        "uploaded": uploaded,
        "errors": errors,
        "new_no_ef": len(data["new_no_ef"]),
        "new_with_ef": len(data["new_with_ef"]),
        "overdue_no_ef": len(data["overdue_no_ef"]),
        **data["summary"],
    }
    logger.info("Archive-only report: %s", summary)
    return summary


def _send_period_report(period_label: str, lookback_days: int) -> Dict[str, Any]:
    recipients = get_report_emails()
    if not recipients:
        logger.warning("%s report skipped – REPORT_EMAILS/ADMIN_EMAILS not set", period_label)
        return {"status": "skipped", "reason": "REPORT_EMAILS not configured"}

    store = TableStore()
    store.ensure_tables()
    data = _collect_period_data(store, lookback_days)
    _refresh_ef_forwarding_targets(data["new_with_ef"])

    sent = send_offboard_consolidated_report(
        report_date=data["report_date"],
        new_no_ef=data["new_no_ef"],
        new_with_ef=data["new_with_ef"],
        overdue_no_ef=data["overdue_no_ef"],
        deleted=data["deleted"],
        summary=data["summary"],
        period_label=period_label,
        period_start=data["period_start"],
    )
    if sent:
        store.append_email_log(
            user_id=f"{period_label.upper()}_REPORT",
            email_type="OFFBOARD_REPORT",
            recipient="REPORT_EMAILS",
            subject=f"{period_label} offboard report – {data['report_date']}",
            status="SENT",
        )

    summary = {
        "status": "sent" if sent else "failed",
        "period": period_label,
        "recipients_count": sent,
        "new_no_ef": len(data["new_no_ef"]),
        "new_with_ef": len(data["new_with_ef"]),
        "overdue_no_ef": len(data["overdue_no_ef"]),
        **data["summary"],
    }
    logger.info("%s report dispatched: %s", period_label, summary)
    return summary


def run_weekly_report() -> Dict[str, Any]:
    """Monday weekly report (default lookback 7 days)."""
    return _send_period_report("Weekly", lookback_days=7)


def run_monthly_report() -> Dict[str, Any]:
    """Monthly report (default lookback 31 days)."""
    return _send_period_report("Monthly", lookback_days=31)

