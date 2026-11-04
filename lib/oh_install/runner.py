"""Run the install's steps: check each, and do what is not done yet."""

from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
import shlex

import oh_cli
from oh_install import progress
from oh_install.model import Action, Command, Step, Task
from oh_install.system import System

RERUN = f"Run thermite install --config {shlex.quote(str(oh_cli.AREA))} again to go on from here."
_TASK_APPROVAL = ContextVar("thermite_task_approval", default=False)


def confirm_run(system: System, operation: str, rerun: str = RERUN) -> bool:
    """One interactive approval, scoped to this invocation's workflow."""
    if not system.interactive():
        system.say(f"Stopped; this {operation} needs you at a terminal. {rerun}")
        return False
    if system.ask(f"{operation.capitalize()}? [y/N] ").strip().lower() not in ("y", "yes"):
        system.say(f"Stopped; no changes were made. {rerun}")
        return False
    return True


def perform(action: Action, system: System, area: Path, rerun: str = RERUN, *,
            root_confirmed: bool | None = None) -> int | None:
    """The action's exit status, or None when the owner said no or no
    terminal is there to ask."""
    if root_confirmed is None:
        root_confirmed = _TASK_APPROVAL.get()
    if root_confirmed and isinstance(action, Command) and action.approved_argv is not None:
        action = replace(action, argv=action.approved_argv)
    root = isinstance(action, Command) and action.root
    if (root or isinstance(action, Task) and action.interactive) and not system.interactive():
        system.say(f"   needs you at a terminal: {action.shown()}")
        system.say(f"Stopped. {rerun}")
        return None
    if root:
        if not root_confirmed or oh_cli.switches.verbose or progress.current.get() is None:
            system.say(f"   next, under sudo: {action.shown()}")
        if not root_confirmed and system.ask("   Run it? [y/N] ").strip().lower() not in ("y", "yes"):
            system.say(f"Stopped; nothing more was run. {rerun}")
            return None
    elif oh_cli.switches.verbose or progress.current.get() is None:
        system.say(f"   {'running' if isinstance(action, Command) else 'now'}: {action.shown()}")
    if isinstance(action, Command):
        with progress.command(terminal=action.terminal, show_output=action.show_output):
            return system.run(action.full, area)
    # A task may request a bounded recovery command. It inherits only this
    # invocation's approval; reset even on exceptions before another run.
    approval = _TASK_APPROVAL.set(root_confirmed)
    try:
        return action.perform(system)
    finally:
        _TASK_APPROVAL.reset(approval)


def compensate(system: System, area: Path, stopped: dict[str, None], rerun: str, *,
               root_confirmed: bool, interrupted: bool = False) -> None:
    """Start what this run stopped, after a failure before its own restart.

    Backups and native preparation stop the runtime, helpers and monitoring;
    a failure before the step that starts them again would otherwise leave
    the house without automation until a rerun. An interruption runs nothing
    more: it names the command instead.
    """
    names = list(stopped)
    if not names:
        return
    command = Command(("systemctl", "start", *names), root=True)
    if interrupted:
        system.say(f"This run stopped {', '.join(names)} and did not start them again. "
                   f"To restore service before rerunning: {command.shown()}")
        return
    system.say(f"This run stopped {', '.join(names)} and failed before starting them again; "
               "starting them now so the house keeps its automation.")
    code = perform(command, system, area, rerun, root_confirmed=root_confirmed)
    if code == 0:
        stopped.clear()
        system.say("Started the stopped services again. The installation is still incomplete. " + rerun)
    else:
        system.say(f"Could not start them again (exit status {code}); fix the reported cause, then run "
                   f"{command.shown()} or rerun.")


def run(steps: Sequence[Step], system: System, area: Path, *, dry_run: bool = False, first: int = 1,
        rerun: str = RERUN, root_confirmed: bool = False) -> int:
    """0 once every step is done. A dry run only reads: it reports each
    step and lists what it would run. Steps are numbered from `first`;
    `rerun` tells the owner how to go on after a stop. `root_confirmed` is
    only for a caller that already obtained approval for this run.

    Units a command stopped (Command.stops) and no later command started
    again (Command.starts) are started once more when the run fails, and
    named when it is interrupted."""
    stopped: dict[str, None] = {}
    try:
        status = _run(steps, system, area, dry_run=dry_run, first=first, rerun=rerun,
                      root_confirmed=root_confirmed, stopped=stopped)
    except KeyboardInterrupt:
        compensate(system, area, stopped, rerun, root_confirmed=root_confirmed, interrupted=True)
        raise
    except BaseException:
        compensate(system, area, stopped, rerun, root_confirmed=root_confirmed)
        raise
    if status:
        compensate(system, area, stopped, rerun, root_confirmed=root_confirmed)
    return status


def _record(action: Action, code: int | None, stopped: dict[str, None]) -> None:
    if not isinstance(action, Command):
        return
    # A failed stop may still have stopped some units; starting is idempotent.
    stopped.update(dict.fromkeys(action.stops))
    if code == 0:
        for name in action.starts:
            stopped.pop(name, None)


def _run(steps: Sequence[Step], system: System, area: Path, *, dry_run: bool, first: int,
         rerun: str, root_confirmed: bool, stopped: dict[str, None]) -> int:
    status = 0
    for number, step in enumerate(steps, first):
        label = f"{number}. {step.title}"
        if dry_run:
            found = step.check(system)
            system.say(f"{label}: {found.problem or ('done, ' if found.done else '') + found.detail}")
            if found.problem:
                status = 1
                continue
            if found.done and not step.always:
                continue
            for action in step.actions(system):
                system.say(f"   {'would run' if isinstance(action, Command) else 'would'}: {action.shown()}")
            continue

        with progress.Progress(system, label) as display:
            found = step.check(system)
            if found.problem:
                display.finish("FAIL", found.problem)
                display.message(f"Stopped: that needs fixing by hand first. {rerun}", error=True)
                return 1
            if found.done and not step.always:
                display.passed(found.detail)
                continue
            display.working(found.detail)
            failed = 0
            for action in step.actions(system):
                code = perform(action, system, area, rerun, root_confirmed=root_confirmed)
                _record(action, code, stopped)
                if code is None:
                    display.finish("STOP", "owner input required or declined")
                    return 1
                if code:
                    display.message(f"   failed, exit status {code}: {action.shown()}", error=True)
                    if not step.always or getattr(step, "stop_on_failure", False):
                        display.finish("FAIL")
                        display.message(f"Stopped. {rerun}", error=True)
                        return code
                    failed = code
            if step.always:
                if failed:
                    display.finish("FAIL")
                else:
                    display.passed(found.detail)
                status = status or failed
                continue
            after = step.after_actions(system)
            if after.problem or not after.done:
                display.finish("FAIL", f"still not done: {after.problem or after.detail}")
                display.message(f"Stopped. {rerun}", error=True)
                return 1
            display.passed(after.detail)
    return status
