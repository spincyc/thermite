"""What thermite does outside itself: a command it runs, a request it sends.

A library says it here, and imports nothing of the program to do so. Each
line goes to the logger NAME, at DEBUG, of the standard logging module.
The CLI shows them on stderr under -v (oh_cli attaches the handler); used
without the CLI, nothing is printed.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Sequence
from pathlib import Path

#: The logger the lines go to, and what the CLI attaches its -v handler to
#: (oh_cli.TRACE: the CLI's package runs where this module is not installed,
#: so it names the logger itself).
NAME = "thermite.trace"

_LOG = logging.getLogger(NAME)


def trace(what: str) -> None:
    """Something thermite does outside itself."""
    _LOG.debug("%s", what)


def run(argv: Sequence[object], cwd: Path | str | None = None) -> None:
    """A command about to run, as a shell would take it."""
    trace("run " + shlex.join(str(part) for part in argv) + (f"   (in {cwd})" if cwd else ""))


def http(method: str, url: str) -> None:
    """A request about to be sent."""
    trace(f"{method} {url}")
