"""
test_local.py
-------------
Local integration test harness for the EF automation project (Workflow B).

Run these tests BEFORE deploying to production:

  cd Email-forwarding
  python -m pytest tests/test_local.py -v

These tests do NOT call Azure AD or send real emails.
They verify the business logic in isolation using mocked dependencies.

Workflow B changes covered here:
  - Monitor still alerts on Day 25/55/85 and deletes on Day 30/60/90.
  - Extensions are now applied when IT sets extensionAttribute11 in Azure AD
    (after HR + Infosec approval), NOT via manager email reply.
  - The reply_webhook module has been removed; those tests are replaced by
    TestWorkflowBAttributeExtension below.
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_legal_hold():
    """Legal-hold lookups hit Exchange; default to 'not held' unless a test overrides it."""
    with patch("utils.graph_api.is_litigation_hold_active", return_value=False):
        yield


def make_record(
    user_id: str = "user-001",
    offboard_days_ago: int = 0,
    ef_required: bool = True,
    extension_count: int = 0,
    status: str = "ACTIVE",
    last_alert: str = "",
    pre_deletion_notice_days_ago: Optional[int] = None,
) -> Dict[str, Any]:
    """Return a synthetic UserTracking dict."""
    today = date.today()
    offboard = today - timedelta(days=offboard_days_ago)
    delete_date = offboard + timedelta(days=(extension_count + 1) * 30)
    notice_date = (
        (today - timedelta(days=pre_deletion_notice_days_ago)).isoformat()
        if pre_deletion_notice_days_ago is not None else ""
    )
    return {
        "userId":              user_id,
        "userEmail":           f"{user_id}@netradyne.com",
        "displayName":         "Test User",
        "managerId":           "mgr-001",
        "managerEmail":        "manager@netradyne.com",
        "country":             "India",
        "usageLocation":       "IN",          # region gate – India accounts are deletable
        "offboardDate":        offboard.isoformat(),
        "efRequired":          ef_required,
        "forwardingAddress":   "manager@netradyne.com",
        "statusCode":          status,
        "extensionCount":      extension_count,
        "deleteDate":          delete_date.isoformat(),
        "deletedDate":         "",
        "efDisabledDate":      "",
        "lastAlertDate":       last_alert,
        "oooSetDate":          "",
        "oooDisabledDate":     "",
        "ticketRef":           "",
        "legalHoldChecked":    "",
        "legalHoldActive":     False,
        "preDeletionNoticeDate": notice_date,
        "sessionsRevokedDate": "",
    }


def make_ad_user(offboard_days_ago: int = 0, extension_attr: str = "") -> Dict[str, Any]:
    """
    Return a minimal Azure AD user dict.

    Pass extension_attr="EXTEND_TO_30" (or "EXTEND_TO_60" / "EXTENDED_MAX")
    to simulate IT setting the CSA (EFAutomation.ExtStatus) after
    HR + Infosec approval (Workflow B).
    """
    today = date.today()
    offboard = today - timedelta(days=offboard_days_ago)
    csa: Dict[str, Any] = {}
    if extension_attr:
        csa = {
            "EFAutomation": {
                "@odata.type": "#microsoft.graph.customSecurityAttributeValue",
                "ExtStatus": extension_attr,
            }
        }
    return {
        "id":          "user-001",
        "mail":        "exstaff@netradyne.com",
        "displayName": "Ex Staff",
        "usageLocation": "IN",            # region gate – India accounts are processable
        "onPremisesExtensionAttributes": {
            "extensionAttribute10": offboard.isoformat() + "T00:00:00Z",
        },
        "customSecurityAttributes": csa,
        "manager": {
            "id":    "mgr-001",
            "mail":  "manager@netradyne.com",
            "displayName": "Some Manager",
        },
    }


# ---------------------------------------------------------------------------
# Tests: monitor_accounts decision logic
# ---------------------------------------------------------------------------

class TestMonitorDecisionLogic:
    """
    Verify the _process_user routing without touching Azure or email.

    These tests use make_ad_user() without extensionAttribute11, so the
    Workflow B attribute check returns False and falls through to the
    standard alert/delete logic unchanged.
    """

    def _run(self, ad_user, today, store, existing_record=None):
        from utils.monitor_accounts import _process_user
        return _process_user(ad_user, today, store, existing_record)

    # ── No EF (policy 4.6.2 – 15-day retention, notice before deletion) ──

    def test_no_ef_before_notice_day_no_action(self):
        """Before NO_EF_NOTICE_DAY nothing happens."""
        store = MagicMock()
        record = make_record(offboard_days_ago=5, ef_required=False)
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del, \
             patch("utils.monitor_accounts.send_no_ef_pre_deletion_notice") as mock_notice:
            ad = make_ad_user(5)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()
        mock_notice.assert_not_called()

    def test_no_ef_notice_day_sends_pre_deletion_notice(self):
        """Day 10: manager and business owner are notified before deletion."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=10, ef_required=False)
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del, \
             patch("utils.monitor_accounts.send_no_ef_pre_deletion_notice", return_value=True) as mock_notice:
            ad = make_ad_user(10)
            result = self._run(ad, today, store, record)
        assert result == "notified"
        mock_notice.assert_called_once()
        assert mock_notice.call_args.kwargs["retention_days"] == 15
        assert mock_notice.call_args.kwargs["days_until_delete"] == 5
        # Stamped in UTC, like every other date the automation writes.
        assert record["preDeletionNoticeDate"] == datetime.now(timezone.utc).date().isoformat()
        mock_del.assert_not_called()

    def test_no_ef_notice_sent_only_once(self):
        """A record that already carries a notice date is not notified again."""
        store = MagicMock()
        record = make_record(
            offboard_days_ago=12, ef_required=False, pre_deletion_notice_days_ago=2,
        )
        today = date.today()
        with patch("utils.monitor_accounts.send_no_ef_pre_deletion_notice") as mock_notice:
            ad = make_ad_user(12)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_notice.assert_not_called()

    def test_no_ef_day15_default_no_action(self):
        """Default (AUTO_DELETE_NO_EF=false): the daily monitor never deletes.
        Monthly cleanup scan is the safety net."""
        store = MagicMock()
        record = make_record(
            offboard_days_ago=15, ef_required=False, pre_deletion_notice_days_ago=5,
        )
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(15)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()

    def test_no_ef_day15_auto_delete_deletes_after_notice(self):
        """AUTO_DELETE_NO_EF=true and notice lead time met → delete on Day 15."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(
            offboard_days_ago=15, ef_required=False, pre_deletion_notice_days_ago=5,
        )
        today = date.today()
        import utils.monitor_accounts as ma
        original = ma._AUTO_DELETE_NO_EF
        try:
            ma._AUTO_DELETE_NO_EF = True
            with patch("utils.monitor_accounts.delete_user", return_value=True) as mock_del, \
                 patch("utils.monitor_accounts.send_deletion_notice"):
                ad = make_ad_user(15)
                result = self._run(ad, today, store, record)
                assert result == "deleted"
            mock_del.assert_called_once_with(record["userId"])
        finally:
            ma._AUTO_DELETE_NO_EF = original

    def test_no_ef_day15_deletion_deferred_until_notice_sent(self):
        """Deletion is held on Day 15 when no notice has gone out yet (policy 4.6.2)."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=15, ef_required=False)
        today = date.today()
        import utils.monitor_accounts as ma
        original = ma._AUTO_DELETE_NO_EF
        try:
            ma._AUTO_DELETE_NO_EF = True
            with patch("utils.monitor_accounts.delete_user") as mock_del, \
                 patch("utils.monitor_accounts.send_no_ef_pre_deletion_notice", return_value=True) as mock_notice:
                ad = make_ad_user(15)
                result = self._run(ad, today, store, record)
            assert result == "notified"
            mock_notice.assert_called_once()
            mock_del.assert_not_called()
            audit_actions = [c[0][1] for c in store.append_audit.call_args_list]
            assert "DELETE_DEFERRED_PENDING_NOTICE" in audit_actions
        finally:
            ma._AUTO_DELETE_NO_EF = original

    def test_no_ef_deletion_deferred_when_notice_too_recent(self):
        """Notice sent yesterday → lead time not met → still no deletion."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(
            offboard_days_ago=16, ef_required=False, pre_deletion_notice_days_ago=1,
        )
        today = date.today()
        import utils.monitor_accounts as ma
        original = ma._AUTO_DELETE_NO_EF
        try:
            ma._AUTO_DELETE_NO_EF = True
            with patch("utils.monitor_accounts.delete_user") as mock_del:
                ad = make_ad_user(16)
                result = self._run(ad, today, store, record)
            assert result == "no_action"
            mock_del.assert_not_called()
        finally:
            ma._AUTO_DELETE_NO_EF = original

    # ── Has EF – alert path ───────────────────────────────────────────────

    def test_ef_day25_sends_alert(self):
        store = MagicMock()
        record = make_record(offboard_days_ago=25, ef_required=True, extension_count=0)
        today = date.today()
        with patch("utils.monitor_accounts.send_ef_alert", return_value=True) as mock_alert, \
             patch("utils.monitor_accounts.set_extension_attribute"):
            ad = make_ad_user(25)
            result = self._run(ad, today, store, record)
        assert result == "alerted"
        mock_alert.assert_called_once()

    def test_ef_day25_alert_not_sent_twice(self):
        """If alert already sent today (within the window), do not re-send."""
        today_str = date.today().isoformat()
        store = MagicMock()
        record = make_record(
            offboard_days_ago=25, ef_required=True, extension_count=0,
            last_alert=today_str,
        )
        with patch("utils.monitor_accounts.send_ef_alert") as mock_alert, \
             patch("utils.monitor_accounts.set_extension_attribute"):
            ad = make_ad_user(25)
            result = self._run(ad, date.today(), store, record)
        assert result == "no_action"
        mock_alert.assert_not_called()

    def test_ef_day30_no_extension_removes_ef_not_deletes(self):
        """Day 30, no extension → EF disabled (grace period). Account NOT yet deleted."""
        store = MagicMock()
        record = make_record(offboard_days_ago=30, ef_required=True, extension_count=0)
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del, \
             patch("utils.monitor_accounts.disable_email_forwarding", return_value=True), \
             patch("utils.monitor_accounts.send_ef_removed_notice"):
            ad = make_ad_user(30)
            result = self._run(ad, today, store, record)
        assert result == "ef_removed"
        mock_del.assert_not_called()

    def test_ef_day46_no_extension_deletes(self):
        """Day 46 (DELETE_DAY_1 default), EF already disabled, no extension → DELETE."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=46, ef_required=True, extension_count=0, status="EF_DISABLED")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user", return_value=True) as mock_del, \
             patch("utils.monitor_accounts.send_deletion_notice"):
            ad = make_ad_user(46)
            result = self._run(ad, today, store, record)
        assert result == "deleted"
        mock_del.assert_called_once()

    def test_ef_day30_with_extension1_no_delete(self):
        """Day 30 but extension already granted: do NOT delete."""
        store = MagicMock()
        record = make_record(offboard_days_ago=30, ef_required=True, extension_count=1, status="EXTENDED")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(30)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()

    def test_ef_day55_with_ext1_sends_alert(self):
        store = MagicMock()
        record = make_record(offboard_days_ago=55, ef_required=True, extension_count=1, status="EXTENDED")
        today = date.today()
        with patch("utils.monitor_accounts.send_ef_alert", return_value=True), \
             patch("utils.monitor_accounts.set_extension_attribute"):
            ad = make_ad_user(55)
            result = self._run(ad, today, store, record)
        assert result == "alerted"

    def test_ef_day60_with_ext1_no_second_extension_removes_ef(self):
        """Day 60, ext_count=1, no second extension → EF disabled (grace). NOT deleted yet."""
        store = MagicMock()
        record = make_record(offboard_days_ago=60, ef_required=True, extension_count=1, status="EXTENDED")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del, \
             patch("utils.monitor_accounts.disable_email_forwarding", return_value=True), \
             patch("utils.monitor_accounts.send_ef_removed_notice"):
            ad = make_ad_user(60)
            result = self._run(ad, today, store, record)
        assert result == "ef_removed"
        mock_del.assert_not_called()

    def test_ef_day76_with_ext1_no_second_extension_deletes(self):
        """Day 76 (DELETE_DAY_2 default), EF already disabled, no second ext → DELETE."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=76, ef_required=True, extension_count=1, status="EF_DISABLED")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user", return_value=True) as mock_del, \
             patch("utils.monitor_accounts.send_deletion_notice"):
            ad = make_ad_user(76)
            result = self._run(ad, today, store, record)
        assert result == "deleted"
        mock_del.assert_called_once()

    def test_ef_day60_with_ext2_no_delete(self):
        """Both extensions used – should not delete until Day 90."""
        store = MagicMock()
        record = make_record(offboard_days_ago=60, ef_required=True, extension_count=2, status="EXTENDED_MAX")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(60)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()

    def test_ef_day90_final_delete(self):
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=90, ef_required=True, extension_count=2, status="EXTENDED_MAX")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user", return_value=True) as mock_del, \
             patch("utils.monitor_accounts.send_final_deletion_notice"):
            ad = make_ad_user(90)
            result = self._run(ad, today, store, record)
        assert result == "deleted"
        mock_del.assert_called_once()

    def test_already_deleted_is_skipped(self):
        store = MagicMock()
        record = make_record(offboard_days_ago=90, ef_required=True, status="DELETED")
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(90)
            result = self._run(ad, today, store, record)
        assert result == "skipped"
        mock_del.assert_not_called()

    def test_missing_extensionattribute10_skips(self):
        store = MagicMock()
        ad_user_no_date = {
            "id": "user-x",
            "mail": "x@netradyne.com",
            "displayName": "No Date",
            "usageLocation": "IN",
            "onPremisesExtensionAttributes": {},
            "customSecurityAttributes": {},
        }
        result = self._run(ad_user_no_date, date.today(), store, None)
        assert result == "skipped"

    def test_non_india_user_skipped(self):
        """Users outside DELETE_REGION (default IN) are skipped entirely."""
        store = MagicMock()
        record = make_record(offboard_days_ago=90, ef_required=True, extension_count=2, status="EXTENDED_MAX")
        record["usageLocation"] = "US"
        ad = make_ad_user(90)
        ad["usageLocation"] = "US"  # non-India
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            result = self._run(ad, today, store, record)
        assert result == "skipped"
        mock_del.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Workflow B – extensionAttribute11 based extension
# ---------------------------------------------------------------------------

class TestWorkflowBAttributeExtension:
    """
    Verify the Workflow B attribute check logic.

    IT sets the Custom Security Attribute EFAutomation.ExtensionStatus to one
    of the predefined values (EXTEND_TO_30 / EXTEND_TO_60 / EXTENDED_MAX) in
    Azure AD (Entra portal) after HR and Infosec approve the manager's
    ServiceDesk ticket.  The daily monitor should detect this, apply the
    extension, clear the attribute, and send a confirmation email — no email
    reply or on-prem AD touch required.
    """

    def _run_check(self, ad_user, record, store):
        from utils.monitor_accounts import _check_and_apply_extension_attribute
        return _check_and_apply_extension_attribute(ad_user, record, store)

    def _run_process(self, ad_user, today, store, existing_record=None):
        from utils.monitor_accounts import _process_user
        return _process_user(ad_user, today, store, existing_record)

    # ── _check_and_apply_extension_attribute unit tests ───────────────────

    def test_attribute_absent_returns_record_unchanged(self):
        """No extensionAttribute11 → no change."""
        store = MagicMock()
        record = make_record(offboard_days_ago=27, extension_count=0)
        ad = make_ad_user(27)  # no extension_attr
        result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 0
        store.upsert_user.assert_not_called()

    def test_extend30_value_applies_first_extension(self):
        """EXTEND_TO_30 with ext_count=0 increments to 1 and recalculates delete date."""
        store = MagicMock()
        record = make_record(offboard_days_ago=27, extension_count=0)
        ad = make_ad_user(27, extension_attr="EXTEND_TO_30")
        with patch("utils.monitor_accounts.clear_extension_attribute") as mock_clear, \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=True):
            result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 1
        assert result["statusCode"] == "EXTENDED"
        expected_delete = (date.today() - timedelta(days=27) + timedelta(days=30)).isoformat()
        assert result["deleteDate"] == expected_delete
        assert result["lastAlertDate"] == ""  # reset for next alert window
        mock_clear.assert_called_once_with("user-001")
        store.upsert_user.assert_called_once()

    def test_extend_to_60_triggers_second_extension(self):
        """'EXTEND_TO_60' is the predefined value for the second extension."""
        store = MagicMock()
        record = make_record(offboard_days_ago=57, extension_count=1, status="EXTENDED")
        ad = make_ad_user(57, extension_attr="EXTEND_TO_60")
        with patch("utils.monitor_accounts.clear_extension_attribute"), \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=True):
            result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 2
        assert result["statusCode"] == "EXTENDED_MAX"

    def test_extended_max_triggers_extension(self):
        """'EXTENDED_MAX' is the predefined value for the final extension."""
        store = MagicMock()
        record = make_record(offboard_days_ago=27, extension_count=0)
        ad = make_ad_user(27, extension_attr="EXTENDED_MAX")
        with patch("utils.monitor_accounts.clear_extension_attribute"), \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=True):
            result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 1

    def test_case_insensitive_extend_value(self):
        """Attribute value matching is case-insensitive (extend_to_30 = EXTEND_TO_30)."""
        store = MagicMock()
        record = make_record(offboard_days_ago=27, extension_count=0)
        ad = make_ad_user(27, extension_attr="extend_to_30")
        with patch("utils.monitor_accounts.clear_extension_attribute"), \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=True):
            result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 1

    def test_attribute_set_at_max_extensions_is_cleared_without_extending(self):
        """IT accidentally sets attribute after max extensions – clears it, no extension."""
        store = MagicMock()
        record = make_record(offboard_days_ago=70, extension_count=2, status="EXTENDED_MAX")
        ad = make_ad_user(70, extension_attr="EXTEND_TO_30")
        with patch("utils.monitor_accounts.clear_extension_attribute") as mock_clear, \
             patch("utils.monitor_accounts.send_extension_confirm") as mock_confirm:
            result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 2  # unchanged
        mock_clear.assert_called_once_with("user-001")
        mock_confirm.assert_not_called()

    def test_confirmation_email_failure_does_not_raise(self):
        """Even if SMTP fails, the extension should still be persisted."""
        store = MagicMock()
        record = make_record(offboard_days_ago=27, extension_count=0)
        ad = make_ad_user(27, extension_attr="EXTEND_TO_30")
        with patch("utils.monitor_accounts.clear_extension_attribute"), \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=False):
            result = self._run_check(ad, record, store)
        assert result["extensionCount"] == 1  # extension still applied
        store.upsert_user.assert_called_once()

    # ── Integration: attribute check wired into _process_user ────────────

    def test_attribute_set_on_day28_prevents_day30_deletion(self):
        """
        IT sets CSA ExtStatus = EXTEND_TO_30 on Day 28.
        Monitor should apply extension; Day 30 deletion check then sees ext_count=1
        and does NOT delete.
        """
        store = MagicMock()
        record = make_record(offboard_days_ago=28, ef_required=True, extension_count=0)
        ad = make_ad_user(28, extension_attr="EXTEND_TO_30")
        today = date.today()
        with patch("utils.monitor_accounts.clear_extension_attribute"), \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=True), \
             patch("utils.monitor_accounts.delete_user") as mock_del:
            result = self._run_process(ad, today, store, record)
        # After the attribute is applied ext_count becomes 1; Day 28 is in the
        # alert window but alert was already "sent" (lastAlertDate reset triggers
        # no_action since we are still in window 1 range with ext_count now 1).
        assert result != "deleted"
        mock_del.assert_not_called()

    def test_attribute_set_on_day30_still_extends_not_deletes(self):
        """
        IT sets attribute on Day 30 itself (same day as deadline).
        Extension should be applied before the deletion check runs.
        """
        store = MagicMock()
        record = make_record(offboard_days_ago=30, ef_required=True, extension_count=0)
        ad = make_ad_user(30, extension_attr="EXTEND_TO_30")
        today = date.today()
        with patch("utils.monitor_accounts.clear_extension_attribute"), \
             patch("utils.monitor_accounts.send_extension_confirm", return_value=True), \
             patch("utils.monitor_accounts.delete_user") as mock_del:
            result = self._run_process(ad, today, store, record)
        assert result != "deleted"
        mock_del.assert_not_called()

    def test_no_attribute_on_day30_removes_ef_not_deletes(self):
        """Control: no attribute set on Day 30 → EF removed (grace period), account NOT deleted."""
        store = MagicMock()
        record = make_record(offboard_days_ago=30, ef_required=True, extension_count=0)
        ad = make_ad_user(30)  # no extension_attr
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del, \
             patch("utils.monitor_accounts.disable_email_forwarding", return_value=True), \
             patch("utils.monitor_accounts.send_ef_removed_notice"):
            result = self._run_process(ad, today, store, record)
        assert result == "ef_removed"
        mock_del.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: deletion exempt + dry-run gates
# ---------------------------------------------------------------------------

class TestDeletionExemptAndDryRun:
    def test_delete_skipped_when_deletion_exempt_on_record(self):
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=46, ef_required=True, extension_count=0, status="EF_DISABLED")
        record["deletionExempt"] = True
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            from utils.monitor_accounts import _process_user
            ad = make_ad_user(46)
            result = _process_user(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()
        assert store.append_audit.call_args[0][1] == "DELETE_SKIPPED_RETAINED"
        assert record["retainedReason"] == "tracking_deletion_exempt"

    def test_cleanup_skips_exempt_user_via_env(self, monkeypatch):
        monkeypatch.setenv("DELETION_EXEMPT_USER_IDS", "user-001")
        store = MagicMock()
        record = make_record(offboard_days_ago=35, ef_required=False)
        from utils.cleanup_scan import _delete_stale
        with patch("utils.cleanup_scan.delete_user") as mock_del:
            result = _delete_stale("user-001", record, store, source="test")
        assert result == "skipped_exempt"
        mock_del.assert_not_called()

    def test_delete_skipped_when_csa_delete_exempt(self):
        """CSA DeleteExempt=true blocks soft-delete without env/config edits."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=46, ef_required=True, extension_count=0, status="EF_DISABLED")
        ad = make_ad_user(46)
        ad["customSecurityAttributes"] = {
            "EFAutomation": {
                "@odata.type": "#microsoft.graph.customSecurityAttributeValue",
                "DeleteExempt": True,
            }
        }
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            from utils.monitor_accounts import _process_user
            result = _process_user(ad, date.today(), store, record)
        assert result == "no_action"
        mock_del.assert_not_called()
        assert store.append_audit.call_args[0][1] == "DELETE_SKIPPED_RETAINED"
        assert record["retainedReason"] == "csa_delete_exempt"

    def test_cleanup_skips_csa_delete_exempt_string(self):
        store = MagicMock()
        record = make_record(offboard_days_ago=35, ef_required=False)
        ad = {
            "id": "user-csa-exempt",
            "customSecurityAttributes": {
                "EFAutomation": {"DeleteExempt": "EXEMPT"},
            },
        }
        from utils.cleanup_scan import _delete_stale
        with patch("utils.cleanup_scan.delete_user") as mock_del:
            result = _delete_stale(
                "user-csa-exempt", record, store, source="test", ad_user=ad,
            )
        assert result == "skipped_exempt"
        mock_del.assert_not_called()

    def test_is_csa_delete_exempt_helpers(self):
        from utils.graph_api import is_csa_delete_exempt
        assert is_csa_delete_exempt(user={"customSecurityAttributes": {}}) is False
        assert is_csa_delete_exempt(user={
            "customSecurityAttributes": {"EFAutomation": {"DeleteExempt": True}},
        }) is True
        assert is_csa_delete_exempt(user={
            "customSecurityAttributes": {"EFAutomation": {"DeleteExempt": "yes"}},
        }) is True
        assert is_csa_delete_exempt(user={
            "customSecurityAttributes": {"EFAutomation": {"DeleteExempt": False}},
        }) is False

    def test_dry_run_blocks_cleanup_delete(self, monkeypatch):
        monkeypatch.setenv("EF_DRY_RUN", "true")
        store = MagicMock()
        record = make_record(offboard_days_ago=35, ef_required=False)
        from utils.cleanup_scan import _delete_stale
        with patch("utils.cleanup_scan.delete_user") as mock_del:
            result = _delete_stale("user-999", record, store, source="test")
        assert result == "dry_run"
        mock_del.assert_not_called()

    def test_outbound_email_suppressed_without_graph(self, monkeypatch):
        monkeypatch.setenv("DISABLE_OUTBOUND_EMAIL", "true")
        monkeypatch.delenv("EF_DRY_RUN", raising=False)
        with patch("utils.email_sender.send_mail") as mock_send:
            from utils.email_sender import _send
            ok = _send("test@example.com", "subject", "<p>hi</p>")
        assert ok is True
        mock_send.assert_not_called()

    def test_servicedesk_url_prefers_servicedesk_env(self, monkeypatch):
        monkeypatch.setenv("SERVICEDESK_TICKET_URL", "https://sd.example/primary")
        monkeypatch.setenv("SDP_TICKET_URL", "https://sd.example/legacy")
        from utils.app_config import get_servicedesk_ticket_url
        assert get_servicedesk_ticket_url() == "https://sd.example/primary"

    def test_report_export_csv_and_xlsx(self):
        from utils.report_export import build_report_archives
        files = build_report_archives(
            report_date="2026-08-07",
            period_label="Weekly",
            period_start="2026-07-31",
            new_no_ef=[{"displayName": "A", "userEmail": "a@x.com",
                        "offboardDate": "2026-07-01", "usageLocation": "IN",
                        "managerEmail": "m@x.com"}],
            new_with_ef=[{"displayName": "B", "userEmail": "b@x.com",
                          "offboardDate": "2026-07-02", "usageLocation": "IN",
                          "managerEmail": "m2@x.com",
                          "forwardingAddress": "fwd@x.com"}],
            overdue_no_ef=[],
            summary={"alerts": 1, "extensions": 0, "deletions": 0, "total_active": 5},
        )
        names = {f[0] for f in files}
        assert any(n.endswith(".csv") for n in names)
        assert any(n.endswith(".xlsx") for n in names)
        csv_bytes = next(c for n, c, _ in files if n.endswith(".csv"))
        assert b"New_EF" in csv_bytes or b"fwd@x.com" in csv_bytes
        assert len(next(c for n, c, _ in files if n.endswith(".xlsx"))) > 100

    def test_email_config_summary_lists_env_mailboxes(self, monkeypatch):
        monkeypatch.setenv("SENDER_EMAIL", "sender@x.com")
        monkeypatch.setenv("IT_EMAIL", "it@x.com")
        monkeypatch.setenv("IT_APPROVAL_EMAIL", "approve@x.com")
        monkeypatch.setenv("ADMIN_EMAILS", "a@x.com,b@x.com")
        monkeypatch.setenv("REPORT_EMAILS", "report@x.com")
        from utils.app_config import get_email_config_summary
        s = get_email_config_summary()
        assert s["SENDER_EMAIL"] == "sender@x.com"
        assert s["IT_APPROVAL_EMAIL"] == "approve@x.com"
        assert "a@x.com" in s["ADMIN_EMAILS"]
        assert "Graph" in s["managers"]

    def test_demo_landing_page(self):
        from utils.demo_flow import handle_demo
        html, status = handle_demo("home", function_code="abc")
        assert status == 200
        assert "Team Demo" in html
        assert "action=emails" in html

    def test_uat_console_landing(self, monkeypatch):
        monkeypatch.setenv("EF_UAT_MODE", "true")
        monkeypatch.setenv("DISABLE_ACCOUNT_DELETION", "true")
        from utils.uat_console import handle_uat
        html, status = handle_uat("home", function_code="abc")
        assert status == 200
        assert "UAT Console" in html
        assert "action=archive" in html
        assert "litigation" in html.lower() or "Litigation" in html

    def test_sharepoint_url_built_from_site_library_folder(self, monkeypatch):
        monkeypatch.delenv("SHAREPOINT_REPORT_URL", raising=False)
        monkeypatch.setenv("SHAREPOINT_SITE_URL", "https://netorg726775.sharepoint.com/sites/ITTEAM259")
        monkeypatch.setenv("SHAREPOINT_LIBRARY", "Shared Documents")
        monkeypatch.setenv("SHAREPOINT_FOLDER", "General/Jagruth/Automation_Reports/EF")
        from utils.app_config import get_sharepoint_report_url
        url = get_sharepoint_report_url()
        assert url.startswith("https://netorg726775.sharepoint.com/sites/ITTEAM259/")
        assert "Shared%20Documents" in url
        assert "Automation_Reports" in url

    def test_consolidated_report_uses_report_emails_and_sharepoint(self, monkeypatch):
        monkeypatch.setenv("REPORT_EMAILS", "prem_testing@netradyne.com")
        monkeypatch.setenv("SHAREPOINT_REPORT_URL", "https://contoso.sharepoint.com/sites/IT/Reports")
        monkeypatch.setenv("EF_TEST_MODE", "false")
        monkeypatch.setenv("DISABLE_OUTBOUND_EMAIL", "false")
        monkeypatch.setenv("EF_DRY_RUN", "false")
        monkeypatch.setenv("SENDER_EMAIL", "it-automation-service@netradyne.com")

        with patch("utils.email_sender.send_mail", return_value=True) as mock_send:
            from utils.email_sender import send_offboard_consolidated_report
            n = send_offboard_consolidated_report(
                report_date="2026-08-05",
                new_no_ef=[{
                    "displayName": "A", "userEmail": "a@x.com",
                    "offboardDate": "2026-07-01", "usageLocation": "IN",
                    "managerEmail": "m@x.com",
                }],
                new_with_ef=[{
                    "displayName": "B", "userEmail": "b@x.com",
                    "offboardDate": "2026-07-02", "usageLocation": "IN",
                    "managerEmail": "mgr@x.com",
                    "forwardingAddress": "fwd@x.com",
                }],
                overdue_no_ef=[],
                summary={"alerts": 1},
                period_label="Weekly",
                period_start="2026-07-29",
            )
        assert n == 1
        html = mock_send.call_args.kwargs["html_body"]
        assert "https://contoso.sharepoint.com/sites/IT/Reports" in html
        assert "Open report folder in SharePoint" in html
        assert "fwd@x.com" in html
        assert "mgr@x.com" in html
        assert "Weekly" in mock_send.call_args.kwargs["subject"]

    def test_ooo_is_internal_only(self):
        with patch("utils.graph_api._patch", return_value=True) as mock_patch:
            from utils.graph_api import set_auto_reply
            ok = set_auto_reply("uid", "Mgr Name", "mgr@netradyne.com")
        assert ok is True
        payload = mock_patch.call_args[0][1]
        setting = payload["automaticRepliesSetting"]
        assert setting["externalAudience"] == "none"
        assert setting["externalReplyMessage"] == ""
        assert "mgr@netradyne.com" in setting["internalReplyMessage"]

    def test_ooo_uses_policy_wording_when_forwarding_active(self):
        """Policy 4.2.3 internal wording, naming the manager as the contact."""
        with patch("utils.graph_api._patch", return_value=True) as mock_patch:
            from utils.graph_api import set_auto_reply
            ok = set_auto_reply(
                "uid", "Mgr Name", "mgr@netradyne.com", forwarding_active=True,
            )
        assert ok is True
        msg = mock_patch.call_args[0][1]["automaticRepliesSetting"]["internalReplyMessage"]
        assert "no longer monitored" in msg
        assert "automatically forwarded to their manager" in msg
        assert "please reach out to Mgr Name (mgr@netradyne.com)" in msg

    def test_ooo_omits_contact_when_manager_unknown(self):
        """No manager on the Entra record → generic wording, no dangling contact."""
        with patch("utils.graph_api._patch", return_value=True) as mock_patch:
            from utils.graph_api import set_auto_reply
            set_auto_reply("uid", "", "", forwarding_active=True)
        msg = mock_patch.call_args[0][1]["automaticRepliesSetting"]["internalReplyMessage"]
        assert msg.endswith("forwarded to their manager for review.")
        assert "reach out to" not in msg


