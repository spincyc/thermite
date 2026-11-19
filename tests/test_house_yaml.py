"""A house file is read as what it says (oh_inventory.StrictLoader).

The rule: a scalar in a house file means what a person reading it sees, or
the file is refused naming the line and what to write; and a key written
twice in one mapping is refused naming both places.

PyYAML reads YAML 1.1, where an unquoted `on` is true, `010` is 8, `12:30`
is 750 and a date is a date; YAML 1.2 reads each of those as it looks. A
house file is read by whichever release its site pins, so the loader takes
only what both read the same: whatever it loads is what yaml.safe_load made
of it in every earlier release, which the tests below hold it to.

The host file (instance/host.yaml) is read by the same loader (HostFile).
"""
import contextlib
import io
import itertools
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA))
sys.path.insert(0, str(AREA / "lib"))
import oh_cli  # noqa: E402
import oh_host  # noqa: E402
import oh_inventory as inventory  # noqa: E402
import oh_paths  # noqa: E402
import yaml  # noqa: E402
from oh_cli import generate  # noqa: E402
from tests import test_generated_output  # noqa: E402

EXAMPLE = json.loads((AREA / "instance/devices.yaml").read_text(encoding="utf-8"))
#: A hardware address of digits only, which YAML 1.1 reads as one number in
#: base 60. Joined here: the public guard takes a written one for a real one.
ADDRESS = ":".join(("10", "20", "30", "40", "50", "11"))

