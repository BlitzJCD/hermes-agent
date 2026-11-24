"""Exact scheduled identities, independent of mutable jobs.json dispatch stamps, plus the
profile-local stale-schedule catch-up counter marker."""
from datetime import datetime, timedelta, UTC
import logging

logger = logging.getLogger(__name__)


def scheduled_instant(value):
    """Canonicalize aware instants; legacy/ambiguous values carry no exact identity."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            return None
        return instant.astimezone(UTC).isoformat()
    except ValueError:
        return None


def occurrence_status(job, instant, *, execution_id=None):
    """Return clear, completed, spent, or held for an exact scheduled occurrence.

    An unavailable ledger cannot establish that a fire is new. A failed
    preparation that never started is eligible again within normal grace.
    """
    from cron.constants import FIRE_CLAIM_SKEW_SECONDS
    from cron.executions import _transaction

    instant = scheduled_instant(instant)
    if instant is None:
        return "clear"
    earliest = datetime.fromisoformat(instant) - timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS)
    try:
        with _transaction() as conn:
            rows = conn.execute(
                "SELECT id, status, started_at, finished_at, claimed_at FROM executions "
                "WHERE job_id=? AND scheduled_instant=? AND id IS NOT ?",
                (str(job["id"]), instant, execution_id),
            ).fetchall()
    except Exception:
        logger.warning("Cannot verify occurrence for job %s; deferring fire", job["id"], exc_info=True)
        return "held"
    disposition = "clear"
    for row in rows:
        status = row["status"]
        if status == "completed":
            completed_at = scheduled_instant(row["finished_at"] or row["claimed_at"])
            if completed_at is None or datetime.fromisoformat(completed_at) >= earliest:
                logger.warning(
                    "Job '%s' (%s): scheduled occurrence %s was already completed by execution "
                    "%s (finished %s); skipping the due slot without a new run",
                    job.get("name", job["id"]), job["id"], instant, row["id"],
                    row["finished_at"] or row["claimed_at"],
                )
                return "completed"
        elif status in {"unknown", "cancelled"} or (status == "failed" and row["started_at"]):
            disposition = "spent"
        elif status in {"claimed", "running"} and disposition == "clear":
            disposition = "held"
    return disposition


def completed_occurrence(job, instant):
    """Whether the ledger contains proof of successful completion."""
    return occurrence_status(job, instant) == "completed"


def resolve_fire_occurrence(job, now, *, manual, expected):
    """Match the dispatch snapshot to its stored slot before issuing a lease."""
    from cron.constants import FIRE_CLAIM_SKEW_SECONDS
    from cron.missed_runs import oneshot_grace_expired

    if manual:
        return expected is None, None
    kind = job.get("schedule", {}).get("kind")
    pending = job.get("pending_slot")
    stored = job.get("next_run_at")
    if kind in {"cron", "interval"} and isinstance(pending, dict):
        stored = pending.get("scheduled_at")
    instant = scheduled_instant(stored)
    if expected is not None and expected != instant:
        return False, None
    if instant is None:
        return True, None  # legacy wall-clock records have no exact ledger identity
    due = datetime.fromisoformat(instant)
    if due - now >= timedelta(seconds=FIRE_CLAIM_SKEW_SECONDS):
        # Off-tick fire before the next slot. It does not consume that slot.
        return expected is None, None
    if kind == "once" and oneshot_grace_expired(job, due, now):
        return False, None
    return True, instant


# --- Pending slot: the occurrence a tick took off the schedule but has not yet claimed ---
#
# The tick advances a recurring job's ``next_run_at`` BEFORE dispatch (at-most-once: a crash
# mid-run must not re-fire on every restart). That leaves a window — advance done, fire claim
# not yet taken (interpreter finalizing, executor refusing work, process killed) — in which the
# process exiting loses the occurrence silently: the restarted scan sees a future ``next_run_at``
# and nothing ever ran (#107485). ``pending_slot`` is the durable record of that window: the due
# scan stamps it with the exact stored instant plus the stamping owner, ``claim_job_for_fire``
# (the point after which side effects may exist) clears it, and any explicit rewrite of the
# schedule drops it. A slot still pending once its owner is provably gone (or its lease has
# expired) was never claimed, so it is restored ONCE as the due instant and then flows through
# the ordinary late / fast-forward / ``cron.catch_up_missed`` policy — never N replays.

def pending_slot_stamp(next_run, now):
    """Store value for a recurring occurrence about to be handed to the dispatcher."""
    from cron.jobs import _machine_id

    return {"scheduled_at": next_run, "at": now.isoformat(), "by": _machine_id()}


def unclaimed_pending_slot(job, now):
    """Stored instant of a slot the dispatcher never claimed, or None.

    None for non-recurring jobs and malformed stamps (never a fire) and for a job still in this
    process's running set (its queued worker will claim and clear the slot itself). A stamp by
    THIS process on a job not running here is orphaned (dispatch refused). A stamp by another
    process is honoured while that owner may still be alive within the fire-claim lease — a
    second live gateway on the same store is mid-dispatch, not dead."""
    from cron.constants import FIRE_CLAIM_TTL_SECONDS
    from cron.jobs import _claim_is_live, _job_running_in_this_process, _machine_id

    pending = job.get("pending_slot")
    if not isinstance(pending, dict):
        return None
    slot = pending.get("scheduled_at")
    if job.get("schedule", {}).get("kind") not in {"cron", "interval"} or not isinstance(slot, str):
        return None
    try:
        datetime.fromisoformat(slot)
    except ValueError:
        return None
    if _job_running_in_this_process(str(job.get("id", ""))):
        return None
    if pending.get("by") != _machine_id() and _claim_is_live(pending, now, FIRE_CLAIM_TTL_SECONDS):
        return None
    return slot


def get_catch_up_occurrence_count() -> int:
    """Return the profile-local stale-schedule catch-up count."""
    from cron.jobs import _current_cron_store

    path = _current_cron_store().cron_dir / "catch_up_occurrences"
    try:
        return max(0, int(path.read_text(encoding="utf-8-sig").strip()))
    except (OSError, ValueError):
        return 0


def record_catch_up_occurrence() -> None:
    """Increment the profile-local stale-schedule catch-up counter, best effort."""
    from cron.jobs import _write_marker

    _write_marker("catch_up_occurrences", str(get_catch_up_occurrence_count() + 1), ".count_")
