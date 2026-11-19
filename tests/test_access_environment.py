"""The contained access services' Python environment: the pinned image
builds it from a wheelhouse alone, and the install fills and proves that
wheelhouse before a transaction begins."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_deploy as deploy  # noqa: E402
import oh_public  # noqa: E402
from oh_access import AccessError, environment, staging  # noqa: E402
from oh_install import changeover, manifest, steps  # noqa: E402
from oh_install.runner import _TASK_APPROVAL  # noqa: E402

VERSIONS = {"atv_helper": {"image": {"ref": "registry.example/python:3.14-slim", "digest": "sha256:" + "5" * 64}}}
IMAGE = "registry.example/python@sha256:" + "5" * 64
REQUIREMENTS = (AREA / "lib/requirements-site.txt").read_bytes()


def options(argv: list[str]) -> tuple[list[str], list[str]]:
    """A podman run's own options, and the command it runs in the image."""
    at = argv.index(IMAGE)
    return argv[2:at], argv[at + 1:]


class Commands(unittest.TestCase):
    def test_every_command_runs_the_pinned_image_as_the_operator_with_nothing_it_does_not_need(self):
        wheels, release = Path("/srv/site/build/sandbox/downloads/wheels/0123456789abcdef"), Path("/srv/thermite/access/releases/a")
        commands = {"fetch": environment.fetch_command(IMAGE, wheels), "verify": environment.verify_command(IMAGE, wheels),
                    "build": environment.build_command(IMAGE, release, wheels)}
        operator = f"{os.geteuid()}:{os.getegid()}"
        for name, argv in commands.items():
            with self.subTest(command=name):
                self.assertEqual(argv[:2], ["podman", "run"])
                flags, command = options(argv)
                volumes = [flags[index + 1] for index, word in enumerate(flags) if word == "--volume"]
                self.assertEqual([word for index, word in enumerate(flags)
                                  if word != "--volume" and flags[index - 1] != "--volume"],
                                 ["--rm", "--runtime=runc", "--network=" + ("host" if name == "fetch" else "none"),
                                  "--read-only", "--user", operator, "--umask=0077", "--cap-drop=all",
                                  "--security-opt=no-new-privileges", "--entrypoint=/bin/sh", "--env", "HOME=/tmp"])
                self.assertEqual(command[0], "-euc")
                # The wheelhouse is written only by the fetch; everything else reads.
                self.assertEqual(volumes[-1] if name == "build" else volumes[0],
                                 f"{wheels}:/wheels:" + ("z" if name == "fetch" else "ro,z"))
        fetch, verify, build = (options(commands[name])[1][1] for name in ("fetch", "verify", "build"))
        for script in (fetch, verify, build):
            self.assertIn("--require-hashes --only-binary :all:", script)
        self.assertIn(" download ", fetch)
        self.assertNotIn("--no-index", fetch)
        for script in (verify, build):
            self.assertIn("--no-index --find-links /wheels ", script)
        self.assertIn(" install --dry-run ", verify)
        # The build: an environment whose interpreter is a copy, the pins, a
        # consistency check and one load of what carries compiled code.
        target = release / "container-env"
        self.assertEqual(options(commands["build"])[0][-6:],
                         ["--volume", f"{target}:{target}:z",
                          "--volume", f"{release}/program/lib/requirements-site.txt:/requirements.txt:ro,z",
                          "--volume", f"{wheels}:/wheels:ro,z"])
        self.assertEqual(options(commands["build"])[1][2:], ["thermite-environment", str(target)])
        self.assertTrue(build.startswith('python -m venv --copies "$1"; '))
        self.assertIn(' check; exec "$1/bin/python" -I -c "import argon2, django, ', build)

    def test_a_path_a_volume_or_a_unit_would_misread_is_refused(self):
        for path in ("/srv/a:b/wheels", "/srv/a b", "/srv/%h", "/srv/$HOME", 'relative/wheels', "/srv/a\nb", '/srv/"a"',
                     "/srv/../etc", ""):
            with self.subTest(path=path), self.assertRaisesRegex(AccessError, "containerized access service cannot use"):
                environment.plain(path)
        self.assertEqual(environment.plain(Path("/srv/thermite_2/access/releases/20261118T000000Z-0a1b2c")),
                         "/srv/thermite_2/access/releases/20261118T000000Z-0a1b2c")
        with self.assertRaises(AccessError):
            environment.verify_command(IMAGE, "/srv/a:b")
        with self.assertRaises(AccessError):
            staging.units("/srv/a b/release", state="/srv/state", image=IMAGE, identity=(9001, 9001))

    def test_the_wheelhouse_is_named_by_the_pins_and_the_image(self):
        first = environment.wheelhouse("/srv/site", IMAGE, REQUIREMENTS)
        self.assertEqual(first.parent, Path("/srv/site/build/sandbox/downloads/wheels"))
        self.assertRegex(first.name, "^[0-9a-f]{16}$")
        self.assertEqual(first, environment.wheelhouse("/srv/site", IMAGE, REQUIREMENTS))
        self.assertNotEqual(first, environment.wheelhouse("/srv/site", IMAGE.replace("5", "6"), REQUIREMENTS))
        self.assertNotEqual(first, environment.wheelhouse("/srv/site", IMAGE, REQUIREMENTS + b"\n"))
        # Every pin has hashes, and the modules loaded at the build are pinned ones.
        text = REQUIREMENTS.decode()
        self.assertEqual(environment.pins(REQUIREMENTS), len([line for line in text.splitlines() if "==" in line]))
        self.assertGreater(environment.pins(REQUIREMENTS), 20)
        pinned = {line.split("==")[0].lower().replace("-", "_") for line in text.splitlines() if "==" in line}
        self.assertLessEqual({"argon2_cffi", "django", "h2", "httpx", "hypercorn", "prometheus_client", "uvicorn",
                              "pyyaml"}, pinned)
        self.assertEqual(environment.IMPORTS, ("argon2", "django", "h2", "httpx", "hypercorn", "prometheus_client",
                                               "uvicorn", "yaml"))


