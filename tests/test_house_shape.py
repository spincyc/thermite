"""The house file's shape is declared once (oh_shape.Key: oh_inventory's
ROOM, DEVICE and TOP, and each integration's and feature's DEVICE_KEYS and
HOUSE_KEYS), and the checks of key names and kinds, the reference page and
the schema all come from that declaration.

Held here: the declaration is true of every house the generator is held
to; a key it does not declare is refused wherever it declares keys; the
sentences the derived checks say are the ones the hand-written checks
said; and the page and the schema hold every declared key.
"""
import copy
import json
import sys
import types
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA))
sys.path.insert(0, str(AREA / "lib"))
import oh_integrations as integrations  # noqa: E402
import oh_inventory  # noqa: E402
import oh_render  # noqa: E402
import oh_shape  # noqa: E402
import oh_site  # noqa: E402
from oh_integrations import _hooks  # noqa: E402
from oh_shape import LIST, MAPPING, NUMBER, TEXT, Key  # noqa: E402
from tests import test_generated_output, test_vrroom_telnet, test_zone_sync  # noqa: E402

EXAMPLE = json.loads((AREA / "instance/devices.yaml").read_text(encoding="utf-8"))
UNDECLARED = "zzz_undeclared"


def houses() -> dict[str, dict]:
    """The example, a house for each integration, and the routed ones."""
    found = {name: house for name, house in test_generated_output.houses().items() if isinstance(house, dict)}
    return {"example": EXAMPLE, **found, "zones": test_zone_sync.house(), "switch": test_vrroom_telnet.house()}


def mappings(key: Key, value: object, path: tuple = ()):
    """The path of every mapping in `value` whose keys `key` declares."""
    if isinstance(value, dict) and key.keys is not None:
        yield path
        for name, inner in key.keys.items():
            if value.get(name) is not None:
                yield from mappings(inner, value[name], (*path, name))
    elif key.each is not None and isinstance(value, (dict, list)):
        for name, entry in (value.items() if isinstance(value, dict) else enumerate(value)):
            yield from mappings(key.each, entry, (*path, name))


class Declaration(unittest.TestCase):
    def test_a_key_says_what_it_holds_and_what_it_is_for(self):
        for wrong in (dict(kind="words", doc="a thing"), dict(kind=TEXT, doc=""), dict(kind=TEXT, doc="A thing."),
                      dict(kind=TEXT, doc="a | b"), dict(kind=TEXT, doc="a thing", keys={}),
                      dict(kind=NUMBER, doc="a thing", each=Key(TEXT, "a thing"))):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                Key(**wrong)
        self.assertEqual(oh_shape.words(Key((NUMBER, LIST), "a thing", each=Key(NUMBER, "one"))), "number or list of number")
        self.assertEqual(oh_shape.words(Key(MAPPING, "a thing", by="room id")), "mapping by room id")
        self.assertEqual(oh_shape.words(Key(TEXT, "a thing", choices=("a", "b"))), "a or b")
        # true and false are numbers to Python, never to a house file.
        self.assertFalse(oh_shape.fits(Key(NUMBER, "a thing"), True))

    def test_an_integration_declares_its_keys_as_keys(self):
        self.assertEqual({_hooks.HOOKS[hook].kind for hook in ("DEVICE_KEYS", "HOUSE_KEYS")}, {_hooks.KEYS})
        check = _hooks.KINDS[_hooks.KEYS]
        self.assertTrue(check({"zones": Key(NUMBER, "how many")}))
        self.assertFalse(check(("zones",)))
        self.assertFalse(check({"zones": "how many"}))
        top, device, readers = oh_inventory.declarations()
        self.assertEqual(readers["DEVICE_KEYS", "sonos_udn"], "sonos")
        self.assertEqual(readers["HOUSE_KEYS", "routing"], "routing")
        self.assertEqual(readers["HOUSE_KEYS", "control4_rooms"], "control4")
        self.assertEqual(oh_inventory.keys(), (frozenset(top), frozenset(device)))

    def test_two_modules_cannot_declare_one_key(self):
        twice = types.SimpleNamespace(__name__="twice", DEVICE_KEYS={"zones": Key(NUMBER, "how many")})
        with self.assertRaisesRegex(ValueError, "DEVICE_KEYS: twice declares zones, which another module declares too"):
            integrations.declared("DEVICE_KEYS", (integrations.denonmarantz_http, twice))

    def test_the_declaration_is_true_of_every_house_the_generator_is_held_to(self):
        shape = oh_inventory.shape()
        for name, house in houses().items():
            with self.subTest(house=name):
                self.assertEqual(oh_render.validate(copy.deepcopy(house)), [])
                self.assertEqual(oh_shape.mismatches(shape, house, "house"), [])

    def test_a_key_nothing_declares_is_refused_wherever_keys_are_declared(self):
        shape = oh_inventory.shape()
        levels = set()
        for name, house in houses().items():
            for path in mappings(shape, house):
                changed = copy.deepcopy(house)
                at = changed
                for step in path:
                    at = at[step]
                at[UNDECLARED] = "x"
                levels.add(tuple("*" if isinstance(step, int) or index and path[index - 1] in ("rooms", "sources")
                                 else step for index, step in enumerate(path)))
                with self.subTest(house=name, path=path):
                    self.assertTrue(oh_render.validate(changed), f"{UNDECLARED} was accepted")
        # Reached: the file, a room, a device, and routing down to a stage.
        self.assertLessEqual({(), ("rooms", "*"), ("devices", "*"), ("routing",), ("routing", "sources", "*"),
                              ("routing", "rooms", "*"), ("routing", "rooms", "*", "display"),
                              ("routing", "rooms", "*", "receiver"), ("routing", "rooms", "*", "hdmi_switch")}, levels)


