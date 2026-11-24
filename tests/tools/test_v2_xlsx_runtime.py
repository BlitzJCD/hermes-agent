"""Exercise the native Excel skill through a disposable Hermes Docker terminal."""

import json
import os
from pathlib import Path
import shlex
import shutil
import uuid

import pytest


_IMAGE = os.getenv("HERMES_TEST_XLSX_IMAGE")
_SKILL = Path(__file__).resolve().parents[2] / "skills/productivity/microsoft-office"
_PYTHON = "/opt/hermes-xlsx/bin/python -I"
_CREATE = f"{_PYTHON} /xlsx-skill/scripts/xlsx_create.py"
_READ = f"{_PYTHON} /xlsx-skill/scripts/xlsx_read.py"

pytestmark = pytest.mark.skipif(not _IMAGE, reason="requires the built XLSX Docker candidate")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from tools.environments import docker

    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    skill = tmp_path / "skill"
    home.mkdir()
    state.mkdir()
    (skill / "scripts").mkdir(parents=True)
    for name in ("xlsx_create.py", "xlsx_read.py"):
        shutil.copyfile(_SKILL / "scripts" / name, skill / "scripts" / name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    monkeypatch.setattr(docker, "_readonly_skill_mount_args", lambda: [])

    terminal = docker.DockerEnvironment(
        image=_IMAGE, image_pinned=True, cwd="/workspace", network=False,
        volumes=[f"{skill}:/xlsx-skill:ro"], persist_across_processes=False,
        task_id=f"xlsx-v2-08-{uuid.uuid4().hex[:12]}",
    )
    try:
        yield terminal
    finally:
        terminal.cleanup()
        assert docker.DockerEnvironment.wait_for_all_teardowns(timeout=45)


def _run(sandbox, command):
    result = sandbox.execute(command)
    assert result["returncode"] == 0, result
    return result["output"].strip()


def _create(sandbox):
    spec = {"sheets": [{"name": "Fixture", "rows": [
        ["Name", "Amount", "Active"], ["Disposable", 42, True],
        ["Formula", {"formula": "B2*2"}],
    ]}]}
    _run(sandbox, f"printf %s {shlex.quote(json.dumps(spec))} | {_CREATE} - workbook.xlsx")


def test_native_skill_round_trip_and_formula_cache(sandbox):
    _create(sandbox)
    readback = json.loads(_run(sandbox, f"{_READ} workbook.xlsx --json --sheet Fixture"))
    assert readback == {"sheet": "Fixture", "rows": [
        ["Name", "Amount", "Active"], ["Disposable", 42, True], ["Formula", "=B2*2", None],
    ]}
    formulas = json.loads(_run(sandbox, f"{_READ} workbook.xlsx --formulas"))
    assert formulas == {"formulas": [{"sheet": "Fixture", "cell": "B3", "formula": "=B2*2", "cached": None}]}


def test_native_skill_rejects_xml_entities(sandbox):
    _create(sandbox)
    # A tiny internal entity tests parser selection without external references
    # or a resource-intensive payload.
    generate = """
from zipfile import ZipFile

with ZipFile("workbook.xlsx") as source, ZipFile("entity.xlsx", "w") as target:
    for name in source.namelist():
        data = source.read(name)
        if name == "[Content_Types].xml":
            data = data.replace(b"</Types>", b'<Override PartName="/xl/sharedStrings.xml" '
                b'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                b"</Types>")
        target.writestr(name, data)
    target.writestr("xl/sharedStrings.xml",
        b'<!DOCTYPE sst [<!ENTITY sample "harmless">]>'
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b'<si><t>&sample;</t></si></sst>')
"""
    _run(sandbox, f"{_PYTHON} -c {shlex.quote(generate)}")
    result = sandbox.execute(f"{_READ} entity.xlsx --json")
    assert result["returncode"] == 1, result
    report = json.loads(result["output"])
    assert report["ok"] is False and "could not read strings" in report["error"]

    check_cause = """
from defusedxml.common import EntitiesForbidden
from openpyxl import load_workbook

try:
    load_workbook("entity.xlsx")
except ValueError as error:
    assert isinstance(error.__cause__, EntitiesForbidden), repr(error.__cause__)
else:
    raise AssertionError("XML entity was accepted")
"""
    _run(sandbox, f"{_PYTHON} -c {shlex.quote(check_cause)}")
