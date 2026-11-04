"""The canonical source is packaged unchanged; site code never supplies it."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import sys

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / 'lib'))
import oh_control4
import oh_package
import oh_paths


class CanonicalPackage(unittest.TestCase):
    def test_package_contains_exact_reviewed_source_bytes(self):
        files = oh_package.public_files(AREA)
        for name, content in files.items():
            self.assertEqual(content, (AREA / name).read_bytes(), name)
        for name in ('lib/oh_features/routing/lifecycle_io.js', 'lib/oh_entry/filter.jar',
                     'lib/oh_units.py', 'web/remote/home.css', 'lib/oh_metrics/yoke.py'):
            self.assertIn(name, files)

    def test_unreviewed_tree_and_symlink_manifest_are_refused(self):
        scratch = AREA / '.scratch/canonical-package-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'application repository'):
                oh_package.public_files(root)
            (root / '.thermite-export.json').symlink_to(AREA / '.thermite-export.json')
            with self.assertRaisesRegex(ValueError, 'application repository'):
                oh_package.public_files(root)

    def test_pep517_uses_the_same_reviewed_source(self):
        reviewed = {'reviewed': b'exact bytes'}
        with patch.object(oh_package, 'public_files', return_value=reviewed) as public, \
                patch.object(oh_package, 'wheel', return_value=(Path('example.whl'), {})) as build:
            self.assertEqual(oh_package.build_wheel('output'), 'example.whl')
        public.assert_called_once_with()
        build.assert_called_once_with(reviewed, Path('output'))


class PrivateControllerEndpoint(unittest.TestCase):
    def test_selected_controller_and_explicit_override(self):
        data = {'control4_director': 'second', 'devices': [
            {'id': 'first', 'kind': 'control4_controller', 'ip': '192.0.2.10'},
            {'id': 'second', 'kind': 'control4_controller', 'ip': '192.0.2.11'}]}
        self.assertEqual(oh_control4.director_ip(data), '192.0.2.11')
        self.assertEqual(oh_control4.director_ip(data, '192.0.2.12'), '192.0.2.12')
        data.pop('control4_director')
        with self.assertRaisesRegex(ValueError, 'select a controller'):
            oh_control4.director_ip(data)
        data['devices'].pop()
        self.assertEqual(oh_control4.director_ip(data), '192.0.2.10')
        with self.assertRaises(ValueError):
            oh_control4.director_ip(data, 'invalid endpoint')