class Host:
    """Root's podman, as the wheel step drives it."""

    def __init__(self, *, held=True, complete=False, fetches=True, usable=True):
        self.held, self.complete, self.fetches, self.usable = held, complete, fetches, usable
        self.ran, self.captured, self.messages = [], [], []
        self.wheels = None

    def interactive(self):
        return True

    def say(self, text):
        self.messages.append(text)

    def capture(self, argv, **kwargs):
        argv = tuple(argv)
        self.captured.append(argv)
        if argv[:5] == ("sudo", "-n", "podman", "image", "exists"):
            return (0 if self.held else 1), ""
        assert "--network=none" in argv, argv
        return (0 if self.complete else 1), ""

    def run(self, argv, cwd):
        argv = tuple(argv)
        self.ran.append(argv)
        if argv[:3] == ("sudo", "podman", "pull"):
            self.held = True
        elif "--network=host" in argv:
            if not self.fetches:
                return 1
            (self.wheels / "fixture-1.0-py3-none-any.whl").write_bytes(b"wheel")
            self.complete = self.usable
        elif "--network=none" in argv:
            return 0 if self.complete else 1
        return 0


class Wheels(unittest.TestCase):
    def setUp(self):
        self.site = Path(self.enterContext(tempfile.TemporaryDirectory())) / "site"
        (self.site / "lib").mkdir(parents=True)
        (self.site / "lib/requirements-site.txt").write_bytes(REQUIREMENTS)
        self.context = steps.Context(self.site, VERSIONS, oh_public.demo_host(), "c" * 40, "d" * 64, "example",
                                     "192.0.2.5")
        self.step = steps.GatewayWheels(self.context)
        self.wheels = environment.wheelhouse(self.site, IMAGE, REQUIREMENTS)
        token = _TASK_APPROVAL.set(True)
        self.addCleanup(_TASK_APPROVAL.reset, token)

    def fetch(self, **host):
        system = Host(**host)
        system.wheels = self.wheels
        return self.step.fetch(system), system

    def test_it_is_prepared_beside_the_addon_files_before_anything_stops(self):
        with mock.patch.object(steps, "configuration_steps", return_value=[]):
            prepared = [type(step) for step in changeover.preparation(self.context)]
        self.assertEqual(prepared.index(steps.GatewayWheels), prepared.index(steps.AddonFiles) + 1)
        self.assertLess(prepared.index(steps.GatewayWheels), prepared.index(steps.InstallationRoot))
        # The image is one every version runs and the transaction records.
        self.assertIn(environment.IMAGE, manifest.IMAGES)
        self.assertEqual(self.step.wanted(), (IMAGE, self.wheels, REQUIREMENTS))
        self.assertEqual(staging.wheels(self.site, IMAGE, self.site), (self.wheels, REQUIREMENTS))
        check = self.step.check(Host())
        self.assertEqual((check.done, check.problem), (False, None))
        self.assertEqual(check.detail, f"the 24 pinned wheels of the gateway's worker and relay, in {self.wheels}, "
                                       "checked by the pinned image's pip without a network")
        self.assertTrue(self.step.always)

    def test_a_complete_set_is_only_read_and_no_network_is_used(self):
        self.wheels.mkdir(parents=True)
        (self.wheels / "kept-1.0-py3-none-any.whl").write_bytes(b"wheel")
        code, system = self.fetch(complete=True)
        self.assertEqual(code, 0)
        self.assertEqual(system.ran, [("sudo", "-v")])
        self.assertEqual(system.captured, [("sudo", "-n", "podman", "image", "exists", IMAGE),
                                           ("sudo", "-n", *environment.verify_command(IMAGE, self.wheels))])
        self.assertEqual((self.wheels / "requirements-site.txt").read_bytes(), REQUIREMENTS)
        self.assertEqual(self.step.pulled, [])

    def test_an_empty_or_partial_set_is_fetched_by_the_image_then_verified_without_a_network(self):
        for case, partial in (("empty", False), ("partial", True)):
            with self.subTest(case=case):
                if partial:
                    (self.wheels / "kept-1.0-py3-none-any.whl").write_bytes(b"wheel")
                code, system = self.fetch()
                self.assertEqual(code, 0)
                self.assertEqual(system.ran, [("sudo", "-v"), ("sudo", *environment.fetch_command(IMAGE, self.wheels)),
                                              ("sudo", *environment.verify_command(IMAGE, self.wheels))])
                # A set with nothing in it is not asked whether it is complete.
                self.assertEqual(len(system.captured), 2 if partial else 1)

    def test_the_image_is_pulled_by_digest_when_root_does_not_hold_it_and_the_pull_is_recorded(self):
        code, system = self.fetch(held=False)
        self.assertEqual(code, 0)
        self.assertEqual(system.ran[:2], [("sudo", "-v"), ("sudo", "podman", "pull", IMAGE)])
        self.assertEqual(self.step.pulled, [IMAGE])

    def test_without_a_network_an_incomplete_set_refuses_naming_the_directory_and_the_command(self):
        for case, host in (("nothing fetched", dict(fetches=False)), ("fetched but not usable", dict(usable=False))):
            with self.subTest(case=case):
                code, system = self.fetch(**host)
                self.assertEqual(code, 1)
                [said] = system.messages[-1:]
                self.assertIn(f"The gateway's pinned wheels in {self.wheels} are incomplete, or not the pinned ones", said)
                self.assertIn("Nothing was changed.", said)
                self.assertIn(f"podman run --rm --volume {self.wheels}:/wheels {IMAGE} python -m pip download "
                              "--require-hashes --only-binary :all: --dest /wheels -r /wheels/requirements-site.txt", said)
                self.assertIn("every wheel is checked against the pinned hashes wherever it came from", said)
                self.assertNotIn(("sudo", "systemctl"), [command[:2] for command in system.ran])

    def test_a_refused_sudo_or_a_failed_pull_stops_before_anything_is_written(self):
        system = Host(held=False)
        system.run = lambda argv, cwd: 1
        self.assertEqual(self.step.fetch(system), 1)
        self.assertFalse(self.wheels.exists())


