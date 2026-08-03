"""
send_test_email.py
------------------
Sends a test alert email to prem_testing@netradyne.com with no CC so you
can verify the HTML template looks correct before going live.

Usage
-----
  # Set your SMTP password in the env (never hardcode it):
  export SENDER_PASSWORD="your-app-password-here"

  # Optionally set the ServiceDesk link (shows the button if set):
  export SERVICEDESK_TICKET_URL="https://itservicedesk.netradyne.com/"
  # (SDP_TICKET_URL is still supported as a legacy alias)

  # Run from the project root:
  python tests/send_test_email.py

  # To test the second-extension (Day 53) or final (Day 83) templates:
  python tests/send_test_email.py --template second
  python tests/send_test_email.py --template final

  # To also see the extension-confirmed template:
  python tests/send_test_email.py --template confirm
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

# ---------------------------------------------------------------------------
# Resolve SMTP password before importing email_sender so the module can
# locate it.  The script reads from env; it will never prompt interactively.
# ---------------------------------------------------------------------------
if not os.environ.get("SENDER_PASSWORD"):
    print(
        "[ERROR] SENDER_PASSWORD env var is not set.\n"
        "  export SENDER_PASSWORD='your-app-password'\n"
        "  then re-run this script."
    )
    sys.exit(1)

# Force no-CC by setting IT_EMAIL to empty string
os.environ.setdefault("IT_EMAIL", "")
os.environ.setdefault("SMTP_SERVER", "smtp.office365.com")
os.environ.setdefault("SMTP_PORT",   "587")
os.environ.setdefault("SENDER_EMAIL", "it-automation-service@netradyne.com")

from utils.email_sender import (   # noqa: E402  (env must be set before import)
    send_ef_alert,
    send_extension_confirm,
)

# ---------------------------------------------------------------------------
# Mock record – realistic data for template preview
# ---------------------------------------------------------------------------

TEST_RECIPIENT = "prem_testing@netradyne.com"

_today        = date.today()
_offboard     = _today - timedelta(days=23)
_delete_date  = _today + timedelta(days=7)

MOCK_RECORD_FIRST = {
    "userId":         "test-user-001",
    "userEmail":      "john.doe@netradyne.com",
    "displayName":    "John Doe",
    "managerId":      "mgr-001",
    "managerEmail":   TEST_RECIPIENT,
    "offboardDate":   _offboard.isoformat(),
    "efRequired":     True,
    "statusCode":     "ACTIVE",
    "extensionCount": 0,
    "deleteDate":     _delete_date.isoformat(),
    "deletedDate":    "",
    "lastAlertDate":  "",
}

MOCK_RECORD_SECOND = {
    **MOCK_RECORD_FIRST,
    "extensionCount": 1,
    "statusCode":     "EXTENDED",
    "offboardDate":   (_today - timedelta(days=53)).isoformat(),
    "deleteDate":     (_today + timedelta(days=7)).isoformat(),
}

MOCK_RECORD_FINAL = {
    **MOCK_RECORD_FIRST,
    "extensionCount": 2,
    "statusCode":     "EXTENDED_MAX",
    "offboardDate":   (_today - timedelta(days=83)).isoformat(),
    "deleteDate":     (_today + timedelta(days=7)).isoformat(),
}

MOCK_RECORD_CONFIRM = {
    **MOCK_RECORD_FIRST,
    "extensionCount": 1,
    "statusCode":     "EXTENDED",
    "deleteDate":     (_offboard + timedelta(days=30)).isoformat(),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a test EF alert email.")
    parser.add_argument(
        "--template",
        choices=["first", "second", "final", "confirm"],
        default="first",
        help=(
            "first   = Day 23 first alert  (default)\n"
            "second  = Day 53 second alert\n"
            "final   = Day 83 final alert\n"
            "confirm = Extension confirmed"
        ),
    )
    args = parser.parse_args()

    print(f"Sending '{args.template}' template to {TEST_RECIPIENT} (no CC)...")

    if args.template == "first":
        ok = send_ef_alert(MOCK_RECORD_FIRST, days_remaining=7, is_final=False)
    elif args.template == "second":
        ok = send_ef_alert(MOCK_RECORD_SECOND, days_remaining=7, is_final=False)
    elif args.template == "final":
        ok = send_ef_alert(MOCK_RECORD_FINAL, days_remaining=7, is_final=True)
    else:  # confirm
        ok = send_extension_confirm(MOCK_RECORD_CONFIRM)

    if ok:
        print(f"[OK]  Email sent successfully to {TEST_RECIPIENT}")
    else:
        print("[FAIL] Email failed to send – check SMTP settings and logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
