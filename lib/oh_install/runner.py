"""Run the install's steps: check each, and do what is not done yet."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from oh_install.model import Action, Command, Step, Task
from oh_install.system import System

RERUN = "Run bin/thermite install again to go on from here."


def perform(action: Action, system: System, area: Path, rerun: str = RERUN, *,
            root_confirmed: bool = False) -> int | None:
    """The action's exit status, or None when the owner said no or no
    terminal is there to ask."""
    root = isinstance(action, Command) and action.root
    if (root or isinstance(action, Task) and action.interactive) and not system.interactive():
        system.say(f"   needs you at a terminal: {action.shown()}")
        system.say(f"Stopped. {rerun}")
        return None
    if root:
        system.say(f"   next, under sudo: {action.shown()}")
        if not root_confirmed and system.ask("   Run it? [y/N] ").strip().lower() not in ("y", "yes"):
            system.say(f"Stopped; nothing more was run. {rerun}")
            return None
    else:
        system.say(f"   {'running' if isinstance(action, Command) else 'now'}: {action.shown()}")
    if isinstance(action, Command):
        return system.run(action.full, area)
    return action.perform(system)


def run(steps: Sequence[Step], system: System, area: Path, *, dry_run: bool = False, first: int = 1,
        rerun: str = RERUN, root_confirmed: bool = False) -> int:
    """0 once every step is done. A dry run only reads: it reports each
    step and lists what it would run. Steps are numbered from `first`;
    `rerun` tells the owner how to go on after a stop. `root_confirmed` is
    only for a caller that already obtained approval for this run."""
    status = 0
    for number, step in enumerate(steps, first):
        label = f"{number}. {step.title}"
        found = step.check(system)
        if found.problem:
            system.say(f"{label}: {found.problem}")
            if not dry_run:
                system.say(f"Stopped: that needs fixing by hand first. {rerun}")
                return 1
            status = 1
            continue
        if found.done and not step.always:
            system.say(f"{label}: done, {found.detail}")
            continue
        system.say(f"{label}: {found.detail}")
        actions = step.actions(system)
        if dry_run:
            for action in actions:
                system.say(f"   {'would run' if isinstance(action, Command) else 'would'}: {action.shown()}")
            continue
        failed = 0
        for action in actions:
            code = perform(action, system, area, rerun, root_confirmed=root_confirmed)
            if code is None:
                return 1
            if code:
                system.say(f"   failed, exit status {code}: {action.shown()}")
                if not step.always:
                    system.say(f"Stopped. {rerun}")
                    return code
                failed = code
        if step.always:
            status = status or failed
            continue
        after = step.check(system)
        if after.problem or not after.done:
            system.say(f"   still not done: {after.problem or after.detail}")
            system.say(f"Stopped. {rerun}")
            return 1
        system.say(f"   done, {after.detail}")
    return status