# ---------------------------------------------------------------------------
# Tests: pre-deletion notice gate in the monthly cleanup scan (policy 4.6.2)
# ---------------------------------------------------------------------------

class TestCleanupPreDeletionNotice:
    def test_cleanup_defers_when_notice_not_sent(self, monkeypatch):
        monkeypatch.delenv("EF_DRY_RUN", raising=False)
        monkeypatch.setenv("DISABLE_ACCOUNT_DELETION", "false")
        monkeypatch.setenv("EF_UAT_MODE", "false")
        monkeypatch.setenv("DISABLE_GRAPH_WRITES", "false")
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=20, ef_required=False)
        from utils.cleanup_scan import _delete_stale
        with patch("utils.cleanup_scan.delete_user") as mock_del, \
             patch("utils.cleanup_scan.get_deleted_user", return_value=None), \
             patch("utils.cleanup_scan.has_email_forwarding", return_value=False), \
             patch("utils.monitor_accounts.send_no_ef_pre_deletion_notice", return_value=True) as mock_notice:
            result = _delete_stale(
                "user-001", record, store, source="tracked_no_ef", days_elapsed=20,
            )
        assert result == "deferred_notice"
        mock_notice.assert_called_once()
        mock_del.assert_not_called()

    def test_cleanup_deletes_once_notice_lead_met(self, monkeypatch):
        monkeypatch.delenv("EF_DRY_RUN", raising=False)
        monkeypatch.setenv("DISABLE_ACCOUNT_DELETION", "false")
        monkeypatch.setenv("EF_UAT_MODE", "false")
        monkeypatch.setenv("DISABLE_GRAPH_WRITES", "false")
        store = MagicMock()
        record = make_record(
            offboard_days_ago=20, ef_required=False, pre_deletion_notice_days_ago=6,
        )
        from utils.cleanup_scan import _delete_stale
        with patch("utils.cleanup_scan.delete_user", return_value=True) as mock_del, \
             patch("utils.cleanup_scan.get_deleted_user", return_value=None), \
             patch("utils.cleanup_scan.has_email_forwarding", return_value=False), \
             patch("utils.cleanup_scan.send_deletion_notice") as mock_notice:
            result = _delete_stale(
                "user-001", record, store, source="tracked_no_ef", days_elapsed=20,
            )
        assert result == "deleted"
        mock_del.assert_called_once_with("user-001")
        mock_notice.assert_called_once_with(record, reason="NO_EF_RETENTION")
        assert store.append_email_log.call_args.kwargs["email_type"] == "DELETION_NOTICE"
        assert record["statusCode"] == "DELETED"
        assert record["deletedBy"] == "cleanup"


