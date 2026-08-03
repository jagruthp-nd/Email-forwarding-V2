"""
deletion_exempt.py
------------------
Skip automated account deletion (daily monitor + monthly cleanup) for named users.

Ways to exempt an account (any one is enough):
  1. UserTracking field deletionExempt = true  (set row in Table Storage / tooling)
  2. DELETION_EXEMPT_USER_IDS  – comma-separated Entra object IDs (app setting)
  3. DELETION_EXEMPT_EMAILS    – comma-separated primary SMTP addresses

Litigation hold still blocks deletion separately; exempt is for operational
holds without legal hold.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .app_config import get_deletion_exempt_emails, get_deletion_exempt_user_ids


def _record_flag(record: Optional[Dict[str, Any]], key: str) -> bool:
    if not record:
        return False
    val = record.get(key)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


def is_automated_deletion_exempt(
    user_id: str,
    record: Optional[Dict[str, Any]] = None,
    user_email: str = "",
) -> Tuple[bool, str]:
    """
    Return (exempt, reason_code).

    reason_code is empty when not exempt; otherwise a short label for logs/audit.
    """
    uid = (user_id or "").strip().lower()
    if uid and uid in get_deletion_exempt_user_ids():
        return True, "env_user_id"

    email = (user_email or (record or {}).get("userEmail", "")).strip().lower()
    if email and email in get_deletion_exempt_emails():
        return True, "env_email"

    if _record_flag(record, "deletionExempt"):
        return True, "tracking_deletion_exempt"
    if _record_flag(record, "skipCleanupDeletion"):
        return True, "tracking_skip_cleanup"

    return False, ""
