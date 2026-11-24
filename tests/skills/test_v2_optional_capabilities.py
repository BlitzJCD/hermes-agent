"""Offline review of a research draft using the native grounded-citations CLI."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "research"
          / "grounded-citations" / "scripts" / "sources.py")


@pytest.fixture
def citation_review(tmp_path):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    task = tmp_path / "review"
    for folder in (home, state, task):
        folder.mkdir()
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state))
    ledger = task / "ledger.json"
    draft = task / "draft.md"

    def run(*args, check=True):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--ledger", str(ledger), *args],
            cwd=task, env=env, text=True, capture_output=True, check=check, timeout=10,
        )

    for source_id, word in ((1, "42"), (2, "19")):
        evidence = task / f"source-{source_id}.txt"
        quote = f"The fixture contains {word} records."
        evidence.write_text(f"Public excerpt\n{quote}\nEnd of excerpt\n", encoding="utf-8")
        run("add", f"https://example.invalid/source-{source_id}")
        run("quote", str(source_id), "--text", quote, "--from", str(evidence))
    return run, draft, ledger, state


def test_native_quote_review_accepts_cited_wording_without_writing(citation_review):
    run, draft, ledger, state = citation_review
    draft.write_text(
        "A source excerpt follows.\n\n> The fixture contains\n> **42 records.**[1]\n",
        encoding="utf-8",
    )
    run("render", "--replace-in", str(draft))
    before = (draft.read_bytes(), ledger.read_bytes())
    result = run("verify", str(draft), "--quotes", "--evidence")
    assert result.stdout.endswith("citations OK\n")
    assert (draft.read_bytes(), ledger.read_bytes()) == before
    assert list(state.iterdir()) == []


@pytest.mark.parametrize(("body", "reason"), [
    ("> The fixture contains 19 records.[1]", "does not match evidence attached to its cited sources"),
    ("> The fixture contains 142 records.[1]", "does not match evidence attached to its cited sources"),
    ("> The fixture contains 42 records.\n\nA nearby citation.[1]", "needs trailing source citations"),
    ("A paraphrase with a citation.[1]", "no Markdown blockquotes found"),
])
def test_native_quote_review_refuses_unbound_evidence(citation_review, body, reason):
    run, draft, _, _ = citation_review
    draft.write_text(body + "\n", encoding="utf-8")
    run("render", "--replace-in", str(draft))
    assert run("verify", str(draft), "--evidence").returncode == 0
    result = run("verify", str(draft), "--quotes", check=False)
    assert result.returncode == 1
    assert reason in result.stderr
