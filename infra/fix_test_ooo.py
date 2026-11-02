"""
fix_test_ooo.py
---------------
One-time repair: auto-replies set while EF_TEST_MODE=true named the test mailbox
(EF_TEST_RECIPIENT) instead of the real manager. Real colleagues saw that text.

Rewrites ONLY auto-replies that still contain the test mailbox address, using the
real manager / forwarding recipient from UserTracking. A message the user wrote
themselves never contains it, so it is never touched.

Usage (same env as backfill_deleted_users.py: local.settings.json + STORAGE_CONNECTION_STRING):
  python infra/fix_test_ooo.py            # dry run – lists affected mailboxes and the new text
  python infra/fix_test_ooo.py --apply    # rewrites them
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _ROOT)

_settings = os.path.join(_ROOT, "local.settings.json")
if os.path.isfile(_settings):
    with open(_settings, encoding="utf-8") as f:
        for _k, _v in (json.load(f).get("Values") or {}).items():
            if _v is not None:
                os.environ.setdefault(_k, str(_v))

from unittest.mock import patch  # noqa: E402

from utils.automation_flags import get_test_recipient, is_graph_write_disabled  # noqa: E402
from utils.graph_api import _GRAPH_BASE, _auth_headers, get_forwarding_address, set_auto_reply  # noqa: E402
from utils.table_store import TableStore  # noqa: E402


def _args(record: dict) -> dict:
    return dict(
        manager_name=record.get("managerName", ""),
        manager_email=record.get("managerEmail", ""),
        forwarding_active=str(record.get("efRequired", "")).lower() in ("true", "1"),
        forwarding_address=record.get("forwardingAddress", ""),
        employee_name=record.get("displayName", ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="rewrite (default: dry run)")
    apply = parser.parse_args().apply
    if apply and is_graph_write_disabled():
        sys.exit("EF_DRY_RUN / DISABLE_GRAPH_WRITES is on in this shell – nothing would be written.")

    test_addr = get_test_recipient().lower()
    headers = _auth_headers()
    records = TableStore().list_active_users()
    if not records:
        sys.exit("No active tracked users read – check STORAGE_CONNECTION_STRING.")

    fixed = failed = 0
    for record in records:
        uid = record["userId"]
        resp = requests.get(f"{_GRAPH_BASE}/users/{uid}/mailboxSettings/automaticRepliesSetting",
                            headers=headers, timeout=60)
        if not resp.ok or test_addr not in (resp.json().get("internalReplyMessage") or "").lower():
            continue
        who = f"{record.get('displayName', '')} <{record.get('userEmail', '')}>"
        # Stored target may be an Entra object ID (older bug) – use the live email address
        record["forwardingAddress"] = get_forwarding_address(uid) or ""
        if not apply:
            # Capture the payload set_auto_reply would send, without sending it
            with patch("utils.graph_api._patch", return_value=True) as fake:
                set_auto_reply(uid, **_args(record))
            msg = fake.call_args[0][1]["automaticRepliesSetting"]["internalReplyMessage"]
            print(f"WOULD FIX {who}\n    → {msg}")
            fixed += 1
        elif set_auto_reply(uid, **_args(record)):
            print(f"FIXED  {who}")
            fixed += 1
        else:
            print(f"FAILED {who}")
            failed += 1

    print(("APPLIED " if apply else "DRY RUN ") + str({"fixed": fixed, "failed": failed}))


if __name__ == "__main__":
    main()
