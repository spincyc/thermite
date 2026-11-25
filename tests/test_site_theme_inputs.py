"""A site theme cannot read mutable files outside its committed inputs."""
from pathlib import Path
import json
import tempfile
import unittest
import sys

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / 'lib'))
import oh_brand
import oh_conf
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
            with oh_conf.instance_at(instance):
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

    def test_yaml_themes_use_strict_loader(self):
        scratch = AREA / '.scratch/site-theme-inputs'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            good = root / 'clean.yaml'
            good.write_text('name: clean\nextends: thermite\n', encoding='utf-8')
            self.assertEqual(oh_brand.load(good).name, 'clean')
            for filename, body in (
                ('dup.yaml', 'name: dup\nname: dup2\nextends: thermite\n'),
                ('anchor.yaml', 'base: &a thermite\nname: anc\nextends: *a\n'),
                ('yaml11.yaml', 'name: y11\nextends: on\n'),
                ('broken.yaml', 'name: [\n'),
            ):
                bad = root / filename
                bad.write_text(body, encoding='utf-8')
                with self.subTest(file=filename), self.assertRaises(oh_brand.PaletteError):
                    oh_brand.load(bad)

    def test_png_rasterization_is_memoized_and_deterministic(self):
        oh_brand._painted.cache_clear()
        palette = oh_brand.load("thermite")
        first = oh_brand.png(palette, mode="dark", size=64, samples=2)
        info1 = oh_brand._painted.cache_info()
        second = oh_brand.png(palette, mode="dark", size=64, samples=2)
        info2 = oh_brand._painted.cache_info()
        self.assertTrue(first.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(first, second)
        self.assertEqual(info1.misses, 1)
        self.assertEqual(info2.hits, info1.hits + 1)
