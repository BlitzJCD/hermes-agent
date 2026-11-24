"""Filesystem emergency stop for admission and cooperative cron cancellation."""

from __future__ import annotations

import json
import logging
import stat
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from hermes_constants import get_default_hermes_root, get_hermes_home

SENTINEL_NAME = "ESTOP"
CANCEL_REASON = "Hermes emergency stop"
_log_lock = threading.Lock()
_logged_components: set[str] = set()


def sentinel_path() -> Path:
    return get_hermes_home() / SENTINEL_NAME


def _canonical_root(home):
    return get_default_hermes_root(home=home)


def _targets():
    local = sentinel_path()
    try:
        fleet = _canonical_root(local.parent).resolve() / SENTINEL_NAME
        return tuple(dict.fromkeys((local.parent.resolve() / SENTINEL_NAME, fleet))), ()
    except (OSError, RuntimeError, ValueError) as exc:
        return (local,), (f"Cannot resolve fleet ESTOP root ({exc})",)


def _probe(path, *, follow=True):
    try:
        stamp = path.lstat()
    except FileNotFoundError:
        return None
    if follow and stat.S_ISLNK(stamp.st_mode):
        path.stat()  # A dangling link is an uncertain stop, not a missing sentinel.
    return stamp


def _observe(paths, errors):
    errors = list(errors)
    observed = []
    for path in paths:
        try:
            if _probe(path) is not None:
                observed.append(path)
        except OSError as exc:
            errors.append(f"Cannot read {path} ({exc})")
    return observed, errors


def is_engaged() -> bool:
    """Any sentinel or uncertainty closes admission."""
    observed, errors = _observe(*_targets())
    return bool(observed or errors)


def engage_checked(reason: str | None = None) -> tuple[Path, list[str]]:
    """Write the local sentinel even when fleet scope cannot be resolved."""
    from utils import atomic_json_write

    path = sentinel_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"engaged_at": datetime.now(UTC).isoformat(), "reason": reason or None},
                          mode=0o600, fsync_dir=True)
    except OSError as exc:
        try:
            # An empty sentinel still works when the metadata write cannot.
            path.touch(exist_ok=False)
        except OSError:
            return path, [f"Cannot write {path} ({exc})"]
    paths, resolution_errors = _targets()
    observed, errors = _observe(paths, resolution_errors)
    if paths[0] not in observed:
        errors.append(f"Local pause could not be confirmed at {path}")
    return path, errors


def _last_pause(path):
    return path.with_name(path.name + ".last")


def disengage_checked() -> tuple[bool, list[str]]:
    """Clear admission while retaining the engagement for workers between polls."""
    paths, resolution_errors = _targets()
    errors = list(resolution_errors)
    lifted = False
    for path in paths:
        try:
            if stat.S_ISDIR(path.lstat().st_mode):
                raise IsADirectoryError(str(path))
            path.replace(_last_pause(path))
            lifted = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"Cannot clear {path} ({exc})")
    observed, read_errors = _observe(paths, ())
    errors.extend(read_errors)
    errors.extend(f"Pause still present at {path}" for path in observed)
    return lifted, errors


def engage(reason: str | None = None) -> Path:
    return engage_checked(reason)[0]


def disengage() -> bool:
    lifted, errors = disengage_checked()
    return lifted and not errors


def get_state() -> dict | None:
    """Read status once; distinguish verified admission hold from uncertainty."""
    observed, errors = _observe(*_targets())
    if not observed and not errors:
        return None
    state = {"reason": None, "engaged_at": None, "errors": errors}
    for path in observed:
        with suppress(OSError, ValueError):
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                state.update(reason=raw.get("reason") or None, engaged_at=raw.get("engaged_at") or None)
                break
    return state


class CronStop:
    """Latch a pause for the lifetime of an admitted cron worker."""

    reason = CANCEL_REASON

    def __init__(self):
        self.paths, errors = _targets()
        self.generation = ()
        self.cancelled = bool(errors)
        try:
            self.generation = self._generation()
        except OSError:
            self.cancelled = True
        self.is_set()

    def _generation(self):
        return tuple((stamp.st_dev, stamp.st_ino, stamp.st_mtime_ns) if stamp else None
                     for stamp in (_probe(_last_pause(path), follow=False) for path in self.paths))

    def is_set(self):
        if not self.cancelled:
            try:
                self.cancelled = (any(_probe(path) is not None for path in self.paths)
                                  or self._generation() != self.generation)
            except OSError:
                self.cancelled = True
        return self.cancelled

    def set(self):
        self.cancelled = True


def paused_reply() -> str | None:
    state = get_state()
    if state is None:
        return None
    if state["errors"]:
        return "⏸️ Hermes cannot verify the emergency stop state. New work is on hold."
    tag = f" ({state['reason']})" if state["reason"] else ""
    return f"⏸️ Hermes is paused{tag}. New work is on hold; run `hermes resume` to pick things back up."


def check_paused(component: str, logger: logging.Logger) -> bool:
    if not is_engaged():
        with _log_lock:
            _logged_components.discard(component)
        return False
    with _log_lock:
        first = component not in _logged_components
        _logged_components.add(component)
    if first:
        logger.info("%s admission held by emergency stop (%s)", component, sentinel_path())
    return True
