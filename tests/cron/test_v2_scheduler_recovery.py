"""V2-03 contracts against isolated native job and execution stores."""

from datetime import timedelta
import os
import sqlite3
import subprocess
import sys

import pytest

from cron import executions, jobs, scheduler
from cron.cancellation import cancel_requested, request_cancel
from cron.scheduler_provider import InProcessCronScheduler
from cron.scheduler_tick import _pre_dispatch_housekeeping


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    home.mkdir()
    state.mkdir()
    (state / "scripts").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    monkeypatch.setenv("TMPDIR", str(state))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    # This fixture is an unpackaged install. Keep the child-environment
    # sanitizer intact while preventing a probe of the real install manifest.
    monkeypatch.setattr("pm.environments._payload_manifest", lambda _root: None)
    return state


def oneshot(state, *, recurring=False):
    script = state / "scripts" / "finite.py"
    script.write_text(
        "from pathlib import Path\n"
        "with Path('ran-once').open('x') as marker:\n"
        "    marker.write('one durable result')\n"
        "print('one durable result')\n",
        encoding="utf-8",
    )
    job = jobs.create_job(
        prompt=None, schedule="every 5m" if recurring else "in 5m",
        repeat=None if recurring else 1, script=script.name, no_agent=True,
        deliver="local", workdir=str(state), interpreter=sys.executable,
    )
    rows = jobs.load_jobs()
    rows[0]["next_run_at"] = (jobs._hermes_now() - timedelta(seconds=1)).isoformat()
    jobs.save_jobs(rows)
    return job["id"], rows[0]["next_run_at"]


