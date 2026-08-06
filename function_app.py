"""
function_app.py
---------------
Azure Functions v2 Python programming model entry point.

Triggers:
  1. monitor_accounts  – Timer, daily 09:00 UTC (alerts / EF / deletes; no daily report email)
  2. weekly_report     – Timer, every Monday (WEEKLY_REPORT_SCHEDULE)
  3. monthly_report    – Timer, 1st of month (MONTHLY_REPORT_SCHEDULE)
  4. monthly_cleanup   – Timer, 1st of month (CLEANUP_SCHEDULE) – NO_EF safety-net deletes
  5. ef_approval       – HTTP Approve/Decline
"""

import json
import logging

import azure.functions as func

from utils.monitor_accounts  import run_monitor
from utils.weekly_report     import run_weekly_report, run_monthly_report
from utils.cleanup_scan      import run_cleanup_scan
from utils.approval_webhook  import handle_get, handle_post

logger = logging.getLogger(__name__)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


@app.timer_trigger(
    arg_name="timer",
    schedule="0 0 9 * * *",
    run_on_startup=False,
    use_monitor=True,
)
def monitor_accounts(timer: func.TimerRequest) -> None:
    """Daily job: scan Azure AD, send EF alerts, remove EF / delete on schedule."""
    if timer.past_due:
        logger.warning("Timer trigger is past due – running catch-up")

    logger.info("monitor_accounts trigger fired")
    try:
        summary = run_monitor()
        logger.info("monitor_accounts completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("monitor_accounts failed with unhandled exception: %s", exc, exc_info=True)
        raise


# Default: every Monday 08:30 UTC (2:00 PM IST)
@app.timer_trigger(
    arg_name="weekly_timer",
    schedule="%WEEKLY_REPORT_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
def weekly_report(weekly_timer: func.TimerRequest) -> None:
    """Weekly Monday consolidated offboard / EF report to REPORT_EMAILS."""
    if weekly_timer.past_due:
        logger.warning("Weekly report trigger is past due – running now")

    logger.info("weekly_report trigger fired")
    try:
        summary = run_weekly_report()
        logger.info("weekly_report completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("weekly_report failed with unhandled exception: %s", exc, exc_info=True)
        raise


# Default: 1st of month 07:30 UTC
@app.timer_trigger(
    arg_name="monthly_report_timer",
    schedule="%MONTHLY_REPORT_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
def monthly_report(monthly_report_timer: func.TimerRequest) -> None:
    """Monthly consolidated offboard / EF report to REPORT_EMAILS."""
    if monthly_report_timer.past_due:
        logger.warning("Monthly report trigger is past due – running now")

    logger.info("monthly_report trigger fired")
    try:
        summary = run_monthly_report()
        logger.info("monthly_report completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("monthly_report failed with unhandled exception: %s", exc, exc_info=True)
        raise


@app.timer_trigger(
    arg_name="cleanup_timer",
    schedule="%CLEANUP_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
def monthly_cleanup(cleanup_timer: func.TimerRequest) -> None:
    """Monthly safety-net deletion of stale NO_EF India accounts."""
    if cleanup_timer.past_due:
        logger.warning("Monthly cleanup trigger is past due – running now")

    logger.info("monthly_cleanup trigger fired")
    try:
        summary = run_cleanup_scan()
        logger.info("monthly_cleanup completed: %s", json.dumps(summary))
    except Exception as exc:
        logger.critical("monthly_cleanup failed with unhandled exception: %s", exc, exc_info=True)
        raise


@app.route(
    route="ef_approval",
    methods=["GET", "POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def ef_approval(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP endpoint for IT to approve or decline an EF extension via email link.

    Anonymous so managers/IT can open the link from email without a function key.
    Security is the one-time ApprovalTokens token (not Function auth).
    """
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
