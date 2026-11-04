"""A site theme cannot read mutable files outside its committed inputs."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import sys

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / 'lib'))
import oh_brand
from oh_features import remote


class ThemeInputs(unittest.TestCase):
    def test_site_theme_chain_is_confined_but_standalone_theme_files_still_work(self):
        scratch = AREA / '.scratch/site-theme-inputs'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder); instance = root / 'instance'; instance.mkdir()
            outside = root / 'outside.json'
            outside.write_text(json.dumps({'name': 'outside', 'extends': 'thermite'}))
            inside = instance / 'inside.json'
            inside.write_text(json.dumps({'name': 'inside', 'extends': 'thermite'}))
            nested = instance / 'themes'; nested.mkdir()
            child = nested / 'child.json'
            child.write_text(json.dumps({'name': 'child', 'extends': '../inside.json'}))
            with patch.object(remote, 'INSTANCE', instance):
                self.assertEqual(remote.palette({'theme': 'themes/child.json'}).name, 'child')
                self.assertEqual(remote.palette({'theme': 'thermite'}).name, 'thermite')
                for path in (str(outside), '../outside.json'):
                    with self.assertRaises(oh_brand.PaletteError):
                        remote.palette({'theme': path})
                for parent in (str(outside), '../../outside.json'):
                    child.write_text(json.dumps({'name': 'child', 'extends': parent}))
                    with self.assertRaises(oh_brand.PaletteError):
                        remote.palette({'theme': 'themes/child.json'})
                child.unlink(); child.symlink_to(outside)
                with self.assertRaises(oh_brand.PaletteError):
                    remote.palette({'theme': 'themes/child.json'})
            self.assertEqual(oh_brand.load(outside).name, 'outside')
