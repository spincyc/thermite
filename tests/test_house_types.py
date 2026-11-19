"""The house file's types are held to its declared shape.

oh_inventory writes the house file down twice: as declarations (Key), from
which validate, the reference page and the schema come, and as types
(HouseFile, Room, Device), which the type checker reads. Nothing derives
the second from the first, so these tests compare them, all the way down:
every key the declarations name, the core's and each integration's, is in
the type with the kind the declaration gives it, required where it is
required, and the type has no other key.
"""
from __future__ import annotations

import sys
import types
import typing
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_inventory  # noqa: E402
from oh_shape import LIST, MAPPING, NUMBER, TEXT, TRUTH, Key  # noqa: E402

#: The mappings typed without their keys although the keys are declared,
#: and why. A path here that is typed after all fails, so this only shrinks.
OPEN = {"routing": "its keys are the routing feature's, to be typed when that feature's modules are checked"}
KINDS = {str: TEXT, bool: TRUTH, int: NUMBER, float: NUMBER}


def kind(member: object) -> str:
    """The declared kind a type holds."""
    origin = typing.get_origin(member)
    if origin is typing.Literal:
        (found,) = {KINDS[type(value)] for value in typing.get_args(member)}
        return found
    if origin is list:
        return LIST
    if origin is dict or typing.is_typeddict(member):
        return MAPPING
    return KINDS[member]


def keys_differ(typed: type, keys: dict[str, Key], where: str = "") -> list[str]:
    """Where a mapping's type differs from its declared keys."""
    hints = typing.get_type_hints(typed, include_extras=True)
    found = [f"{where}{name}: the type lacks it" for name in keys if name not in hints]
    found += [f"{where}{name}: nothing declares it" for name in hints if name not in keys]
    for name, hint in hints.items():
        if name not in keys:
            continue
        mark = typing.get_origin(hint)
        required = mark is typing.Required or (mark is not typing.NotRequired and typed.__total__)
        if required != keys[name].required:
            found.append(f"{where}{name}: required in one and not in the other")
        inner = typing.get_args(hint)[0] if mark in (typing.Required, typing.NotRequired) else hint
        found += differ(inner, keys[name], where + name)
    return found


def differ(annotation: object, key: Key, where: str) -> list[str]:
    """Where a type differs from a key's declaration."""
    union = typing.get_origin(annotation) in (typing.Union, types.UnionType)
    members = typing.get_args(annotation) if union else (annotation,)
    if (type(None) in members) != key.empty:
        return [f"{where}: None in one and not in the other"]
    held = {kind(member): member for member in members if member is not type(None)}
    if set(held) != set(key.kinds()):
        return [f"{where}: the type holds {sorted(held)}, declared {sorted(key.kinds())}"]
    found = []
    for name, member in held.items():
        choices = typing.get_args(member) if typing.get_origin(member) is typing.Literal else ()
        if set(choices) != set(key.choices):
            found.append(f"{where}: the type's choices are {sorted(choices)}, declared {sorted(key.choices)}")
        if name == LIST and key.each is not None:
            found += differ(typing.get_args(member)[0], key.each, where + "[]")
        if name != MAPPING:
            continue
        if typing.is_typeddict(member):
            if key.keys is None or where in OPEN:
                found.append(f"{where}: typed with keys, {'listed as open' if where in OPEN else 'declared without'}")
            else:
                found += keys_differ(member, key.keys, where + ".")
        elif key.keys is not None:
            found += [] if where in OPEN else [f"{where}: its declared keys are not typed"]
        elif key.each is not None:
            found += differ(typing.get_args(member)[1], key.each, where + ".<>")
    return found


class Types(unittest.TestCase):
    def test_the_house_file_is_typed_as_it_is_declared(self):
        """The whole file: its top level, a room, a device with every
        integration's keys, and each mapping under them."""
        self.assertEqual(keys_differ(oh_inventory.HouseFile, oh_inventory.shape().keys), [])

    def test_a_device_and_a_room_are_the_house_files_own(self):
        _, device, _ = oh_inventory.declarations()
        self.assertEqual(keys_differ(oh_inventory.Device, device), [])
        self.assertEqual(keys_differ(oh_inventory.Room, oh_inventory.ROOM), [])

    def test_a_type_that_differs_from_its_declaration_is_told(self):
        """The comparison itself: a key the type lacks, one nothing
        declares, another kind, required against optional, a choice, None,
        and a mapping's keys left untyped."""
        class Inner(typing.TypedDict):
            port: int

        Typed = typing.TypedDict("Typed", {
            "id": typing.NotRequired[str], "extra": str, "count": str, "mode": typing.Literal["a", "b"],
            "names": list[int], "rooms": dict[str, str] | None, "inner": Inner, "plain": dict[str, typing.Any]})
        text = Key(TEXT, "x")
        keys = {"id": Key(TEXT, "x", required=True), "gone": text, "count": Key(NUMBER, "x", required=True),
                "mode": Key(TEXT, "x", required=True, choices=("a", "c")),
                "names": Key(LIST, "x", required=True, each=text),
                "rooms": Key(MAPPING, "x", required=True, each=text),
                "inner": Key(MAPPING, "x", required=True, keys={"host": text}),
                "plain": Key(MAPPING, "x", required=True, keys={"host": text})}
        self.assertEqual(keys_differ(Typed, keys), [
            "gone: the type lacks it",
            "extra: nothing declares it",
            "id: required in one and not in the other",
            "count: the type holds ['text'], declared ['number']",
            "mode: the type's choices are ['a', 'b'], declared ['a', 'c']",
            "names[]: the type holds ['number'], declared ['text']",
            "rooms: None in one and not in the other",
            "inner.host: the type lacks it",
            "inner.port: nothing declares it",
            "plain: its declared keys are not typed"])


if __name__ == "__main__":
    unittest.main()
