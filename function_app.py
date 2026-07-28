"""
function_app.py
---------------
Azure Functions v2 Python programming model entry point.

Registers three triggers (Workflow B – attribute-based approval):
  1. monitor_accounts  – Timer trigger, daily at 09:00 UTC
  2. weekly_report     – Timer trigger, every Friday (configurable via
                          WEEKLY_REPORT_SCHEDULE app setting)
  3. monthly_cleanup   – Timer trigger, 1st of every month (configurable via
                          CLEANUP_SCHEDULE app setting); safety-net deletion
                          of stale NO_EF accounts missed during manual offboarding.

Note: The reply_webhook HTTP trigger (Workflow A) has been removed.
Extensions are driven by IT setting the CSA ExtStatus attribute in Azure AD
after HR and Infosec approve the manager's ServiceDesk ticket.
"""

import json
import logging

import azure.functions as func

from utils.monitor_accounts  import run_monitor
from utils.weekly_report     import run_weekly_report
from utils.cleanup_scan      import run_cleanup_scan
from utils.approval_webhook  import handle_get, handle_post

logger = logging.getLogger(__name__)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# ---------------------------------------------------------------------------
# Trigger 1: Daily account monitoring (Timer)
# ---------------------------------------------------------------------------
# NCRONTAB format: {second} {minute} {hour} {day} {month} {weekday}
# "0 0 9 * * *"  →  every day at 09:00:00 UTC
# ---------------------------------------------------------------------------

@app.timer_trigger(
    arg_name="timer",
    schedule="0 0 9 * * *",
    run_on_startup=False,    # set True temporarily during local testing
    use_monitor=True,        # Azure Functions will track missed runs
)
def monitor_accounts(timer: func.TimerRequest) -> None:
    """
    Daily job: scan Azure AD for terminated accounts, send EF alerts,
    and delete accounts on schedule.
    """
    if timer.past_due:
        logger.warning("Timer trigger is past due – running catch-up")

    logger.info("monitor_accounts trigger fired")

    try:
        summary = run_monitor()
        logger.info("monitor_accounts completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("monitor_accounts failed with unhandled exception: %s", exc, exc_info=True)
        raise   # re-raise so Azure Functions marks the invocation as failed


# ---------------------------------------------------------------------------
# Trigger 2: Weekly admin report (Timer – every Friday)
# ---------------------------------------------------------------------------
# Default: 0 30 8 * * 5  →  Every Friday at 08:30 UTC (2:00 PM IST)
# Override by setting the WEEKLY_REPORT_SCHEDULE app setting, e.g.:
#   "0 30 8 * * 5"  (Friday 08:30 UTC)
#   "0 0 7 * * 1"   (Monday 07:00 UTC)
# ---------------------------------------------------------------------------

@app.timer_trigger(
    arg_name="weekly_timer",
    schedule="%WEEKLY_REPORT_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
def weekly_report(weekly_timer: func.TimerRequest) -> None:
    """
    Weekly Friday job: generate and email an EF Automation summary report
    to all addresses listed in the ADMIN_EMAILS app setting.
    """
    if weekly_timer.past_due:
        logger.warning("Weekly report trigger is past due – running now")

    logger.info("weekly_report trigger fired")

    try:
        summary = run_weekly_report()
        logger.info("weekly_report completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("weekly_report failed with unhandled exception: %s", exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Trigger 3: Monthly cleanup scan (Timer – 1st of every month)
# ---------------------------------------------------------------------------
# Default: 0 0 6 1 * *  →  1st of every month at 06:00 UTC (11:30 AM IST)
# Override via CLEANUP_SCHEDULE app setting.
# Deletes stale NO_EF accounts (India region) that IT missed during manual
# offboarding, and Azure AD accounts that slipped past the daily monitor.
# ---------------------------------------------------------------------------

@app.timer_trigger(
    arg_name="cleanup_timer",
    schedule="%CLEANUP_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
def monthly_cleanup(cleanup_timer: func.TimerRequest) -> None:
    """
    Monthly job: find and delete stale terminated accounts (no EF, India)
    that were not deleted during manual offboarding.
    """
    if cleanup_timer.past_due:
        logger.warning("Monthly cleanup trigger is past due – running now")

    logger.info("monthly_cleanup trigger fired")

    try:
        summary = run_cleanup_scan()
        logger.info("monthly_cleanup completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("monthly_cleanup failed with unhandled exception: %s", exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Trigger 4: EF Extension Approve / Decline (HTTP)
# ---------------------------------------------------------------------------
# Called when IT clicks Approve or Decline in the alert notification email.
# GET  ?token=xxx&action=approve → shows form to enter SD+ ticket number
# GET  ?token=xxx&action=decline → logs decline, shows confirmation
# POST with form body (token + ticket_ref) → sets CSA, applies extension
# ---------------------------------------------------------------------------

@app.route(route="ef_approval", methods=["GET", "POST"])
def ef_approval(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP endpoint for IT to approve or decline an EF extension via email link."""
    try:
        if req.method == "GET":
            token  = req.params.get("token", "")
            action = req.params.get("action", "")
            if not token or not action:
                return func.HttpResponse(
                    "<h2>Missing parameters.</h2>", status_code=400, mimetype="text/html"
                )
            html, status = handle_get(token, action)
            return func.HttpResponse(html, status_code=status, mimetype="text/html")

        elif req.method == "POST":
            token      = req.form.get("token", "") or req.params.get("token", "")
            ticket_ref = req.form.get("ticket_ref", "")
            if not token:
                return func.HttpResponse(
                    "<h2>Missing token.</h2>", status_code=400, mimetype="text/html"
                )
            html, status = handle_post(token, ticket_ref)
            return func.HttpResponse(html, status_code=status, mimetype="text/html")

        return func.HttpResponse("<h2>Method not allowed.</h2>", status_code=405, mimetype="text/html")

    except Exception as exc:
        logger.critical("ef_approval failed: %s", exc, exc_info=True)
        return func.HttpResponse(
            "<h2>Internal error. Please contact IT administration.</h2>",
            status_code=500, mimetype="text/html",
        )
