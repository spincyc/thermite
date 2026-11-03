"""Inline bounded native I/O into each device's independent rule context."""

from pathlib import Path

SOURCE = Path(__file__).with_suffix(".js")
MARK = "// THERMITE_BOUNDED_IO"


def embed(source: str) -> str:
    if source.count(MARK) != 1:
        raise ValueError("a driver must include exactly one bounded I/O marker")
    return source.replace(MARK, SOURCE.read_text(encoding="utf-8"))


def script_path(rule: str, device: dict) -> str:
    return f"{rule[:-3]}_{device['id']}.js"
