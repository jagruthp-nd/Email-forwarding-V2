"""
uat_console.py
--------------
HTTP console for UAT operators:
  - View current UAT / safety gates
  - On-demand SharePoint archive (no email)
  - Optional: email the weekly-style report (still respects EF_TEST_MODE)

Requires Function key (?code=...).
"""

from __future__ import annotations

import html as html_lib
import logging
import os
from typing import Any, Dict, Tuple
from urllib.parse import quote

from .app_config import get_email_config_summary, get_func_base_url, get_sharepoint_report_url
from .automation_flags import (
    get_test_recipient,
    is_account_deletion_disabled,
    is_dry_run,
    is_ef_revoke_disabled,
    is_graph_write_disabled,
    is_outbound_email_disabled,
    is_test_mode,
    is_uat_mode,
    log_active_gates,
)

logger = logging.getLogger(__name__)


def _flag(name: str, default: str = "false") -> str:
    return os.environ.get(name, default) or default


def _link(code: str, action: str, **extra: str) -> str:
    base = get_func_base_url() or ""
    parts = []
    if code:
        parts.append(f"code={quote(code)}")
    parts.append(f"action={quote(action)}")
    for k, v in extra.items():
        parts.append(f"{quote(k)}={quote(str(v))}")
    return f"{base}/api/uat?{'&'.join(parts)}"


def _settings_rows() -> str:
    rows = [
        ("EF_UAT_MODE", _flag("EF_UAT_MODE"), "UAT profile: emails + EF revoke; no account deletes"),
        ("DISABLE_ACCOUNT_DELETION", _flag("DISABLE_ACCOUNT_DELETION"), "Hard stop on soft-delete"),
        ("DISABLE_GRAPH_WRITES", _flag("DISABLE_GRAPH_WRITES"), "Blocks EF revoke / CSA / deletes when true"),
        ("EF_TEST_MODE", _flag("EF_TEST_MODE"), "Redirect all mail to EF_TEST_RECIPIENT"),
        ("EF_TEST_RECIPIENT", get_test_recipient() if is_test_mode() else _flag("EF_TEST_RECIPIENT", ""), "Test inbox"),
        ("AUTO_DELETE_NO_EF", _flag("AUTO_DELETE_NO_EF"), "If true, daily job deletes NO_EF at day 30"),
        ("CLEANUP_MIN_OFFBOARD_DAYS", _flag("CLEANUP_MIN_OFFBOARD_DAYS", "30"), "Monthly cleanup min age for NO_EF"),
        ("CLEANUP_REGION", _flag("CLEANUP_REGION", "IN"), "Monthly cleanup region gate"),
        ("DELETE_DAY_1 / DELETE_DAY_2", f"{_flag('DELETE_DAY_1','46')} / {_flag('DELETE_DAY_2','76')}", "EF path delete days (no extension)"),
        ("Max policy delete", "90", "EF path final soft-delete (hardcoded)"),
        ("Effective: account deletion disabled?", str(is_account_deletion_disabled()), "Computed from UAT / flags"),
        ("Effective: EF revoke disabled?", str(is_ef_revoke_disabled()), "Computed from UAT / flags"),
        ("SharePoint folder", get_sharepoint_report_url() or "(not set)", "Archive target"),
    ]
    out = []
    for k, v, note in rows:
        out.append(
            "<tr>"
            f"<td style='padding:8px;border:1px solid #dee2e6;font-weight:bold;'>{html_lib.escape(k)}</td>"
            f"<td style='padding:8px;border:1px solid #dee2e6;font-family:monospace;font-size:12px;'>"
            f"{html_lib.escape(str(v))}</td>"
            f"<td style='padding:8px;border:1px solid #dee2e6;font-size:12px;color:#555;'>"
            f"{html_lib.escape(note)}</td>"
            "</tr>"
        )
    return "".join(out)


