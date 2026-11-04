"""A controlling PTY and synthetic sudo exercise nested deployment auth.

The only executable named sudo is a fixture under a temporary PATH. It
executes only this test's finite helper; no real privilege or service is used.
"""
from pathlib import Path
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


AREA = Path(__file__).resolve().parents[1]
FIXTURE = r'''
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

root = Path(os.environ['THERMITE_SUDO_TEST_ROOT'])
fixture = root / 'fixture.py'
role = 'sudo' if Path(sys.argv[0]).name == 'sudo' else sys.argv.pop(1)
fault = os.environ['THERMITE_SUDO_TEST_FAULT']
quiet = os.environ['THERMITE_SUDO_TEST_QUIET'] == '1'

def event(kind, **details):
    with (root / 'events.jsonl').open('a') as target:
        target.write(json.dumps({'kind': kind, **details}) + '\n')

def terminal():
    descriptor = os.open('/dev/tty', os.O_RDONLY)
    try:
        assert os.isatty(descriptor)
        assert os.tcgetpgrp(descriptor) == os.getpgrp()
        return {'tty': os.fstat(descriptor).st_rdev, 'sid': os.getsid(0)}
    finally:
        os.close(descriptor)

helper = (sys.executable, str(fixture), 'helper', str(root / 'state'),
          '--uid', '9001', '--gid', '9001')

if role == 'launch':
    os.login_tty(int(sys.argv[1]))
    os.execv(sys.executable, (sys.executable, str(fixture), 'parent'))

elif role == 'sudo':
    args = sys.argv[1:]
    context = {'parent': os.getppid(), **terminal()}
    previous = json.loads((root / 'cache.json').read_text())
    if args == ['-v']:
        event('validate', context=context, previous_matches=previous == context)
        descriptor = os.open('/dev/tty', os.O_RDWR)
        try:
            os.write(descriptor, b'SYNTHETIC SUDO AUTH: ')
            assert os.read(descriptor, 128).strip() == b'fixture answer'
        finally:
            os.close(descriptor)
        if fault == 'authentication':
            raise SystemExit(1)
        (root / 'cache.json').write_text(json.dumps(context))
        raise SystemExit(0)
    assert args and args[0] == '-n', 'finite helper must never prompt'
    if previous != context:
        event('cache_miss', context=context)
        raise SystemExit(1)
    command = tuple(args[1:])
    assert command in (helper, (*helper, '--check-pairing-logging')), 'unexpected executable'
    event('execute', command=list(command))
    os.execv(sys.executable, command)

elif role == 'helper':
    checking = '--check-pairing-logging' in sys.argv
    event('check' if checking else 'repair', deployed=(root / 'deployment-written').exists())
    if checking and fault == 'inspection':
        raise SystemExit(2)
    if checking and fault == 'unprotected':
        raise SystemExit(3)
    raise SystemExit(0)

elif role == 'child':
    import oh_cli
    import oh_deploy as deploy
    parser = argparse.ArgumentParser()
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    assert args.quiet == quiet
    assert sys.stdin.isatty()
    (root / 'child-context.json').write_text(json.dumps({'pid': os.getpid(), **terminal()}))
    print('CHILD_TERMINAL_VISIBLE', flush=True)
    if args.quiet:
        oh_cli._whole_process = True
        oh_cli._apply(argparse.Namespace(quiet=True, no_color=True))
    try:
        with mock.patch.object(deploy.os, 'geteuid', return_value=1000), \
                mock.patch.object(deploy, 'ownership_argv', return_value=helper):
            with deploy.ownership_handoff(deploy.State(root / 'state'), {'uid': 9001, 'gid': 9001},
                                          False, True, approved=args.yes):
                (root / 'deployment-written').write_text('fixture deployment only\n')
    except deploy.DeployError as error:
        print(str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)

elif role == 'parent':
    import oh_cli
    from oh_install import progress, runner
    from oh_install.model import Command
    from oh_install.system import System
    # An existing outer credential is deliberately scoped to this installer,
    # rather than the deployment child's PPID. No real sudo authenticates.
    (root / 'cache.json').write_text(json.dumps({'parent': os.getpid(), **terminal()}))
    if quiet:
        oh_cli._whole_process = True
        oh_cli._apply(argparse.Namespace(quiet=True, no_color=True))
    system = System()
    system.say('Synthetic installer fixture')
    if not runner.confirm_run(system, 'install'):
        raise SystemExit(1)
    argv = (sys.executable, str(fixture), 'child')
    approved = (*argv, '--yes', *(('--quiet',) if quiet else ()))
    action = Command(argv, approved_argv=approved, terminal=True)
    with progress.Progress(system, 'Nested deploy fixture'):
        raise SystemExit(runner.perform(action, system, root, root_confirmed=True))
else:
    raise AssertionError('unknown fixture role')
'''


