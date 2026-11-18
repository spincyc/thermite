"""Inline bounded native I/O, and what device scripts share, into each
device's independent rule context."""

from pathlib import Path

from oh_conf import fill
from . import _commands

SOURCE = Path(__file__).with_suffix(".js")
MARK = "// THERMITE_BOUNDED_IO"
#: The parts the script integrations share (_device.js). Its connections use
#: thermiteIO, so in a script that opens one it goes after MARK; a script
#: that needs only the I/O (the routing lifecycle module) leaves it out, and
#: one that opens no connection of its own takes it alone (shared()).
DEVICE = Path(__file__).with_name("_device.js")
DEVICE_MARK = "// THERMITE_DEVICE"


def device(integration: str) -> str:
    """_device.js as `integration`'s script embeds it: with the command
    record's vocabulary and the suffix of its tally Items (_commands)."""
    return fill(DEVICE.read_text(encoding="utf-8"), {_commands.TABLE: _commands.table(integration)}, DEVICE,
                header=False)


def _marked(source: str) -> int:
    shared = source.splitlines().count(DEVICE_MARK)
    if shared > 1 or source.count(DEVICE_MARK) != shared:
        raise ValueError("a driver may include the shared device marker once, on its own line")
    return shared


def embed(source: str, integration: str | None = None) -> str:
    """`source` with the bounded I/O and, where it marks it, the shared
    device part, which then needs the name of its integration."""
    if source.count(MARK) != 1:
        raise ValueError("a driver must include exactly one bounded I/O marker")
    shared = _marked(source)
    if shared and source.index(DEVICE_MARK) < source.index(MARK):
        raise ValueError("the shared device marker must follow the bounded I/O marker")
    if shared and not integration:
        raise ValueError("a driver with the shared device marker must name its integration")
    source = source.replace(MARK, SOURCE.read_text(encoding="utf-8"))
    return source.replace(DEVICE_MARK, device(integration)) if shared else source


def shared(source: str, integration: str) -> str:
    """`source` with the shared device part alone: a script that commands
    its device through openHAB opens no connection of its own."""
    if MARK in source or _marked(source) != 1:
        raise ValueError("a script without bounded I/O must include the shared device marker once, on its own line")
    return source.replace(DEVICE_MARK, device(integration))


def script_path(rule: str, device: dict) -> str:
    return f"{rule[:-3]}_{device['id']}.js"