def _policy_html() -> str:
    return """
    <h2 style="font-size:17px;margin:22px 0 8px;">Deletion policy (production behaviour)</h2>
    <ol style="line-height:1.7;font-size:14px;">
      <li><strong>With email forwarding (EF)</strong>
        <ul>
          <li>Alerts around days 23 / 53 / 83; EF disabled at 30 / 60 if no extension.</li>
          <li>Account soft-deleted at <strong>day 46</strong> or <strong>day 76</strong> if no extension approved.</li>
          <li>After max extensions, soft-deleted at <strong>day 90</strong> (max policy).</li>
          <li>Soft-delete = Entra recycle bin (~30 days), not hard purge by this app.</li>
        </ul>
      </li>
      <li><strong>No email forwarding (NO_EF)</strong>
        <ul>
          <li>Daily monitor does <em>not</em> delete (unless <code>AUTO_DELETE_NO_EF=true</code>).</li>
          <li>Listed on weekly/monthly reports (overdue after reminder day).</li>
          <li><strong>Monthly cleanup</strong> soft-deletes NO_EF accounts past
              <code>CLEANUP_MIN_OFFBOARD_DAYS</code> (default 30) in <code>CLEANUP_REGION</code>
              — unless UAT / deletion-disabled / exempt / still has EF.</li>
        </ul>
      </li>
      <li><strong>Ways to hold off deletion</strong> (not only litigation hold):
        <ul>
          <li>Litigation / in-place hold (checked before delete)</li>
          <li><code>DELETION_EXEMPT_USER_IDS</code> / <code>DELETION_EXEMPT_EMAILS</code></li>
          <li>Tracking flags <code>deletionExempt</code> / <code>skipCleanupDeletion</code></li>
          <li>Approved EF extensions (delays until day 90 max)</li>
          <li>Region gate (non-<code>DELETE_REGION</code> accounts skipped)</li>
          <li><code>EF_UAT_MODE</code> / <code>DISABLE_ACCOUNT_DELETION</code> (current UAT)</li>
        </ul>
      </li>
    </ol>
    """


