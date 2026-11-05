"""What an install step is made of."""

from __future__ import annotations

import dataclasses
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from oh_install.system import System


@dataclasses.dataclass(frozen=True)
class Command:
    """A program, run from the area's directory; under sudo if `root`."""

    argv: tuple[str, ...]
    root: bool = False
    # Explicit child-command acknowledgement after the caller approved the
    # whole workflow. Normal invocations keep their own confirmation.
    approved_argv: tuple[str, ...] | None = None
    # The payload may prompt (including nested sudo): inherit the terminal.
    # Ordinary root helpers authenticate separately, then run with sudo -n.
    terminal: bool = False
    # Keep successful diagnostics visible through bounded, noninteractive
    # capture. Quiet mode still suppresses successful output.
    show_output: bool = False

    @property
    def full(self) -> tuple[str, ...]:
        return ("sudo", *self.argv) if self.root else self.argv

    def shown(self) -> str:
        return shlex.join(self.full)


@dataclasses.dataclass(frozen=True)
class Task:
    """Work done in Python rather than by a program: waiting, or asking the
    owner for something. `interactive` ones need a terminal."""

    description: str
    perform: Callable[[System], int]
    interactive: bool = False

    def shown(self) -> str:
        return self.description


Action = Union[Command, Task]


@dataclasses.dataclass(frozen=True)
class Check:
    """What a step found: whether it is done, in words, and anything in the
    way that none of its commands can fix."""

    done: bool
    detail: str
    problem: str | None = None


class Step:
    """One part of the install. check() only reads the host; actions() lists
    what makes check() report done. An `always` step (the final check) runs
    its actions on every run and has no done state."""

    title = ""
    always = False

    def check(self, system: System) -> Check:
        raise NotImplementedError

    def actions(self, system: System) -> list[Action]:
        return []

    def after_actions(self, system: System) -> Check:
        """Verify completed actions; ordinary installation steps read back state."""
        return self.check(system)

    def desired(self, system: System) -> list:
        """What this step installs, as manifest.Item paths and classes: the
        set an install transaction collects, and records once committed.
        It reads the host only to choose what exists (AppArmor's state)."""
        return []


class ReadinessStep(Step):
    """Hand a polling task's successful live observation to the runner once.

    check() remains a fresh observation, including dry runs and later runs.
    Only post-action verification can consume the result of the current wait.
    """

    def __init__(self):
        self._readiness_result = None

    def begin_wait(self):
        self._readiness_result = None

    def ready(self, result: Check) -> int:
        if not result.done or result.problem:
            raise ValueError("readiness completion requires a successful live check")
        self._readiness_result = result
        return 0

    def after_actions(self, system: System) -> Check:
        result, self._readiness_result = self._readiness_result, None
        return result if result is not None else self.check(system)
