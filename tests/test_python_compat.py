"""Sources parse on the oldest Python that pyproject.toml supports.

Development usually runs a newer Python, which accepts f-string forms
(PEP 701, Python 3.12) that older interpreters reject outright.
"""
import ast
import io
from pathlib import Path
import re
import subprocess
import sys
import tokenize
import unittest

AREA = Path(__file__).resolve().parents[1]
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 is unsupported
    tomllib = None


def minimum():
    declared = tomllib.loads((AREA / "pyproject.toml").read_text())["project"]["requires-python"]
    major, minor = re.fullmatch(r">=\s*(\d+)\.(\d+)", declared).groups()
    return int(major), int(minor)


def sources():
    listed = subprocess.run(["git", "-C", str(AREA), "ls-files", "-z", "*.py", "bin/thermite"],
                            capture_output=True, text=True, check=True).stdout.split("\0")
    return [AREA / name for name in listed if name]


def pep701(text):
    """(line, why) for f-string expressions Python 3.11 cannot parse."""
    problems, frames = [], []  # [quote, brace depth] per open f-string
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        open_quotes = [frame[0] for frame in frames if frame[1] > 0]
        if token.type == tokenize.FSTRING_START:
            quote = token.string.lstrip("rRfFbBuU")
            if any(len(outer) == 1 and outer == quote[0] for outer in open_quotes):
                problems.append((token.start[0], "nested f-string reuses the enclosing quote"))
            frames.append([quote, 0])
        elif token.type == tokenize.FSTRING_END:
            frames.pop()
        elif frames and token.type == tokenize.OP and token.string in "{}":
            frames[-1][1] += 1 if token.string == "{" else -1
        elif open_quotes:
            if token.type == tokenize.COMMENT:
                problems.append((token.start[0], "comment inside an f-string expression"))
            elif token.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE) and "\\" in token.string and frames[-1][1] > 0:
                problems.append((token.start[0], "backslash inside an f-string expression"))
            elif token.type == tokenize.STRING and any(len(outer) == 1 and token.string.lstrip("rRbBuU")[0] == outer
                                                       for outer in open_quotes):
                problems.append((token.start[0], "string reuses the enclosing f-string quote"))
    return problems


@unittest.skipUnless(tomllib, "needs tomllib")
class OldestPython(unittest.TestCase):
    def test_every_source_parses_on_the_oldest_supported_python(self):
        version = minimum()
        problems = []
        for path in sources():
            text = path.read_text(encoding="utf-8")
            name = path.relative_to(AREA).as_posix()
            try:
                ast.parse(text, filename=name, feature_version=version)
            except SyntaxError as exc:
                problems.append(f"{name}:{exc.lineno}: {exc.msg}")
            if version < (3, 12) and sys.version_info >= (3, 12):
                problems.extend(f"{name}:{line}: {why}" for line, why in pep701(text))
        self.assertEqual(problems, [], f"sources must parse on Python {version[0]}.{version[1]}")

    def test_the_check_recognises_each_newer_form(self):
        if sys.version_info < (3, 12):
            self.skipTest("this interpreter rejects these forms itself")
        newer = 'x = {"a": 1}\na = f"{x["a"]}"\nb = f"{"\\\\n".join([])}"\nc = f"{x  # note\n}"\nok = f"""{x["a"]}"""\n'
        self.assertEqual([line for line, _ in pep701(newer)], [2, 3, 4])


if __name__ == "__main__":
    unittest.main()
