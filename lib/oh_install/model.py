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
