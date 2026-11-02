from pathlib import Path
import hashlib
import json
import sys
import tempfile
import os
import unittest
from unittest.mock import patch
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_androidtv_patch import maintenance as m
from oh_androidtv_patch import owner_files as f


class SystemGuards(unittest.TestCase):
    def test_mount_preflight_rejects_wrong_state_and_duplicate_destination(self):
        adapter = m.SystemAdapter(Path('/synthetic/native'))
        mounts = [{"Destination": "/openhab/" + name, "Source": "/synthetic/native/" + name}
                  for name in ("conf", "userdata", "addons")]
        with patch.object(adapter, 'is_running', return_value=True), patch.object(adapter, 'command') as command:
            command.return_value = json.dumps(mounts)
            adapter.preflight({'state': '/synthetic/native'})
            mounts[1]['Source'] = '/another/runtime/userdata'
            command.return_value = json.dumps(mounts)
            with self.assertRaisesRegex(f.OwnerFileError, 'native_state_mount_mismatch'):
                adapter.preflight({'state': '/synthetic/native'})
            mounts[1]['Source'] = '/synthetic/native/userdata'
            command.return_value = json.dumps(mounts + [mounts[0]])
            with self.assertRaisesRegex(f.OwnerFileError, 'native_state_mount_mismatch'):
                adapter.preflight({'state': '/synthetic/native'})

    def test_stopped_is_more_than_MainPID_zero_and_requires_retired_container(self):
        adapter = m.SystemAdapter(Path('/synthetic/native'))
        status = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'MainPID': '0', 'ControlGroup': ''}
        with patch.object(adapter, 'status', return_value=status), patch.object(adapter, 'cgroup_pids') as pids:
            pids.return_value = {99}
            with self.assertRaisesRegex(f.OwnerFileError, 'old_native_cgroup_not_retired'):
                adapter.assert_stopped()
            pids.return_value = set()
            with patch.object(m.subprocess, 'run') as run:
                run.return_value.returncode = 1
                adapter.assert_stopped() # Removed Quadlet container is a valid stopped state.
                run.return_value.returncode = 0
                with patch.object(adapter, 'command', return_value='true 123'):
                    with self.assertRaisesRegex(f.OwnerFileError, 'native_container_not_stopped'):
                        adapter.assert_stopped()
                with patch.object(adapter, 'command', return_value='false 0'):
                    adapter.assert_stopped()

    def test_watched_addons_exact_owner_and_original_manifest_tamper_guards(self):
        scratch = AREA.parents[1] / '.scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            addons = root / 'addons'
            addons.mkdir()
            jar = addons / 'innocent-name.jar'
            with zipfile.ZipFile(jar, 'w') as out:
                out.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\nBundle-SymbolicName: org.openhab.binding.androidtv\n\n')
            with self.assertRaisesRegex(f.OwnerFileError, 'competing_watched_androidtv'):
                m.addon_state(root)
            with zipfile.ZipFile(jar, 'w') as out:
                out.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\r\nBundle-SymbolicName: org.openhab.binding.android\r\n tv;singleton:=true\r\n\r\n')
            with self.assertRaisesRegex(f.OwnerFileError, 'competing_watched_androidtv'):
                m.addon_state(root)
            jar.unlink()
            (addons / 'another.kar').write_bytes(b'no archive interpretation needed')
            with self.assertRaisesRegex(f.OwnerFileError, 'watched_feature_archive'):
                m.addon_state(root)
            (addons / 'another.kar').unlink()
            source = root / 'source.jar'
            with zipfile.ZipFile(source, 'w') as out:
                out.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\nBundle-Version: 5.2.3\n\n')
                out.writestr('Original.class', b'pinned class')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            cache = root / 'cache.jar'
            def make(extra='', cls=b'pinned class'):
                with zipfile.ZipFile(cache, 'w') as out:
                    out.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\nBundle-Version: 5.2.3\n' + extra + '\n')
                    out.writestr('Original.class', cls)
            with patch.object(m, 'pins', return_value={'assets': {'bundle': {'sha256': digest}}}):
                exact = 'Bundle-UpdateLocation: mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3\n'
                make(exact)
                self.assertEqual(m.original_cache_report(source, cache)['entries'], 2)
                for header in (exact.replace('/5.2.3', '/5.2.4'), exact + 'Unexpected: yes\n', exact + exact):
                    make(header)
                    with self.assertRaises(ValueError):
                        m.original_cache_report(source, cache)
                make(exact, b'tampered class')
                with self.assertRaisesRegex(f.OwnerFileError, 'original_cache_entry_bytes'):
                    m.original_cache_report(source, cache)

    def test_owner_launcher_refuses_user_owned_ancestry_without_importing_operator(self):
        from oh_androidtv_patch import owner_run
        from types import SimpleNamespace
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o40700, st_uid=1000)):
            with self.assertRaisesRegex(ValueError, 'root_owned_nonwritable'):
                owner_run.check_owner(AREA)

    def test_owner_launcher_rejects_fifo_and_oversized_receipt_before_reading(self):
        from oh_androidtv_patch import owner_run
        scratch = AREA.parents[1] / '.scratch'
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            path = root / 'program.json'
            os.mkfifo(path, 0o600)
            with patch.object(owner_run, 'check_owner'):
                with self.assertRaisesRegex(ValueError, 'program_receipt_not_regular_bounded'):
                    owner_run.validate(root, 'a' * 64)
                path.unlink()
                path.write_bytes(b'x' * 32769)
                with self.assertRaisesRegex(ValueError, 'program_receipt_not_regular_bounded'):
                    owner_run.validate(root, 'a' * 64)