# ---------------------------------------------------------------------------
# Tests: deleted-user tracking + reporting
# ---------------------------------------------------------------------------

class TestDeletedUserTracking:
    def _run_monitor(self, records, deleted_lookup):
        store = MagicMock()
        store.list_active_users.return_value = records
        store.get_compliance_record.return_value = None
        from utils.monitor_accounts import run_monitor
        with patch("utils.monitor_accounts.TableStore", return_value=store), \
             patch("utils.monitor_accounts.get_terminated_users", return_value=[]), \
             patch("utils.monitor_accounts.get_deleted_user", side_effect=deleted_lookup):
            return run_monitor(), store

    def test_manual_deletion_detected_from_recycle_bin(self):
        record = make_record(offboard_days_ago=12, ef_required=False)
        summary, store = self._run_monitor(
            [record], lambda uid: {"id": uid, "deletedDateTime": "2026-10-18T10:00:00Z"},
        )
        assert summary["deleted_externally"] == 1
        assert record["statusCode"] == "DELETED"
        assert record["deletedBy"] == "manual"
        assert record["deletedDate"] == "2026-10-18"
        store.upsert_user.assert_called_with(record)

    def test_missing_user_not_in_recycle_bin_left_alone(self):
        record = make_record(offboard_days_ago=12, ef_required=False)
        summary, store = self._run_monitor([record], lambda uid: None)
        assert summary["deleted_externally"] == 0
        assert record["statusCode"] == "ACTIVE"
        store.upsert_user.assert_not_called()

    def test_period_report_lists_deleted_in_window_only(self):
        today = date.today()
        recent = make_record("u-recent", offboard_days_ago=50, status="DELETED")
        recent.update(deletedDate=(today - timedelta(days=2)).isoformat(),
                      deleteReason="NO_EXTENSION_DAY30", deletedBy="automation")
        old = make_record("u-old", offboard_days_ago=90, status="DELETED")
        old["deletedDate"] = (today - timedelta(days=40)).isoformat()
        store = MagicMock()
        store.list_all_users.return_value = [recent, old]
        # only u-recent has a DELETED audit row inside the window
        store.list_recent_audits.return_value = [{"PartitionKey": "u-recent", "action": "DELETED"}]
        from utils.weekly_report import _collect_period_data
        data = _collect_period_data(store, lookback_days=7)
        assert [r["userId"] for r in data["deleted"]] == ["u-recent"]
        row = data["deleted"][0]
        assert row["deletedBy"] == "automation"
        assert row["recoverableUntil"] == (today + timedelta(days=28)).isoformat()
        assert data["summary"]["deletions"] == 1

    def test_report_export_includes_deleted_section(self):
        from utils.report_export import build_report_csv
        csv_bytes = build_report_csv(
            report_date="2026-10-20", period_label="Weekly", period_start="2026-10-13",
            new_no_ef=[], new_with_ef=[], overdue_no_ef=[], summary={},
            deleted=[{"displayName": "D", "userEmail": "d@x.com", "deletedDate": "2026-10-18",
                      "deletedBy": "manual", "deleteReason": "MANUAL_OR_EXTERNAL",
                      "recoverableUntil": "2026-11-17"}],
        )
        assert b"Deleted,2026-10-20" in csv_bytes
        assert b"2026-11-17" in csv_bytes


