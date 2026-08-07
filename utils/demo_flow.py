"""
demo_flow.py
------------
Team demo HTTP helper – walks through EF Automation safely.

Actions (query param `action`):
  (default)   Landing page with steps + config
  emails      Send sample manager / IT / admin templates (EF_TEST_MODE routing)
  report      Send weekly-style report + attempt SharePoint upload
  approval    Create a real Approve/Decline token and email IT_APPROVAL_EMAIL
  full        emails → report → approval

Requires Function key (?code=...). Does not delete Entra users when
DISABLE_GRAPH_WRITES=true.
"""

from __future__ import annotations

import html as html_lib
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

from .app_config import get_email_config_summary, get_func_base_url, get_sharepoint_report_url
from .automation_flags import (
    get_test_recipient,
    is_graph_write_disabled,
    is_test_mode,
    log_active_gates,
)
from .email_sender import (
    send_deletion_notice,
    send_ef_alert,
    send_ef_removed_notice,
    send_extension_confirm,
    send_final_deletion_notice,
    send_it_approval_notification,
    send_offboard_consolidated_report,
)

logger = logging.getLogger(__name__)


def _demo_record(**overrides: Any) -> Dict[str, Any]:
    today = date.today()
    offboard = today - timedelta(days=23)
    rec = {
        "userId": "demo-user-001",
        "userEmail": "demo.employee@netradyne.com",
        "displayName": "Demo Employee",
        "managerEmail": get_test_recipient(),
        "offboardDate": offboard.isoformat(),
        "efRequired": True,
        "statusCode": "ACTIVE",
        "extensionCount": 0,
        "deleteDate": (today + timedelta(days=7)).isoformat(),
        "usageLocation": "IN",
        "forwardingAddress": "manager.fwd@netradyne.com",
    }
    rec.update(overrides)
    return rec


def _link(code: str, action: str) -> str:
    base = get_func_base_url() or ""
    q = f"code={quote(code)}&action={quote(action)}" if code else f"action={quote(action)}"
    return f"{base}/api/demo?{q}"


