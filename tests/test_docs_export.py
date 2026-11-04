"""The docs export keeps its sources, and the committed site is what they build."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_site

#: The Python whose argparse builds the committed command reference.
HELP_PYTHON = (3, 13)


def thermite(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = oh_cli.main(list(argv))
    return status, out.getvalue(), err.getvalue()


class DocsExport(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", AREA / ".scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="docs-export-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_every_built_path_is_one_export_may_remove(self):
        for path in oh_site.repository_files():
            if path.startswith("docs/"):
                self.assertTrue(oh_site.BUILT.fullmatch(path), path)
        for source in oh_site.DOCUMENT_PAGES.values():
            self.assertFalse(oh_site.BUILT.fullmatch(f"docs/{source.name}"), source)

    def test_export_keeps_sources_and_removes_only_its_own_stray_files(self):
        kept = {"docs/installation.md": "# Sources stay\n", "docs/installation-handoff.md": "kept\n",
                "docs/notes.txt": "not the site's\n", "docs/drafts/page.md": "draft\n"}
        removed = {"docs/retired-page.html": "<p>old</p>\n", "docs/assets/retired.css": "p{}\n",
                   "docs/themes/retired.json": "{}\n"}
        for path, text in {**kept, **removed}.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)
        status, out, err = thermite("docs", "export", "--to", str(self.root), "--dry-run")
        self.assertEqual(status, 0, err)
        self.assertEqual({line.split()[-1] for line in out.splitlines() if line.startswith("would remove")},
                         set(removed))
        self.assertTrue(all((self.root / path).is_file() for path in removed))
        status, out, err = thermite("docs", "export", "--to", str(self.root))
        self.assertEqual(status, 0, err)
        for path, text in kept.items():
            self.assertEqual((self.root / path).read_text(), text, path)
        for path in removed:
            self.assertFalse((self.root / path).exists(), path)
        self.assertTrue((self.root / "docs/installation.html").is_file())
        self.assertEqual(oh_site.compare(self.root, oh_site.repository_files()), ([], []))


class CommittedSite(unittest.TestCase):
    def test_committed_docs_are_what_their_sources_build(self):
        root = oh_cli.PROGRAM_ROOT
        if not (root / "docs/index.html").is_file():
            self.skipTest("no committed docs site here")
        stale, stray = oh_site.compare(root, oh_site.repository_files())
        if sys.version_info < HELP_PYTHON:
            # The command reference embeds argparse help, whose usage lines
            # wrap differently before Python 3.13; it is built and checked there.
            stale = [path for path in stale if path != "docs/commands.html"]
        self.assertEqual((stale, stray), ([], []),
                         "the committed site drifted from site/, docs/*.md or the help: "
                         "run bin/thermite docs export --to . and commit the result")


if __name__ == "__main__":
    unittest.main()