# ---------------------------------------------------------------------------
# Tests: manager review feedback (legal hold, forwarding recipient, user OOO)
# ---------------------------------------------------------------------------

class TestManagerFeedback:
    def test_ooo_names_forwarding_recipient_when_not_manager(self):
        with patch("utils.graph_api._patch", return_value=True) as mock_patch:
            from utils.graph_api import set_auto_reply
            set_auto_reply("uid", "Mgr Name", "mgr@netradyne.com", forwarding_active=True,
                           forwarding_address="peer@netradyne.com", employee_name="Asha Rao")
        msg = mock_patch.call_args[0][1]["automaticRepliesSetting"]["internalReplyMessage"]
        assert msg.startswith("Thank you for your message. Asha Rao is no longer with Netradyne")
        assert "please reach out to peer@netradyne.com" in msg
        assert "mgr@netradyne.com" not in msg

    def test_users_own_ooo_is_preserved_and_never_scheduled_off(self):
        from utils.monitor_accounts import _ensure_ooo
        store = MagicMock()
        record = make_record(ef_required=False)
        with patch("utils.monitor_accounts.is_auto_reply_enabled", return_value=True), \
             patch("utils.monitor_accounts.set_auto_reply") as mock_set:
            _ensure_ooo("user-001", record, store)
        mock_set.assert_not_called()
        assert record["oooPreserved"] is True
        assert record["oooSetDate"] == ""   # Day-30 disable keys off oooSetDate

    def test_unreadable_ooo_state_never_overwrites(self):
        from utils.monitor_accounts import _ensure_ooo
        record = make_record(ef_required=False)
        with patch("utils.monitor_accounts.is_auto_reply_enabled", return_value=None), \
             patch("utils.monitor_accounts.set_auto_reply") as mock_set:
            _ensure_ooo("user-001", record, MagicMock())
        mock_set.assert_not_called()
        assert not record.get("oooPreserved") and record["oooSetDate"] == ""

    def test_legal_hold_day90_ends_forwarding_but_keeps_account(self, monkeypatch):
        monkeypatch.setenv("EF_UAT_MODE", "false")
        monkeypatch.setenv("DISABLE_ACCOUNT_DELETION", "false")
        monkeypatch.setenv("DISABLE_GRAPH_WRITES", "false")
        monkeypatch.delenv("EF_DRY_RUN", raising=False)
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=90, extension_count=2)
        with patch("utils.graph_api.is_litigation_hold_active", return_value=True), \
             patch("utils.monitor_accounts.delete_user") as mock_del, \
             patch("utils.monitor_accounts.disable_email_forwarding", return_value=True) as mock_off, \
             patch("utils.monitor_accounts.send_ef_removed_notice", return_value=True) as mock_off_mail, \
             patch("utils.monitor_accounts.send_final_deletion_notice") as mock_mail:
            from utils.monitor_accounts import _do_delete
            result = _do_delete("user-001", record, "MAX_POLICY_DAY90", store, is_final=True,
                                ad_user={"customSecurityAttributes": {}})
        assert result == "no_action"
        mock_del.assert_not_called()
        mock_mail.assert_not_called()   # no "account deleted" email
        mock_off.assert_called_once_with("user-001")
        assert mock_off_mail.call_args.kwargs["retained"] is True
        assert record["statusCode"] == "EF_DISABLED" and record["legalHoldActive"] is True
        assert record["retainedReason"] == "legal_hold"

    def _delete_env(self, monkeypatch):
        monkeypatch.setenv("EF_UAT_MODE", "false")
        monkeypatch.setenv("DISABLE_ACCOUNT_DELETION", "false")
        monkeypatch.setenv("DISABLE_GRAPH_WRITES", "false")
        monkeypatch.delenv("EF_DRY_RUN", raising=False)

    def test_delete_exempt_without_hold_is_retained_too(self, monkeypatch):
        self._delete_env(monkeypatch)
        monkeypatch.setenv("DELETION_EXEMPT_EMAILS", "user-001@netradyne.com")
        record = make_record(offboard_days_ago=46, status="EF_DISABLED")
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            from utils.monitor_accounts import _do_delete
            result = _do_delete("user-001", record, "NO_EXTENSION_DAY30", MagicMock(),
                                ad_user={"customSecurityAttributes": {}})
        assert result == "no_action"
        mock_del.assert_not_called()
        assert record["retainedReason"] == "env_email"

    def test_unreadable_hold_is_not_retained_for_good(self, monkeypatch):
        self._delete_env(monkeypatch)
        record = make_record(offboard_days_ago=46, status="EF_DISABLED")
        with patch("utils.graph_api.is_litigation_hold_active", side_effect=RuntimeError("EXO 503")), \
             patch("utils.monitor_accounts.delete_user") as mock_del:
            from utils.monitor_accounts import _do_delete
            _do_delete("user-001", record, "NO_EXTENSION_DAY30", MagicMock(),
                       ad_user={"customSecurityAttributes": {}})
        mock_del.assert_not_called()
        assert not record.get("retainedReason")   # retried on the next run

    def test_retained_is_sticky_even_after_hold_is_lifted(self):
        from utils.deletion_exempt import is_automated_deletion_exempt
        record = {"retainedReason": "legal_hold"}   # autouse fixture: hold now reads as lifted
        assert is_automated_deletion_exempt("user-001", record) == (True, "legal_hold")

    def test_retained_account_is_never_picked_up(self):
        from utils.monitor_accounts import _process_user
        record = make_record(offboard_days_ago=120)
        record["retainedReason"] = "legal_hold"
        assert _process_user(make_ad_user(120), date.today(), MagicMock(), record) == "skipped"

    def test_retained_emails_never_mention_deletion(self):
        record = make_record(offboard_days_ago=90, extension_count=2)
        record["managerEmail"] = "mgr@netradyne.com"
        with patch("utils.email_sender._send", return_value=True) as mock_send:
            from utils.email_sender import send_ef_alert, send_ef_removed_notice
            send_ef_alert(record, days_remaining=5, is_final=True, retained=True)
            send_ef_alert(record, days_remaining=5, is_final=False, retained=True)
            send_ef_removed_notice(record, days_until_delete=0, reason="MAX_POLICY_DAY90", retained=True)
        for call in mock_send.call_args_list:
            subject, html = call.args[1], call.args[2]
            assert "delet" not in (subject + html).lower(), subject
        assert mock_send.call_args_list[-1].args[1] == "Email Forwarding Disabled – Test User"

    def test_legal_hold_lookup_error_fails_closed(self):
        from utils.deletion_exempt import is_automated_deletion_exempt
        with patch("utils.graph_api.is_litigation_hold_active", side_effect=RuntimeError("EXO 503")):
            assert is_automated_deletion_exempt(
                "user-001", {}, ad_user={"customSecurityAttributes": {}},
            ) == (True, "legal_hold_check_failed")

    def test_ef_alert_goes_to_manager_and_forwarding_recipient(self, monkeypatch):
        monkeypatch.setenv("IT_EMAIL", "it@netradyne.com")
        record = make_record()
        record.update(managerEmail="mgr@netradyne.com", forwardingAddress="peer@netradyne.com")
        with patch("utils.email_sender._send", return_value=True) as mock_send:
            from utils.email_sender import send_ef_alert
            assert send_ef_alert(record, days_remaining=7) is True
        sent = [(c.args[0], c.kwargs.get("cc_address")) for c in mock_send.call_args_list]
        assert sent == [("mgr@netradyne.com", "it@netradyne.com"), ("peer@netradyne.com", None)]

    def test_test_mode_never_puts_test_mailbox_in_ooo(self, monkeypatch):
        """EF_TEST_MODE redirects our emails only; the auto-reply is read by real colleagues."""
        monkeypatch.setenv("EF_TEST_MODE", "true")
        from utils.monitor_accounts import _ensure_ooo
        record = make_record()
        record.update(managerName="Roshan Mathews", managerEmail="roshan.mathews@netradyne.com",
                      forwardingAddress="roshan.mathews@netradyne.com", displayName="Rakesh Pelluri")
        with patch("utils.monitor_accounts.is_auto_reply_enabled", return_value=False), \
             patch("utils.graph_api._patch", return_value=True) as mock_patch:
            _ensure_ooo("user-001", record, MagicMock())
        msg = mock_patch.call_args[0][1]["automaticRepliesSetting"]["internalReplyMessage"]
        assert "prem_testing" not in msg
        assert "please reach out to Roshan Mathews (roshan.mathews@netradyne.com)" in msg

    def test_internal_forwarding_target_id_is_resolved_to_email(self):
        """EXO returns an Entra object ID (not SMTP) when forwarding goes to an internal recipient."""
        from utils import exchange_admin
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"ForwardingAddress": "c0779691-b976-47c3-945e-ea9adcb24a75",
                                  "ForwardingSmtpAddress": None, "DeliverToMailboxAndForward": True}
        with patch.object(exchange_admin, "_exo_headers", return_value={}), \
             patch.object(exchange_admin, "_tenant", return_value="t"), \
             patch.object(exchange_admin.requests, "get", return_value=resp), \
             patch("utils.graph_api._get", return_value={"mail": "neha.singh@netradyne.com"}):
            out = exchange_admin.get_mailbox_forwarding("tabinda.qureshi@netradyne.com")
        assert out["forwardingAddress"] == "neha.singh@netradyne.com"


