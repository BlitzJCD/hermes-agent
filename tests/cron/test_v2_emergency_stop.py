from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shlex
import time

import pytest

from agent import estop
from hermes_cli.subcommands.pause import cmd_pause, cmd_resume


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    return state


def test_uncertain_fleet_root_still_writes_local_pause(state, monkeypatch, capsys):
    from hermes_cli.status import _estop_status_line

    def inaccessible(_home):
        raise OSError("fleet root unavailable")

    monkeypatch.setattr(estop, "_canonical_root", inaccessible)
    assert cmd_pause(Namespace(reason="fixture")) == 1
    assert (state / "ESTOP").is_file()
    assert "fleet root unavailable" in capsys.readouterr().out
    assert "uncertain" in _estop_status_line()
    assert estop.is_engaged()
    assert cmd_resume(Namespace()) == 1
    assert estop.is_engaged()


def test_pause_write_and_resume_clear_failures_are_reported(state, monkeypatch, capsys):
    import utils

    def denied(*args, **kwargs):
        raise PermissionError("fixture permission denied")

    with monkeypatch.context() as patch:
        patch.setattr(utils, "atomic_json_write", denied)
        patch.setattr(Path, "touch", denied)
        assert cmd_pause(Namespace(reason=None)) == 1
        output = capsys.readouterr().out
        assert "Cannot write" in output
        assert "cancellation was requested" not in output

    assert cmd_pause(Namespace(reason=None)) == 0
    assert "cancellation was requested" in capsys.readouterr().out
    monkeypatch.setattr(Path, "replace", denied)
    assert cmd_resume(Namespace()) == 1
    assert "Cannot clear" in capsys.readouterr().out
    assert estop.is_engaged()


@pytest.mark.platforms("linux")
def test_dangling_sentinel_holds_admission(state):
    sentinel = state / "ESTOP"
    sentinel.symlink_to(state / "missing")
    assert estop.is_engaged()
    assert "cannot verify" in estop.paused_reply()
    assert cmd_pause(Namespace(reason="replace uncertain sentinel")) == 0
    assert estop.get_state()["reason"] == "replace uncertain sentinel"
    assert cmd_resume(Namespace()) == 0
    assert not estop.is_engaged()


@pytest.mark.platforms("linux")
def test_active_cron_worker_acknowledges_stop_and_resume_does_not_replay(state, monkeypatch):
    from cron import jobs, scheduler
    from cron.executions import list_executions
    from tools.environments import local

    # This shell fixture needs no credentials or managed-runtime discovery.
    monkeypatch.setattr(local, "build_subprocess_env", lambda **_: {
        "PATH": "/usr/bin:/bin", "HOME": str(state.parent / "user-home"),
        "HERMES_HOME": str(state),
    })

    scripts = state / "scripts"
    scripts.mkdir()
    started = state / "started"
    finished = state / "finished"
    pid_file = state / "pid"
    script = scripts / "worker.sh"
    script.write_text(
        f"echo $$ > {shlex.quote(str(pid_file))}\n"
        f"echo started >> {shlex.quote(str(started))}\n"
        "sleep 8\n"
        f"touch {shlex.quote(str(finished))}\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    when = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    job = jobs.create_job(prompt=None, schedule=when, script=script.name, no_agent=True, deliver="local")

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(scheduler._run_one_job_body, job)
        deadline = time.monotonic() + 5
        while not started.exists() and not running.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            assert started.exists(), jobs.get_job(job["id"])["last_error"]
            assert cmd_pause(Namespace(reason="disposable test")) == 0
            assert scheduler.tick(verbose=False) == 0
            # Resume immediately so the worker must honor a stop even between polls.
            assert cmd_resume(Namespace()) == 0
            assert running.result(timeout=5) is True
        finally:
            if not running.done():
                estop.engage(reason="fixture cleanup")

    records = list_executions(job_id=job["id"])
    assert len(records) == 1
    assert records[0]["status"] == "cancelled"
    assert records[0]["error"] == estop.CANCEL_REASON
    assert not finished.exists()
    assert not Path("/proc", pid_file.read_text().strip()).exists()
    assert scheduler.tick(verbose=False) == 0
    assert list_executions(job_id=job["id"]) == records
    assert started.read_text() == "started\n"
