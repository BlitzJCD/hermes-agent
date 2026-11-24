"""Durable admission policy shared by implementation and review claims."""

import sqlite3

# Explicit operator reruns and workflow handoffs start a new retry budget.
MAX_DISPATCH_ATTEMPTS = 3

# Install after the legacy run and event schemas have been migrated.
ADMISSION_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS kanban_no_run_revival
BEFORE UPDATE OF status, ended_at ON task_runs
WHEN NEW.status = 'running' AND (OLD.status != 'running' OR OLD.ended_at IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'kanban terminal run cannot be revived');
END;

CREATE TRIGGER IF NOT EXISTS kanban_run_identity_update
BEFORE UPDATE OF id, task_id ON task_runs
WHEN NEW.id IS NOT OLD.id OR NEW.task_id IS NOT OLD.task_id
BEGIN
    SELECT RAISE(ABORT, 'kanban run identity cannot be changed');
END;

CREATE TRIGGER IF NOT EXISTS kanban_run_identity_insert
BEFORE INSERT ON task_runs
WHEN EXISTS (SELECT 1 FROM task_runs WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'kanban run identity already exists');
END;

CREATE TRIGGER IF NOT EXISTS kanban_owner_admission_insert
BEFORE INSERT ON task_events
WHEN EXISTS (SELECT 1 FROM task_events WHERE id = NEW.id AND kind = 'owner_admitted')
  OR (NEW.kind = 'owner_admitted' AND (
      EXISTS (SELECT 1 FROM task_events WHERE run_id = NEW.run_id AND kind = 'owner_admitted')
      OR NOT EXISTS (
          SELECT 1 FROM tasks AS t JOIN task_runs AS r ON r.id = t.current_run_id
          WHERE t.id = NEW.task_id AND r.task_id = t.id AND r.id = NEW.run_id
            AND t.status = 'running' AND r.status = 'running' AND r.ended_at IS NULL
            AND t.claim_lock IS NOT NULL AND t.claim_lock = r.claim_lock
      )
  ))
BEGIN
    SELECT RAISE(ABORT, 'kanban owner admission requires a new current claim');
END;

CREATE TRIGGER IF NOT EXISTS kanban_owner_admission_update
BEFORE UPDATE OF id, task_id, run_id, kind, payload ON task_events
WHEN OLD.kind = 'owner_admitted' OR NEW.kind = 'owner_admitted'
BEGIN
    SELECT RAISE(ABORT, 'kanban owner admission cannot be changed');
END;
"""


def authorize_dispatch(
    conn: sqlite3.Connection, task_id: str, reason: str, *, lane: str | None = None,
) -> None:
    """Record an operator rerun boundary in the same transaction as the action."""
    from hermes_cli import kanban_db as kb

    previous = conn.execute(
        "SELECT MAX(id) FROM task_runs WHERE task_id = ?", (task_id,),
    ).fetchone()[0]
    kb._append_event(conn, task_id, "dispatch_authorized", {"reason": reason, "lane": lane}, run_id=previous)


def dispatch_block_reason(
    conn: sqlite3.Connection, task_id: str, lane: str, *, manual: bool = False,
) -> str | None:
    """Read the ledger under the claim writer lock, before creating the next run."""
    return conn.execute(
        """
        WITH boundary AS (
            SELECT id, COALESCE(run_id, 0) AS run_id
              FROM task_events
             WHERE task_id = :task AND (
                 (kind = 'dispatch_authorized'
                     AND COALESCE(CASE WHEN json_valid(payload)
                                       THEN json_extract(payload, '$.lane') END, :lane) = :lane)
                 OR (:lane = 'review' AND kind = 'review_requested')
                 OR (:lane = 'ready' AND kind = 'changes_requested')
             )
             ORDER BY id DESC LIMIT 1
        ), attempts AS (
            SELECT * FROM task_runs
             WHERE task_id = :task AND id > COALESCE((SELECT run_id FROM boundary), 0)
        ), latest AS (
            SELECT * FROM attempts ORDER BY id DESC LIMIT 1
        )
        SELECT CASE
            WHEN EXISTS (
                SELECT 1 FROM task_runs WHERE task_id = :task
                  AND status = 'running' AND ended_at IS NULL
            ) THEN 'open_run'
            WHEN :manual THEN NULL
            WHEN EXISTS (
                SELECT 1 FROM task_events
                 WHERE task_id = :task AND id > COALESCE((SELECT id FROM boundary), 0)
                   AND (kind IN ('completed', 'archived', 'review_requested', 'changes_requested')
                        OR (kind = 'gave_up' AND CASE WHEN json_valid(payload)
                            THEN json_extract(payload, '$.sticky') IS 1 ELSE 0 END))
            ) THEN 'terminal_run'
            WHEN NOT EXISTS (SELECT 1 FROM boundary)
                 AND EXISTS (SELECT 1 FROM tasks WHERE id = :task AND completed_at IS NOT NULL)
                 AND NOT EXISTS (SELECT 1 FROM latest) THEN 'terminal_run'
            WHEN EXISTS (
                SELECT 1 FROM latest
                 WHERE CASE
                    WHEN outcome IN ('reclaimed', 'stale', 'timed_out', 'spawn_failed',
                                     'rate_limited') THEN 0
                    WHEN outcome = 'crashed' THEN
                        CASE WHEN json_valid(metadata)
                             THEN json_extract(metadata, '$.terminal_provider') IS 1 ELSE 0 END
                    WHEN outcome = 'blocked' THEN NOT EXISTS (
                        SELECT 1 FROM task_events AS e
                         WHERE e.run_id = latest.id AND e.task_id = :task
                           AND e.kind IN ('blocked', 'dependency_wait')
                           AND CASE WHEN json_valid(e.payload)
                               THEN json_extract(e.payload, '$.kind') IN ('transient', 'dependency')
                               ELSE 0 END
                    )
                    ELSE 1
                 END
            ) THEN 'terminal_run'
            WHEN (SELECT COUNT(*) FROM attempts) >= :ceiling THEN 'attempt_ceiling'
        END
        """,
        {"task": task_id, "lane": lane, "manual": manual, "ceiling": MAX_DISPATCH_ATTEMPTS},
    ).fetchone()[0]


def claim_and_open_run(
    conn: sqlite3.Connection, task_id: str, source_status: str, lock: str, expires: int, now: int,
    *, event_extra: dict | None = None, manual: bool = False,
) -> int | None:
    """CAS ``source_status -> running``, open a run row, emit ``claimed``; None
    when the CAS lost or admission is denied. Caller holds the write txn."""
    from hermes_cli import kanban_db as kb

    if dispatch_block_reason(conn, task_id, source_status, manual=manual):
        return None
    cur = conn.execute(
        f"""
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = '{source_status}'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    if manual:
        authorize_dispatch(conn, task_id, "manual_claim")
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            task_id, trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
            lock, expires, trow["max_runtime_seconds"] if trow else None, now,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    kb._append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id