# ---------------------------------------------------------------------------
# Tests: pre-production review fixes
# ---------------------------------------------------------------------------

class TestPreProductionFixes:
    def test_failed_forwarding_removal_is_not_recorded_and_retries(self):
        """Forwarding not confirmed off → no 'disabled' email, status unchanged so tomorrow retries."""
        from utils.monitor_accounts import _do_remove_ef
        store = MagicMock()
        record = make_record(offboard_days_ago=30)
        with patch("utils.monitor_accounts.disable_email_forwarding", return_value=False), \
             patch("utils.monitor_accounts.send_ef_removed_notice") as mock_mail:
            result = _do_remove_ef("user-001", record, store, "NO_EXTENSION_DAY30", 16)
        assert result == "errors"
        mock_mail.assert_not_called()
        assert record["statusCode"] == "ACTIVE"
        assert store.append_audit.call_args[0][1] == "EF_DISABLE_FAILED"

    def test_disable_forwarding_switches_off_inbox_rules_and_fails_if_unreadable(self, monkeypatch):
        monkeypatch.setenv("EF_UAT_MODE", "false")
        monkeypatch.setenv("DISABLE_GRAPH_WRITES", "false")
        monkeypatch.delenv("EF_DRY_RUN", raising=False)
        from utils import graph_api
        rules = {"value": [
            {"id": "r1", "isEnabled": True, "actions": {"forwardTo": [{"emailAddress": {"address": "x@y.com"}}]}},
            {"id": "r2", "isEnabled": True, "actions": {"moveToFolder": "abc"}},
        ]}
        with patch.object(graph_api, "_user_upn", return_value="u@netradyne.com"), \
             patch("utils.exchange_admin.clear_mailbox_forwarding", return_value=True), \
             patch.object(graph_api, "_get", return_value=rules), \
             patch.object(graph_api, "_patch", return_value=True) as mock_patch:
            assert graph_api.disable_email_forwarding("user-001") is True
        assert [c.args[0].rsplit("/", 1)[-1] for c in mock_patch.call_args_list] == ["r1"]
        assert mock_patch.call_args.args[1] == {"isEnabled": False}
        with patch.object(graph_api, "_user_upn", return_value="u@netradyne.com"), \
             patch("utils.exchange_admin.clear_mailbox_forwarding", return_value=True), \
             patch.object(graph_api, "_get", return_value=None):
            assert graph_api.disable_email_forwarding("user-001") is False

    def test_final_alert_sends_no_it_approval(self, monkeypatch):
        import utils.monitor_accounts as ma
        monkeypatch.setattr(ma, "_IT_APPROVAL_EMAIL", "it-approve@netradyne.com")
        monkeypatch.delenv("EF_DRY_RUN", raising=False)
        record = make_record(offboard_days_ago=83, extension_count=2)
        with patch.object(ma, "send_ef_alert", return_value=True), \
             patch.object(ma, "_is_retained", return_value=False), \
             patch.object(ma, "set_extension_attribute"), \
             patch.object(ma, "generate_approval_urls") as mock_urls, \
             patch.object(ma, "send_it_approval_notification") as mock_it:
            assert ma._do_alert(record, MagicMock(), days_remaining=7, is_final=True) == "alerted"
        mock_urls.assert_not_called()
        mock_it.assert_not_called()

    def test_forwarding_disabled_email_shows_real_deletion_date(self):
        record = make_record(offboard_days_ago=30)
        record["managerEmail"] = "mgr@netradyne.com"
        expected = (datetime.now(timezone.utc).date() + timedelta(days=16)).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        with patch("utils.email_sender._send", return_value=True) as mock_send:
            from utils.email_sender import send_ef_removed_notice
            send_ef_removed_notice(record, days_until_delete=16, reason="NO_EXTENSION_DAY30")
        html = mock_send.call_args.args[2]
        assert expected in html and today not in html

    def test_decline_link_only_shows_form_until_confirmed(self):
        from utils import approval_webhook
        store = MagicMock()
        store.get_approval_token.return_value = {
            "token": "t1", "userId": "user-001", "extType": "EXTEND_TO_30", "used": False,
            "expiresAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        store.get_user.return_value = {"displayName": "Test User"}
        with patch("utils.table_store.TableStore", return_value=store):
            page, status = approval_webhook.handle_get("t1", "decline")
            assert status == 200 and 'name="action" value="decline"' in page
            store.mark_token_used.assert_not_called()   # a link scanner opening it changes nothing
            page, status = approval_webhook.handle_post("t1", "", "decline", "it.admin@netradyne.com")
        assert status == 200 and "Extension Declined" in page
        assert store.mark_token_used.call_args.args == ("t1",)
        assert store.mark_token_used.call_args.kwargs["decision"] == "DECLINED"
        assert store.mark_token_used.call_args.kwargs["decidedBy"] == "it.admin@netradyne.com"
        assert store.append_audit.call_args[0][1] == "EXTENSION_DECLINED"


class TestApprovalDecisionReport:
    def _store(self):
        store = MagicMock()
        store.get_approval_token.return_value = {
            "token": "t1", "userId": "user-001", "extType": "EXTEND_TO_30", "used": False,
            "expiresAt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
        store.get_user.return_value = {"displayName": "Test User", "userEmail": "test.user@netradyne.com"}
        return store

    def test_approve_requires_company_email_and_records_it(self, monkeypatch):
        monkeypatch.delenv("SENDER_EMAIL", raising=False)   # default sender → netradyne.com
        from utils import approval_webhook
        store = self._store()
        with patch("utils.table_store.TableStore", return_value=store), \
             patch("utils.graph_api.set_extension_attribute", return_value=True), \
             patch("utils.graph_api.set_ticket_ref_attribute", return_value=True):
            assert approval_webhook.handle_post("t1", "SD-1", "approve", "")[1] == 400
            assert approval_webhook.handle_post("t1", "SD-1", "approve", "x@gmail.com")[1] == 400
            store.mark_token_used.assert_not_called()
            page, status = approval_webhook.handle_post("t1", "SD-1", "approve", " IT.Admin@Netradyne.com ")
        assert status == 200
        kw = store.mark_token_used.call_args.kwargs
        assert (kw["decision"], kw["decidedBy"], kw["decidedByVerified"], kw["ticketRef"]) == \
            ("APPROVED", "it.admin@netradyne.com", False, "SD-1")

    def test_signed_in_identity_wins_and_is_verified(self):
        from utils.approval_webhook import _decided_by
        assert _decided_by("typed@netradyne.com", "real.person@netradyne.com") == ("real.person@netradyne.com", True)

    def test_period_report_includes_approvals_in_sharepoint_files(self):
        store = MagicMock()
        store.list_all_users.return_value = []
        store.list_recent_audits.return_value = []
        store.list_token_decisions.return_value = [{
            "displayName": "Test User", "userEmail": "t@netradyne.com", "decision": "APPROVED",
            "extType": "EXTEND_TO_30", "ticketRef": "SD-1", "decidedBy": "it.admin@netradyne.com",
            "decidedByVerified": False, "usedAt": "2026-11-02T10:00:00+00:00",
        }]
        from utils.weekly_report import _collect_period_data
        from utils.report_export import build_report_csv
        data = _collect_period_data(store, lookback_days=7)
        assert data["approvals"][0]["decidedBy"] == "it.admin@netradyne.com"
        csv_bytes = build_report_csv(
            report_date="2026-11-02", period_label="Weekly", period_start="2026-10-26",
            new_no_ef=[], new_with_ef=[], overdue_no_ef=[], summary={}, approvals=data["approvals"],
        )
        assert b"Approval,2026-11-02" in csv_bytes and b"it.admin@netradyne.com" in csv_bytes

    def test_approvals_email_not_sent_while_dl_setting_is_blank(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_REPORT_EMAILS", "")
        with patch("utils.email_sender._send") as mock_send:
            from utils.email_sender import send_approvals_report
            assert send_approvals_report([], "Weekly", "2026-10-26", "2026-11-02") == 0
        mock_send.assert_not_called()
