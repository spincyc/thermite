"""A house file becomes a House in one place (oh_conf.House.load), which
validates it; and a house file that is wrong in shape ends in a ConfigError
that names the problem, whatever is wrong with it.

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
EXAMPLE = json.loads(HOUSE.read_text(encoding="utf-8"))

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
    @classmethod
    def setUpClass(cls):
        # The example's conf/, which `thermite validate` reads beside each
        # changed house: what a site holds between an edit and a generate.
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)
        test_generated_output.written(HOUSE, cls.root)
        cls.objects = inventory.conf_objects(cls.root / "conf")

    def verbs(self):
        """thermite validate, then thermite generate --check, on the house
        file; each as its main() runs, into an empty tree."""
        things, items, problems = self.objects
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(generate, "AREA", AREA), \
                mock.patch.object(inventory, "conf_objects", lambda conf: (things, items, list(problems))):
            self.assertIn(validate.main(["--house", str(HOUSE), "--conf", str(self.root / "conf")]), (0, 1))
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
            house.write_text(json.dumps({"version": 1, "rooms": {"den": {"label": "Den", "tag": "Room"}}}))
            (Path(scratch) / "out").mkdir()
            self.assertIn("conf/yaml/model.yaml", test_generated_output.written(house, Path(scratch) / "out"))


class Loader(unittest.TestCase):
    def test_a_loaded_house_indexes_the_mapping_it_read_and_copies_nothing(self):
        house = House.load(HOUSE)
        self.assertEqual(house.data, EXAMPLE)
        self.assertEqual(list(house.by_id), [device["id"] for device in EXAMPLE["devices"]])
        for listed, device in zip(house.data["devices"], house.devices):
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
        shapes = {
            "devices: null": ({**EXAMPLE, "devices": None},
                              "devices must be a list of devices; write `devices: []` for a house with none"),
            "a device that is text": ({**EXAMPLE, "devices": ["example_tv"]},
                                      "devices[0]: must be a mapping with at least id, room and kind"),
            "a list where routing names a device": ({**EXAMPLE, "routing": {**EXAMPLE["routing"], "hdmi_matrix": []}},
                                                    "routing.hdmi_matrix: '[]' is not a device id"),
            "not a mapping at all": ([1, 2], "devices.yaml: version must be 1"),
        }
        with tempfile.TemporaryDirectory() as scratch:
            for name, (content, problem) in shapes.items():
                path = Path(scratch) / "devices.yaml"
                path.write_text(json.dumps(content))
                for argv in (["validate", "--house"], ["generate", "--check", "--house"],
                             ["dhcp-reservations", "--house"]):
                    with self.subTest(shape=name, verb=argv[0]):
                        code, err = run(*argv, str(path))
                        self.assertEqual(code, 2)
                        self.assertIn(f"thermite {argv[0]}: {path}: {problem}\n", err)
                        self.assertNotIn("Traceback", err)

    def test_a_verb_that_talks_to_devices_refuses_a_house_with_problems_before_it_opens_a_connection(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "devices.yaml"
            path.write_text(json.dumps({**EXAMPLE, "devices": [{**EXAMPLE["devices"][0], "model": 900}]}))
            for argv in (["reboot"], ["pair", "--check"], ["matrix-settings"], ["cameras", "list"]):
                with self.subTest(verb=argv[0]), \
                        mock.patch.object(socket, "socket", side_effect=AssertionError("opened a connection")):
                    code, err = run(*argv, "--house", str(path))
                    self.assertEqual(code, 2)
                    self.assertIn(f"thermite {argv[0]}: {path}: {EXAMPLE['devices'][0]['id']}: model 900 must be text\n", err)
                    self.assertIn(f"thermite {argv[0]}: {path}: ", err.splitlines()[-1])
                    self.assertIn("nothing done", err.splitlines()[-1])

    def test_devices_may_be_empty_or_left_out_but_not_null(self):
        import oh_render
        empty = {"version": 1, "rooms": {}}
        self.assertEqual(oh_render.validate(empty), [])
        self.assertEqual(oh_render.validate({**empty, "devices": []}), [])
        self.assertEqual(oh_render.validate({**empty, "devices": None}),
                         ["devices must be a list of devices; write `devices: []` for a house with none"])
        # The other sections may stand empty above entries that are commented out.
        self.assertEqual(oh_render.validate({**empty, "later": None, "services": None, "routing": None}), [])


class OneReader(unittest.TestCase):
    #: Parsers of the house file that validate nothing, each to become
    #: House.load: the installer's two, which hand the gateway's callback
    #: commissioning (oh_access.native_boundary.configure) the raw mapping.
    UNCHECKED = {"lib/oh_install/access.py": 1, "lib/oh_install/steps.py": 1}

    def test_only_the_loader_parses_a_house_file(self):
        call = re.compile(r"(?<![.\w])(?:oh_)?inventory\.load\(|\bfrom oh_inventory import [^\n]*\bload\b")
        found = {}
        for path in sorted((AREA / "lib").rglob("*.py")):
            count = len(call.findall(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(AREA).as_posix()] = count
        self.assertEqual(found, {"lib/oh_conf.py": 1, **self.UNCHECKED},
                         "read a house file with oh_conf.House.load(path), which validates it; "
                         "oh_inventory.load checks nothing")


if __name__ == "__main__":
    unittest.main()