class Staged(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.site = self.root / "site"
        for path in ("lib", "bin", "assets"):
            (self.site / path).mkdir(parents=True)
        (self.site / "lib/requirements-site.txt").write_bytes(REQUIREMENTS)
        (self.site / "bin/thermite").write_text("launcher\n")
        from oh_features.remote import FILES
        for name in FILES:
            (self.site / "assets" / name).write_text("asset\n")
        from oh_access.files import atomic_private, json_private
        for name in ("bearer", "key"):
            atomic_private(self.root / name, b"fixture")
        (self.root / "cert").write_text("fixture")
        self.json_private = json_private

    def stage(self, podman):
        def compile_contract(house, assets, output, **_):
            self.json_private(output, {"rooms": {}})
        with mock.patch.object(staging, "_disposable", return_value=False), mock.patch.object(staging, "_state_location"), \
                mock.patch.object(staging, "_service_parents"), mock.patch.object(staging, "_trusted_ancestors"), \
                mock.patch.object(staging, "_private_plan_parent"), \
                mock.patch("oh_access.service.compile_contract", side_effect=compile_contract), \
                mock.patch("oh_access.service.load_config"), mock.patch.object(staging, "_install_environment") as host:
            receipt = staging.stage(self.root / "release", state=self.root / "state", house="unused",
                                    assets=self.site / "assets", runtime_url="http://127.0.0.1:13602",
                                    runtime_token_file=self.root / "bearer", tls_cert=self.root / "cert",
                                    tls_key=self.root / "key", allowed_hosts=["site.example"], listen=["127.0.0.1"],
                                    port=13601, timezone="Etc/UTC", image=IMAGE, area=self.site, application=self.site,
                                    plan_directory=self.root / "plan", podman=podman)
        host.assert_called_once()
        return receipt

    def test_the_image_builds_the_contained_services_environment_into_the_release_before_custody(self):
        release, built = self.root / "release", []

        def podman(argv):
            target = release / "container-env"
            # The directory is there, the operator's and private, before the container is.
            self.assertEqual((stat.S_IMODE(target.stat().st_mode), target.stat().st_uid), (0o700, os.geteuid()))
            self.assertEqual(argv, environment.build_command(IMAGE, release, environment.wheelhouse(self.site, IMAGE, REQUIREMENTS)))
            built.append(argv)
            (target / "bin").mkdir()
            (target / "lib").mkdir()
            (target / "bin/python").write_bytes(b"\x7fELF")
            (target / "bin/python").chmod(0o755)
            (target / "lib/module.py").write_text("")
            (target / "lib64").symlink_to("lib")

        receipt = self.stage(podman)
        self.assertEqual(len(built), 1)
        self.assertTrue(receipt["environment_installed"])
        # Custody takes it with the rest: private modes, its launcher executable, its one link inside it.
        modes = {path.relative_to(release / "container-env").as_posix(): mode
                 for path, mode in staging._inventory(release, release=True) if (release / "container-env") in path.parents}
        self.assertEqual(modes, {"bin": 0o700, "bin/python": 0o700, "lib": 0o700, "lib/module.py": 0o600, "lib64": None})
        self.assertEqual(stat.S_IMODE((release / "container-env/bin/python").stat().st_mode), 0o700)

    def test_a_failed_build_removes_the_partial_release_and_a_link_out_of_the_environment_is_refused(self):
        def fails(argv):
            (self.root / "release/container-env/partial").write_text("")
            raise AccessError("gateway deployment command failed")

        with self.assertRaisesRegex(AccessError, "gateway deployment command failed"):
            self.stage(fails)
        self.assertFalse((self.root / "release").exists())
        self.assertFalse((self.root / "plan").exists())

        def escapes(argv):
            (self.root / "release/container-env/python").symlink_to("/usr/bin/python3")

        with self.assertRaisesRegex(AccessError, "only regular files, directories and internal environment links"):
            self.stage(escapes)
        self.assertFalse((self.root / "release").exists())

    def test_a_stage_that_installs_environments_needs_a_way_to_run_podman(self):
        with self.assertRaisesRegex(AccessError, "needs a way to run podman"):
            staging.stage(self.root / "release", state=self.root / "state", house="unused", assets="unused",
                          runtime_url="unused", runtime_token_file="unused", tls_cert="unused", tls_key="unused",
                          allowed_hosts=[], listen=["127.0.0.1"], port=13601, timezone="UTC", image=IMAGE,
                          area=self.site, application=self.site,
                          plan_directory=self.root / "plan")
        self.assertFalse((self.root / "release").exists())


if __name__ == "__main__":
    unittest.main()
