"""
function_app.py
---------------
Azure Functions v2 Python programming model entry point.

Registers one trigger (Workflow B – attribute-based approval):
  1. monitor_accounts  – Timer trigger, daily at 09:00 UTC

Note: The reply_webhook HTTP trigger (Workflow A) has been removed.
Extensions are now driven by IT setting extensionAttribute11 in Azure AD
after HR and Infosec approve the manager's ServiceDesk ticket.
"""

import json
import logging

import azure.functions as func

from utils.monitor_accounts import run_monitor

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