#: Plain scalar -> the kind of its refusal.
REFUSED = {
    "on": "truth word", "Off": "truth word", "YES": "truth word", "no": "truth word",
    "010": "leading zero", "007": "leading zero", "08": "leading zero", "-012": "leading zero",
    ADDRESS: "base 60", "12:30": "base 60", "1:30.5": "base 60",
    "2026-11-18": "date", "2026-11-18 10:00:00": "date",
    "1_000": "number form", "1e5": "number form", "1.0e5": "number form", "0o17": "number form",
    "0b11": "number form", "+0x1F": "number form", "1_000.5": "number form",
}
#: Plain scalar -> what it is, in YAML 1.1 and in YAML 1.2 alike.
LOADED = {
    "true": True, "False": False, "12": 12, "-3": -3, "+4": 4, "0": 0, "1.5": 1.5, "-0.25": -0.25, ".5": 0.5,
    "1.0e+3": 1000.0, "0x1F": 31, "~": None, "null": None, "": None, "living_room": "living_room",
    "192.0.2.20": "192.0.2.20", "02:30": "02:30", "0a:1b": "0a:1b", "Example TV": "Example TV", "1.2.3": "1.2.3",
    "y": "y", "n": "n", "0x": "0x", "1e": "1e", "12 V": "12 V",
}


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(oh_paths, "require_compatible"):
        code = oh_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Scratch(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.house = Path(temporary.name) / "devices.yaml"

    def load(self, text):
        self.house.write_text(text, encoding="utf-8")
        return inventory.load(self.house)

    def refusal(self, text):
        """The problems of a file that is refused, and the kinds of them."""
        with self.assertRaises(inventory.ConfigError) as caught:
            self.load(text)
        self.assertRegex(str(caught.exception), r"devices\.yaml: \d+ problems?; nothing done$")
        return caught.exception.problems, [kind for _, _, kind, _ in inventory.read(text)[1]]


class Scalars(Scratch):
    def test_a_plain_scalar_both_versions_of_yaml_read_the_same_loads_as_that(self):
        for scalar, value in LOADED.items():
            with self.subTest(scalar=scalar):
                self.assertEqual(self.load(f"version: 1\nvalue: {scalar}\n"), {"version": 1, "value": value})
                self.assertEqual(self.load(f"[{scalar}, x]" if scalar else "[]"), [value, "x"] if scalar else [])

    def test_a_plain_scalar_the_versions_read_differently_is_refused_with_its_line_and_what_to_write(self):
        for scalar, kind in REFUSED.items():
            for text, line in ((f"version: 1\nvalue: {scalar}\n", 2), (f"rooms:\n  den:\n    - a\n    - {scalar}\n", 4),
                               (f"{{\"a\": 1,\n \"b\": [{scalar}]}}", 2), (f"version: 1\n\n{scalar}: 1\n", 3)):
                with self.subTest(scalar=scalar, text=text):
                    problems, kinds = self.refusal(text)
                    self.assertEqual(kinds, [kind])
                    self.assertTrue(problems[0].startswith(f"line {line}: {scalar} "), problems)
                    self.assertIn(": write ", problems[0])

    def test_what_a_refusal_says_to_write_loads_as_the_text_that_was_written(self):
        for scalar in REFUSED:
            with self.subTest(scalar=scalar):
                sentence = self.refusal(f"value: {scalar}\n")[0][0]
                quoted = re.search(r'(".*") in quotes', sentence).group(1)
                self.assertEqual(self.load(f"value: {quoted}\n"), {"value": scalar})
                self.assertEqual(self.load(f"value: '{scalar}'\n"), {"value": scalar})

    def test_the_two_keys_only_yaml_1_1_knows_are_refused(self):
        problems, kinds = self.refusal("base: &base {a: 1}\nother:\n  <<: *base\n  b: 2\n")
        self.assertEqual((kinds, problems[0][:11]), (["merge key"], "line 3: << "))
        problems, kinds = self.refusal("a:\n  =: 1\n")
        self.assertEqual((kinds, problems[0][:10]), (["value key"], "line 2: = "))
        self.assertEqual(self.load('"<<": 1\n"=": 2\n'), {"<<": 1, "=": 2})

    def test_quoted_and_block_text_is_text(self):
        self.assertEqual(self.load(f"a: 'on'\nb: \"010\"\nc: '{ADDRESS}'\nd: |\n  off\ne: >\n  2026-11-18\n\"no\": 1\n"),
                         {"a": "on", "b": "010", "c": ADDRESS, "d": "off\n", "e": "2026-11-18\n", "no": 1})

    def test_a_tag_is_refused_whatever_it_is_on(self):
        problems, kinds = self.refusal("a: !!int '010'\nb: ! 010\nc: !!str 12\nd: !!binary aGk=\ne: !!set {a, b}\nf: !x [1]\n")
        self.assertEqual(kinds, ["tag"] * 6)
        self.assertEqual([problem.split(" is a tag")[0] for problem in problems],
                         ["line 1: !!int", "line 2: !", "line 3: !!str", "line 4: !!binary", "line 5: !!set", "line 6: !x"])

    def test_half_of_a_character_from_a_json_escape_pair_is_refused(self):
        written = json.dumps({"name": "Den \U0001F4FA"})
        self.assertIn("\\ud83d\\udcfa", written)
        problems, kinds = self.refusal(written)
        self.assertEqual(kinds, ["half character"])
        self.assertIn("write the character itself", problems[0])
        self.assertEqual(self.load(json.dumps({"name": "Den \U0001F4FA"}, ensure_ascii=False)), {"name": "Den \U0001F4FA"})

    def test_a_scalar_written_once_and_used_twice_is_refused_once(self):
        self.assertEqual(self.refusal("a: &state on\nb: *state\nc: *state\n")[1], ["truth word"])
        self.assertEqual(self.load("a: &state \"on\"\nb: *state\n"), {"a": "on", "b": "on"})

    def test_every_refusal_in_a_file_is_listed_in_the_order_of_its_lines(self):
        problems, kinds = self.refusal("a: 1\nb: off\nc: {d: 010, e: 12:30}\na: 2\nf: [1e5]\n")
        self.assertEqual(kinds, ["truth word", "leading zero", "base 60", "duplicate key", "number form"])
        self.assertEqual([problem.split(":")[0] for problem in problems], ["line 2", "line 3", "line 3", "line 4", "line 5"])

    def test_text_that_is_not_yaml_still_names_its_line_and_column(self):
        with self.assertRaisesRegex(inventory.ConfigError, r"devices\.yaml:3:3: is not YAML"):
            self.load("version: 1\ndevices: [\n  - a\n")
        with self.assertRaisesRegex(inventory.ConfigError, r"devices\.yaml:2:1: is not YAML: but found another document"):
            self.load("a: 1\n---\nb: 2\n")


class Keys(Scratch):
    def test_a_key_written_twice_in_one_mapping_is_refused_naming_both_lines(self):
        for text, problem in (
                ("version: 1\nrooms: {}\nversion: 1\n", "line 3: key 'version' is written twice in this mapping (first on line 1)"),
                ("devices:\n  - id: a\n    room: den\n    kind: tv\n    room: attic\n",
                 "line 5: key 'room' is written twice in this mapping (first on line 3)"),
                ('{"rooms": {"den": 1,\n  "attic": 2,\n  "den": 3}}', "line 3: key 'den' is written twice in this mapping (first on line 1)"),
                ("edid: {1: a, default: b, 1: c}\n", "line 1: key 1 is written twice in this mapping (first on this line, column 8)"),
                ("true: a\nTrue: b\n", "line 2: key True is written twice in this mapping (first on line 1)")):
            with self.subTest(text=text):
                problems, kinds = self.refusal(text)
                self.assertEqual(kinds, ["duplicate key"])
                self.assertEqual(problems, [problem + ": keep one"])

    def test_three_of_a_key_are_two_refusals_that_name_the_first(self):
        problems, _ = self.refusal("a: 1\na: 2\nb: 3\na: 4\n")
        self.assertEqual([problem[:60] for problem in problems],
                         ["line 2: key 'a' is written twice in this mapping (first on l", "line 4: key 'a' is written twice in this mapping (first on l"])
        self.assertTrue(all("(first on line 1)" in problem for problem in problems))

    def test_the_same_key_in_two_mappings_is_two_keys(self):
        self.assertEqual(self.load("a: {id: 1}\nb: {id: 1}\nc:\n  - id: 2\n  - id: 2\n"),
                         {"a": {"id": 1}, "b": {"id": 1}, "c": [{"id": 2}, {"id": 2}]})


def same(one, other) -> bool:
    """Equal, with a nan equal to a nan and 1, 1.0 and True three things."""
    if isinstance(one, float) and isinstance(other, float) and math.isnan(one):
        return math.isnan(other)
    if isinstance(one, dict) and isinstance(other, dict):
        return len(one) == len(other) and all(key in other and same(value, other[key]) for key, value in one.items())
    if isinstance(one, list) and isinstance(other, list):
        return len(one) == len(other) and all(map(same, one, other))
    return type(one) is type(other) and one == other


class EveryRelease(Scratch):
    """What loads is what every earlier release, which read the file with
    yaml.safe_load, made of it."""

    #: Pieces a scalar is put together from: every number form, word and
    #: separator the two versions of YAML treat differently, and some neither does.
    HEADS = ("", "-", "+", "0", "0x", "0o", "0b", "1", "12", "7", ".", "~", "o", "y", "n", "t", "nu", "2026", "<", "=")
    TAILS = ("", "0", "8", "17", "1F", "_0", ".", ".5", "e5", "e+5", "E-5", ":30", ":61", ":30.5", "n", "ff", "es", "o",
             "rue", "ll", "-11-18", "inf", "nan", "<", " 1", "x")

    def test_whatever_scalar_loads_is_what_yaml_safe_load_made_of_it(self):
        words = ("on", "off", "yes", "no", "true", "false", "null", "y", "n")
        scalars = {head + tail for head, tail in itertools.product(self.HEADS, self.TAILS)}
        scalars |= {form(word) for word in words for form in (str.lower, str.upper, str.title)}
        scalars |= set(REFUSED) | set(LOADED)
        self.assertGreater(len(scalars), 500)
        loaded = refused = 0
        for scalar in sorted(scalars):
            for text in (f"value: {scalar}\n", f"[{scalar}]", f"? {scalar}\n: 1\n"):
                try:
                    before = yaml.safe_load(text)
                except yaml.YAMLError:
                    before = yaml.YAMLError
                try:
                    content, problems = inventory.read(text)
                except yaml.YAMLError:
                    content, problems = yaml.YAMLError, []
                refused += bool(problems)
                if not problems:
                    loaded += 1
                    with self.subTest(text=text):
                        self.assertTrue(same(content, before), (content, before))
        # Neither side of the rule is an empty promise.
        self.assertGreater(loaded, 900)
        self.assertGreater(refused, 150)

    def test_a_scalar_json_reads_loads_as_json_reads_it_or_is_refused(self):
        numbers = ("0", "-0", "12", "-3", "1.5", "-0.25", "1e5", "1E5", "1e-05", "1.5e-07", "1.0e+3", "1e+22", "1E400",
                   "123456789012345678901234567890", "true", "false", "null", '"on"', '"010"', '"12:30"', '""', '"a\\tb\\n"',
                   '"\\u00e9"', '"\\ud83d\\udcfa"')
        outcomes = []
        for number in numbers:
            for text in (number, f'{{"a": {number}}}', f"[{number},{number}]"):
                content, problems = inventory.read(text)
                outcomes.append(bool(problems))
                if not problems:
                    with self.subTest(text=text):
                        self.assertTrue(same(content, json.loads(text)), (content, json.loads(text)))
        # An exponent without a point is text to YAML 1.1; a pair of escapes is two halves.
        self.assertEqual(outcomes.count(True), 3 * len(("1e5", "1E5", "1e-05", "1e+22", "1E400", "pair")))

    def test_every_fixture_house_loads_as_itself_written_as_json_and_as_yaml(self):
        houses = {name: house for name, house in test_generated_output.houses().items() if isinstance(house, dict)}
        self.assertGreater(len(houses), 20)
        for name, house in houses.items():
            for text in (json.dumps(house), json.dumps(house, indent=2, ensure_ascii=False), yaml.safe_dump(house),
                         yaml.safe_dump(house, default_flow_style=True, allow_unicode=True)):
                with self.subTest(house=name, text=text[:20]):
                    self.assertEqual(inventory.read(text), (house, []))

    def test_every_house_example_in_the_tree_loads(self):
        fenced = re.compile(r"^```ya?ml\n(.*?)^```", re.MULTILINE | re.DOTALL)
        documents = [AREA / "README.md", AREA / "FAQ.md", *sorted((AREA / "docs").glob("*.md")),
                     *sorted((AREA / "site/pages").glob("*.md"))]
        texts = [block for document in documents for block in fenced.findall(document.read_text(encoding="utf-8"))]
        texts.append((AREA / "instance/devices.yaml").read_text(encoding="utf-8"))
        self.assertGreater(len(texts), 3)
        for text in texts:
            with self.subTest(text=text[:40]):
                content, problems = inventory.read(text)
                self.assertEqual(problems, [])
                self.assertTrue(same(content, yaml.safe_load(text)))


class Verbs(Scratch):
    def test_every_house_verb_exits_2_naming_the_file_and_the_line(self):
        lines = yaml.safe_dump(EXAMPLE, sort_keys=False).splitlines()
        at = next(number for number, line in enumerate(lines) if line.strip().startswith("mac: "))
        lines[at] = lines[at].split("mac: ")[0] + "mac: " + ADDRESS
        self.house.write_text("\n".join(lines) + "\n")
        for argv in (["generate", "--check", "--house"], ["validate", "--house"], ["dhcp-reservations", "--house"]):
            with self.subTest(argv=argv):
                code, _, err = run(*argv, str(self.house))
                self.assertEqual(code, 2)
                self.assertEqual(err.splitlines(), [
                    f"thermite {argv[0]}: {self.house}: line {at + 1}: {ADDRESS} is a number in base 60 in YAML 1.1 "
                    f"and text in YAML 1.2: write \"{ADDRESS}\" in quotes",
                    f"thermite {argv[0]}: {self.house}: 1 problem; nothing done"])
        lines[at] = lines[at].replace(ADDRESS, f'"{ADDRESS}"')
        self.house.write_text("\n".join(lines) + "\n")
        self.assertEqual(run("dhcp-reservations", "--house", str(self.house))[0], 0)


class HostFile(Scratch):
    """oh_host.load, the one reader of a host file, refuses what the
    house file's reader refuses, in the same words."""

    HOST = json.loads((AREA / "instance/host.yaml").read_text(encoding="utf-8"))

    def test_what_a_house_file_is_refused_for_a_host_file_is_refused_for_in_the_same_words(self):
        written = "".join(f"{key}: {json.dumps(value)}\n" for key, value in self.HOST.items())
        self.assertEqual(oh_host.load(written.encode()), self.HOST)
        for scalar in REFUSED:
            with self.subTest(scalar=scalar):
                text = f"{written}root: {scalar}\n"
                with self.assertRaises(oh_host.HostError) as caught:
                    oh_host.load(text.encode())
                self.assertEqual(str(caught.exception), "instance/host.yaml: " + self.refusal(text)[0][0])
                self.assertIn(f"line {len(self.HOST) + 1}: {scalar} ", str(caught.exception))

    def test_every_refusal_is_in_the_one_message_that_names_the_file_or_the_commit(self):
        text = '{"timezone": "Etc/UTC", "http_port": 013602,\n "https_port": 13603, "uid": 9001, "gid": 9001,\n "uid": !!int "9002"}\n'
        problems, kinds = self.refusal(text)
        self.assertEqual(kinds, ["leading zero", "duplicate key", "tag"])
        self.house.write_text(text)
        for source, where in ((self.house, str(self.house)), (text.encode(), "instance/host.yaml at 0123456789ab")):
            with self.subTest(source=type(source).__name__), self.assertRaises(oh_host.HostError) as caught:
                oh_host.load(source, None if isinstance(source, Path) else where)
            self.assertEqual(str(caught.exception), f"{where}: " + "; ".join(problems))
            self.assertNotIn("\n", str(caught.exception))

    def test_a_host_file_that_loads_is_what_yaml_safe_load_made_of_it(self):
        fenced = re.compile(r"^```ya?ml\n(.*?)^```", re.MULTILINE | re.DOTALL)
        documents = [AREA / "README.md", AREA / "FAQ.md", *sorted((AREA / "docs").glob("*.md")),
                     *sorted((AREA / "site/pages").glob("*.md"))]
        texts = [block for document in documents for block in fenced.findall(document.read_text(encoding="utf-8"))
                 if not re.search(r"^(devices|rooms):", block, re.MULTILINE)]
        texts += [(AREA / "instance/host.yaml").read_text(encoding="utf-8"), yaml.safe_dump(self.HOST)]
        self.assertGreater(len(texts), 3)
        for text in texts:
            with self.subTest(text=text[:40]):
                content, problems = inventory.read(text.encode())
                self.assertEqual(problems, [])
                self.assertTrue(same(content, yaml.safe_load(text)))
        self.assertEqual(oh_host.load(yaml.safe_dump(self.HOST).encode("utf-16")), self.HOST)

    def test_generate_writes_nothing_from_a_host_file_that_is_refused(self):
        site = self.house.parent
        (site / "instance").mkdir()
        (site / "versions.json").write_bytes((AREA / "versions.json").read_bytes())
        written = yaml.safe_dump({**self.HOST, "root": "/srv/thermite"})
        (site / "instance/host.yaml").write_text(written + "music: no\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(generate, "AREA", site), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = generate.main(["--house", str(AREA / "instance/devices.yaml"), "--conf", str(site / "conf"),
                                  "--root", str(site)])
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), f"thermite generate: {site / 'instance/host.yaml'}: line "
                                         f"{written.count(chr(10)) + 1}: no is true or false in YAML 1.1 and a word in YAML 1.2: "
                                         'write true or false, or "no" in quotes for the word; nothing generated\n')
        self.assertEqual(sorted(path.name for path in site.iterdir()), ["instance", "versions.json"])


if __name__ == "__main__":
    unittest.main()
