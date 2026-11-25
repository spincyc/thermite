"""A house file becomes a House in one place (oh_conf.House.load), which
validates it, as constructing one from a mapping does; and a house file
that is wrong in shape ends in a ConfigError that names the problem,
whatever is wrong with it.

The mutation tests are the guard for the second. They take each fixture
house, replace each of its values in turn by a null, a text, a number, a
list and a mapping, and remove each key and list entry; each changed house
is then loaded as a house file would be, and the example's go through the
verbs as well. Any outcome but "valid" or ConfigError (a TypeError from a
validator that looked a list up in a dict, say) fails, naming the change
and where it raised.
"""
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import signal
import socket
import sys
import tempfile
import traceback
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA))
sys.path.insert(0, str(AREA / "lib"))
import oh_cli  # noqa: E402
import oh_inventory as inventory  # noqa: E402
import oh_paths  # noqa: E402
from oh_cli import generate, validate  # noqa: E402
from oh_conf import House  # noqa: E402
from tests import test_generated_output  # noqa: E402

HOUSE = AREA / "instance/devices.yaml"
EXAMPLE = inventory.load(HOUSE)

#: What takes a value's place: one of each kind a house file can hold, the
#: empty list and mapping (`x or {}` reads them as absent), and a number
#: that is no integer. REMOVED takes the key or the list entry away.
REMOVED = object()
REPLACEMENTS = (None, "x", 7, 1.5, True, [], ["x"], {}, {"x": 1}, REMOVED)
#: The most changed houses one house may make: the houses are fixed, so this
#: only stops a house that grew from making the suite slow unnoticed.
MOST = 3000
#: Seconds one changed house may take (a valid one generates in about 0.2).
DEADLINE = 20
#: The most failures a test lists.
SHOWN = 20


def run(*argv) -> tuple[int, str]:
    """(exit status, what it wrote to stderr) of `thermite ARGV`."""
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
            mock.patch.object(oh_paths, "require_compatible"):
        return oh_cli.main(list(argv)), err.getvalue()


class Stuck(BaseException):
    """A changed house took over DEADLINE seconds: nothing may catch this."""


def places(node, prefix=()):
    """The path of every value below `node`, outermost first."""
    for key, value in node.items() if isinstance(node, dict) else enumerate(node):
        yield (*prefix, key)
        if isinstance(value, (dict, list)):
            yield from places(value, (*prefix, key))


def changes(house: dict):
    """(description, changed copy of `house`) for every replacement of every
    value in it, one at a time."""
    for place in places(house):
        current = house
        for key in place:
            current = current[key]
        for value in REPLACEMENTS:
            if value is not REMOVED and type(value) is type(current) and value == current:
                continue
            changed = copy.deepcopy(house)
            holder = changed
            for key in place[:-1]:
                holder = holder[key]
            if value is REMOVED:
                del holder[place[-1]]
            else:
                holder[place[-1]] = value
            where = ".".join(str(key) for key in place)
            yield (f"{where} removed" if value is REMOVED else f"{where} = {value!r}"), changed


class Changed(unittest.TestCase):
    """Runs changed houses, each as the content of the house file."""

    def setUp(self):
        self.content = None
        read = inventory.load
        patched = mock.patch.object(inventory, "load",
                                    lambda path: self.content if Path(path) == HOUSE else read(path))
        patched.start()
        self.addCleanup(patched.stop)

        def stuck(*_):
            raise Stuck()
        before = signal.signal(signal.SIGALRM, stuck)
        self.addCleanup(signal.signal, signal.SIGALRM, before)
        self.addCleanup(signal.alarm, 0)

    def survey(self, house: dict, use) -> tuple[int, int]:
        """use() on every changed house; (how many there were, how many were
        valid). Fails naming each that raised anything but ConfigError."""
        count = valid = 0
        failures = []
        for what, self.content in changes(house):
            count += 1
            self.assertLessEqual(count, MOST, "the house has grown: mutate a part of it, or sample")
            signal.alarm(DEADLINE)
            try:
                use()
                valid += 1
            except inventory.ConfigError as error:
                self.assertTrue(error.problems, f"{what}: {error}")
            except Stuck:
                self.fail(f"{what}: still running after {DEADLINE} s")
            except Exception as error:
                frame = traceback.extract_tb(error.__traceback__)[-1]
                failures.append(f"{what}: {type(error).__name__}: {error} "
                                f"({Path(frame.filename).relative_to(AREA)}:{frame.lineno})")
            finally:
                signal.alarm(0)
        if failures:
            self.fail(f"{len(failures)} of {count} changed houses raised something other than ConfigError:\n"
                      + "\n".join(failures[:SHOWN]))
        return count, valid


