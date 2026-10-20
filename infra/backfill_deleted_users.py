"""
backfill_deleted_users.py
-------------------------
One-time pass before go-live: mark tracked users that IT already deleted as DELETED.

The daily monitor only catches manual deletions still in the Entra recycle bin
(~30 days). Older ones were purged, so they would stay "active" forever. This
script checks each active tracked user with exact HTTP status codes, so a Graph
error is never mistaken for "user gone".

  200 on /users/{id}                       → still exists, left alone
  404 on /users, 200 on deletedItems/{id}  → in recycle bin, marked like the daily job
  404 on both                              → purged, marked DELETED (kept out of reports)
  anything else                            → skipped and printed

Usage (same env vars as the Function App: AZURE_*, storage connection string):
  python infra/backfill_deleted_users.py            # dry run, prints what it would do
  python infra/backfill_deleted_users.py --apply    # writes to Table Storage
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _ROOT)

# Same as tests/send_test_email.py: shell env wins, local.settings.json fills the rest.
_settings = os.path.join(_ROOT, "local.settings.json")
if os.path.isfile(_settings):
    with open(_settings, encoding="utf-8") as f:
        for _k, _v in (json.load(f).get("Values") or {}).items():
            if _v is not None:
                os.environ.setdefault(_k, str(_v))

from utils.graph_api import _GRAPH_BASE, _auth_headers  # noqa: E402
from utils.monitor_accounts import mark_deleted  # noqa: E402
from utils.table_store import TableStore  # noqa: E402


def _status(url: str) -> tuple:
    resp = requests.get(url, headers=_auth_headers(), timeout=60)
    return resp.status_code, (resp.json() if resp.status_code == 200 else {})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    apply = parser.parse_args().apply

    store = TableStore()
    counts = {"exists": 0, "recycle_bin": 0, "purged": 0, "skipped": 0}
    records = store.list_active_users()
    if not records:
        # list_active_users() logs and returns [] on storage errors – don't report "0" as success
        sys.exit("No active tracked users read. If an error is shown above, set "
                 "STORAGE_CONNECTION_STRING (the Function App's AzureWebJobsStorage value) and retry.")
    for record in records:
        uid = record["userId"]
        who = f"{record.get('displayName', '')} <{record.get('userEmail', '')}>"

        code, _ = _status(f"{_GRAPH_BASE}/users/{uid}?$select=id")
        if code == 200:
            counts["exists"] += 1
            continue
        if code != 404:
            counts["skipped"] += 1
            print(f"SKIP   {who}: /users returned {code}")
            continue

        code, deleted = _status(f"{_GRAPH_BASE}/directory/deletedItems/{uid}")
        if code == 200:
            counts["recycle_bin"] += 1
            print(f"RECYCLE_BIN {who} deleted {deleted.get('deletedDateTime', '?')}")
            if apply:
                mark_deleted(
                    record, store, "MANUAL_OR_EXTERNAL", deleted_by="manual",
                    deleted_date=(deleted.get("deletedDateTime") or "")[:10] or None,
                )
        elif code == 404:
            counts["purged"] += 1
            print(f"PURGED {who} (offboard {record.get('offboardDate', '?')})")
            if apply:
                # Real deletion date is unknown; a non-"DELETED" audit action keeps
                # these old accounts out of the "deleted this period" report.
                record.update(statusCode="DELETED", deletedBy="manual",
                              deleteReason="PURGED_BEFORE_TRACKING", deletedDate="")
                store.upsert_user(record)
                store.append_audit(uid, "DELETED_BACKFILL", "purged from Entra before deletion tracking existed")
        else:
            counts["skipped"] += 1
            print(f"SKIP   {who}: deletedItems returned {code}")

    print(("APPLIED " if apply else "DRY RUN ") + str(counts))


if __name__ == "__main__":
    main()
