"""Production claim and owner receipt contracts on a disposable board."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    return state / "kanban.db"


def create(conn):
    return kb.create_task(conn, title="Dispatch admission fixture", assignee="default")


def fail_spawn(conn, task_id):
    dispatch._record_task_failure(
        conn, task_id, "temporary supervisor failure", outcome="spawn_failed",
        release_claim=True, end_run=True, infrastructure=True,
    )


def receipt(conn, task_id, run_id):
    with kb.write_txn(conn):
        kb._append_event(
            conn, task_id, "owner_admitted", {"session_id": f"session-{run_id}"},
            run_id=run_id,
        )


def test_concurrent_automatic_and_manual_claims_create_one_run(board):
    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
    start = Barrier(2)

    def claim(manual):
        with kbc.connect_closing(board) as conn:
            start.wait(timeout=10)
            return kb.claim_task(conn, task_id, manual=manual)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (False, True)))
    winner, = [task for task in results if task is not None]
    with kbc.connect_closing(board) as conn:
        assert [run.id for run in kb.list_runs(conn, task_id)] == [winner.current_run_id]
        receipt(conn, task_id, winner.current_run_id)
        with pytest.raises(sqlite3.IntegrityError, match="new current claim"):
            receipt(conn, task_id, winner.current_run_id)


def test_attempt_ceiling_survives_restart_and_operator_restarts_budget(board):
    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
    prior_ids = []
    for _ in range(3):
        with kbc.connect_closing(board) as conn:
            prior_ids.append(kb.claim_task(conn, task_id).current_run_id)
            fail_spawn(conn, task_id)
    restart = subprocess.run(
        [
            sys.executable, "-c",
            "from pathlib import Path; import sys\n"
            "from hermes_cli import kanban_db as kb, kanban_db_connect as kbc\n"
            "with kbc.connect_closing(Path(sys.argv[1])) as conn:\n"
            "    assert kb.claim_task(conn, sys.argv[2]) is None\n",
            str(board), task_id,
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert restart.returncode == 0, restart.stderr
    with kbc.connect_closing(board) as conn:
        assert dispatch.check_respawn_guard(conn, task_id) == "attempt_ceiling"
        assert kb.claim_task(conn, task_id) is None
        operator = kb.claim_task(conn, task_id, manual=True)
        assert operator.current_run_id > max(prior_ids)
        fail_spawn(conn, task_id)
        next_attempt = kb.claim_task(conn, task_id)
        assert sorted(run.id for run in kb.list_runs(conn, task_id)) == [
            *prior_ids, operator.current_run_id, next_attempt.current_run_id,
        ]
        assert all(
            run.outcome == "spawn_failed" for run in kb.list_runs(conn, task_id)
            if run.id in prior_ids
        )


def test_uncertain_owner_receipt_holds_claim_but_unadmitted_expiry_retries(board):
    with kbc.connect_closing(board) as conn:
        uncertain = create(conn)
        first = kb.claim_task(conn, uncertain)
        receipt(conn, uncertain, first.current_run_id)
        abandoned = create(conn)
        other = kb.claim_task(conn, abandoned)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = 1 WHERE status = 'running'")
    with kbc.connect_closing(board) as conn:
        assert kb.release_stale_claims(conn, failure_limit=3) == 1
        assert kb.get_task(conn, uncertain).current_run_id == first.current_run_id
        assert kb.claim_task(conn, uncertain) is None
        assert not kb.reclaim_task(conn, uncertain)
        assert kb.claim_task(conn, abandoned).current_run_id != other.current_run_id


def test_terminal_run_cannot_be_automatically_reopened_or_reused(board):
    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
        first = kb.claim_task(conn, task_id)
        receipt(conn, task_id, first.current_run_id)
        assert kb.complete_task(conn, task_id, expected_run_id=first.current_run_id, result="Fixture finished")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, task_id) is None
        with pytest.raises(sqlite3.IntegrityError, match="cannot be revived"):
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET status = 'running', ended_at = NULL WHERE id = ?",
                    (first.current_run_id,),
                )
        new = kb.claim_task(conn, task_id, manual=True)
        assert new.current_run_id != first.current_run_id
        receipt(conn, task_id, new.current_run_id)
        assert {run.id: run.outcome for run in kb.list_runs(conn, task_id)} == {
            new.current_run_id: None,
            first.current_run_id: "completed",
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'owner_admitted'",
            (task_id,),
        ).fetchone()[0] == 2


def test_operator_unblock_authorizes_new_run_and_automatic_unblock_does_not(board):
    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
        first = kb.claim_task(conn, task_id)
        assert kb.block_task(conn, task_id, kind="needs_input", expected_run_id=first.current_run_id)
        assert kb.unblock_task(conn, task_id, operator=False)
        assert kb.claim_task(conn, task_id) is None
        assert kb.block_task(conn, task_id, kind="capability")
        assert kb.unblock_task(conn, task_id, operator=True)
        assert kb.claim_task(conn, task_id).current_run_id != first.current_run_id


def test_review_handoff_has_own_bounded_admissions(board):
    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
        implementation = kb.claim_task(conn, task_id)
        assert kb.request_review(conn, task_id, expected_run_id=implementation.current_run_id)
        for _ in range(3):
            assert kb.claim_review_task(conn, task_id)
            fail_spawn(conn, task_id)
        assert kb.claim_review_task(conn, task_id) is None


@pytest.mark.parametrize(
    ("launch_error", "attempts"),
    [(OSError("temporary worker launch failure"), 3),
     (ValueError("invalid worker configuration"), 1)],
)
def test_dispatcher_limits_launch_attempts(board, monkeypatch, launch_error, attempts):
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda _: True)
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 0)
    attempted = []

    def unavailable(task, workspace):
        attempted.append(task.current_run_id)
        raise launch_error

    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
        for _ in range(4):
            dispatch.dispatch_once(conn, spawn_fn=unavailable, failure_limit=10)
        assert [run.id for run in kb.list_runs(conn, task_id)] == attempted
        assert len(attempted) == attempts
        assert not dispatch.has_spawnable_ready(conn)


def test_closed_claim_with_owner_marker_cannot_start_uncommitted_work(board, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    from gateway.session_kanban import run_task
    from hermes_state_runtime import RuntimeStoreError

    with kbc.connect_closing(board) as conn:
        task_id = create(conn)
        claimed = kb.claim_task(conn, task_id)
        receipt(conn, task_id, claimed.current_run_id)
        assert kb.complete_task(conn, task_id, expected_run_id=claimed.current_run_id, result="Fixture finished")

    # Even a stale task pointer and lock cannot make the closed run live.
    with kbc.connect_closing(board) as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = ?, claim_lock = ? WHERE id = ?",
            (claimed.current_run_id, claimed.claim_lock, task_id),
        )

    params = {
        "board": "default", "task_id": task_id, "run_id": claimed.current_run_id,
        "claim_lock": claimed.claim_lock, "db": str(board),
    }
    ref = SimpleNamespace(session_id=f"session-{claimed.current_run_id}")
    policy = SimpleNamespace(kanban_json=json.dumps(params))
    monkeypatch.setattr("hermes_state_local.local_receipt", lambda *_: {"policy": {}})
    monkeypatch.setattr("hermes_state_runtime.find_session_admission", lambda *_, **__: None)
    monkeypatch.setattr("gateway.session_policy.restore_policy", lambda _: policy)
    monkeypatch.setattr("gateway.session_local_recovery.restore_local_session", lambda *_: ref)

    async def unexpected(*args, **kwargs):
        pytest.fail("a closed claim without committed work cannot resume or submit")

    db = SimpleNamespace(get_session=lambda _: object(), db_path=board.parent / "owner.db")
    authority = SimpleNamespace(db=db, profile_id="default", submit=unexpected)
    actor = SimpleNamespace(subject="operator", capabilities={"session:create"})
    connection = SimpleNamespace(native_owner=True, resume=unexpected, actor=actor, authority=authority)
    with pytest.raises(RuntimeStoreError, match="stale_kanban_claim"):
        asyncio.run(run_task(connection, params))