class Sentences(unittest.TestCase):
    """What the derived checks say, word for word what an owner saw before."""

    def problems(self, change):
        data = copy.deepcopy(EXAMPLE)
        change(data)
        return oh_render.validate(data)

    def test_the_derived_checks_say_what_the_hand_written_ones_said(self):
        tv = EXAMPLE["devices"][0]["id"]
        west = next(iter(EXAMPLE["routing"]["rooms"]))
        source = next(iter(EXAMPLE["routing"]["sources"]))
        cases = (
            (lambda d: d.update(version=2), "devices.yaml: version must be 1"),
            (lambda d: d.update(romos={}), "unknown top-level key 'romos'; did you mean rooms?"),
            (lambda d: d.update(rooms=[]), "rooms must be a mapping of room ids"),
            (lambda d: d.update(devices={}), "devices must be a list of devices"),
            (lambda d: d.update(routing=[]), "routing must be a mapping"),
            (lambda d: d.update(later={}), "later must be a list of devices"),
            (lambda d: d.update(services={}), "services must be a list of service ids"),
            (lambda d: d["rooms"]["example_west"].update(colour="x"), "rooms.example_west: unknown key 'colour'"),
            (lambda d: d["devices"][0].update(modle="x"), f"{tv}: unknown key 'modle'; did you mean model?"),
            (lambda d: d["devices"][0].update(model=900), f"{tv}: model 900 must be text"),
            (lambda d: d["devices"][0].update(monitor=5), f"{tv}: monitor 5 must be text"),
            (lambda d: d["devices"][0].update(monitor="never"), f"{tv}: monitor must be required or optional"),
            (lambda d: d["devices"][0].pop("room"), f"{tv}: missing room"),
            (lambda d: d["devices"][0].pop("ip"), f"{tv}: missing ip"),
            (lambda d: d["devices"][0].update(controls=[]), f"{tv}: controls must be a mapping of phase to integration names"),
            (lambda d: d["devices"][0]["controls"].update(observe="sony_rest"),
             f"{tv}: controls.observe must be a list of integration names"),
            (lambda d: d["routing"].update(matrix="x"), "routing: unknown key 'matrix'"),
            (lambda d: d["routing"]["sources"][source].update(input=1), f"routing.sources.{source}: unknown key 'input'"),
            (lambda d: d["routing"]["rooms"][west].update(output=1), f"routing.rooms.{west}: unknown key 'output'"),
            (lambda d: d["routing"]["rooms"][west]["display"].update(port=1), f"routing.rooms.{west}.display: unknown key 'port'"),
            (lambda d: d["routing"].update(edid={"idel": "x"}), "routing.edid: unknown key 'idel'"),
        )
        for change, sentence in cases:
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, self.problems(change))

    def test_true_is_not_a_version(self):
        self.assertEqual(self.problems(lambda d: d.update(version=True)), ["devices.yaml: version must be 1"])


class Reference(unittest.TestCase):
    def test_the_page_holds_every_declared_key_with_what_it_is_for(self):
        page = oh_site.house_reference()
        top, device, readers = oh_inventory.declarations()
        for name, key in (*top.items(), *device.items()):
            with self.subTest(key=name):
                self.assertIn(f"| `{name}` | {oh_shape.words(key)}", page)
        for path, key in oh_shape.rows({"routing": top["routing"]}):
            with self.subTest(key=path):
                self.assertIn(key.doc[:1].upper() + key.doc[1:] + ".", page)
        self.assertIn("| `routing.rooms.<room id>.display.passive` | true or false |", page)
        self.assertNotIn("receiver.passive", page)
        self.assertIn("| `sonos_udn` | text |", page)
        self.assertIn("| sonos |", page)
        built = oh_site.build()["house-file.html"].decode()
        self.assertIn("<code>routing.rooms.&lt;room id&gt;.hdmi_output</code>", built)
        self.assertNotIn("{{house_keys}}", built)
        self.assertIn('href="house-file.html"', oh_site.build()["start.html"].decode())

    def test_the_schema_holds_the_same_keys_and_is_published(self):
        schema = oh_site.house_schema()
        top, device, _ = oh_inventory.declarations()
        self.assertEqual(set(schema["properties"]), set(top))
        self.assertEqual(set(schema["properties"]["devices"]["items"]["properties"]), set(device))
        self.assertEqual(schema["required"], ["version"])
        self.assertEqual(schema["properties"]["devices"]["items"]["required"], ["id", "room", "kind"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["later"]["type"], ["array", "null"])
        rooms = schema["properties"]["routing"]["properties"]["rooms"]["additionalProperties"]["properties"]
        self.assertEqual(rooms["usb_unused"]["enum"], ["preserve", "park"])
        self.assertEqual(json.loads(oh_site.build()["house.schema.json"]), schema)
        self.assertTrue(oh_site.BUILT.fullmatch("docs/house.schema.json"))


if __name__ == "__main__":
    unittest.main()
