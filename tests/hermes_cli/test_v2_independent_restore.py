"""Independent, offline restore exercises using only disposable fixture inputs."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import zipfile

import pytest

from hermes_cli.backup_independent import RestoreRefused, exercise_restore, main


pytestmark = pytest.mark.platforms("linux")

COVERAGE = {
    "state": ["hermes/config.yaml", "hermes/state.db"],
    "tasks": ["hermes/kanban.db", "hermes/attachments/fixture.txt"],
    "skills": ["hermes/skills/example/SKILL.md"],
    "project_source": ["project/working-tree.txt"],
    "obsidian": ["vault/fixture.md"],
    "windows_runtime": ["windows/runtime-inventory.txt"],
}


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def owner_only(path):
    path.chmod(0o600)
    return path


def zip_fixture(root, *, extra=None, omitted=None, sidecar=False):
    archive = root / "input.zip"
    coverage = {key: list(paths) for key, paths in COVERAGE.items()}
    if sidecar:
        coverage["state"].append("hermes/state.db-wal")
    with zipfile.ZipFile(archive, "w") as bundle:
        for category, paths in coverage.items():
            for path in paths:
                if category == omitted:
                    continue
                if path.endswith(".db"):
                    db_file = root / Path(path).name
                    with closing(sqlite3.connect(db_file)) as db, db:
                        table = "tasks" if category == "tasks" else "sessions"
                        db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                        db.execute(f"INSERT INTO {table} VALUES (1)")
                    bundle.write(db_file, path)
                    db_file.unlink()
                else:
                    bundle.writestr(path, "fixture: private representative data\n")
        if extra is not None:
            bundle.writestr(extra, b"rejected extra")
    owner_only(archive)
    manifest = root / "coverage.json"
    manifest.write_text(json.dumps(coverage), encoding="utf-8")
    owner_only(manifest)
    return archive


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    workspace = tmp_path / "restore-fixture"
    for path in (home, state, workspace):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    # A test can verify these remain byte-identical after the exercise.
    for path in (home, state):
        (path / "sentinel").write_bytes(b"preserve this data")
    password = workspace / "restic-password"
    password.write_text(secrets.token_hex(24) + "\n", encoding="utf-8")
    owner_only(password)
    return workspace


def test_encrypted_copy_restores_from_separate_repository_with_digest_readback(isolated):
    archive = zip_fixture(isolated)
    expected = fingerprint(archive)
    result = exercise_restore(isolated, expected, dry_run=False)

    assert result == {
        "dry_run": False, "crc_checked": True,
        "coverage": {category: len(paths) for category, paths in COVERAGE.items()},
        "files_verified": 8, "sqlite_verified": 2,
        "local_copy_verified": True, "off_device_verified": False,
    }
    run = isolated / "exercise"
    restored = run / "restored"
    assert fingerprint(archive) == fingerprint(run / "recovery.zip") == expected
    assert (run / "repository-copy" / "config").is_file()
    assert (run / "repository-copy" / "config").read_bytes() == (run / "repository" / "config").read_bytes()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.namelist():
            actual = restored / member
            assert actual.read_bytes() == bundle.read(member)
            assert stat.S_IMODE(actual.stat().st_mode) == 0o600
            assert stat.S_IMODE(actual.parent.stat().st_mode) == 0o700
    for key in ("HOME", "HERMES_HOME"):
        assert [(path.name, path.read_bytes()) for path in Path(os.environ[key]).iterdir()] == [
            ("sentinel", b"preserve this data"),
        ]

    with pytest.raises(RestoreRefused, match="exercise_target_exists"):
        exercise_restore(isolated, expected, dry_run=False)
    assert fingerprint(archive) == expected


def test_default_cli_dry_run_makes_no_snapshot_copy_or_restore(isolated, capsys):
    archive = zip_fixture(isolated)
    assert main(["--workspace", str(isolated), "--sha256", fingerprint(archive)]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["dry_run"] and result["crc_checked"]
    assert result["files_verified"] == result["sqlite_verified"] == 0
    assert not result["local_copy_verified"] and not result["off_device_verified"]
    assert "fixture.md" not in output
    run = isolated / "exercise"
    assert not (run / "restored").exists()
    assert not (run / "recovery.zip").exists()
    assert not (run / "repository-copy" / "config").exists()
    snapshots = subprocess.run(
        ["/usr/bin/restic", "--no-cache", "--repo", str(run / "repository"),
         "--password-file", str(isolated / "restic-password"), "snapshots", "--json"],
        env={"HOME": str(run / "user-home"), "HERMES_HOME": str(run / "hermes-state")},
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert json.loads(snapshots.stdout) == []


@pytest.mark.parametrize(
    ("member", "code"),
    [
        ("../outside", "unsafe_zip_path"),
        ("windows/Zone.Identifier:stream", "unsafe_zip_path"),
        ("windows/COM1.txt", "unsafe_zip_path"),
        ("_external/notes.md", "external_zip_mapping"),
        ("HERMES/config.yaml", "duplicate_zip_destination"),
        ("hermes/config.yaml/child", "zip_file_directory_conflict"),
    ],
)
def test_preflight_refuses_portable_path_hazards_before_output(isolated, member, code):
    archive = zip_fixture(isolated, extra=member)
    with pytest.raises(RestoreRefused, match=code):
        exercise_restore(isolated, fingerprint(archive), dry_run=False)
    assert not (isolated / "exercise").exists()


def test_preflight_refuses_link_members_and_link_inputs(isolated):
    link = zipfile.ZipInfo("windows/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = zip_fixture(isolated, extra=link)
    expected = fingerprint(archive)
    with pytest.raises(RestoreRefused, match="unsupported_zip_member_type"):
        exercise_restore(isolated, expected)
    moved = archive.rename(isolated / "plain.zip")
    archive.symlink_to(moved)
    with pytest.raises(RestoreRefused, match="input_not_private_regular_file"):
        exercise_restore(isolated, expected)
    assert not (isolated / "exercise").exists()


def test_crc_failure_is_rejected_before_transport(isolated):
    archive = zip_fixture(isolated)
    archive.write_bytes(archive.read_bytes().replace(b"private representative", b"damaged representative", 1))
    with pytest.raises(RestoreRefused, match="zip_integrity_failed"):
        exercise_restore(isolated, fingerprint(archive), dry_run=False)
    assert not (isolated / "exercise").exists()


def test_manifest_missing_coverage_and_wrong_digest_are_rejected(isolated):
    archive = zip_fixture(isolated, omitted="obsidian")
    with pytest.raises(RestoreRefused, match="input_digest_mismatch"):
        exercise_restore(isolated, "0" * 64)
    with pytest.raises(RestoreRefused, match="invalid_coverage_members"):
        exercise_restore(isolated, fingerprint(archive))
    assert not (isolated / "exercise").exists()


def test_sqlite_sidecar_refuses_integrity_claim_and_preserves_evidence(isolated):
    archive = zip_fixture(isolated, sidecar=True)
    expected = fingerprint(archive)
    with pytest.raises(RestoreRefused, match="sqlite_sidecar_present"):
        exercise_restore(isolated, expected, dry_run=False)
    assert fingerprint(archive) == expected
    restored = isolated / "exercise" / "restored" / "hermes"
    assert (restored / "state.db-wal").read_bytes() == b"fixture: private representative data\n"
    assert not (restored / "state.db-shm").exists()


def test_workspace_must_be_disjoint_and_resolve_without_symlinks(isolated):
    alias = isolated.with_name("workspace-alias")
    alias.symlink_to(isolated, target_is_directory=True)
    with pytest.raises(RestoreRefused, match="workspace_path_alias"):
        exercise_restore(alias, "0" * 64)
    with pytest.raises(RestoreRefused, match="workspace_overlaps_live_tree"):
        exercise_restore(Path(os.environ["HERMES_HOME"]), "0" * 64)
