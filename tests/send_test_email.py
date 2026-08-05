"""
send_test_email.py
------------------
Send EF email templates / consolidated report to prem_testing@netradyne.com only.
Uses Microsoft Graph Mail.Send — no SMTP password. CC always suppressed.

Usage
-----
  python tests/send_test_email.py --template manager_all
  python tests/send_test_email.py --template report
  python tests/send_test_email.py --template first
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def _load_local_settings() -> None:
    settings_path = _ROOT / "local.settings.json"
    if not settings_path.is_file():
        return
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Could not read local.settings.json: {exc}")
        return
    for key, value in (data.get("Values") or {}).items():
        if value is None:
            continue
        os.environ.setdefault(str(key), str(value))


_load_local_settings()

os.environ["EF_TEST_MODE"] = "true"
os.environ["EF_TEST_RECIPIENT"] = "prem_testing@netradyne.com"
os.environ["IT_EMAIL"] = ""
os.environ["IT_APPROVAL_EMAIL"] = "prem_testing@netradyne.com"
os.environ["ADMIN_EMAILS"] = "prem_testing@netradyne.com"
os.environ["REPORT_EMAILS"] = "prem_testing@netradyne.com"
os.environ.setdefault("SENDER_EMAIL", "it-automation-service@netradyne.com")
os.environ.setdefault(
    "SERVICEDESK_TICKET_URL",
    os.environ.get("SDP_TICKET_URL", "https://itservicedesk.netradyne.com/"),
)
# Demo SharePoint link until real folder URL is provided
os.environ.setdefault(
    "SHAREPOINT_REPORT_URL",
    "https://netradyne.sharepoint.com/sites/IT/Shared%20Documents/EF-Automation-Reports",
)
os.environ["EF_DRY_RUN"] = "false"
os.environ["DISABLE_OUTBOUND_EMAIL"] = "false"

_missing = [
    k for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    if not os.environ.get(k) or str(os.environ.get(k)).startswith("<")
]
if _missing:
    print(f"[ERROR] Missing app registration settings: {', '.join(_missing)}")
    sys.exit(1)

from utils.email_sender import (  # noqa: E402
    send_deletion_notice,
    send_ef_alert,
    send_ef_removed_notice,
    send_extension_confirm,
    send_final_deletion_notice,
    send_it_approval_notification,
    send_offboard_consolidated_report,
)

TEST_RECIPIENT = "prem_testing@netradyne.com"
_today = date.today()
_offboard = _today - timedelta(days=23)
_delete_date = _today + timedelta(days=7)

BASE_RECORD = {
    "userId": "test-user-001",
    "userEmail": "john.doe@netradyne.com",
    "displayName": "John Doe",
    "managerId": "mgr-001",
    "managerEmail": TEST_RECIPIENT,
    "offboardDate": _offboard.isoformat(),
    "efRequired": True,
    "statusCode": "ACTIVE",
    "extensionCount": 0,
    "deleteDate": _delete_date.isoformat(),
    "deletedDate": "",
    "lastAlertDate": "",
    "usageLocation": "IN",
}

MANAGER_TEMPLATES = ("first", "second", "final", "confirm", "ef_removed", "deletion", "final_deletion", "it_approval")
TEMPLATES = MANAGER_TEMPLATES + ("report", "manager_all", "all")


def _record(**overrides):
    rec = dict(BASE_RECORD)
    rec.update(overrides)
    return rec


def _send_one(name: str) -> bool:
    print(f"  → {name} …", end=" ", flush=True)

    if name == "first":
        ok = send_ef_alert(_record(), days_remaining=7, is_final=False)
    elif name == "second":
        ok = send_ef_alert(
            _record(
                extensionCount=1,
                statusCode="EXTENDED",
                offboardDate=(_today - timedelta(days=53)).isoformat(),
            ),
            days_remaining=7,
            is_final=False,
        )
    elif name == "final":
        ok = send_ef_alert(
            _record(
                extensionCount=2,
                statusCode="EXTENDED_MAX",
                offboardDate=(_today - timedelta(days=83)).isoformat(),
            ),
            days_remaining=7,
            is_final=True,
        )
    elif name == "confirm":
        ok = send_extension_confirm(
            _record(
                extensionCount=1,
                statusCode="EXTENDED",
                deleteDate=(_offboard + timedelta(days=30)).isoformat(),
            )
        )
    elif name == "ef_removed":
        ok = send_ef_removed_notice(
            _record(statusCode="EF_DISABLED"),
            days_until_delete=16,
            reason="NO_EXTENSION_DAY30",
        )
    elif name == "deletion":
        ok = send_deletion_notice(_record(statusCode="DELETED"), reason="NO_EXTENSION_DAY30")
    elif name == "final_deletion":
        ok = send_final_deletion_notice(_record(extensionCount=2, statusCode="DELETED"))
    elif name == "it_approval":
        ok = send_it_approval_notification(
            _record(),
            approve_url="https://func-ef-forwarding.azurewebsites.net/api/ef_approval?token=test&action=approve",
            decline_url="https://func-ef-forwarding.azurewebsites.net/api/ef_approval?token=test&action=decline",
            ext_type="EXTEND_TO_30",
        )
    elif name == "report":
        sample_no_ef = [
            {
                "displayName": "Sample User A",
                "userEmail": "user.a@netradyne.com",
                "offboardDate": (_today - timedelta(days=10)).isoformat(),
                "usageLocation": "IN",
                "managerEmail": "mgr.a@netradyne.com",
            },
            {
                "displayName": "Sample User B",
                "userEmail": "user.b@netradyne.com",
                "offboardDate": (_today - timedelta(days=5)).isoformat(),
                "usageLocation": "IN",
                "managerEmail": "mgr.b@netradyne.com",
            },
        ]
        sample_ef = [
            {
                "displayName": "Sample EF User",
                "userEmail": "ef.user@netradyne.com",
                "offboardDate": (_today - timedelta(days=20)).isoformat(),
                "usageLocation": "IN",
                "managerEmail": TEST_RECIPIENT,
            }
        ]
        sample_overdue = [
            {
                "displayName": "Overdue NO_EF",
                "userEmail": "overdue@netradyne.com",
                "offboardDate": (_today - timedelta(days=45)).isoformat(),
                "usageLocation": "IN",
                "managerEmail": "mgr.c@netradyne.com",
                "daysElapsed": 45,
            }
        ]
        ok = send_offboard_consolidated_report(
            report_date=_today.isoformat(),
            new_no_ef=sample_no_ef,
            new_with_ef=sample_ef,
            overdue_no_ef=sample_overdue,
            summary={"checked": 191, "alerted": 1, "ef_removed": 0, "deleted": 0, "errors": 0},
        ) > 0
    else:
        print("unknown")
        return False

    print("OK" if ok else "FAIL")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Send EF templates to prem_testing (no CC).")
    parser.add_argument(
        "--template",
        choices=list(TEMPLATES),
        default="manager_all",
        help="manager_all = manager+IT templates; report = consolidated daily report; all = both",
    )
    args = parser.parse_args()

    if args.template == "manager_all":
        names = list(MANAGER_TEMPLATES)
    elif args.template == "all":
        names = list(MANAGER_TEMPLATES) + ["report"]
    else:
        names = [args.template]

    print(
        f"Sending {len(names)} email(s) via Graph Mail.Send\n"
        f"  From: {os.environ.get('SENDER_EMAIL')}\n"
        f"  To:   {TEST_RECIPIENT} only (EF_TEST_MODE, no CC)\n"
    )

    failed = [n for n in names if not _send_one(n)]
    if failed:
        print(f"\n[FAIL] {', '.join(failed)}")
        sys.exit(1)
    print(f"\n[OK] Sent {len(names)} email(s) to {TEST_RECIPIENT}.")


if __name__ == "__main__":
    main()