def fresh_python(code, job_id):
    result = subprocess.run(
        [sys.executable, "-c", code, job_id], env=os.environ.copy(),
        capture_output=True, text=True, timeout=40, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_finite_script_writes_one_result_and_survives_restart(state):
    job_id, _ = oneshot(state)
    provider = InProcessCronScheduler()
    claimed = provider.claim_fire(job_id)
    fresh_python(
        "import sys\n"
        "from cron import executions, jobs, scheduler\n"
        "from cron.scheduler_tick import _pre_dispatch_housekeeping\n"
        "_pre_dispatch_housekeeping(scheduler, headless=True)\n"
        "job_id = sys.argv[1]\n"
        "assert jobs.get_job(job_id)['fire_claim']\n"
        "assert not jobs.get_due_jobs()\n"
        "rows = executions.list_executions(job_id=job_id)\n"
        "assert len(rows) == 1 and rows[0]['status'] == 'claimed'\n",
        job_id,
    )
    assert provider.fire_claimed(claimed)
    stored = jobs.get_job(job_id)
    assert stored["last_status"] == "ok", stored.get("last_error")
    assert not stored.get("run_claim") and not stored.get("fire_claim")
    assert (state / "ran-once").read_text() == "one durable result"
    files = list((state / "cron" / "output" / job_id).glob("*.md"))
    assert len(files) == 1
    assert "one durable result" in files[0].read_text()
    fresh_python(
        "import sys\n"
        "from cron import executions, jobs\n"
        "from cron.scheduler_recovery import settle_terminal_fire_claims\n"
        "settle_terminal_fire_claims()\n"
        "job_id = sys.argv[1]\n"
        "assert jobs.get_job(job_id)['last_status'] == 'ok'\n"
        "assert not jobs.get_due_jobs()\n"
        "rows = executions.list_executions(job_id=job_id)\n"
        "assert len(rows) == 1 and rows[0]['status'] == 'completed'\n",
        job_id,
    )


def test_restart_settles_unknown_claim_and_cancellation_marker_without_replay(state):
    job_id, _ = oneshot(state)
    fresh_python(
        "import sys\n"
        "from cron import executions, jobs\n"
        "from cron.cancellation import request_cancel\n"
        "from cron.scheduler_provider import InProcessCronScheduler\n"
        "claimed = InProcessCronScheduler().claim_fire(sys.argv[1])\n"
        "assert jobs.claim_dispatch(sys.argv[1])\n"
        "assert executions.mark_execution_running(claimed['execution_id'])\n"
        "request_cancel(claimed['execution_id'], reason='fixture cancellation')\n",
        job_id,
    )
    execution_id = jobs.get_job(job_id)["fire_claim"]["execution_id"]
    assert cancel_requested(execution_id)
    _pre_dispatch_housekeeping(scheduler, headless=True)
    row = executions.get_execution(execution_id)
    assert row["status"] == "unknown" and not row["handoff_pending"]
    stored = jobs.get_job(job_id)
    assert stored["last_status"] == "unknown"
    assert not stored.get("run_claim") and not stored.get("fire_claim")
    assert not cancel_requested(execution_id)
    assert not jobs.get_due_jobs()
    assert not (state / "ran-once").exists()


def test_cancelled_native_fire_settles_both_leases(state):
    job_id, _ = oneshot(state)
    assert [job["id"] for job in jobs.get_due_jobs()] == [job_id]
    provider = InProcessCronScheduler()
    claimed = provider.claim_fire(job_id)
    execution_id = claimed["execution_id"]
    request_cancel(execution_id, reason="cancel the finite fixture")
    assert provider.fire_claimed(claimed)
    stored = jobs.get_job(job_id)
    assert stored["last_status"] == "cancelled"
    assert not stored.get("run_claim") and not stored.get("fire_claim")
    assert executions.get_execution(execution_id)["status"] == "cancelled"
    assert not cancel_requested(execution_id)
    assert not (state / "ran-once").exists()


def test_ledger_read_error_holds_due_slot_and_recovers_within_grace(state, monkeypatch):
    job_id, slot = oneshot(state)
    with monkeypatch.context() as outage:
        def unavailable():
            raise sqlite3.OperationalError("fixture occurrence ledger unavailable")

        outage.setattr(executions, "_transaction", unavailable)
        assert not jobs.get_due_jobs()
        assert not jobs.get_job(job_id).get("run_claim")
        assert not jobs.claim_job_for_fire(job_id)
    assert jobs.get_job(job_id)["next_run_at"] == slot
    assert InProcessCronScheduler().fire_due(job_id)
    assert jobs.get_job(job_id)["last_status"] == "ok"


def test_expired_one_shot_is_not_revived_after_ledger_recovers(state, monkeypatch):
    job_id, _ = oneshot(state)
    rows = jobs.load_jobs()
    old = (jobs._hermes_now() - timedelta(hours=1)).isoformat()
    rows[0].update(next_run_at=old, run_claim={"at": old, "by": "fixture-dead-owner"})
    jobs.save_jobs(rows)
    with monkeypatch.context() as outage:
        def unavailable():
            raise sqlite3.OperationalError("fixture occurrence ledger unavailable")

        outage.setattr(executions, "_transaction", unavailable)
        assert not jobs.get_due_jobs()
    fresh_python(
        "import sys\n"
        "from cron import executions, jobs\n"
        "assert not jobs.claim_job_for_fire(sys.argv[1])\n"
        "assert not jobs.get_due_jobs()\n"
        "assert [row['status'] for row in executions.list_executions(job_id=sys.argv[1])] == ['missed']\n",
        job_id,
    )
    assert not (state / "ran-once").exists()


def test_tick_claim_checks_original_occurrence_after_recurring_advance(state):
    job_id, slot = oneshot(state, recurring=True)
    due, = jobs.get_due_jobs()
    jobs.advance_next_runs([job_id])
    winning = executions.create_execution(job_id, source="fixture", scheduled_instant=slot)
    executions.mark_execution_running(winning["id"])
    executions.finish_execution(winning["id"], success=True)
    queued = executions.create_execution(job_id, source="builtin", scheduled_instant=slot)
    due["execution_id"] = queued["id"]
    scheduler._process_due_job(due, None, None, False)
    assert executions.get_execution(queued["id"])["started_at"] is None
    assert not jobs.get_job(job_id).get("pending_slot")
    assert not (state / "ran-once").exists()


def test_outage_cannot_replay_completed_occurrence(state, monkeypatch):
    job_id, slot = oneshot(state, recurring=True)
    row = executions.create_execution(job_id, source="fixture", scheduled_instant=slot)
    assert executions.mark_execution_running(row["id"])
    assert executions.finish_execution(row["id"], success=True)

    def unavailable():
        raise sqlite3.OperationalError("fixture occurrence ledger unavailable")

    with monkeypatch.context() as outage:
        outage.setattr(executions, "_transaction", unavailable)
        assert not jobs.get_due_jobs()
        assert not jobs.claim_job_for_fire(job_id)
        assert jobs.get_job(job_id)["next_run_at"] == slot

    assert not jobs.get_due_jobs()
    assert jobs.get_job(job_id)["next_run_at"] != slot
    assert not (state / "ran-once").exists()


def test_claim_time_outage_releases_only_unstarted_scan_lease(state, monkeypatch):
    job_id, slot = oneshot(state)
    due, = jobs.get_due_jobs()
    queued = executions.create_execution(job_id, source="builtin", scheduled_instant=slot)
    due["execution_id"] = queued["id"]
    transaction = executions._transaction

    def fail_lookup_once():
        monkeypatch.setattr(executions, "_transaction", transaction)
        raise sqlite3.OperationalError("fixture claim-time ledger lookup failed")

    monkeypatch.setattr(executions, "_transaction", fail_lookup_once)
    scheduler._process_due_job(due, None, None, False)
    row = executions.get_execution(queued["id"])
    assert row["status"] == "failed" and row["started_at"] is None
    assert not jobs.get_job(job_id).get("run_claim")
    assert not (state / "ran-once").exists()
    assert [job["id"] for job in jobs.get_due_jobs()] == [job_id]