def _landing_html(code: str, flash: str = "") -> str:
    cfg = get_email_config_summary()
    rows = "".join(
        f"<tr><td style='padding:6px 10px;border:1px solid #dee2e6;font-weight:bold;'>"
        f"{html_lib.escape(k)}</td>"
        f"<td style='padding:6px 10px;border:1px solid #dee2e6;font-size:13px;'>"
        f"{html_lib.escape(v)}</td></tr>"
        for k, v in cfg.items()
    )
    flash_html = (
        f"<div style='background:#d4edda;border:1px solid #c3e6cb;padding:14px;"
        f"border-radius:4px;margin:16px 0;'>{flash}</div>"
        if flash else ""
    )
    gates = (
        f"EF_TEST_MODE={is_test_mode()} · "
        f"DISABLE_GRAPH_WRITES={is_graph_write_disabled()} · "
        f"test inbox={get_test_recipient()}"
    )
    sp = get_sharepoint_report_url() or "(not configured)"

    def btn(action: str, label: str, color: str) -> str:
        return (
            f"<a href='{_link(code, action)}' "
            f"style='display:inline-block;margin:6px 8px 6px 0;padding:12px 18px;"
            f"background:{color};color:#fff;text-decoration:none;border-radius:4px;"
            f"font-weight:bold;'>{label}</a>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>EF Automation Demo</title></head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#333;max-width:880px;margin:28px auto;padding:16px;">
  <div style="background:#243a5e;color:#fff;padding:22px;border-radius:6px 6px 0 0;">
    <h1 style="margin:0;font-size:22px;">EF Automation – Team Demo</h1>
    <p style="margin:8px 0 0;opacity:.9;font-size:14px;">Safe walkthrough (emails → test inbox; Graph writes gated)</p>
  </div>
  <div style="background:#f8f9fa;padding:22px;border:1px solid #dee2e6;border-top:none;border-radius:0 0 6px 6px;">
    {flash_html}
    <p style="font-size:13px;color:#666;"><strong>Safety gates:</strong> {html_lib.escape(gates)}</p>
    <p style="font-size:13px;color:#666;"><strong>SharePoint folder:</strong>
      <a href="{html_lib.escape(sp)}" target="_blank">{html_lib.escape(sp)}</a></p>

    <h2 style="font-size:17px;margin:22px 0 8px;">How it works</h2>
    <ol style="line-height:1.7;font-size:14px;">
      <li><strong>Daily monitor</strong> finds terminated accounts with/without email forwarding.</li>
      <li><strong>Manager emails</strong> warn before expiry (managers from Graph; not env vars).</li>
      <li><strong>IT approval</strong> Approve/Decline links update CSA after SD+ ticket.</li>
      <li><strong>Weekly (Mon) + monthly</strong> reports go to REPORT_EMAILS and upload to SharePoint.</li>
      <li><strong>Soft-delete</strong> at policy end (recycle bin ~30 days) — admins see technical detail.</li>
    </ol>

    <h2 style="font-size:17px;margin:22px 0 8px;">Configured mailboxes (env)</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:18px;">{rows}</table>

    <h2 style="font-size:17px;margin:22px 0 8px;">Run a demo step</h2>
    <p style="font-size:13px;color:#666;">All demo mail goes to <strong>{html_lib.escape(get_test_recipient())}</strong>
    when EF_TEST_MODE=true.</p>
    {btn("emails", "1. Send sample emails", "#0078d4")}
    {btn("report", "2. Report + SharePoint", "#28a745")}
    {btn("approval", "3. Live Approve/Decline links", "#fd7e14")}
    {btn("full", "Run full demo (1→2→3)", "#6f42c1")}
    <p style="margin-top:20px;font-size:12px;color:#888;">
      Keep this URL private — it includes your function key.
      Open from: <code>/api/demo?code=…</code>
    </p>
  </div>
</body></html>"""


def _run_emails() -> List[str]:
    lines: List[str] = []
    steps = [
        ("alert (first)", lambda: send_ef_alert(_demo_record(), 7, False)),
        ("alert (final)", lambda: send_ef_alert(
            _demo_record(extensionCount=2, statusCode="EXTENDED_MAX"), 7, True)),
        ("extension confirm", lambda: send_extension_confirm(
            _demo_record(extensionCount=1, statusCode="EXTENDED"))),
        ("EF removed", lambda: send_ef_removed_notice(
            _demo_record(statusCode="EF_DISABLED"), 16, "NO_EXTENSION_DAY30")),
        ("deletion (manager + admin soft-delete)", lambda: send_deletion_notice(
            _demo_record(statusCode="DELETED"), "NO_EXTENSION_DAY30")),
        ("final deletion", lambda: send_final_deletion_notice(
            _demo_record(extensionCount=2, statusCode="DELETED"))),
    ]
    for name, fn in steps:
        ok = bool(fn())
        lines.append(f"{'OK' if ok else 'FAIL'}: {name}")
    return lines


def _run_report() -> List[str]:
    today = date.today()
    n = send_offboard_consolidated_report(
        report_date=today.isoformat(),
        new_no_ef=[{
            "displayName": "Demo NO_EF User",
            "userEmail": "demo.noef@netradyne.com",
            "offboardDate": (today - timedelta(days=10)).isoformat(),
            "usageLocation": "IN",
            "managerEmail": "mgr.demo@netradyne.com",
        }],
        new_with_ef=[{
            "displayName": "Demo EF User",
            "userEmail": "demo.ef@netradyne.com",
            "offboardDate": (today - timedelta(days=20)).isoformat(),
            "usageLocation": "IN",
            "managerEmail": get_test_recipient(),
            "forwardingAddress": "manager.fwd@netradyne.com",
        }],
        overdue_no_ef=[],
        summary={"alerts": 1, "extensions": 0, "deletions": 0, "total_active": 12},
        period_label="Weekly",
        period_start=(today - timedelta(days=7)).isoformat(),
    )
    return [
        f"{'OK' if n else 'FAIL'}: weekly-style report emailed ({n} recipient(s))",
        f"SharePoint folder: {get_sharepoint_report_url() or '(not set)'}",
        "Check inbox + SharePoint folder for EF_Weekly_Report_*.xlsx / *.csv "
        "(needs Sites.ReadWrite.All if upload fails).",
    ]


def _run_approval() -> List[str]:
    from .approval_webhook import generate_approval_urls

    token, approve, decline = generate_approval_urls("demo-user-001", 0)
    ok = send_it_approval_notification(
        _demo_record(),
        approve_url=approve,
        decline_url=decline,
        ext_type="EXTEND_TO_30",
    )
    return [
        f"{'OK' if ok else 'FAIL'}: IT approval email",
        f"Approve: {approve}",
        f"Decline: {decline}",
        f"Token: {token[:8]}… (one-time; expires per APPROVAL_TOKEN_DAYS)",
    ]


def handle_demo(action: str, function_code: str = "") -> Tuple[str, int]:
    """
    Returns (html_body, status_code).
    """
    log_active_gates("demo")
    action = (action or "").strip().lower() or "home"

    if action in ("", "home", "overview"):
        return _landing_html(function_code), 200

    results: List[str] = []
    try:
        if action == "emails":
            results = _run_emails()
        elif action == "report":
            results = _run_report()
        elif action == "approval":
            results = _run_approval()
        elif action == "full":
            results = ["=== Emails ==="] + _run_emails()
            results += ["=== Report ==="] + _run_report()
            results += ["=== Approval ==="] + _run_approval()
        else:
            return _landing_html(
                function_code,
                flash=f"Unknown action <code>{html_lib.escape(action)}</code>",
            ), 400
    except Exception as exc:
        logger.exception("Demo action %s failed", action)
        return _landing_html(
            function_code,
            flash=f"<strong>Error:</strong> {html_lib.escape(str(exc))}",
        ), 500

    flash = (
        f"<strong>Action <code>{html_lib.escape(action)}</code> finished.</strong>"
        "<ul style='margin:8px 0 0;padding-left:20px;'>"
        + "".join(f"<li style='word-break:break-all;'>{html_lib.escape(r)}</li>" for r in results)
        + "</ul>"
        f"<p style='margin:10px 0 0;'>Check <strong>{html_lib.escape(get_test_recipient())}</strong>.</p>"
    )
    return _landing_html(function_code, flash=flash), 200
