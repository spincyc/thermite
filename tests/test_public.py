"""Portable offline checks; no device or runtime process is started."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_brand
import oh_inventory
import oh_render
from oh_features import remote_model

def command(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = oh_cli.run("thermite", argv)
        except SystemExit as error:
            result = error.code
    if result:
        raise AssertionError(" ".join(argv) + " failed: " + err.getvalue() + out.getvalue())

class Portable(unittest.TestCase):
    def test_every_available_help_and_themes(self):
        command(["--help"])
        for verb in oh_cli.VERBS:
            command([verb, "--help"])
        command(["theme", "check", *oh_brand.builtin()])

    def test_sample_generation_and_editing(self):
        scratch = AREA / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary)
            house = root / "devices.yaml"
            shutil.copyfile(AREA / "instance/devices.yaml", house)
            args = ["--house", str(house), "--conf", str(root / "conf")]
            command(["generate", *args, "--root", str(root)])
            command(["generate", "--check", *args, "--root", str(root)])
            command(["validate", *args])
            data = oh_inventory.load(house)
            data["devices"][0]["name"] = "Renamed example display"
            data["devices"][0]["volume_max"] = 73
            house.write_text(json.dumps(data))
            command(["generate", *args, "--root", str(root)])
            model = remote_model.literal((root / "conf/html/remote/house.js").read_text())
            self.assertEqual(model["devices"][data["devices"][0]["id"]]["label"], "Renamed example display")
            self.assertEqual(model["devices"][data["devices"][0]["id"]]["control"]["volume"]["max"], 73)
            self.assertEqual(set(model["devices"]), {d["id"] for d in data["devices"]})

    def test_docs_and_lint(self):
        scratch = AREA / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            command(["docs", "build", "--out", temporary])
            self.assertTrue((Path(temporary) / "index.html").is_file())
        command(["lint"])
