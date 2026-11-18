"""How a page reaches the house: through the gateway, in everything thermite
generate writes and an install serves, or at the runtime directly (native), in
the copy of conf/ a sandbox runs on, which has no gateway (oh_render.direct).

Native mode is a testing mode, never an authentication fallback, and no house
file selects it. This setting does not edit account, credential or service
state.
"""

import json

MODES = ("authenticated", "native")


def script(mode: str = "authenticated") -> str:
    """No inventory or identity data is needed before the access boundary."""
    if mode not in MODES:
        raise ValueError(f"no page mode {mode!r}")
    return "globalThis.THERMITE_ACCESS_CONFIG = Object.freeze(" + json.dumps(
        {"version": 1, "mode": mode}, separators=(",", ":")) + ");\n"
