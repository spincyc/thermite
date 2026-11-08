"""Inline bounded native I/O, and what device scripts share, into each
device's independent rule context."""

from pathlib import Path

SOURCE = Path(__file__).with_suffix(".js")
MARK = "// THERMITE_BOUNDED_IO"
#: The parts the script integrations share (_device.js). It uses thermiteIO,
#: so it goes after MARK; a script that needs only the I/O (the routing
#: lifecycle module) leaves it out.
DEVICE = Path(__file__).with_name("_device.js")
DEVICE_MARK = "// THERMITE_DEVICE"


def embed(source: str) -> str:
    if source.count(MARK) != 1:
        raise ValueError("a driver must include exactly one bounded I/O marker")
    lines = source.splitlines()
    shared = lines.count(DEVICE_MARK)
    if shared > 1 or source.count(DEVICE_MARK) != shared:
        raise ValueError("a driver may include the shared device marker once, on its own line")
    if shared and source.index(DEVICE_MARK) < source.index(MARK):
        raise ValueError("the shared device marker must follow the bounded I/O marker")
    source = source.replace(MARK, SOURCE.read_text(encoding="utf-8"))
    return source.replace(DEVICE_MARK, DEVICE.read_text(encoding="utf-8")) if shared else source


def script_path(rule: str, device: dict) -> str:
    return f"{rule[:-3]}_{device['id']}.js"
