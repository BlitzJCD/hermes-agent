"""Commands for the existing Hermes emergency stop."""

from __future__ import annotations

import argparse


def cmd_pause(args: argparse.Namespace) -> int:
    """Engage the global emergency stop."""
    from agent.estop import engage_checked

    path, errors = engage_checked(reason=getattr(args, "reason", None))
    print(f"Emergency stop sentinel: {path}")
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Hermes is paused for new work. Active cron cancellation was requested.")
    print("Check the execution status for cancellation acknowledgement.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Disengage the global emergency stop."""
    from agent.estop import disengage_checked

    lifted, errors = disengage_checked()
    if errors:
        for error in errors:
            print(error)
        return 1
    print("Hermes resumed." if lifted else "Hermes is already accepting new work.")
    return 0


def build_pause_parser(subparsers) -> None:
    """Attach the ``pause`` and ``resume`` subcommands to ``subparsers``."""
    pause_parser = subparsers.add_parser(
        "pause", help="Emergency stop: pause cron/kanban dispatch and new gateway turns",
        description="Hold new work and request cancellation of active cron runs.")
    pause_parser.add_argument(
        "--reason", default=None, help="Optional reason stored in the sentinel and shown to users")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser(
        "resume", help="Lift the emergency stop set by `hermes pause`",
        description="Remove the ESTOP sentinel; dispatch resumes on the next tick.")
    resume_parser.set_defaults(func=cmd_resume)