class Mutations(Changed):
    def setUp(self):
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def verbs(self):
        """thermite validate, then thermite generate --check, on the house
        file; each as its main() runs, into an empty tree. A changed house
        that loads builds a conf that agrees with it (oh_render.build), so
        validate finds no problem."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(generate, "AREA", AREA):
            self.assertEqual(validate.main(["--house", str(HOUSE)]), 0)
            # Everything is missing from the empty tree, hence 1; nothing is written.
            self.assertEqual(generate.main(["--check", "--house", str(HOUSE), "--conf", str(self.root / "none/conf"),
                                            "--root", str(self.root / "none")]), 1)

    def test_every_single_change_to_the_example_house_validates_and_generates_or_is_a_config_error(self):
        count, valid = self.survey(EXAMPLE, self.verbs)
        # Both outcomes occur, so the survey reaches the generator as well as the validators.
        self.assertGreater(valid, 20)
        self.assertGreater(count - valid, 500)

    def test_every_single_change_to_every_other_fixture_house_loads_or_is_a_config_error(self):
        # One house per integration and phase, and the routed house (zones
        # that follow one another, an HDMI switch, USB, EDID): the fields
        # and the routing the example has none of. These are only loaded;
        # generating each valid one as well takes over a minute.
        houses = {name: house for name, house in test_generated_output.houses().items() if isinstance(house, dict)}
        self.assertGreater(len(houses), 20)
        for name, house in houses.items():
            with self.subTest(house=name):
                count, valid = self.survey(house, lambda: House.load(HOUSE))
                self.assertGreater(valid, 10)
                self.assertGreater(count - valid, 100)

    def test_a_house_with_rooms_and_no_devices_key_generates(self):
        with tempfile.TemporaryDirectory() as scratch:
            house = Path(scratch) / "devices.yaml"
            house.write_text(json.dumps({"version": 2, "rooms": {"den": {"label": "Den", "tag": "Room"}}}))
            (Path(scratch) / "out").mkdir()
            self.assertIn("conf/yaml/model.yaml", test_generated_output.written(house, Path(scratch) / "out"))


class Loader(unittest.TestCase):
    def test_a_loaded_house_indexes_the_mapping_it_read_and_copies_nothing(self):
        house = House.load(HOUSE)
        self.assertEqual(house.data, EXAMPLE)
        self.assertEqual(list(house.by_id), [device["id"] for device in EXAMPLE["devices"]])
        for listed, device in zip(house.data["devices"], house.devices, strict=True):
            self.assertIs(device, listed)
            self.assertIs(house.by_id[device["id"]], listed)
        self.assertIs(house.rooms, house.data["rooms"])

    def test_a_house_with_problems_is_a_config_error_naming_each(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "devices.yaml"
            path.write_text(json.dumps({**EXAMPLE, "devices": [{**EXAMPLE["devices"][0], "model": 900}, "x"]}))
            with self.assertRaises(inventory.ConfigError) as caught:
                House.load(path)
        self.assertEqual(str(caught.exception), f"{path}: 2 problems; nothing done")
        self.assertEqual(len(caught.exception.problems), 2)

    def test_a_house_file_of_the_wrong_shape_exits_2_with_a_message_from_every_verb_that_reads_it(self):
        """But check, which found the problem, exits 1."""
        shapes = {
            "devices: null": ({**EXAMPLE, "devices": None},
                              "devices must be a list of devices; write `devices: []` for a house with none"),
            "a device that is text": ({**EXAMPLE, "devices": ["example_tv"]},
                                      "devices[0]: must be a mapping with at least id, room and kind"),
            "a list where routing names a device": ({**EXAMPLE, "routing": {**EXAMPLE["routing"], "hdmi_matrix": []}},
                                                    "routing.hdmi_matrix: '[]' is not a device id"),
            "not a mapping at all": ([1, 2], "devices.yaml: version must be 2"),
        }
        with tempfile.TemporaryDirectory() as scratch:
            for name, (content, problem) in shapes.items():
                path = Path(scratch) / "devices.yaml"
                path.write_text(json.dumps(content))
                for argv in (["check", "house", "--house"], ["generate", "--check", "--house"],
                             ["dhcp-reservations", "--house"]):
                    with self.subTest(shape=name, verb=argv[0]):
                        code, err = run(*argv, str(path))
                        self.assertEqual(code, 1 if argv[0] == "check" else 2)
                        self.assertIn(f"thermite {argv[0]}: {path}: {problem}\n", err)
                        self.assertNotIn("Traceback", err)

    def test_a_verb_that_talks_to_devices_refuses_a_house_with_problems_before_it_opens_a_connection(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "devices.yaml"
            path.write_text(json.dumps({**EXAMPLE, "devices": [{**EXAMPLE["devices"][0], "model": 900}]}))
            for argv in (["reboot"], ["pair"], ["matrix-settings"], ["cameras", "list"]):
                with self.subTest(verb=argv[0]), \
                        mock.patch.object(socket, "socket", side_effect=AssertionError("opened a connection")):
                    code, err = run(*argv, "--house", str(path))
                    self.assertEqual(code, 2)
                    self.assertIn(f"thermite {argv[0]}: {path}: {EXAMPLE['devices'][0]['id']}: model 900 must be text\n", err)
                    self.assertIn(f"thermite {argv[0]}: {path}: ", err.splitlines()[-1])
                    self.assertIn("nothing done", err.splitlines()[-1])

    def test_devices_may_be_empty_or_left_out_but_not_null(self):
        import oh_render
        empty = {"version": 2, "rooms": {}}
        self.assertEqual(oh_render.validate(empty), [])
        self.assertEqual(oh_render.validate({**empty, "devices": []}), [])
        self.assertEqual(oh_render.validate({**empty, "devices": None}),
                         ["devices must be a list of devices; write `devices: []` for a house with none"])
        # The other sections may stand empty above entries that are commented out.
        self.assertEqual(oh_render.validate({**empty, "later": None, "services": None, "routing": None}), [])


class Constructed(unittest.TestCase):
    """Whoever holds a House holds a valid one."""

    WRONG = {**EXAMPLE, "devices": [{**EXAMPLE["devices"][0], "model": 900}]}

    def test_a_mapping_with_problems_does_not_become_a_house(self):
        with self.assertRaises(inventory.ConfigError) as caught:
            House(copy.deepcopy(self.WRONG))
        problem = f"{EXAMPLE['devices'][0]['id']}: model 900 must be text"
        self.assertEqual(caught.exception.problems, [problem])
        self.assertEqual(str(caught.exception), "not a valid house: " + problem)
        data = copy.deepcopy(EXAMPLE)
        self.assertIs(House(data).data, data)

    def test_the_unchecked_construction_checks_nothing_and_only_the_places_named_here_use_it(self):
        self.assertEqual(House.unchecked({**EXAMPLE, "version": 99}).data["version"], 99)
        unchecked, bare = {}, {}
        for path in sorted((AREA / "lib").rglob("*.py")):
            name, text = path.relative_to(AREA).as_posix(), path.read_text(encoding="utf-8")
            if name == "lib/oh_conf.py" or "/tests/" in name:
                continue
            for found, call in ((unchecked, r"\bHouse\.unchecked\("), (bare, r"(?<![.\w])(?:oh_conf\.)?House\(")):
                if count := len(re.findall(call, text)):
                    found[name] = count
        self.assertEqual(bare, {}, "a House comes from House.load(path); House(mapping) validates again")
        # Validators (including physical audio links), which run while the mapping is being checked, and
        # the gateway's planner, which is handed its contract's mapping.
        self.assertEqual(unchecked, {"lib/oh_features/remote_diagnostics.py": 1, "lib/oh_features/routing/__init__.py": 3,
                                     "lib/oh_room_policy/plan.py": 1})


class Includes(unittest.TestCase):
    """Top-level include: merges sibling YAML files under instance/ safely."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "instance"
        self.root.mkdir()

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_include_merges_rooms_profiles_devices_and_routing_deterministically(self):
        import oh_render
        self.write("profiles.yaml", (
            "profiles:\n"
            "  sony_tv:\n"
            "    kind: tv\n"
            "    make: Sony\n"
            "    controls:\n"
            "      observe: [sony_rest]\n"
            "      control: [sony_ip_control]\n"
        ))
        self.write("rooms/west.yaml", (
            "rooms:\n"
            "  west:\n"
            "    label: West\n"
            "    tag: Room\n"
            "devices:\n"
            "  - id: west_tv\n"
            "    room: west\n"
            "    profile: sony_tv\n"
            "    model: Example TV\n"
            "    ip: 192.0.2.40\n"
            "routing:\n"
            "  sources:\n"
            "    west_player:\n"
            "      label: Player\n"
            "  rooms:\n"
            "    west:\n"
            "      sources: [west_player]\n"
            "      display:\n"
            "        device: west_tv\n"
            "        sources:\n"
            "          west_player: hdmi1\n"
        ))
        main = self.write("devices.yaml", (
            "version: 2\n"
            "include:\n"
            "  - profiles.yaml\n"
            "  - rooms/west.yaml\n"
            "devices:\n"
            "  - id: west_player\n"
            "    room: west\n"
            "    kind: streamer\n"
            "    ip: 192.0.2.41\n"
            "    controls:\n"
            "      control: [pyatv_http]\n"
        ))
        house = House.load(main)
        self.assertNotIn("include", house.data)
        self.assertEqual(sorted(house.by_id), ["west_player", "west_tv"])
        self.assertEqual(house.by_id["west_tv"]["make"], "Sony")
        self.assertEqual(house.by_id["west_tv"]["controls"], {"observe": ["sony_rest"], "control": ["sony_ip_control"]})
        self.assertEqual(oh_render.validate(inventory.load(main), instance_dir=self.root), [])

    def test_include_refuses_duplicate_ids_and_conflicting_sections(self):
        self.write("a.yaml", "rooms:\n  den:\n    label: Den A\n")
        self.write("b.yaml", "rooms:\n  den:\n    label: Den B\n")
        main = self.write("devices.yaml", "version: 2\ninclude: [a.yaml, b.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError) as caught:
            House.load(main)
        self.assertTrue(any("rooms.den is defined in both" in p for p in caught.exception.problems), caught.exception.problems)

        # Duplicate profile id
        self.write("p1.yaml", "profiles:\n  base:\n    kind: tv\n")
        self.write("p2.yaml", "profiles:\n  base:\n    kind: avr\n")
        main_p = self.write("dup_p.yaml", "version: 2\ninclude: [p1.yaml, p2.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError) as caught:
            House.load(main_p)
        self.assertTrue(any("profiles.base is defined in both" in p for p in caught.exception.problems), caught.exception.problems)

        # Duplicate routing.sources / routing.rooms id
        self.write("r1.yaml", "routing:\n  sources:\n    src_a:\n      label: A\n")
        self.write("r2.yaml", "routing:\n  sources:\n    src_a:\n      label: B\n")
        main_r = self.write("dup_r.yaml", "version: 2\ninclude: [r1.yaml, r2.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError) as caught:
            House.load(main_r)
        self.assertTrue(any("routing.sources.src_a is defined in both" in p for p in caught.exception.problems), caught.exception.problems)

        # Conflicting top-level scalar/section definitions
        self.write("t1.yaml", "theme: thermite\n")
        self.write("t2.yaml", "theme: slate\n")
        main_t = self.write("dup_t.yaml", "version: 2\ninclude: [t1.yaml, t2.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError) as caught:
            House.load(main_t)
        self.assertTrue(any("theme conflicts between" in p for p in caught.exception.problems), caught.exception.problems)

    def test_include_jails_paths_detects_cycles_and_enforces_strict_yaml(self):
        outside = self.root.parent / "secret.yaml"
        outside.write_text("rooms:\n  outside:\n    label: Outside\n", encoding="utf-8")
        (self.root / "escape_link.yaml").symlink_to(outside)

        for bad in ("../secret.yaml", str(outside), "escape_link.yaml", "missing.yaml"):
            with self.subTest(include=bad):
                main = self.write("devices.yaml", f"version: 2\ninclude:\n  - {json.dumps(bad)}\ndevices: []\n")
                with self.assertRaises(inventory.ConfigError):
                    House.load(main)

        # Cycle: a.yaml -> b.yaml -> a.yaml
        self.write("a.yaml", "include: [b.yaml]\nrooms:\n  a:\n    label: A\n")
        self.write("b.yaml", "include: [a.yaml]\nrooms:\n  b:\n    label: B\n")
        cyc = self.write("cycle.yaml", "version: 2\ninclude: [a.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError) as caught:
            House.load(cyc)
        self.assertTrue(any("cycle" in p for p in caught.exception.problems), caught.exception.problems)

        # Non-mapping included file
        self.write("list.yaml", "- not_a_mapping\n")
        bad_kind = self.write("bad_kind.yaml", "version: 2\ninclude: [list.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError):
            House.load(bad_kind)

        # StrictLoader enforced inside included file (unquoted YAML 1.1 truth word)
        self.write("strict.yaml", "rooms:\n  den:\n    label: on\n")
        strict_main = self.write("strict_main.yaml", "version: 2\ninclude: [strict.yaml]\ndevices: []\n")
        with self.assertRaises(inventory.ConfigError) as caught:
            House.load(strict_main)
        self.assertTrue(any("truth" in p or "quotes" in p for p in caught.exception.problems), caught.exception.problems)


class Profiles(unittest.TestCase):
    """Top-level profiles: and device.profile inheritance, overrides, and cycle checks."""

    def test_multi_level_extends_merges_controls_config_and_timing_without_mutating_input(self):
        raw = {
            "version": 2,
            "rooms": {"den": {"label": "Den", "tag": "Room"}},
            "profiles": {
                "base_tv": {
                    "kind": "tv",
                    "make": "Sony",
                    "controls": {"observe": ["sony_rest"]},
                    "config": {"sony_ip_control": {"speaker_audio": False, "inputs": ["hdmi1", "hdmi2"]}},
                    "timing": {"settle": {"ON": 12000}, "poll_s": 15},
                    "monitor": "optional",
                },
                "controlled_tv": {
                    "extends": "base_tv",
                    "model": "Bravia XR",
                    "controls": {"control": ["sony_ip_control"]},
                    "config": {"sony_ip_control": {"speaker_audio": True}},
                    "timing": {"settle": {"OFF": 2000}},
                },
            },
            "devices": [{
                "id": "den_tv",
                "room": "den",
                "profile": "controlled_tv",
                "ip": "192.0.2.40",
                "timing": {"poll_s": 8},
                "monitor": "required",
            }],
        }
        snapshot = copy.deepcopy(raw)
        house = House(raw)
        # Caller dict is never mutated.
        self.assertEqual(raw, snapshot)
        tv = house.by_id["den_tv"]
        self.assertEqual(tv["kind"], "tv")
        self.assertEqual(tv["make"], "Sony")
        self.assertEqual(tv["model"], "Bravia XR")
        self.assertEqual(tv["monitor"], "required")
        self.assertEqual(tv["controls"], {"observe": ["sony_rest"], "control": ["sony_ip_control"]})
        self.assertEqual(tv["config"]["sony_ip_control"], {"speaker_audio": True, "inputs": ["hdmi1", "hdmi2"]})
        self.assertEqual(tv["timing"], {"settle": {"ON": 12000, "OFF": 2000}, "poll_s": 8})

    def test_profile_cycles_and_unknown_profiles_are_refused(self):
        import oh_render
        cyc = {
            "version": 2,
            "rooms": {"den": {"label": "Den"}},
            "profiles": {
                "a": {"extends": "b", "kind": "tv"},
                "b": {"extends": "a", "make": "Sony"},
            },
            "devices": [],
        }
        problems = oh_render.validate(cyc)
        self.assertTrue(any("cycle" in p for p in problems), problems)

        unknown_dev = {
            "version": 2,
            "rooms": {"den": {"label": "Den"}},
            "profiles": {"sony_tv": {"kind": "tv"}},
            "devices": [{"id": "den_tv", "room": "den", "kind": "tv", "profile": "sony_tvv"}],
        }
        problems = oh_render.validate(unknown_dev)
        self.assertTrue(any("profile 'sony_tvv' is not in profiles; did you mean sony_tv?" in p for p in problems), problems)

        unknown_ext = {
            "version": 2,
            "rooms": {},
            "profiles": {"child": {"extends": "parent_missing", "kind": "tv"}},
            "devices": [],
        }
        problems = oh_render.validate(unknown_ext)
        self.assertTrue(any("extends 'parent_missing' is not in profiles" in p for p in problems), problems)


class OneReader(unittest.TestCase):
    def test_only_the_loader_parses_a_house_file(self):
        call = re.compile(r"(?<![.\w])(?:oh_)?inventory\.load\(|\bfrom oh_inventory import [^\n]*\bload\b")
        found = {}
        for path in sorted((AREA / "lib").rglob("*.py")):
            count = len(call.findall(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(AREA).as_posix()] = count
        self.assertEqual(found, {"lib/oh_conf.py": 1},
                         "read a house file with oh_conf.House.load(path), which validates it; "
                         "oh_inventory.load checks nothing")


if __name__ == "__main__":
    unittest.main()
