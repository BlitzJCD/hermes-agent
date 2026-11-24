"""V2-05 project profile and owner-review contracts using a native agent turn."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.fakes.fake_llm_provider import FakeLLMServer, Text


SOURCE = "obsidian://open?vault=project-a&file=capacity.md#revision-2"
BASELINE = "Project A capacity: 7."
FACT = f"Project A capacity: 11. Source: {SOURCE}."
PENDING = "Unapproved capacity proposal: 99."
CANARY = "Project B private material: fixture-b-99531."
SECRET = "sk-proj-" + "A9xy" * 20


@pytest.fixture
def projects(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    other = state / "profiles" / "project-b"
    home.mkdir()
    other.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    for root in (state, other):
        (root / "config.yaml").write_text(
            "memory:\n  owner_approval_only: true\n  memory_enabled: true\n"
            "  user_profile_enabled: false\n  nudge_interval: 0\n"
            "updates:\n  check: false\n"
            "telemetry:\n  shared_metrics:\n    enabled: false\n    send: false\n",
            encoding="utf-8",
        )
    return home, state, other


@contextmanager
def scope(path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(path)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def stage(store, action, content, *, old_text=None):
    from tools.memory_tool import memory_tool

    result = json.loads(memory_tool(action=action, content=content, old_text=old_text, store=store))
    assert result["success"] and result["staged"], result
    return result["pending_id"]


def review(store, *args, local_owner=False):
    from hermes_cli.write_approval_commands import handle_pending_subcommand

    return handle_pending_subcommand("memory", list(args), memory_store=store, local_owner=local_owner)


def test_native_local_cli_approves_one_exact_correction(projects, monkeypatch, capsys):
    from hermes_cli.cli_commands_mixin import CLICommandsMixin
    from tools import approval_context, write_approval
    from tools.memory_tool import load_on_disk_store

    _, project, _ = projects
    with scope(project):
        store = load_on_disk_store()
        baseline = stage(store, "add", BASELINE)
        assert "Approved 1" in review(store, "approve", baseline, local_owner=True)
        correction = stage(store, "patch", FACT, old_text=BASELINE)
        pending = stage(store, "add", PENDING)
        assert "interactive local" in review(store, "pending")
        assert "interactive local" in review(store, "approve", correction)
        assert "exact ID" in review(store, "approve", "all", local_owner=True)
        assert "requires local" in review(store, "approval", "off", local_owner=True)
        assert load_on_disk_store().memory_entries == [BASELINE]

        cli = SimpleNamespace(agent=SimpleNamespace(_memory_store=store))
        monkeypatch.setattr(approval_context, "_no_user_can_answer", lambda: False)
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
        CLICommandsMixin._handle_memory_command(cli, f"/memory approve {correction}")
        assert "interactive local" in capsys.readouterr().out
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
        CLICommandsMixin._handle_memory_command(cli, f"/memory approve {correction}")
        assert "Approved 1" in capsys.readouterr().out
        assert load_on_disk_store().memory_entries == [FACT]
        assert [item["id"] for item in write_approval.list_pending("memory")] == [pending]


def test_project_scope_rejects_foreign_store_and_linked_memory(projects):
    from tools.memory_tool import load_on_disk_store, memory_tool

    _, project, other = projects
    with scope(other):
        foreign_store = load_on_disk_store()
        pending = stage(foreign_store, "add", CANARY)
        assert "Approved 1" in review(foreign_store, "approve", pending, local_owner=True)
        foreign_store.load_from_disk()
        assert CANARY in foreign_store.format_for_system_prompt("memory")
    with scope(project):
        own_store = load_on_disk_store()
        assert foreign_store.format_for_system_prompt("memory") is None
        result = json.loads(memory_tool(action="add", content="scope probe", store=foreign_store))
        assert result == {"success": False, "error": "Memory store belongs to another profile."}
        local_pending = stage(own_store, "add", "Project A scoped fact.")
        assert "Failed:" in review(foreign_store, "approve", local_pending, local_owner=True)
        assert not own_store.memory_entries
        assert "Approved 1" in review(own_store, "approve", local_pending, local_owner=True)
        local = project / "memories" / "MEMORY.md"
        local.unlink()
        local.symlink_to(other / "memories" / "MEMORY.md")
        with pytest.raises(ValueError, match="Memory path escapes its profile"):
            load_on_disk_store()


NATIVE_TURN = r"""
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