@unittest.skipUnless(hasattr(os, 'login_tty'), 'a controlling POSIX PTY is required')
class DeployAuthentication(unittest.TestCase):
    def run_fixture(self, *, quiet=False, fault=''):
        scratch = Path(os.environ.get('TMPDIR', AREA / '.scratch/split_export_audit/deploy_auth'))
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='deploy-auth-', dir=scratch) as temporary:
            root = Path(temporary)
            (root / 'bin').mkdir()
            source = '#!' + sys.executable + '\n' + textwrap.dedent(FIXTURE).lstrip()
            fixture = root / 'fixture.py'
            fixture.write_text(source)
            synthetic_sudo = root / 'bin/sudo'
            synthetic_sudo.write_text(source)
            synthetic_sudo.chmod(0o700)
            master, slave = pty.openpty()
            environment = {**os.environ, 'PATH': str(root / 'bin'), 'PYTHONPATH': str(AREA / 'lib'),
                           'PYTHONDONTWRITEBYTECODE': '1',
                           'TERM': 'dumb', 'THERMITE_SUDO_TEST_ROOT': str(root),
                           'THERMITE_SUDO_TEST_FAULT': fault,
                           'THERMITE_SUDO_TEST_QUIET': str(int(quiet))}
            try:
                process = subprocess.Popen((sys.executable, '-B', str(fixture), 'launch', str(slave)),
                                           cwd=AREA, env=environment, pass_fds=(slave,),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except BaseException:
                os.close(master)
                os.close(slave)
                raise
            os.close(slave)
            transcript = bytearray()
            approved = authenticated = False
            deadline = time.monotonic() + 8
            retired = False
            try:
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.05)
                    if readable:
                        try:
                            data = os.read(master, 8192)
                        except OSError:
                            break
                        if not data:
                            break
                        transcript.extend(data)
                        self.assertLess(len(transcript), 128 * 1024, 'fixture output was unbounded')
                        if not approved and b'[y/N]' in transcript:
                            os.write(master, b'y\n')
                            approved = True
                        if not authenticated and b'SYNTHETIC SUDO AUTH:' in transcript:
                            os.write(master, b'fixture answer\n')
                            authenticated = True
                    elif process.poll() is not None:
                        break
            finally:
                # A PTY can close immediately before a normal exit is reaped.
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            process.kill()
                        process.wait(timeout=2)
                finally:
                    os.close(master)
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    retired = True
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                startup_errors = process.stderr.read().decode(errors='replace')
                process.stderr.close()
            text = transcript.decode(errors='replace') + startup_errors
            self.assertTrue(retired, 'fixture left processes in its isolated process group')
            self.assertTrue(approved, text)
            self.assertTrue(authenticated, text)
            self.assertEqual(text.count('[y/N]'), 1, text)
            self.assertEqual(text.count('SYNTHETIC SUDO AUTH:'), 1, text)
            self.assertIn('CHILD_TERMINAL_VISIBLE', text)
            events = [json.loads(line) for line in (root / 'events.jsonl').read_text().splitlines()]
            child = json.loads((root / 'child-context.json').read_text())
            validation = [event for event in events if event['kind'] == 'validate']
            self.assertEqual(len(validation), 1, text)
            self.assertFalse(validation[0]['previous_matches'])
            self.assertEqual(validation[0]['context'], {'parent': child['pid'], 'tty': child['tty'], 'sid': child['sid']})
            self.assertFalse(any(event['kind'] == 'cache_miss' for event in events), text)
            return process.returncode, text, events, (root / 'deployment-written').exists()

    def test_nested_deploy_refreshes_authentication_in_its_own_terminal_and_parent_context(self):
        code, text, events, deployed = self.run_fixture()
        self.assertEqual(code, 0, text)
        self.assertTrue(deployed)
        self.assertEqual([event['kind'] for event in events], ['validate', 'execute', 'check', 'execute', 'repair'])
        self.assertEqual([event['deployed'] for event in events if event['kind'] in ('check', 'repair')], [False, True])

    def test_quiet_install_retains_real_terminal_authentication_without_another_approval(self):
        code, text, events, deployed = self.run_fixture(quiet=True)
        self.assertEqual(code, 0, text)
        self.assertTrue(deployed)
        self.assertEqual(sum(event['kind'] == 'check' for event in events), 1)
        self.assertEqual(sum(event['kind'] == 'repair' for event in events), 1)

    def test_authentication_failure_changes_no_deployment_files_or_logging_diagnosis(self):
        code, text, events, deployed = self.run_fixture(quiet=True, fault='authentication')
        self.assertEqual(code, 1, text)
        self.assertFalse(deployed)
        self.assertEqual([event['kind'] for event in events], ['validate'])
        self.assertIn('sudo authentication failed before deployment', text)
        self.assertNotIn('pairing credentials would enter', text)

    def test_unreadable_logging_check_is_unverified_and_does_not_write(self):
        code, text, events, deployed = self.run_fixture(quiet=True, fault='inspection')
        self.assertEqual(code, 1, text)
        self.assertFalse(deployed)
        self.assertEqual([event['kind'] for event in events], ['validate', 'execute', 'check'])
        self.assertIn('could not verify native pairing-log protection', text)
        self.assertNotIn('pairing credentials would enter', text)

    def test_only_verified_missing_protection_gets_the_specific_logging_diagnosis(self):
        code, text, events, deployed = self.run_fixture(quiet=True, fault='unprotected')
        self.assertEqual(code, 1, text)
        self.assertFalse(deployed)
        self.assertEqual([event['kind'] for event in events], ['validate', 'execute', 'check'])
        self.assertIn('pairing credentials would enter', text)


if __name__ == '__main__':
    unittest.main()
