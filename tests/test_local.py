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

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def make_record(
    user_id: str = "user-001",
    offboard_days_ago: int = 0,
    ef_required: bool = True,
    extension_count: int = 0,
    status: str = "ACTIVE",
    last_alert: str = "",
) -> Dict[str, Any]:
    """Return a synthetic UserTracking dict."""
    today = date.today()
    offboard = today - timedelta(days=offboard_days_ago)
    delete_date = offboard + timedelta(days=(extension_count + 1) * 30)
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
        "licensesRemovedDate": "",
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

    # ── No EF ────────────────────────────────────────────────────────────

    def test_no_ef_before_day30_no_action(self):
        store = MagicMock()
        record = make_record(offboard_days_ago=10, ef_required=False)
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(10)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()

    def test_no_ef_day30_default_no_action(self):
        """Default (AUTO_DELETE_NO_EF=false): NO_EF account on Day 30 → no_action.
        Monthly cleanup scan is the safety net; daily monitor does not auto-delete."""
        store = MagicMock()
        record = make_record(offboard_days_ago=30, ef_required=False)
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(30)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()

    def test_no_ef_day30_auto_delete_flag_deletes(self):
        """With AUTO_DELETE_NO_EF=true: NO_EF account on Day 30 → deleted (legacy mode)."""
        store = MagicMock()
        store.get_compliance_record.return_value = None
        record = make_record(offboard_days_ago=30, ef_required=False)
        today = date.today()
        import utils.monitor_accounts as ma
        original = ma._AUTO_DELETE_NO_EF
        try:
            ma._AUTO_DELETE_NO_EF = True
            with patch("utils.monitor_accounts.delete_user", return_value=True) as mock_del, \
                 patch("utils.monitor_accounts.send_deletion_notice"), \
                 patch("utils.monitor_accounts.is_litigation_hold_active", return_value=False), \
                 patch("utils.monitor_accounts.remove_all_licenses", return_value=True):
                ad = make_ad_user(30)
                result = self._run(ad, today, store, record)
                assert result == "deleted"
            mock_del.assert_called_once_with(record["userId"])
        finally:
            ma._AUTO_DELETE_NO_EF = original

    def test_no_ef_past_day30_still_no_action(self):
        """Day 35, NO_EF, AUTO_DELETE_NO_EF=false → no_action (monthly cleanup handles)."""
        store = MagicMock()
        record = make_record(offboard_days_ago=35, ef_required=False)
        today = date.today()
        with patch("utils.monitor_accounts.delete_user") as mock_del:
            ad = make_ad_user(35)
            result = self._run(ad, today, store, record)
        assert result == "no_action"
        mock_del.assert_not_called()

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
             patch("utils.monitor_accounts.send_deletion_notice"), \
             patch("utils.monitor_accounts.is_litigation_hold_active", return_value=False), \
             patch("utils.monitor_accounts.remove_all_licenses", return_value=True):
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
             patch("utils.monitor_accounts.send_deletion_notice"), \
             patch("utils.monitor_accounts.is_litigation_hold_active", return_value=False), \
             patch("utils.monitor_accounts.remove_all_licenses", return_value=True):
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
             patch("utils.monitor_accounts.send_final_deletion_notice"), \
             patch("utils.monitor_accounts.is_litigation_hold_active", return_value=False), \
             patch("utils.monitor_accounts.remove_all_licenses", return_value=True):
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
