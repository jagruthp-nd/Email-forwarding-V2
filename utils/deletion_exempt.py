"""
deletion_exempt.py
------------------
Skip automated account deletion (daily monitor + monthly cleanup).

Ways to exempt an account (any one is enough):
  1. Entra CSA  EFAutomation.DeleteExempt = true / EXEMPT
     (env: CSA_ATTRIBUTE_SET + CSA_DELETE_EXEMPT_NAME) — preferred for ops
  2. UserTracking field deletionExempt = true
  3. UserTracking field skipCleanupDeletion = true
  4. DELETION_EXEMPT_USER_IDS  – comma-separated Entra object IDs (app setting)
  5. DELETION_EXEMPT_EMAILS    – comma-separated primary SMTP addresses

Litigation hold still blocks deletion separately; exempt is for operational
holds without legal hold.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .app_config import get_deletion_exempt_emails, get_deletion_exempt_user_ids

logger = logging.getLogger(__name__)


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
    ad_user: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Return (exempt, reason_code).

    reason_code is empty when not exempt; otherwise a short label for logs/audit.
    Prefer passing *ad_user* (with customSecurityAttributes) to avoid an extra GET.
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

    # Entra CSA DeleteExempt — set in portal per user; no app-setting edits needed
    try:
        from .graph_api import is_csa_delete_exempt
        if is_csa_delete_exempt(user=ad_user, user_id=user_id if not ad_user else ""):
            return True, "csa_delete_exempt"
    except Exception as exc:
        logger.warning(
            "CSA DeleteExempt check failed for userId=%s: %s – continuing without CSA exempt",
            user_id, exc,
        )

    return False, ""
