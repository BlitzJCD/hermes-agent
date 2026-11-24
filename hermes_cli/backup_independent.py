"""Offline restore exercise with an encrypted local transport copy.

Run with python -m hermes_cli.backup_independent --help. This never imports
a restored profile, starts services, or reads a configured rclone remote.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
import zipfile
import zlib

from hermes_cli.archive_safe import normalize_archive_parts

RESTIC = "/usr/bin/restic"
RCLONE = "/usr/bin/rclone"
MAX_ARCHIVE = 256 << 20
MAX_MEMBER = 512 << 20
MAX_TOTAL = 2 << 30
MAX_MEMBERS = 30_000
MAX_RATIO = 200
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
CATEGORIES = ("state", "tasks", "skills", "project_source", "obsidian", "windows_runtime")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    prefix + digit for prefix in ("com", "lpt") for digit in "123456789¹²³"
}


class RestoreRefused(ValueError):
    """A fixed rejection code that does not disclose archive paths or contents."""


@contextmanager
def _private_input(path: Path, limit: int):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.geteuid() or before.st_mode & 0o077):
        raise RestoreRefused("input_not_private_regular_file")
    if before.st_size > limit:
        raise RestoreRefused("input_size_limit")
    # Restic inherits the ZIP's descriptor. Keep seek() and its actual offset identical.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb", buffering=0) as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise RestoreRefused("input_changed")
        yield stream
        if _identity(os.fstat(stream.fileno())) != _identity(opened) or _identity(path.lstat()) != _identity(opened):
            raise RestoreRefused("input_changed")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _exclusive_file(path, *, parent_fd=None):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600, dir_fd=parent_fd), "wb")


def _member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.orig_filename
    clean_name = name.removesuffix("/")
    parts = tuple(clean_name.split("/"))
    if (len(name) > 1024 or "\\" in name or ":" in name
            or unicodedata.normalize("NFC", name) != name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(not part or part in {".", ".."} or part[-1] in ". " for part in parts)
            or any(part.split(".")[0].casefold() in WINDOWS_RESERVED for part in parts)):
        raise RestoreRefused("unsafe_zip_path")
    try:
        if tuple(normalize_archive_parts(name)) != parts:
            raise RestoreRefused("unsafe_zip_path")
    except ValueError:
        raise RestoreRefused("unsafe_zip_path") from None
    if any(part.casefold() == "_external" for part in parts):
        raise RestoreRefused("external_zip_mapping")
    expected_kind = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
    if stat.S_IFMT(info.external_attr >> 16) not in {0, expected_kind}:
        raise RestoreRefused("unsupported_zip_member_type")
    if info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise RestoreRefused("unsupported_zip_encoding")
    return parts


def _zip_members(archive: zipfile.ZipFile) -> dict[tuple[str, ...], zipfile.ZipInfo]:
    members = archive.infolist()
    if not 0 < len(members) <= MAX_MEMBERS:
        raise RestoreRefused("zip_member_count")
    entries = {}
    spellings = {}
    total = 0
    for info in members:
        parts = _member_parts(info)
        total += info.file_size
        if (info.file_size > MAX_MEMBER or total > MAX_TOTAL
                or info.file_size > max(1, info.compress_size) * MAX_RATIO
                or (info.is_dir() and info.file_size)):
            raise RestoreRefused("zip_expansion_limit")
        if parts in entries:
            raise RestoreRefused("duplicate_zip_destination")
        entries[parts] = info
        for end in range(1, len(parts) + 1):
            prefix = parts[:end]
            folded = tuple(part.casefold() for part in prefix)
            if spellings.setdefault(folded, prefix) != prefix:
                raise RestoreRefused("duplicate_zip_destination")
    for parts in entries:
        for end in range(1, len(parts)):
            parent = entries.get(parts[:end])
            if parent is not None and not parent.is_dir():
                raise RestoreRefused("zip_file_directory_conflict")
    return entries


def _zip_preflight(archive, coverage):
    entries = _zip_members(archive)
    files = {"/".join(parts) for parts, info in entries.items() if not info.is_dir()}
    _verify_coverage(coverage, files)
    hashes = {}
    for parts, info in entries.items():
        with archive.open(info) as member:
            hashes[parts] = _member_digest(member, info.file_size)
    return entries, hashes


def _member_digest(stream, expected_size, output=None):
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(64 * 1024):
        size += len(chunk)
        if size > expected_size:
            raise RestoreRefused("zip_stream_size_mismatch")
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    if size != expected_size:
        raise RestoreRefused("zip_stream_size_mismatch")
    return digest.hexdigest()


def _verify_coverage(coverage, files):
    if not isinstance(coverage, dict) or set(coverage) != set(CATEGORIES):
        raise RestoreRefused("coverage_categories_missing")
    listed = set()
    for category, names in coverage.items():
        if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
            raise RestoreRefused("invalid_coverage_members")
        if category in {"state", "tasks"} and not any(Path(name).suffix.lower() in DB_SUFFIXES for name in names):
            raise RestoreRefused("sqlite_coverage_missing")
        if category == "state" and not any(Path(name).name.casefold() in {"config.yaml", "config.yml"} for name in names):
            raise RestoreRefused("config_coverage_missing")
        for name in names:
            if name in listed or name not in files:
                raise RestoreRefused("invalid_coverage_members")
            listed.add(name)
    if listed != files:
        raise RestoreRefused("coverage_inventory_mismatch")


@contextmanager
def _private_directory(root_fd, parts):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RestoreRefused("restore_directory_not_private")
        yield fd
    finally:
        os.close(fd)


def _restore_members(archive, entries, hashes, root):
    root.mkdir(mode=0o700)
    databases = []
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for parts, info in entries.items():
            if info.is_dir():
                with _private_directory(root_fd, parts):
                    continue
            path = root.joinpath(*parts)
            with _private_directory(root_fd, parts[:-1]) as parent:
                with archive.open(info) as member, _exclusive_file(parts[-1], parent_fd=parent) as output:
                    _member_digest(member, info.file_size, output)
                    output.flush()
                    os.fsync(output.fileno())
            with _private_input(path, MAX_MEMBER) as restored:
                if hashlib.file_digest(restored, "sha256").hexdigest() != hashes[parts]:
                    raise RestoreRefused("restore_digest_mismatch")
                if path.name.casefold() in {"config.yaml", "config.yml"}:
                    _check_config_text(restored)
            if path.suffix.lower() in DB_SUFFIXES:
                databases.append(path)
    finally:
        os.close(root_fd)
    if len(databases) > 128:
        raise RestoreRefused("sqlite_count_limit")
    for database in databases:
        _check_sqlite(database)
    return len(databases)


def _check_config_text(stream):
    stream.seek(0)
    data = stream.read((1 << 20) + 1)
    try:
        if len(data) > 1 << 20 or b"\0" in data or not data.decode("utf-8").strip():
            raise RestoreRefused("config_text_invalid")
    except UnicodeError:
        raise RestoreRefused("config_text_invalid") from None


def _check_sqlite(path):
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise RestoreRefused("sqlite_sidecar_present")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            deadline = time.monotonic() + 15
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            if db.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
                raise RestoreRefused("sqlite_integrity_failed")
    except sqlite3.Error:
        raise RestoreRefused("sqlite_integrity_failed") from None


def _workspace(path: Path) -> Path:
    from hermes_constants import get_hermes_home

    target = path.absolute()
    try:
        resolved = target.resolve(strict=True)
        info = target.lstat()
    except OSError:
        raise RestoreRefused("workspace_io_failed") from None
    if resolved != target:
        raise RestoreRefused("workspace_path_alias")
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RestoreRefused("workspace_not_private")
    protected = (Path.home(), get_hermes_home(), Path(__file__).resolve().parents[1])
    if any(target.is_relative_to(root.resolve()) or root.resolve().is_relative_to(target) for root in protected):
        raise RestoreRefused("workspace_overlaps_live_tree")
    return target


def _run_tool(binary, args, env, *, stdin=None, stdout=None):
    try:
        subprocess.run(
            [binary, *map(str, args)], env=env, check=True, timeout=600,
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.DEVNULL if stdout is None else stdout, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise RestoreRefused(f"{Path(binary).name}_command_failed") from None


def _transport(archive_stream, password, root, *, dry_run):
    run_dir = root / "exercise"
    run_dir.mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(run_dir / "user-home"),
           "HERMES_HOME": str(run_dir / "hermes-state"), "TMPDIR": str(run_dir / "tmp")}
    for key in ("HOME", "HERMES_HOME", "TMPDIR"):
        Path(env[key]).mkdir(mode=0o700)
    # Repository and copy are always fresh absolute local paths. A caller cannot
    # inject a backend, external config, credential command, or repository URL.
    primary = run_dir / "repository"
    copy = run_dir / "repository-copy"
    restic = ["--no-cache", "--password-file", password]
    rclone = ["--config", os.devnull, "--cache-dir", run_dir / "rclone-cache"]
    _run_tool(RESTIC, [*restic, "--repo", primary, "init"], env)
    _run_tool(
        RESTIC,
        [*restic, "--repo", primary, "backup", "--stdin", "--stdin-filename", "recovery.zip",
         *(["--dry-run"] if dry_run else [])],
        env, stdin=archive_stream,
    )
    _run_tool(
        RCLONE, [*rclone, "copy", "--immutable", primary, copy, *(["--dry-run"] if dry_run else [])], env,
    )
    if dry_run:
        return None
    _run_tool(RCLONE, [*rclone, "check", "--download", primary, copy], env)
    _run_tool(RESTIC, [*restic, "--repo", copy, "check", "--read-data"], env)
    restored = run_dir / "recovery.zip"
    with _exclusive_file(restored) as output:
        _run_tool(RESTIC, [*restic, "--repo", copy, "dump", "latest", "/recovery.zip"], env, stdout=output)
        output.flush()
        os.fsync(output.fileno())
    return restored


def exercise_restore(workspace: Path, expected_sha256: str, *, dry_run: bool = True) -> dict:
    """Use private input.zip, coverage.json and restic-password in a dedicated workspace.

    Dry-run verifies the ZIP and coverage, then creates only an empty temporary
    Restic repository and runs both tools' dry-run modes. Use a fresh workspace
    for every invocation. The local encrypted copy is a transport check.
    """
    if os.name != "posix":
        raise RestoreRefused("posix_required")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_sha256):
        raise RestoreRefused("invalid_archive_digest")
    root = _workspace(workspace)
    if os.path.lexists(root / "exercise"):
        raise RestoreRefused("exercise_target_exists")
    password = root / "restic-password"
    try:
        with _private_input(root / "coverage.json", 2 << 20) as stream:
            coverage = json.load(stream)
        with _private_input(password, 4096) as secret, _private_input(root / "input.zip", MAX_ARCHIVE) as stream:
            if os.fstat(secret.fileno()).st_size == 0:
                raise RestoreRefused("empty_repository_password")
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256.lower():
                raise RestoreRefused("input_digest_mismatch")
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                entries, hashes = _zip_preflight(archive, coverage)
            stream.seek(0)
            restored = _transport(stream, password, root, dry_run=dry_run)
        result = {"dry_run": dry_run, "crc_checked": True, "coverage": {k: len(v) for k, v in coverage.items()},
                  "files_verified": 0, "sqlite_verified": 0, "local_copy_verified": False,
                  "off_device_verified": False}
        if restored is not None:
            with _private_input(restored, MAX_ARCHIVE) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256.lower():
                    raise RestoreRefused("transport_digest_mismatch")
                stream.seek(0)
                with zipfile.ZipFile(stream) as archive:
                    result["sqlite_verified"] = _restore_members(archive, entries, hashes, restored.parent / "restored")
            result["files_verified"] = sum(not info.is_dir() for info in entries.values())
            result["local_copy_verified"] = True
        return result
    except (zipfile.BadZipFile, EOFError, zlib.error):
        raise RestoreRefused("zip_integrity_failed") from None
    except (json.JSONDecodeError, UnicodeError):
        raise RestoreRefused("invalid_coverage_json") from None
    except OSError:
        raise RestoreRefused("workspace_io_failed") from None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="coverage.json maps state, tasks, skills, project_source, obsidian and windows_runtime "
               "to lists of exact ZIP file names. State and tasks each require a SQLite file.",
    )
    parser.add_argument("--workspace", type=Path, required=True,
                        help="owner-only directory outside the live homes, with input.zip, coverage.json and restic-password")
    parser.add_argument("--sha256", required=True, help="reviewed SHA-256 of input.zip")
    parser.add_argument("--restore", action="store_true", help="perform the disposable local round trip")
    args = parser.parse_args(argv)
    try:
        result = exercise_restore(args.workspace, args.sha256, dry_run=not args.restore)
    except RestoreRefused as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
