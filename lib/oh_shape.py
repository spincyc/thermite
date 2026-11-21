"""The shape of a configuration file, declared once: each key, what kind of
value it holds and what it is for.

A Key is one key's declaration. A mapping's fixed keys are Keys under
`keys`; what a list holds, or a mapping whose keys the owner chooses (room
ids, device ids), is the one Key under `each`. Whoever reads a key declares
it: the inventory core its own (oh_inventory), an integration or a feature
the ones it reads (its DEVICE_KEYS and HOUSE_KEYS, oh_integrations._hooks).

Three things are derived from a declaration, so none of them is written by
hand beside it: the checks of key names and of kinds of value
(unknown(), fits(), mismatches()), the reference page of the docs site
(rows(), oh_site.house_reference()) and a JSON Schema an editor can check a
file against (schema()). What a value means beyond its kind (that a room id
names a room, that two devices do not share an address) stays with the
module that reads it, in its own words.

This module imports nothing of thermite's, so anything may declare with it.
"""

from __future__ import annotations

from dataclasses import dataclass

TEXT, NUMBER, TRUTH, LIST, MAPPING = "text", "number", "true or false", "list", "mapping"
#: Whether a parsed value is of each kind. true and false are numbers to
#: Python and never to a house file.
KINDS = {
    TEXT: lambda value: isinstance(value, str),
    NUMBER: lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    TRUTH: lambda value: isinstance(value, bool),
    LIST: lambda value: isinstance(value, list),
    MAPPING: lambda value: isinstance(value, dict),
}
#: Each kind's name in JSON Schema.
JSON = {TEXT: "string", NUMBER: "number", TRUTH: "boolean", LIST: "array", MAPPING: "object"}


@dataclass(frozen=True)
class Key:
    #: One of KINDS, or several where a value may be of any of them.
    kind: str | tuple[str, ...]
    #: What it is for, as the reference page says it: a sentence without
    #: its full stop.
    doc: str
    #: A file without it is refused.
    required: bool = False
    #: A mapping's keys, where they are fixed names.
    keys: dict[str, Key] | None = None
    #: What each entry of a list is, or each value of a mapping whose keys
    #: the owner chooses.
    each: Key | None = None
    #: What those chosen keys are ("room id"), for such a mapping.
    by: str = ""
    #: The values it may have, where they are few.
    choices: tuple = ()
    #: What the file's own check says a value of another kind must be ("a
    #: mapping of room ids"). Empty where the module that reads the key
    #: refuses it in its own words.
    must: str = ""
    #: Standing empty (`later:` above entries that are all commented out)
    #: is the same as leaving it out.
    empty: bool = False

    def __post_init__(self):
        kinds = self.kinds()
        if not kinds or any(kind not in KINDS for kind in kinds):
            raise ValueError(f"{self.kind!r} is not a kind of value")
        if not self.doc.strip() or self.doc.endswith(".") or "|" in self.doc or "\n" in self.doc:
            raise ValueError(f"{self.doc!r}: a key's doc is one sentence without its full stop, a line break or a |")
        if (self.keys is not None or self.by) and MAPPING not in kinds:
            raise ValueError("only a mapping has keys")
        if self.each is not None and not {LIST, MAPPING} & set(kinds):
            raise ValueError("only a list or a mapping has entries")

    def kinds(self) -> tuple[str, ...]:
        return (self.kind,) if isinstance(self.kind, str) else tuple(self.kind)


def of_kind(key: Key, value: object) -> bool:
    """Whether the value is of the key's kind."""
    return any(KINDS[kind](value) for kind in key.kinds())


def fits(key: Key, value: object) -> bool:
    """Whether the value is of the key's kind, and one of its choices."""
    return of_kind(key, value) and (not key.choices or value in key.choices)


def unknown(keys: dict[str, Key], mapping: dict) -> list:
    """The keys of `mapping` that `keys` does not declare, in its order."""
    return [name for name in mapping if name not in keys]


def words(key: Key) -> str:
    """The kind of value a key holds, for a person: "list of text"."""
    if key.choices:
        return " or ".join(str(choice) for choice in key.choices)
    out = []
    for kind in key.kinds():
        if kind == LIST and key.each is not None:
            out.append(f"list of {words(key.each)}")
        elif kind == MAPPING and key.by:
            out.append(f"mapping by {key.by}")
        else:
            out.append(kind)
    return " or ".join(out)


def mismatches(key: Key, value: object, where: str) -> list[str]:
    """Where a value differs from its declaration, all the way down: a
    value of another kind, a key nothing declares, a required key left
    out. A null is a key left out, as the readers take it."""
    if not fits(key, value):
        return [f"{where}: {value!r} is not {words(key)}"]
    found: list[str] = []
    if isinstance(value, dict):
        if key.keys is not None:
            found += [f"{where}: unknown key '{name}'" for name in unknown(key.keys, value)]
            for name, inner in key.keys.items():
                if value.get(name) is None:
                    found += [f"{where}: missing {name}"] if inner.required else []
                else:
                    found += mismatches(inner, value[name], f"{where}.{name}")
        elif key.each is not None:
            for name, entry in value.items():
                found += mismatches(key.each, entry, f"{where}.{name}")
    elif isinstance(value, list) and key.each is not None:
        for index, entry in enumerate(value):
            found += mismatches(key.each, entry, f"{where}[{index}]")
    return found


def rows(keys: dict[str, Key], prefix: str = "") -> list[tuple[str, Key]]:
    """(path, Key) of every declared key under `keys`, each mapping's own
    keys after it: `rooms.<room id>.label`, `devices[].id`."""
    out: list[tuple[str, Key]] = []
    for name, key in keys.items():
        path = prefix + name
        out.append((path, key))
        inner = key
        while inner is not None:
            if inner.keys is not None:
                out += rows(inner.keys, path + ".")
                break
            if inner.each is None:
                break
            path += f".<{inner.by}>" if inner.by else "[]"
            inner = inner.each
    return out


def schema(key: Key) -> dict:
    """The key as JSON Schema (draft 2020-12): names and kinds, which is
    less than thermite check house checks."""
    kinds = [JSON[kind] for kind in key.kinds()] + (["null"] if key.empty else [])
    out: dict = {"description": key.doc, "type": kinds[0] if len(kinds) == 1 else kinds}
    if key.choices:
        out["enum"] = list(key.choices)
    if key.keys is not None:
        out["properties"] = {name: schema(inner) for name, inner in key.keys.items()}
        out["additionalProperties"] = False
        required = [name for name, inner in key.keys.items() if inner.required]
        if required:
            out["required"] = required
    elif key.each is not None:
        out["additionalProperties" if MAPPING in key.kinds() and LIST not in key.kinds() else "items"] = schema(key.each)
    return out
