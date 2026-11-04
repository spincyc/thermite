"""Every application file is reviewed into .thermite-export.json, and only those.

Packaging copies exactly the manifest's paths. A file that Git carries but the
manifest omits silently disappears from releases; a manifest path that Git
does not carry cannot be reproduced from a clean checkout. Untracked files that
are not ignored count as application files too, so a new file must be listed
before it is committed. Transient work belongs in ignored .scratch/.
"""
import json
from pathlib import Path
import subprocess
import unittest

AREA = Path(__file__).resolve().parents[1]
MANIFEST = AREA / ".thermite-export.json"

#: Reviewed files that Git carries but releases deliberately omit, with why.
EXCLUDED = {
    "design/hardware-endpoints.md": "proposed hardware design notes, not part of the application",
}


def git(*argv):
    return subprocess.run(["git", "-C", str(AREA), *argv], capture_output=True, text=True, check=False)


class ExportManifest(unittest.TestCase):
    def setUp(self):
        top = git("rev-parse", "--show-toplevel")
        if top.returncode or Path(top.stdout.strip()).resolve() != AREA.resolve():
            self.skipTest("not a Git checkout of the application (an installed or exported release)")

    def test_manifest_and_application_files_match(self):
        listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        deleted = git("ls-files", "-z", "--deleted")
        self.assertEqual((listed.returncode, deleted.returncode), (0, 0), listed.stderr + deleted.stderr)
        files = set(filter(None, listed.stdout.split("\0"))) - set(filter(None, deleted.stdout.split("\0")))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["paths"]
        duplicates = sorted({path for path in manifest if manifest.count(path) > 1})
        self.assertEqual(duplicates, [], "duplicate .thermite-export.json paths")
        self.assertEqual(manifest, sorted(manifest), ".thermite-export.json paths must stay sorted")
        missing = sorted(files - set(manifest) - set(EXCLUDED))
        unknown = sorted(set(manifest) - files)
        stale = sorted(set(EXCLUDED) - files)
        both = sorted(set(EXCLUDED) & set(manifest))
        problems = [*(f"add to .thermite-export.json (or EXCLUDED here, with a reason): {path}" for path in missing),
                    *(f"remove from .thermite-export.json, or add the file to Git: {path}" for path in unknown),
                    *(f"remove the stale EXCLUDED entry: {path}" for path in stale),
                    *(f"listed both in the manifest and EXCLUDED: {path}" for path in both)]
        if problems:
            self.fail(f"{len(problems)} export manifest problem(s):\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