base_url, other = sys.argv[1], Path(sys.argv[2]).resolve()
provider = urlparse(base_url)
checkout = Path.cwd()
assert sys.stdin.read() == ""
denials = []

def guard(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).resolve()
        private = path.name.startswith((".env", ".op.env")) or path.name in {"auth.json", "credentials.json"}
        installation_state = path.is_relative_to(checkout.parent) and not path.is_relative_to(checkout)
        if private or installation_state or path.is_relative_to(other):
            frame = sys._getframe(1)
            calls = []
            while frame and len(calls) < 6:
                filename = Path(frame.f_code.co_filename)
                if filename.is_relative_to(checkout):
                    calls.append(str(filename.relative_to(checkout)) + ":" + frame.f_code.co_name)
                frame = frame.f_back
            kind = ("foreign_profile" if path.is_relative_to(other) else
                    "dotenv" if path.name.startswith((".env", ".op.env")) else
                    "auth" if private else "installation_metadata")
            denials.append((kind, path.exists(), calls))
            raise PermissionError("private read refused")
    if event == "socket.connect" and args[1] != ("127.0.0.1", provider.port):
        denials.append("outbound_connection")
        raise PermissionError("connection outside fixture refused")

sys.addaudithook(guard)
from run_agent import AIAgent

agent = AIAgent(
    provider="custom", base_url=base_url, api_key="fixture-service-key",
    model="fake-model", quiet_mode=True, enabled_toolsets=["memory"],
    skip_context_files=True, skip_background_review=True, platform="cli",
    session_id="v2-approved-memory-fresh", max_iterations=1,
)
try:
    result = agent.run_conversation("Give current Project A capacity and its exact source.")
    assert result.get("completed"), result.get("finish_reason")
    assert not denials, denials
    print("V2_MEMORY_NATIVE_TURN_OK")
finally:
    agent.close()
"""


def test_fresh_native_turn_sends_only_approved_project_memory(projects, tmp_path):
    from tools.memory_tool import load_on_disk_store

    home, project, other = projects
    vault = tmp_path / "project-a-vault"
    vault.mkdir()
    note = vault / "capacity.md"
    note.write_text(FACT + "\n", encoding="utf-8")
    with scope(project):
        store = load_on_disk_store()
        proposal = stage(store, "add", BASELINE)
        assert "Approved 1" in review(store, "approve", proposal, local_owner=True)
        proposal = stage(store, "patch", note.read_text().strip(), old_text=BASELINE)
        assert "Approved 1" in review(store, "approve", proposal, local_owner=True)
        stage(store, "add", PENDING)
        # Imported older memory can predate local review. Its token must stay off the wire.
        memory = project / "memories" / "MEMORY.md"
        with memory.open("a", encoding="utf-8") as stream:
            stream.write(f"\n§\nLegacy fixture API key: {SECRET}\n")
        before = memory.read_bytes()
    with scope(other):
        store = load_on_disk_store()
        proposal = stage(store, "add", CANARY)
        assert "Approved 1" in review(store, "approve", proposal, local_owner=True)

    with FakeLLMServer([Text("Capacity is 11. Source: " + SOURCE)]) as provider:
        env = {
            "HOME": str(home), "USERPROFILE": str(home), "HERMES_HOME": str(project),
            "PATH": os.defpath, "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "TMPDIR": str(project), "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "HERMES_SINGLE_QUERY_SESSION": "1",
        }
        child = subprocess.run(
            [sys.executable, "-B", "-c", NATIVE_TURN, provider.base_url, str(other)],
            cwd=Path(__file__).resolve().parents[2], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=40, check=False,
        )
        assert child.returncode == 0, child.stderr
        assert "V2_MEMORY_NATIVE_TURN_OK" in child.stdout
        request, = provider.main_requests()
        system_text = "\n".join(str(message["content"]) for message in request["messages"]
                                if message["role"] == "system")
        assert FACT in system_text and SOURCE in system_text
        wire_text = json.dumps(request)
        assert not any(item in wire_text for item in (BASELINE, PENDING, CANARY, SECRET))
        assert SECRET not in child.stdout + child.stderr

    assert memory.read_bytes() == before
