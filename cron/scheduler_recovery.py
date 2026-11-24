"""Settle job leases from durable execution receipts without running job effects."""

import logging
import sqlite3

logger = logging.getLogger(__name__)


def settle_terminal_fire_claims() -> None:
    """Reconcile the exact lease still on disk after an interrupted scheduler."""
    from cron.cancellation import clear_cancel
    from cron.executions import get_execution
    from cron.jobs import load_jobs, mark_job_run
    from cron.scheduler_authority import journal_path
    from cron.unreachable_retry import is_retry_run

    for job in load_jobs():
        claim = job.get("fire_claim")
        if not isinstance(claim, dict):
            continue
        execution_id, owner = claim.get("execution_id"), claim.get("by")
        if not execution_id or not owner:
            continue
        try:
            # Agent admission journals can still resolve an unknown remote outcome.
            if journal_path(job["id"], execution_id).exists():
                continue
            execution = get_execution(execution_id)
            if not execution or execution["job_id"] != job["id"]:
                continue
            status = execution["status"]
            if status not in {"completed", "failed", "cancelled", "unknown"}:
                continue
            if mark_job_run(
                job["id"], status == "completed", execution.get("error"),
                status=status if status in {"cancelled", "unknown"} else None,
                expected_fire_owner=owner, ladder_rung=is_retry_run(job),
            ):
                clear_cancel(execution_id)
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("Could not settle cron receipt %s", execution_id, exc_info=True)