def _landing(code: str, flash: str = "") -> str:
    flash_html = (
        f"<div style='background:#d4edda;border:1px solid #c3e6cb;padding:14px;"
        f"border-radius:4px;margin:16px 0;'>{flash}</div>"
        if flash else ""
    )
    mail = get_email_config_summary()
    mail_rows = "".join(
        f"<tr><td style='padding:6px 10px;border:1px solid #dee2e6;'>{html_lib.escape(k)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #dee2e6;font-size:12px;'>"
        f"{html_lib.escape(v)}</td></tr>"
        for k, v in mail.items()
    )

    def btn(action: str, label: str, color: str, **extra: str) -> str:
        return (
            f"<a href='{_link(code, action, **extra)}' "
            f"style='display:inline-block;margin:6px 8px 6px 0;padding:12px 18px;"
            f"background:{color};color:#fff;text-decoration:none;border-radius:4px;"
            f"font-weight:bold;'>{label}</a>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>EF UAT Console</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:920px;margin:28px auto;padding:16px;">
  <div style="background:#243a5e;color:#fff;padding:22px;border-radius:6px 6px 0 0;">
    <h1 style="margin:0;font-size:22px;">EF Automation – UAT Console</h1>
    <p style="margin:8px 0 0;opacity:.9;font-size:14px;">
      Settings view · on-demand SharePoint archive · optional report email
    </p>
  </div>
  <div style="background:#f8f9fa;padding:22px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    {flash_html}

    <p style="font-size:13px;color:#666;">
      Live gates: UAT={is_uat_mode()} · deletion_disabled={is_account_deletion_disabled()} ·
      graph_writes_off={is_graph_write_disabled()} · ef_revoke_off={is_ef_revoke_disabled()} ·
      dry_run={is_dry_run()} · outbound_off={is_outbound_email_disabled()} ·
      test_mode={is_test_mode()}
    </p>

    <h2 style="font-size:17px;margin:18px 0 8px;">UAT / safety settings</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:12px;">
      <tr style="background:#e9ecef;">
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Setting</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Value</th>
        <th style="padding:8px;border:1px solid #dee2e6;text-align:left;">Meaning</th>
      </tr>
      {_settings_rows()}
    </table>

    {_policy_html()}

    <h2 style="font-size:17px;margin:22px 0 8px;">Configured mailboxes</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:18px;">{mail_rows}</table>

    <h2 style="font-size:17px;margin:22px 0 8px;">On-demand actions</h2>
    <p style="font-size:13px;color:#666;">Archive uploads Excel/CSV to SharePoint only (no email).
    Email report still respects <code>EF_TEST_MODE</code>.</p>
    {btn("archive", "Archive report → SharePoint (no email)", "#0078d4", lookback_days="7")}
    {btn("archive30", "Archive last 30 days → SharePoint", "#0d6efd", lookback_days="30")}
    {btn("email_report", "Email weekly-style report + archive", "#28a745")}
    {btn("home", "Refresh settings", "#6c757d")}

    <p style="margin-top:20px;font-size:12px;color:#888;">
      Keep this URL private (includes function key). Change gates in Azure App Settings, then Refresh.
    </p>
  </div>
</body></html>"""


def handle_uat(action: str, function_code: str = "", lookback_days: int = 7) -> Tuple[str, int]:
    log_active_gates("uat_console")
    action = (action or "home").strip().lower()

    if action in ("", "home", "settings", "overview"):
        return _landing(function_code), 200

    try:
        if action in ("archive", "archive7"):
            from .weekly_report import archive_period_report
            summary = archive_period_report("OnDemand", lookback_days=lookback_days or 7)
            flash = _format_archive_flash(summary)
            return _landing(function_code, flash), 200 if summary.get("status") == "archived" else 500

        if action == "archive30":
            from .weekly_report import archive_period_report
            summary = archive_period_report("OnDemand30d", lookback_days=30)
            flash = _format_archive_flash(summary)
            return _landing(function_code, flash), 200 if summary.get("status") == "archived" else 500

        if action == "email_report":
            from .weekly_report import run_weekly_report
            summary = run_weekly_report()
            flash = (
                f"<strong>Email report:</strong> {html_lib.escape(str(summary.get('status')))} · "
                f"recipients={summary.get('recipients_count')} · "
                f"new_EF={summary.get('new_with_ef')} · new_NO_EF={summary.get('new_no_ef')}"
            )
            return _landing(function_code, flash), 200

        return _landing(
            function_code,
            flash=f"Unknown action <code>{html_lib.escape(action)}</code>",
        ), 400
    except Exception as exc:
        logger.exception("UAT console action %s failed", action)
        return _landing(
            function_code,
            flash=f"<strong>Error:</strong> {html_lib.escape(str(exc))}",
        ), 500


def _format_archive_flash(summary: Dict[str, Any]) -> str:
    files = summary.get("uploaded") or []
    links = "".join(
        f"<li><a href='{html_lib.escape(f.get('web_url',''))}' target='_blank'>"
        f"{html_lib.escape(f.get('filename',''))}</a></li>"
        for f in files
    )
    err = summary.get("errors") or []
    err_html = (
        "<p style='color:#856404;'>Errors: "
        + html_lib.escape(str(err))
        + "</p>"
        if err else ""
    )
    return (
        f"<strong>Archive {html_lib.escape(str(summary.get('status')))}</strong> "
        f"(email_sent={summary.get('email_sent')}) · "
        f"new_EF={summary.get('new_with_ef')} · new_NO_EF={summary.get('new_no_ef')} · "
        f"overdue_NO_EF={summary.get('overdue_no_ef')}"
        f"<ul style='margin:8px 0 0;padding-left:20px;'>{links or '<li>No files</li>'}</ul>"
        f"{err_html}"
    )
