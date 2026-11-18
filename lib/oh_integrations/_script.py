"""What the integrations with a rule of their own share in rendering it:
the rule's text from its source, one script per device, and a rendered
script pointed at a stand-in. What a rule says to its device stays in its
integration.
"""

from __future__ import annotations

import json
from pathlib import Path

from oh_conf import RULE_SENDS, compact, constant, fill
from . import _io

#: How a rendered script's device table starts (fill()).
DEVICES = "const DEVICES = "


def rule(source: Path, integration: str, constants: dict[str, object], *, connects: bool = True,
         encode=compact, header: bool = True) -> str:
    """An integration's rule: its source with what the script integrations
    share embedded (_io), the bounded I/O too in a rule that opens
    connections of its own (`connects`), and its constants filled in
    (oh_conf.fill, which takes `encode` and `header`). The source holds
    RULE_SENDS, which a read-only copy turns off (oh_render.read_only)."""
    text = source.read_text(encoding="utf-8")
    text = _io.embed(text, integration) if connects else _io.shared(text, integration)
    return fill(text, constants, source, keep=(RULE_SENDS,), encode=encode, header=header)


def per_device(rule_file: str, rule):
    """(rule_path, files): the hooks of an integration that renders one
    script per device, each rule([device]) at a path named after
    `rule_file` and the device. A script is one context: its rules run one
    at a time, so a device's polls and commands stay ordered and a slow
    device holds up no other."""
    def rule_path(device: dict) -> str:
        return _io.script_path(rule_file, device)

    def files(devices: list[dict], house) -> dict[str, str]:
        return {rule_path(device): rule([device]) for device in devices}
    return rule_path, files


def aimed(files: dict[str, str], paths: tuple[str, ...], device: dict, change) -> dict[str, str]:
    """`files` with the device's entry of a rendered script's device table
    changed by change(entry): how an integration's aim() points its rule at
    a stand-in. The script is the first of `paths` among the files."""
    path = next((path for path in paths if path in files), None)
    if path is None:
        raise ValueError(f"{paths[0]} has no device {device['id']}")
    lines = files[path].split("\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(DEVICES))
    entries = json.loads(lines[index][len(DEVICES):-1])
    matching = [entry for entry in entries if entry["id"] == device["id"]]
    if not matching:
        raise ValueError(f"{path} has no device {device['id']}")
    for entry in matching:
        change(entry)
    lines[index] = constant("DEVICES", entries)
    return {**files, path: "\n".join(lines)}
