"""The copies in web/remote/ of the page's generated files are what
`thermite generate` writes for the example house (instance/devices.yaml).

Generate writes some of the page's files itself instead of copying them
(lib/oh_features/remote.py): house.js, access-config.js, the JSON catalogues
and the theme's colors and icons. web/remote/ still holds a copy of each,
which nothing generates, for the page opened from its sources:

    house.js           every page as a source preview (?backend=mock, or
                       opened from disk), and the terminal remote and the
                       Node harnesses in a checkout with no conf/
    cameras.json,      the Cameras and Topology pages as source previews, and
    topology.json      `thermite remote --mock`
    access-config.js,  every page as a source preview
    theme.css, icons
    activity.json,     nothing: a served page fetches them from /static/remote/
    diagnostics.json,  and a preview does without, so these only show the
    system.json        format

A copy that falls behind the generator shows a preview, or tests a harness,
on a model the generator no longer writes. Each is the generated file byte
for byte, but a JSON catalogue, which generate writes on one line, is
indented here so that its changes can be read and merged. Rewrite them:

    python -m tests.test_remote_samples --write
"""
import contextlib
import io
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_cli import generate  # noqa: E402
from oh_features import remote  # noqa: E402

REWRITE = f"cd {shlex.quote(str(AREA))} && {shlex.quote(sys.executable)} -m tests.test_remote_samples --write"


def copied(name: str) -> bool:
    """Whether web/remote/ holds this file of the page as generate writes it.
    Generate rewrites the links of an HTML page and of the web manifest
    (remote.served_page, remote.served_manifest), whose sources those are."""
    return not name.endswith(".html") and name != "manifest.webmanifest"


def samples() -> dict[str, bytes]:
    """File name -> what its copy in web/remote/ must hold: what generate
    writes for the example house, a JSON catalogue indented."""
    out, problems = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        # versions.json and instance/host.yaml are this checkout's, whatever
        # site THERMITE_SITE selects.
        with mock.patch.object(generate, "AREA", AREA), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(problems):
            status = generate.main(["--house", str(AREA / "instance/devices.yaml"), "--conf", str(root / "conf"),
                                    "--root", str(root)])
        if status:
            raise AssertionError(f"thermite generate exited {status} for the example house:\n{problems.getvalue()}")
        written = {name: (root / "conf" / remote.TARGET / name).read_bytes() for name in remote.FILES if copied(name)}
    return {name: (json.dumps(json.loads(content), indent=2) + "\n").encode() if name.endswith(".json") else content
            for name, content in written.items()}


def behind(wanted: dict[str, bytes]) -> list[str]:
    """The copies that are missing or differ, by name."""
    return sorted(name for name, content in wanted.items()
                  if not (remote.SOURCE / name).is_file() or (remote.SOURCE / name).read_bytes() != content)


class RemoteSamples(unittest.TestCase):
    def test_each_copy_is_what_generate_writes_for_the_example_house(self):
        stale = behind(samples())
        if stale:
            self.fail(f"web/remote/ is behind what thermite generate writes for the example house in {len(stale)} "
                      f"file(s):\n" + "\n".join(f"  web/remote/{name}" for name in stale)
                      + f"\nRewrite them, and commit them with the change to the generator:\n  {REWRITE}")


def main(argv: list[str]) -> int:
    if argv != ["--write"]:
        unittest.main()
    wanted = samples()
    stale = behind(wanted)
    for name in stale:
        (remote.SOURCE / name).write_bytes(wanted[name])
        print(f"wrote web/remote/{name}")
    print(f"{len(stale)} of {len(wanted)} copies rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
