"""Explicit site access policy, shared by generated pages and the TUI launcher.

Native mode is an owner-selected testing mode, never an authentication fallback.
This setting does not edit account, credential or service state.
"""

import json

MODES = ("authenticated", "native")


def validate(data: dict) -> list[str]:
    if "access" not in data:
        return []
    value = data["access"]
    if not isinstance(value, dict) or set(value) != {"mode"} or value.get("mode") not in MODES:
        return ["access: must contain only mode: authenticated or mode: native"]
    return []


def mode(data: dict) -> str:
    problems = validate(data)
    if problems:
        raise ValueError(problems[0])
    return data.get("access", {}).get("mode", "authenticated")


def script(data: dict) -> str:
    """No inventory or identity data is needed before the access boundary."""
    return "globalThis.THERMITE_ACCESS_CONFIG = Object.freeze(" + json.dumps(
        {"version": 1, "mode": mode(data)}, separators=(",", ":")) + ");\n"
