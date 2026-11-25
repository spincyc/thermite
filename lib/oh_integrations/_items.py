"""Shared Item and command-guard builders for integration modules.

Always emits `commandFromItemScript` on `transform:JS` profiles — never
openHAB's deprecated `toHandlerScript`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from oh_conf import GUARD_PROFILE, options_text


def enum_guard_js(allowed: Mapping[str, str] | Iterable[str], *, otherwise: str = "null",
                  wrap: bool = False) -> str:
    """Inline JS allowlist guard: passes only values in `allowed`, else `otherwise`.

    `allowed` may be a mapping `{command: transformed}` or an iterable of
    allowed command strings (mapped to themselves). Returns `"|..."` for
    `transform:JS` profiles (`commandFromItemScript` / `toItemScript`), or
    `"JS(|...)"` when `wrap=True` (HTTP binding channel transformations).
    """
    table = dict(allowed) if isinstance(allowed, Mapping) else {str(value): str(value) for value in allowed}
    expr = (f"|(function (table) {{ return Object.prototype.hasOwnProperty.call(table, input) ? table[input] : "
            f"{otherwise}; }})({json.dumps(table, separators=(',', ':'))})")
    return f"JS({expr})" if wrap else expr


def control_point(
    device_id: str,
    type_: str,
    label: str,
    tags: list[str],
    channel: str | None = None,
    *,
    icon: str | None = None,
    link: dict | None = None,
    state: dict | None = None,
    options: object = None,
    command_options: object = None,
    metadata: dict | None = None,
) -> dict:
    """A commanded point Item (`autoupdate: False`), optionally linked to a
    channel directly or through a `transform:JS` profile using
    `commandFromItemScript` (never deprecated `toHandlerScript`).

    `options` populates both `stateDescription` and `commandDescription`
    unless `command_options` is passed (pass `""` to omit `commandDescription`
    or a separate sequence/string for differing read vs command options).
    """
    if link is not None and "toHandlerScript" in link:
        raise ValueError("use commandFromItemScript, never deprecated toHandlerScript")
    item: dict = {"type": type_, "label": label}
    if icon:
        item["icon"] = icon
    item.update(groups=[device_id], tags=list(tags), autoupdate=False)
    if channel is not None:
        if link is None:
            item["channel"] = channel
        else:
            item["channels"] = {channel: {"profile": GUARD_PROFILE, **link}}
    meta: dict = dict(metadata) if metadata else {}
    state_cfg: dict = dict((meta.get("stateDescription") or {}).get("config") or {})
    if state:
        state_cfg.update(state)
    read_opts = options if isinstance(options, str) else (options_text(options) if options else None)
    if read_opts:
        state_cfg["options"] = read_opts
    if state_cfg:
        meta["stateDescription"] = {"config": state_cfg}
    cmd_src = options if command_options is None else command_options
    cmd_opts = cmd_src if isinstance(cmd_src, str) else (options_text(cmd_src) if cmd_src else None)
    if cmd_opts:
        meta["commandDescription"] = {"config": {"options": cmd_opts}}
    if meta:
        item["metadata"] = meta
    return item
