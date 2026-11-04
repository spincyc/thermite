import argparse
import configparser
import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import sys

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_logging  # noqa: E402
import oh_units  # noqa: E402
import oh_sandbox as sandbox_lib  # noqa: E402
from oh_cli import sandbox as sandbox_cli  # noqa: E402
from oh_cli import health as health_cli  # noqa: E402
from oh_cli import deploy as deploy_cli  # noqa: E402
from oh_cli import generate as generate_cli  # noqa: E402

VERSIONS = {
    "openhab": {"version": "5.2.3", "image": {
        "ref": "docker.io/openhab/openhab:5.2.3-debian", "digest": "sha256:" + "a" * 64}},
    "atv_helper": {"image": {
        "ref": "docker.io/library/python:3.14.10-slim-trixie", "digest": "sha256:" + "b" * 64}},
    **{name: {"version": "1.0.0", "image": {"ref": f"docker.io/example/{name}:v1.0.0", "digest": "sha256:" + c * 64}}
       for name, c in (("prometheus", "c"), ("node_exporter", "d"), ("blackbox_exporter", "e"), ("grafana", "f"), ("alertmanager", "1"), ("ntfy", "2"))},
}
HOST = {"timezone": "America/Chicago", "http_port": 8080,
        "https_port": 8443, "uid": 9001, "gid": 9001}


def fresh(name: str):
    """A new copy of module `name`, whose import-time settings follow the
    environment as it is now."""
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blob(text: str, program: bool = False) -> deploy.Blob:
    return deploy.Blob(text.encode("utf-8"), program)


class Units(unittest.TestCase):
    def test_quadlet_pins_by_digest(self):
        text = deploy.render_quadlet(VERSIONS, HOST)
        self.assertIn("Image=docker.io/openhab/openhab@sha256:" + "a" * 64, text)
        self.assertNotIn(":5.2.3-debian", text)
        self.assertIn("Network=host", text)
        self.assertIn("Volume=/srv/thermite/runtime/userdata:/openhab/userdata:Z", text)

    def test_tag_is_refused(self):
        versions = {"openhab": {"version": "5.2.3", "image": {"ref": "x:5", "digest": "latest"}}}
        with self.assertRaises(ValueError):
            deploy.image(versions)

    def test_helper_runs_its_deployed_code_as_thermite(self):
        text = deploy.render_atv_quadlet(VERSIONS, HOST)
        self.assertIn("Image=docker.io/library/python@sha256:" + "b" * 64, text)
        self.assertIn("Network=host", text)
        self.assertIn("User=9001", text)
        self.assertIn("Volume=/srv/thermite/runtime/atv/app:/app:ro,Z", text)
        self.assertIn("Volume=/srv/thermite/runtime/atv/secrets:/secrets:Z", text)
        self.assertIn(f"Environment=OPENHAB_ATV_VENV={deploy.ATV_VENV}", text)
        self.assertIn("Exec=python3 /app/bin/thermite apple-tv serve --setup --secrets /secrets", text)
        self.assertNotIn("--allow-remote", text)

    def test_native_entrypoint_and_every_container_start_non_root(self):
        host = {**HOST, "uid": 1234, "gid": 2345}
        for name, text in deploy.render_units(VERSIONS, host).items():
            if name.endswith(".container"):
                with self.subTest(name=name):
                    self.assertIn("User=1234\nGroup=2345\n", text)
                    self.assertIn("NoNewPrivileges=true\nDropCapability=all\n", text)
                    self.assertIn("UMask=00", text)
                    self.assertIn("PodmanArgs=--umask=00", text)
                    self.assertIn("PodmanArgs=--runtime=runc\n", text)
                    self.assertIn("PodmanArgs=--replace=false\n", text)
                    self.assertIn("PodmanArgs=--cidfile=%t/%N.cid\n", text)
                    setting = "Environment=CONTAINERS_CONF_OVERRIDE=/etc/thermite/node-exporter-containers.conf"
                    if name.endswith("thermite-node-exporter.container"):
                        self.assertIn("After=apparmor.service\n", text)
                        self.assertIn("SuccessExitStatus=143\n", text)
                        self.assertIn(setting, text.split("[Service]\n", 1)[1])
                        self.assertNotIn(setting, text.split("[Service]\n", 1)[0])
                    else:
                        self.assertNotIn(setting, text)
        native = deploy.render_quadlet(VERSIONS, host)
        self.assertIn("PodmanArgs=--entrypoint=/bin/sh\n", native)
        self.assertNotIn("\nEntrypoint=", native)
        self.assertIn("PodmanArgs=--stop-timeout=60\n", native)
        self.assertIn("TimeoutStopSec=90\n", native)
        self.assertIn("Native userdata needs offline preparation", native)
        self.assertNotIn("su-exec", native)

    def test_every_payload_renderer_refuses_root_or_invalid_numeric_identity(self):
        service = deploy.metrics.services(HOST)[0]
        renderers = (deploy.render_units, deploy.render_quadlet, deploy.render_atv_quadlet,
                     lambda versions, host: deploy.render_metrics_quadlet(versions, host, service),
                     deploy.prepare_openhab_argv)
        for uid, gid in ((0, 9001), (9001, 0), (-1, 9001), (9001, -1), (True, 9001),
                         (9001, "9001"), (9001.0, 9001)):
            for renderer in renderers:
                with self.subTest(uid=uid, gid=gid, renderer=renderer), self.assertRaises(ValueError):
                    renderer(VERSIONS, {**HOST, "uid": uid, "gid": gid})

    def test_offline_native_preparation_keeps_payload_non_root(self):
        argv = deploy.prepare_openhab_argv(VERSIONS, HOST)
        self.assertEqual(argv[argv.index("--user") + 1], "9001:9001")
        self.assertIn("--entrypoint=/bin/sh", argv)
        self.assertIn("--runtime=runc", argv)
        self.assertIn(deploy.image(VERSIONS), argv)
        self.assertIn(deploy.OPENHAB_UPDATE_SHA256, argv[-1])
        self.assertIn("thermite-before-", argv[-1])

    @unittest.skipUnless(Path("/usr/lib/systemd/system-generators/podman-system-generator").is_file(), "Podman Quadlet generator absent")
    def test_real_quadlet_generator_accepts_payload_identity_and_native_entrypoint(self):
        generator = "/usr/lib/systemd/system-generators/podman-system-generator"
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in deploy.render_units(VERSIONS, HOST).items():
                if name.endswith(".container"):
                    Path(tmp, Path(name).name).write_text(text)
            generated = subprocess.run([generator, "-dryrun", "-no-kmsg-log"],
                                       env={**os.environ, "QUADLET_UNIT_DIRS": tmp}, capture_output=True, text=True)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertIn("Slice=thermite.slice", generated.stdout)
        self.assertIn("PartOf=thermite.target", generated.stdout)
        self.assertNotIn("After=thermite.target", generated.stdout)
        commands = [line for line in generated.stdout.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(len(commands), 2 + len(deploy.METRICS_QUADLETS))
        for command in commands:
            self.assertRegex(command, r"--user(?:=| )9001:9001(?: |$)")
            self.assertIn("--runtime=runc", command)
            self.assertIn("--umask=00", command)
            # Podman's boolean flag parser keeps the final value. Parse the
            # actual generated command so an ordering change cannot silently
            # restore Quadlet's destructive default for a foreign namesake.
            argv = shlex.split(command.removeprefix("ExecStart="))
            image_at = next(i for i, word in enumerate(argv) if "@sha256:" in word)
            replacement = [(i, word) for i, word in enumerate(argv[:image_at])
                           if word == "--replace" or word.startswith("--replace=")]
            self.assertEqual([word for _, word in replacement], ["--replace", "--replace=false"])
            self.assertLess(replacement[0][0], replacement[1][0])
            self.assertIn("--cidfile=%t/%N.cid", argv[:image_at])
        native = next(command for command in commands if re.search(r" --name(?:=| )thermite-openhab ", command))
        self.assertIn("--entrypoint=/bin/sh", native)
        self.assertIn("$$(id", native)

    @unittest.skipUnless(Path("/usr/lib/systemd/system-generators/podman-system-generator").is_file()
                         and shutil.which("systemd-analyze"), "Quadlet generator or systemd-analyze absent")
    def test_systemd_merges_cleanup_dropins_and_removes_only_each_invocations_cid(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, generated = Path(tmp, "quadlets"), Path(tmp, "systemd")
            source.mkdir(); generated.mkdir()
            rendered = deploy.render_units(VERSIONS, HOST)
            for name, text in rendered.items():
                if name.endswith(".container"):
                    (source / Path(name).name).write_text(text)
                else:
                    target = generated / Path(name).relative_to("deploy")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
            from oh_install.ingress import UNIT, unit_text
            (generated / UNIT).write_text(unit_text())
            built = subprocess.run(["/usr/lib/systemd/system-generators/podman-system-generator", str(generated)],
                                   env={**os.environ, "QUADLET_UNIT_DIRS": str(source)},
                                   capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            # Offline verification loads real systemd drop-in/reset semantics;
            # it neither contacts the running manager nor executes commands.
            merged = subprocess.run(["systemd-analyze", "--generators=no", "--man=no", "verify",
                                     *(str(generated / unit) for unit in oh_units.DAEMONS)],
                                    env={**os.environ, "SYSTEMD_LOG_LEVEL": "debug", "LANG": "C",
                                         "SYSTEMD_UNIT_PATH": str(generated) + ":"},
                                    capture_output=True, text=True)
            self.assertEqual(merged.returncode, 0, merged.stderr)
        commands = [shlex.split(line.split("Command Line: ", 1)[1])
                    for line in merged.stdout.splitlines() if "Command Line: " in line]
        expected = {f"/run/{unit.removesuffix('.service')}.cid" for unit in oh_units.DAEMONS}
        starts = [argv for argv in commands if argv[:2] == ["/usr/bin/podman", "run"]]
        self.assertEqual(len(starts), 10)
        self.assertEqual({word.removeprefix("--cidfile=") for argv in starts for word in argv
                          if word.startswith("--cidfile=")}, expected)
        prestarts = [argv for argv in commands if argv[:3] == ["/usr/bin/rm", "-f", "--"]]
        self.assertEqual(len(prestarts), 10)
        self.assertEqual({argv[3] for argv in prestarts}, expected)
        self.assertTrue(all(len(argv) == 4 for argv in prestarts))
        cleanup = [argv for argv in commands if argv[:2] == ["/usr/bin/podman", "rm"]]
        self.assertEqual(len(cleanup), 20)  # ExecStop and ExecStopPost for each container.
        for argv in cleanup:
            self.assertEqual(argv[:5], ["/usr/bin/podman", "rm", "-v", "-f", "-i"])
            self.assertEqual(len(argv), 6)
            self.assertTrue(argv[5].startswith("--cidfile="))
            self.assertIn(argv[5].removeprefix("--cidfile="), expected)
        self.assertEqual({argv[5].removeprefix("--cidfile=") for argv in cleanup}, expected)
        for cid in expected:
            self.assertEqual(sum(argv[5] == "--cidfile=" + cid for argv in cleanup), 2)

    def test_restart_path_watches_the_deploy_trigger(self):
        self.assertIn("PathChanged=/srv/thermite/runtime/deploy/atv-restart", deploy.render_atv_restart_path(HOST))
        self.assertIn("ExecStart=/usr/bin/systemctl try-restart thermite-atv.service",
                      deploy.render_atv_restart_service(HOST))

    def test_every_generated_unit_and_dropin_has_an_install_destination(self):
        self.assertEqual(set(deploy.render_units(VERSIONS, HOST)), set(deploy.INSTALLED))

    def test_target_groups_only_thermite_members_and_services_share_the_slice(self):
        rendered = deploy.render_units(VERSIONS, HOST)
        self.assertEqual(len(rendered), 26)
        self.assertEqual(sum(not name.endswith(".conf") for name in rendered), 16)
        self.assertEqual({path.name for path in deploy.INSTALLED.values()},
                         {Path(name).name for name in rendered})
        containers = set()
        for name, text in rendered.items():
            with self.subTest(name=name):
                if name.endswith(".conf"):
                    self.assertIn(name, deploy.CONTAINER_DROPINS.values())
                    self.assertEqual(text, deploy.render_container_dropin())
                    continue
                self.assertTrue(Path(name).name.startswith("thermite"))
                parsed = configparser.ConfigParser(interpolation=None, strict=False)
                parsed.read_string(text)
                if name == deploy.TARGET:
                    self.assertEqual(parsed["Install"]["WantedBy"], "multi-user.target")
                    self.assertNotIn("Wants", parsed["Unit"])
                    self.assertNotIn("Requires", parsed["Unit"])
                elif name == deploy.SLICE:
                    self.assertEqual(parsed["Slice"]["CPUAccounting"], "yes")
                    self.assertEqual(parsed["Slice"]["MemoryAccounting"], "yes")
                    self.assertNotIn("Install", parsed)
                else:
                    self.assertEqual(parsed["Unit"]["PartOf"], "thermite.target")
                    self.assertNotIn("After=thermite.target", text)
                    if name == deploy.QUADLET:
                        # Wants=, not Requires=: restarting the boundary must
                        # not restart openHAB, whose own ExecStartPre loads the
                        # same rules and fails its start without them.
                        self.assertIn("Wants=thermite-native-boundary.service", text)
                        self.assertIn("After=thermite-native-boundary.service", text)
                        self.assertIn("ExecStartPre=/usr/bin/env nft -f /etc/thermite-native.nft", text)
                        self.assertIn(f" {deploy.JAVA_HEAP_OPTS}\"", text)
                    self.assertNotIn("Requires=", text)
                    self.assertNotIn("BindsTo=", text)
                    if name.endswith((".container", ".path")):
                        self.assertEqual(parsed["Install"]["WantedBy"], "thermite.target")
                    else:
                        self.assertEqual(parsed["Service"]["Type"], "oneshot")
                        self.assertNotIn("Install", parsed)
                    if "Service" in parsed:
                        self.assertEqual(parsed["Service"]["Slice"], "thermite.slice")
                    if "Container" in parsed:
                        containers.add(parsed["Container"]["ContainerName"])
        self.assertEqual(containers, set(oh_units.CONTAINERS.values()))

    def test_runtime_heap_is_bounded_alike_in_the_unit_and_the_sandbox(self):
        self.assertEqual(deploy.JAVA_HEAP_OPTS.split(), ["-Xmx1536m", "-XX:+ExitOnOutOfMemoryError"])
        unit = next(line for line in deploy.render_quadlet(VERSIONS, HOST).splitlines()
                    if line.startswith('Environment="EXTRA_JAVA_OPTS='))
        root = Path("/fictional/sandbox")
        instance = mock.Mock(conf=root / "conf", userdata=root / "userdata", state=root, http_address="127.0.0.1",
                             http_port=18080, https_port=18443)
        env = sandbox_lib.instance_env(mock.Mock(java_home=root / "jdk"), instance)
        for option in deploy.JAVA_HEAP_OPTS.split():
            self.assertIn(option, unit.removesuffix('"').split())
            self.assertIn(option, env["EXTRA_JAVA_OPTS"].split())

    def test_no_host_is_named(self):
        _, host = deploy.load(AREA)
        self.assertEqual(set(host) & {"host", "address"}, set())

    def test_generate_writes_units_and_check_rejects_changed_units(self):
        import oh_public
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            (site / "instance").mkdir()
            house = site / "instance/devices.yaml"
            house.write_text(json.dumps(oh_public.demo()))
            (site / "instance/host.yaml").write_text(json.dumps(HOST))
            (site / "versions.json").write_text(json.dumps(VERSIONS))
            argv = ["--house", str(house), "--conf", str(site / "conf"), "--root", str(site)]
            with mock.patch.object(generate_cli, "AREA", site), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(generate_cli.main(argv), 0)
                for name, text in deploy.render_units(VERSIONS, HOST).items():
                    self.assertEqual((site / name).read_text(), text, name)
                self.assertEqual(generate_cli.main([*argv, "--check"]), 0)
                (site / "deploy/thermite-openhab.container").write_text("owner change\n")
                self.assertEqual(generate_cli.main([*argv, "--check"]), 1)
                self.assertEqual((site / "deploy/thermite-openhab.container").read_text(), "owner change\n")

    def test_installed_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            area, etc = Path(tmp, "area"), Path(tmp, "etc")
            (area / "deploy").mkdir(parents=True)
            etc.mkdir()
            (area / "deploy/a.container").write_text("a\n")
            (area / "deploy/b.container").write_text("b\n")
            (etc / "a.container").write_text("a\n")
            (etc / "b.container").write_text("old\n")
            report = deploy.installed_units(area, {"deploy/a.container": etc / "a.container",
                                                   "deploy/b.container": etc / "b.container",
                                                   "deploy/c.path": etc / "c.path"})
        self.assertEqual([how for _, _, how in report], ["current", "differs", "missing"])


class GrafanaConditions(unittest.TestCase):
    def setUp(self):
        repository = next((parent for parent in (AREA, *AREA.parents) if (parent / ".git").exists()), AREA)
        scratch = Path(os.environ.get("TMPDIR", repository / ".scratch/review-grafana-conditions"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        # The generated command must retain these as literal filename bytes.
        self.host = {**HOST, "uid": 1234, "gid": 2345,
                     "root": str(Path(self.temporary.name) / 'state with $cache 80% "quote" \\ slash')}
        self.secret = deploy.state(self.host).monitoring_secrets / deploy.metrics.GRAFANA_ADMIN_PASSWORD
        self.secret.parent.mkdir(parents=True, mode=0o700)

    def conditions(self):
        service = next(service for service in deploy.metrics.services(self.host) if service.unit == "thermite-grafana.service")
        text = deploy.render_metrics_quadlet(VERSIONS, self.host, service)
        service_text = text.split("[Service]\n", 1)[1]
        lines = [line.removeprefix("ExecCondition=") for line in service_text.splitlines() if line.startswith("ExecCondition=")]
        self.assertEqual(len(lines), 4)
        # exec_word quotes arguments and doubles systemd's literal specifier/
        # environment markers. Only the post-setpriv tails execute below, as
        # the ordinary test user; no identity change or privileged code runs.
        return [[word.replace("%%", "%").replace("$$", "$") for word in shlex.split(line)] for line in lines]

    def statuses(self):
        conditions = self.conditions()
        for command in conditions:
            self.assertEqual(command[:5], ["/usr/bin/setpriv", "--reuid=1234", "--regid=2345", "--clear-groups",
                                           "--no-new-privs"])
            self.assertEqual(command[-1], str(self.secret))
        for command in conditions[:3]:
            self.assertEqual(command[:6], ["/usr/bin/setpriv", "--reuid=1234", "--regid=2345", "--clear-groups",
                                           "--no-new-privs", "/usr/bin/test"])
        # Only the unprivileged /usr/bin/test tails run. Guest acceptance checks
        # the configured identity switch and systemd's combined skip behavior.
        return {(command[-2] if index < 3 else "effective"):
                subprocess.run(command[5:], check=False, timeout=5, capture_output=True).returncode
                for index, command in enumerate(conditions)}

    def test_grafana_only_conditions_preserve_numeric_identity_and_quoted_path(self):
        commands = self.conditions()
        self.assertEqual({command[-2] for command in commands[:3]}, {"-f", "-r", "-s"})
        for command in commands[:3]:
            self.assertEqual(command, ["/usr/bin/setpriv", "--reuid=1234", "--regid=2345", "--clear-groups",
                                       "--no-new-privs", "/usr/bin/test", command[-2], str(self.secret)])
        self.assertEqual(commands[3], ["/usr/bin/setpriv", "--reuid=1234", "--regid=2345", "--clear-groups",
                                      "--no-new-privs", "/usr/bin/sh", "-ec",
                                      'credential=$(/usr/bin/cat -- "$1") && test -n "$credential"',
                                      "thermite-grafana", str(self.secret)])
        # Quoting is defense in depth: the host boundary refuses such a root.
        with self.assertRaisesRegex(deploy.HostError, "root must be"):
            deploy.render_units(VERSIONS, self.host)
        for name, text in deploy.render_units(VERSIONS, {**self.host, "root": "/srv/thermite"}).items():
            if not name.endswith("thermite-grafana.container"):
                with self.subTest(unit=name):
                    self.assertNotIn("ExecCondition=", text)

    def test_missing_directory_and_empty_credentials_block_start(self):
        self.assertEqual(self.statuses(), {"-f": 1, "-r": 1, "-s": 1, "effective": 1})
        self.secret.mkdir(mode=0o700)
        statuses = self.statuses()
        self.assertEqual(statuses["-f"], 1, "a nonempty readable directory is not a credential file")
        self.assertEqual(statuses["effective"], 1)
        self.secret.rmdir()
        self.secret.write_bytes(b"")
        self.secret.chmod(0o600)
        self.assertEqual(self.statuses(), {"-f": 0, "-r": 0, "-s": 1, "effective": 1})
        self.secret.write_bytes(b"\n\n")
        self.assertEqual(self.statuses(), {"-f": 0, "-r": 0, "-s": 0, "effective": 1})

    @unittest.skipIf(os.geteuid() == 0, "read-permission refusal requires a non-root test user")
    def test_nonempty_unreadable_credential_blocks_start(self):
        self.secret.write_bytes(b"fixture generated password")
        self.secret.chmod(0)
        try:
            self.assertEqual(self.statuses(), {"-f": 0, "-r": 1, "-s": 0, "effective": 1})
        finally:
            self.secret.chmod(0o600)

    def test_private_regular_nonempty_credential_allows_start_without_modification(self):
        self.secret.write_bytes(b"fixture generated password")
        self.secret.chmod(0o600)
        before = self.secret.stat()
        self.assertEqual(self.statuses(), {"-f": 0, "-r": 0, "-s": 0, "effective": 0})
        after = self.secret.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_mode, after.st_uid, after.st_gid),
                         (before.st_ino, before.st_mtime_ns, before.st_mode, before.st_uid, before.st_gid))
        self.assertEqual(self.secret.read_bytes(), b"fixture generated password")


class NativeStartup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "openhab")
        self.tools = Path(self.tmp.name, "bin")
        self.tools.mkdir()
        tini = self.tools / "tini"
        tini.write_text('#!/bin/sh\nprintf "%s\\n" "$(id -u):$(id -g) $*" > "$OPENHAB_USERDATA/started"\n')
        tini.chmod(0o700)
        for path in ("conf", "userdata", "addons", "dist/conf", "dist/userdata/etc"):
            (self.root / path).mkdir(parents=True)
        (self.root / "dist/conf/config").write_text("default configuration")
        (self.root / "dist/userdata/etc/version.properties").write_text("openhab-distro = 5.2.3\n")
        self.environment = {**os.environ, "PATH": str(self.tools) + ":/usr/bin:/bin",
                            "OPENHAB_HOME": str(self.root), "OPENHAB_USERDATA": str(self.root / "userdata"),
                            "EXTRA_JAVA_OPTS": "", "OPENHAB_BACKUPS": str(self.root / "userdata/backup"),
                            "OPENHAB_LOGDIR": str(self.root / "userdata/logs")}

    def tearDown(self):
        self.tmp.cleanup()

    def start(self):
        return subprocess.run(["sh", "-ec", deploy.openhab_start()], cwd=self.root,
                              env=self.environment, capture_output=True, text=True)

    def test_fresh_and_same_version_start_without_account_creation_or_root_writes(self):
        first = self.start()
        self.assertEqual(first.returncode, 0, first.stderr)
        marker = self.root / "userdata/started"
        self.assertEqual(marker.read_text().strip(), f"{os.geteuid()}:{os.getegid()} -s ./start.sh server")
        pairing = self.root / "userdata/pairing"
        pairing.write_text("identity preserved")
        before = (pairing.stat().st_ino, pairing.read_bytes())
        second = self.start()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((pairing.stat().st_ino, pairing.read_bytes()), before)
        override = self.root / "userdata/etc/thermite-java.security"
        self.assertEqual(override.read_text(), "crypto.policy=unlimited\n")
        self.assertEqual(stat.S_IMODE(override.stat().st_mode), 0o600)

    def test_mismatched_userdata_refuses_before_starting_native_process(self):
        self.assertEqual(self.start().returncode, 0)
        marker = self.root / "userdata/started"
        marker.unlink()
        version = self.root / "userdata/etc/version.properties"
        version.write_text("openhab-distro = 5.1.0\n")
        before = version.read_bytes()
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("thermite upgrade", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(version.read_bytes(), before)

    def test_incomplete_existing_userdata_is_not_seeded_over(self):
        existing = self.root / "userdata/keystore"
        existing.write_text("preserve")
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete native userdata", result.stderr)
        self.assertEqual(existing.read_text(), "preserve")
        self.assertFalse((self.root / "userdata/etc/version.properties").exists())


class MetricsUnits(unittest.TestCase):
    def unit(self, name: str) -> str:
        return deploy.render_units(VERSIONS, HOST)[f"deploy/thermite-{name}.container"]

    def test_each_pins_its_image_and_runs_as_thermite_without_privileges(self):
        for name, pin, c in (("prometheus", "prometheus", "c"), ("node-exporter", "node_exporter", "d"),
                             ("blackbox", "blackbox_exporter", "e"), ("grafana", "grafana", "f"),
                             ("metrics-exporter", "atv_helper", "b"), ("notifications", "atv_helper", "b"),
                             ("alertmanager", "alertmanager", "1"), ("ntfy", "ntfy", "2")):
            with self.subTest(name=name):
                text = self.unit(name)
                repository = VERSIONS[pin]["image"]["ref"].rsplit(":", 1)[0]
                self.assertIn(f"Image={repository}@sha256:{c * 64}", text)
                self.assertNotIn(":v1.0.0", text)
                for line in ("Network=host", "User=9001", "Group=9001", "NoNewPrivileges=true", "DropCapability=all",
                             "Restart=always", "WantedBy=thermite.target"):
                    self.assertIn(f"\n{line}\n", text)

    def test_only_grafana_listens_beyond_this_host(self):
        for name in ("prometheus", "node-exporter", "blackbox", "metrics-exporter", "alertmanager", "ntfy", "notifications"):
            with self.subTest(name=name):
                self.assertIn(": only this host reaches it.", self.unit(name))
                self.assertNotIn("0.0.0.0", self.unit(name))
        self.assertIn("# Listens on 0.0.0.0:13610: phones and computers on the LAN reach it.", self.unit("grafana"))

    def test_volumes_are_labelled_privately_except_the_shared_secrets_and_host_mounts(self):
        prometheus = self.unit("prometheus")
        self.assertIn("Volume=/srv/thermite/runtime/metrics/prometheus:/etc/prometheus:ro,Z\n", prometheus)
        self.assertIn("Volume=/srv/thermite/runtime/monitoring/prometheus:/prometheus:Z\n", prometheus)
        self.assertIn("Volume=/srv/thermite/runtime/monitoring/secrets:/secrets:ro,z\n", prometheus)
        node = self.unit("node-exporter")
        self.assertIn("Volume=/:/host:ro,rslave\n", node)
        self.assertIn("Volume=/run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro\n", node)
        self.assertIn("PodmanArgs=--pid=host\n", node)

    def test_retention_and_reload(self):
        text = self.unit("prometheus")
        self.assertIn("--storage.tsdb.retention.time=395d --storage.tsdb.retention.size=12GB", text)
        self.assertIn("--web.listen-address=127.0.0.1:13606 --web.enable-lifecycle", text)

    def test_the_exporter_runs_its_deployed_code(self):
        text = self.unit("metrics-exporter")
        self.assertIn("Volume=/srv/thermite/runtime/exporter/app:/app:ro,z\n", text)
        self.assertIn("Exec=python3 /app/lib/oh_metrics/exporter.py --listen=127.0.0.1:13609 ", text)
        self.assertIn("--openhab=http://127.0.0.1:8080 --token-file=/secrets/openhab-token\n", text)

    def test_grafana_reads_its_password_from_a_file(self):
        text = self.unit("grafana")
        self.assertIn("Environment=GF_SECURITY_ADMIN_PASSWORD__FILE=/secrets/grafana-admin-password\n", text)
        self.assertIn("Volume=/srv/thermite/runtime/monitoring/grafana:/var/lib/grafana:Z\n", text)
        self.assertNotIn("Exec=", text)

    def test_exec_words_survive_systemd_unescaping(self):
        self.assertEqual(deploy.exec_word("--a=b"), "--a=b")
        self.assertEqual(deploy.exec_word(r"x=(a|b)\.c"), r'"x=(a|b)\\.c"')
        self.assertEqual(deploy.exec_word("50%$HOME"), '"50%%$$HOME"')

    def test_the_restart_path_restarts_the_exporter_and_grafana(self):
        units = deploy.render_units(VERSIONS, HOST)
        self.assertIn("PathChanged=/srv/thermite/runtime/deploy/metrics-restart\n", units[deploy.METRICS_RESTART_PATH])
        self.assertIn("ExecStart=/usr/bin/systemctl try-restart thermite-metrics-exporter.service "
                      "thermite-grafana.service thermite-notifications.service thermite-ntfy.service\n", units[deploy.METRICS_RESTART_SERVICE])

    def test_installed_means_any_metrics_quadlet_is_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = {name: Path(tmp, Path(name).name) for name in deploy.INSTALLED}
            self.assertFalse(deploy.metrics_installed(installed))
            Path(tmp, "thermite-openhab.container").write_text("x\n")
            self.assertFalse(deploy.metrics_installed(installed))
            Path(tmp, "thermite-grafana.container").write_text("x\n")
            self.assertTrue(deploy.metrics_installed(installed))


class MetricsHealth(unittest.TestCase):
    TARGETS = {"data": {"activeTargets": [
        {"health": "up", "labels": {"job": "openhab", "instance": "127.0.0.1:8080"}},
        {"health": "down", "labels": {"job": "devices", "instance": "192.0.2.40:80"}}]}}

    def answers(self, table):
        def get(url):
            for prefix, answer in table.items():
                if url.startswith(prefix):
                    return answer
            return None, b""
        return get

    def test_ready_targets_and_grafana(self):
        get = self.answers({"http://127.0.0.1:13606/-/ready": (200, b""),
                            "http://127.0.0.1:13606/api/v1/targets": (200, json.dumps(self.TARGETS).encode()),
                            "http://192.0.2.5:13610/api/health": (200, b"{}")})
        health = deploy.metrics_health(HOST, "192.0.2.5", get)
        self.assertEqual((health.up, health.total, health.down), (1, 2, ("devices 192.0.2.40:80",)))
        self.assertTrue(health.prometheus_ok and health.grafana_ok)
        self.assertFalse(health.targets_ok)
        self.assertEqual(health.describe(), "Prometheus http://127.0.0.1:13606 ready, targets 1/2 up; "
                                            "Grafana http://192.0.2.5:13610/ answers")

    def test_the_systemd_collector(self):
        def collector(*values):
            result = [{"metric": {"collector": "systemd"}, "value": [1.0, value]} for value in values]
            return 200, json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode()

        asked = []
        for answer, expected in ((collector("1"), True), (collector("0"), False), (collector(), None),
                                 ((200, b"<html>"), None), ((None, b""), None)):
            with self.subTest(answer=answer):
                table = self.answers({"http://127.0.0.1:13606/-/ready": (200, b""),
                                      "http://127.0.0.1:13606/api/v1/targets": (200, json.dumps(self.TARGETS).encode()),
                                      "http://127.0.0.1:13606/api/v1/query": answer,
                                      "http://192.0.2.5:13610/api/health": (200, b"{}")})
                health = deploy.metrics_health(HOST, "192.0.2.5", lambda url: asked.append(url) or table(url))
                self.assertIs(health.systemd, expected)
                self.assertEqual("systemd collector fails" in health.describe(), expected is False)
                report = health_cli.metrics_report(HOST, installed=True, health=health)
                self.assertEqual(report["systemd_collector"], expected)
        self.assertIn("http://127.0.0.1:13606/api/v1/query?query=node_scrape_collector_success%7B"
                      "collector%3D%22systemd%22%7D", asked)
        self.assertFalse(health_cli.metrics_report(HOST, installed=True, health=deploy.MetricsHealth(
            "p", 200, 1, 1, (), "g", 200, False))["ok"])
        self.assertTrue(health_cli.metrics_report(HOST, installed=True, health=deploy.MetricsHealth(
            "p", 200, 1, 1, (), "g", 200, None))["ok"])

    def test_empty_target_discovery_is_not_ready_despite_a_saved_collector_sample(self):
        get = self.answers({
            "http://127.0.0.1:13606/-/ready": (200, b""),
            "http://127.0.0.1:13606/api/v1/targets": (200, b'{"data":{"activeTargets":[]}}'),
            "http://127.0.0.1:13606/api/v1/query": (200, b'{"data":{"result":[{"value":[1,"1"]}]}}'),
            "http://192.0.2.5:13610/api/health": (200, b"{}"),
        })
        health = deploy.metrics_health(HOST, "192.0.2.5", get)
        self.assertEqual((health.up, health.total), (0, 0))
        self.assertTrue(health.prometheus_ok and health.grafana_ok and health.systemd)
        self.assertFalse(health.targets_ok)
        self.assertFalse(health_cli.metrics_report(HOST, installed=True, health=health)["ok"])

    def test_nothing_answers(self):
        health = deploy.metrics_health(HOST, "192.0.2.5", self.answers({}))
        self.assertFalse(health.prometheus_ok or health.grafana_ok or health.targets_ok)
        self.assertEqual(health.describe(), "Prometheus http://127.0.0.1:13606 not answering; "
                                            "Grafana http://192.0.2.5:13610/ not answering")

    def test_unreadable_targets(self):
        get = self.answers({"http://127.0.0.1:13606/-/ready": (200, b""),
                            "http://127.0.0.1:13606/api/v1/targets": (200, b"<html>")})
        health = deploy.metrics_health(HOST, "192.0.2.5", get)
        self.assertIsNone(health.total)
        self.assertIn("its targets unreadable", health.describe())

    def test_health_reports_metrics_only_once_installed(self):
        report = health_cli.metrics_report(HOST, installed=False)
        self.assertEqual((report["installed"], report["ok"]), (False, True))
        self.assertIn("not installed yet; thermite upgrade installs them", report["summary"])
        get = self.answers({"http://127.0.0.1:13606/-/ready": (200, b""),
                            "http://127.0.0.1:13606/api/v1/targets": (200, json.dumps(self.TARGETS).encode()),
                            "http://192.0.2.5:13610/api/health": (200, b"{}")})
        report = health_cli.metrics_report(HOST, installed=True,
                                           health=deploy.metrics_health(HOST, "192.0.2.5", get))
        self.assertFalse(report["ok"])
        self.assertEqual((report["targets_up"], report["targets"], report["down"]), (1, 2, ["devices 192.0.2.40:80"]))
        self.assertIn("down: devices 192.0.2.40:80", report["summary"])


class Trees(unittest.TestCase):
    def test_digest_covers_paths_and_contents_not_order_or_modes(self):
        a = {"x/a": blob("1"), "b": blob("2")}
        self.assertEqual(deploy.tree_digest(a), deploy.tree_digest({"b": blob("2"), "x/a": blob("1", True)}))
        self.assertNotEqual(deploy.tree_digest(a), deploy.tree_digest({"x/a": blob("1"), "b": blob("3")}))
        self.assertNotEqual(deploy.tree_digest(a), deploy.tree_digest({"x/c": blob("1"), "b": blob("2")}))

    def test_read_tree_matches_a_synced_tree(self):
        files = {"yaml/a.yaml": blob("a: 1\n"), "html/i.png": deploy.Blob(b"\x89PNG\x00"), "bin/x": blob("#!", True)}
        with tempfile.TemporaryDirectory() as tmp:
            deploy.sync(files, Path(tmp))
            self.assertEqual(deploy.read_tree(Path(tmp)), files)

    def test_boot_check_stamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(deploy.boot_checked(Path(tmp), "d" * 64))
            deploy.record_boot_check(Path(tmp), "d" * 64)
            self.assertTrue(deploy.boot_checked(Path(tmp), "d" * 64))
            self.assertFalse(deploy.boot_checked(Path(tmp), "e" * 64))


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name, "conf")
        self.target.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def mode(self, path: str) -> int:
        return stat.S_IMODE((self.target / path).stat().st_mode)

    def test_new_entries_are_group_writable(self):
        changes = deploy.sync({"yaml/a.yaml": blob("a"), "bin/run": blob("#!", True)}, self.target)
        self.assertEqual(changes.added, ["bin/run", "yaml/a.yaml"])
        self.assertEqual(self.mode("yaml"), 0o2770)
        self.assertEqual(self.mode("yaml/a.yaml"), 0o660)
        self.assertEqual(self.mode("bin/run"), 0o770)

    def test_writes_only_what_differs_by_renaming_over_it(self):
        deploy.sync({"a": blob("1"), "b": blob("2")}, self.target)
        before = {name: (self.target / name).stat().st_ino for name in ("a", "b")}
        changes = deploy.sync({"a": blob("1"), "b": blob("3")}, self.target)
        self.assertEqual((changes.changed, changes.unchanged), (["b"], 1))
        self.assertEqual((self.target / "a").stat().st_ino, before["a"])
        self.assertNotEqual((self.target / "b").stat().st_ino, before["b"])
        self.assertEqual((self.target / "b").read_text(), "3")
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["a", "b"])

    def test_never_chmods_an_existing_directory(self):
        (self.target / "yaml").mkdir()
        os.chmod(self.target / "yaml", 0o755)
        deploy.sync({"yaml/a.yaml": blob("a")}, self.target)
        self.assertEqual(self.mode("yaml"), 0o755)

    def test_removes_only_what_a_deploy_installed(self):
        deploy.sync({"old/a": blob("1"), "keep": blob("2")}, self.target)
        (self.target / "theirs").mkdir()
        (self.target / "theirs/x").write_text("x")
        changes = deploy.sync({"keep": blob("2")}, self.target, previous={"old/a": "", "keep": ""})
        self.assertEqual(changes.removed, ["old/a"])
        self.assertFalse((self.target / "old").exists())
        self.assertTrue((self.target / "theirs/x").exists())

    def test_dry_run_writes_nothing(self):
        changes = deploy.sync({"a/b": blob("1")}, self.target, dry_run=True)
        self.assertEqual(changes.added, ["a/b"])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_script_layout_migration_removes_old_rules_before_publishing_replacements(self):
        aggregate = {"automation/js/driver.js": blob("// aggregate")}
        split = {"automation/js/driver_one.js": blob("// one"),
                 "automation/js/driver_two.js": blob("// two")}
        deploy.sync(aggregate, self.target)
        replace = deploy._replace
        for before, after in ((aggregate, split), (split, aggregate)):
            obsolete = set(before) - set(after)

            def checked_replace(destination, data, mode):
                self.assertTrue(all(not (self.target / path).exists() for path in obsolete))
                return replace(destination, data, mode)

            with mock.patch.object(deploy, "_replace", side_effect=checked_replace):
                changes = deploy.sync(after, self.target, previous=before)
            self.assertEqual(changes.removed, sorted(obsolete))

    def test_script_migration_dry_run_retains_original_rules(self):
        old = "automation/js/driver.js"
        deploy.sync({old: blob("// aggregate")}, self.target)
        changes = deploy.sync({"automation/js/driver_one.js": blob("// one")},
                              self.target, previous=[old], dry_run=True)
        self.assertEqual(changes.removed, [old])
        self.assertTrue((self.target / old).is_file())
        self.assertFalse((self.target / "automation/js/driver_one.js").exists())

    def test_refuses_paths_outside_the_target(self):
        for path in ("../x", "/etc/x", "a/../../x", "a//b"):
            with self.assertRaises(deploy.DeployError, msg=path):
                deploy.sync({path: blob("1")}, self.target)

    def test_refuses_a_directory_where_a_file_goes(self):
        (self.target / "a").mkdir()
        with self.assertRaises(deploy.DeployError):
            deploy.sync({"a": blob("1")}, self.target)

    def test_never_traverses_a_symlink_parent_on_write_or_removal(self):
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        (outside / "existing").write_text("keep")
        (self.target / "link").symlink_to(outside, target_is_directory=True)
        for files, previous in (({"link/new": blob("bad")}, ()), ({}, ("link/existing",))):
            with self.assertRaisesRegex(deploy.DeployError, "symlink deployment parent"):
                deploy.sync(files, self.target, previous)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["existing"])
        self.assertEqual((outside / "existing").read_text(), "keep")

    def test_drift(self):
        deploy.sync({"a": blob("1"), "b": blob("2")}, self.target)
        recorded = {"a": deploy.sha256(b"1"), "b": deploy.sha256(b"2"), "c": deploy.sha256(b"3")}
        (self.target / "b").write_text("changed")
        (self.target / "mine").write_text("x")
        self.assertEqual(deploy.drift(self.target, recorded),
                         {"modified": ["b"], "missing": ["c"], "foreign": ["mine"]})


class Install(unittest.TestCase):
    FILES = {"conf/yaml/a.yaml": blob("a: 1\n"), "conf/html/remote/index.html": blob("<p>"),
             "bin/thermite": blob("#!/usr/bin/env python3\n", True), "lib/oh_cli/__init__.py": blob(""),
             "lib/oh_cli/apple_tv.py": blob("y = 1\n"), "lib/oh_atv.py": blob("x = 1\n"),
             "lib/oh_ports.py": blob("# listener defaults\n"),
             "lib/oh_paths.py": blob("# shared paths\n"), "lib/oh_release.py": blob("# release identity\n"),
             "lib/oh_service.py": blob("# service custody\n"),
             "lib/oh_units.py": blob("# Thermite host identities\n"),
             "lib/requirements-pyatv.txt": blob("pyatv==0\n"), "README.md": blob("not deployed")}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.layout = deploy.State(root / "state")
        self.var = root / "var"
        for mode, paths in self.layout.directories():
            for path in paths:
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(mode)
        conf = {p[len("conf/"):]: b for p, b in self.FILES.items() if p.startswith("conf/")}
        deploy.record_boot_check(self.var, deploy.tree_digest(conf))

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, files=None, **options):
        return deploy.install(files or self.FILES, "c" * 40, "a subject", self.layout, self.var, **options)

    def test_installs_conf_and_helper_and_records_them(self):
        result = self.install()
        self.assertEqual(sorted(deploy.read_tree(self.layout.conf)), ["html/remote/index.html", "yaml/a.yaml"])
        self.assertEqual(sorted(deploy.read_tree(self.layout.atv_app)), sorted(deploy.ATV_APP))
        self.assertFalse((self.layout.root / "README.md").exists())
        record = deploy.read_record(self.layout)
        self.assertEqual((record["commit"], record["subject"], record["boot_checked"]), ("c" * 40, "a subject", True))
        self.assertEqual(sorted(record["conf"]), ["html/remote/index.html", "yaml/a.yaml"])
        self.assertTrue(result.atv_app)
        self.assertTrue(self.layout.atv_restart.exists())
        self.assertEqual(len(deploy.history(self.layout)), 1)

    def test_operator_noninteractive_handoff_refuses_before_any_write(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(deploy.DeployError, "no files were changed"):
                self.install(host=host)
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_declined_handoff_refuses_before_any_write(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch("builtins.input", return_value="no"), \
                mock.patch.object(deploy.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(deploy.DeployError, "declined"):
                self.install(host=host)
        run.assert_not_called()
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_approved_handoff_executes_only_displayed_finite_command(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        expected = ("sudo", "-n", *deploy.ownership_argv(self.layout, host))
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch("builtins.input", return_value="yes"), \
                mock.patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.install(host=host)
        self.assertEqual(run.call_args_list, [mock.call(("sudo", "-v"), check=False), mock.call(expected, check=True)])
        self.assertIn(str(self.layout.root), output.getvalue())
        self.assertIn("--uid", output.getvalue())

    def test_parent_approval_reuses_handoff_permission_without_a_child_terminal_or_question(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        expected = ("sudo", "-n", *deploy.ownership_argv(self.layout, host))
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch("builtins.input", side_effect=AssertionError("nested approval question")) as ask, \
                mock.patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.install(host=host, ownership_approved=True)
        ask.assert_not_called()
        self.assertEqual(run.call_args_list, [mock.call(("sudo", "-n", "-v"), check=False), mock.call(expected, check=True)])
        self.assertTrue(self.layout.record.exists())

    def test_parent_approval_cannot_bypass_missing_sudo_or_write_before_privilege_check(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.object(deploy.shutil, "which", return_value=None), \
                mock.patch("builtins.input", side_effect=AssertionError("nested approval question")), \
                mock.patch.object(deploy.subprocess, "run") as run:
            with self.assertRaisesRegex(deploy.DeployError, "sudo is unavailable"):
                self.install(host=host, ownership_approved=True)
        run.assert_not_called()
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_parent_approval_does_not_force_unverified_configuration(self):
        receipt = deploy.passed_dir(self.var) / deploy.tree_digest(
            {path[len("conf/"):]: content for path, content in self.FILES.items() if path.startswith("conf/")})
        receipt.unlink()
        with mock.patch("builtins.input", side_effect=AssertionError("unexpected approval question")), \
                mock.patch.object(deploy.subprocess, "run") as run:
            with self.assertRaisesRegex(deploy.DeployError, "thermite test boot.*has not passed"):
                self.install(ownership_approved=True)
        run.assert_not_called()
        self.assertFalse(self.layout.record.exists())

    def test_parent_approval_preserves_the_privileged_pairing_logging_precheck(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        files = {**self.FILES, "conf/automation/js/sony_ip_control.js": blob("// pairing rule\n")}
        expected = ("sudo", "-n", *deploy.ownership_argv(self.layout, host), "--check-pairing-logging")
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch("builtins.input", side_effect=AssertionError("nested approval question")), \
                mock.patch.object(deploy.subprocess, "run", side_effect=[subprocess.CompletedProcess([], code)
                                  for code in (0, deploy.oh_service.PAIRING_LOGGING_UNPROTECTED)]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(deploy.DeployError, "pairing credentials would enter"):
                self.install(files, host=host, force=True, ownership_approved=True)
        self.assertEqual(run.call_args_list, [mock.call(("sudo", "-n", "-v"), check=False), mock.call(expected, check=False)])
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_private_logging_precheck_runs_before_writes_and_handoff_after_them(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        command = ("sudo", "-n", *deploy.ownership_argv(self.layout, host))
        files = {**self.FILES, "conf/automation/js/sony_ip_control.js": blob("// pairing rule\n")}
        calls = []

        def run(argv, *, check):
            calls.append((argv, check, self.layout.record.exists()))
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch("builtins.input", return_value="yes"), \
                mock.patch.object(deploy.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            self.install(files, host=host, force=True)
        self.assertEqual(calls, [(("sudo", "-v"), False, False),
                                ((*command, "--check-pairing-logging"), False, False), (command, True, True)])

    def test_failed_privileged_logging_precheck_is_not_proof_of_unsafe_logging(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        files = {**self.FILES, "conf/automation/js/sony_ip_control.js": blob("// pairing rule\n")}
        for code in (1, 2, -9):
            with self.subTest(code=code), mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                    mock.patch("builtins.input", return_value="yes"), \
                    mock.patch.object(deploy.subprocess, "run", side_effect=[subprocess.CompletedProcess([], value)
                                      for value in (0, code)]) as run, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(deploy.DeployError, "could not verify") as error:
                    self.install(files, host=host, force=True)
            self.assertNotIn("pairing credentials would enter", str(error.exception))
            self.assertEqual(run.call_count, 2)
            self.assertFalse(self.layout.record.exists())
            self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_failed_authentication_refuses_even_a_deploy_without_pairing_before_writes(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        for result in (subprocess.CompletedProcess([], 1), FileNotFoundError("sudo disappeared")):
            with self.subTest(result=type(result).__name__), mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                    mock.patch.object(deploy.subprocess, "run", side_effect=[result]) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(deploy.DeployError, "sudo authentication failed.*no deployment files") as error:
                    self.install(host=host, ownership_approved=True)
            self.assertNotIn("pairing credentials would enter", str(error.exception))
            run.assert_called_once_with(("sudo", "-v"), check=False)
            self.assertFalse(self.layout.record.exists())
            self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_direct_unreadable_logging_is_not_proof_of_unsafe_logging(self):
        files = {**self.FILES, "conf/automation/js/sony_ip_control.js": blob("// pairing rule\n")}
        with mock.patch.object(deploy.oh_service, "pairing_logging", side_effect=PermissionError("unreadable")):
            with self.assertRaisesRegex(deploy.DeployError, "could not verify"):
                self.install(files, force=True)
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(list(self.layout.conf.iterdir()), [])

    def test_expired_final_handoff_fails_without_retrying_the_mutator(self):
        host = {**HOST, "uid": os.geteuid() + 100, "gid": os.getegid() + 100}
        command = ("sudo", "-n", *deploy.ownership_argv(self.layout, host))
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/sudo"), \
                mock.patch.object(deploy.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0),
                                  subprocess.CalledProcessError(1, command)]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(deploy.DeployError, "ownership handoff failed; deployment is incomplete"):
                self.install(host=host, ownership_approved=True)
        self.assertTrue(self.layout.record.exists())
        self.assertEqual(run.call_args_list, [mock.call(("sudo", "-v"), check=False), mock.call(command, check=True)])

    def test_partial_write_failure_still_hands_ownership_back(self):
        with mock.patch.object(deploy, "sync", side_effect=OSError("disk full")), \
                mock.patch.object(deploy.oh_service, "normalize_state") as normalize:
            with self.assertRaisesRegex(OSError, "disk full"):
                self.install()
        normalize.assert_called_once_with(self.layout.root, os.geteuid(), os.getegid(), deployment=True)

    def test_handoff_failure_is_not_reported_as_success(self):
        with mock.patch.object(deploy.oh_service, "normalize_state", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(deploy.DeployError, "ownership handoff failed"):
                self.install()

    def test_success_normalizes_owner_group_and_private_modes(self):
        self.install()
        self.assertEqual(deploy.oh_service.audit_state(self.layout.root, os.geteuid(), os.getegid()), [])
        self.assertEqual(stat.S_IMODE(self.layout.userdata.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.layout.record.stat().st_mode), 0o660)
        self.assertEqual(stat.S_IMODE(self.layout.history.stat().st_mode), 0o660)
        self.assertEqual(stat.S_IMODE(self.layout.atv_restart.stat().st_mode), 0o660)

    def test_pairing_conf_is_not_copied_before_native_logging_is_protected(self):
        files = {**self.FILES, "conf/automation/js/sony_ip_control.js": blob("// pairing rule\n")}
        with self.assertRaisesRegex(deploy.DeployError, "pairing credentials would enter"):
            self.install(files, force=True)
        self.assertFalse(self.layout.record.exists())
        self.assertEqual(list(self.layout.conf.iterdir()), [])
        native = self.layout.userdata / oh_logging.RELATIVE
        native.parent.mkdir(parents=True)
        native.write_bytes(oh_logging.guarded(b"<Configuration><Loggers><Root/></Loggers></Configuration>"))
        selector = self.layout.userdata / oh_logging.SELECTOR
        selector.write_text("org.ops4j.pax.logging.log4j2.config.file=${karaf.etc}/custom.xml\n")
        with self.assertRaisesRegex(deploy.DeployError, "pairing credentials would enter"):
            self.install(files, force=True)
        self.assertEqual(list(self.layout.conf.iterdir()), [])
        selector.write_bytes(oh_logging.DEFAULT_SELECTOR)
        self.install(files, force=True)
        self.assertTrue((self.layout.conf / "automation/js/sony_ip_control.js").exists())

    def test_android_or_apple_only_pairing_also_requires_the_native_filter(self):
        for name in ("androidtv_googletv", "pyatv_http"):
            with self.subTest(name=name), self.assertRaisesRegex(deploy.DeployError, "pairing credentials would enter"):
                self.install({**self.FILES, f"conf/automation/js/{name}.js": blob("// pairing rule\n")}, force=True)
        with self.assertRaisesRegex(deploy.DeployError, "pairing credentials would enter"):
            self.install({**self.FILES, "conf/yaml/other.yaml": blob("items:\n  dev_pairing:\n    metadata:\n"
                                                                       "      pairing:\n        value: other\n")}, force=True)

    def test_restarts_the_helper_only_when_it_changes(self):
        self.install()
        self.layout.atv_restart.unlink()
        files = dict(self.FILES, **{"conf/yaml/a.yaml": blob("a: 2\n")})
        deploy.record_boot_check(self.var, deploy.tree_digest(
            {p[5:]: b for p, b in files.items() if p.startswith("conf/")}))
        result = self.install(files)
        self.assertEqual(result.conf.changed, ["yaml/a.yaml"])
        self.assertFalse(result.atv_app)
        self.assertFalse(self.layout.atv_restart.exists())
        files["lib/oh_atv.py"] = blob("x = 2\n")
        self.install(files)
        self.assertTrue(self.layout.atv_restart.exists())

    def test_removes_files_the_commit_dropped(self):
        self.install()
        files = {p: b for p, b in self.FILES.items() if p != "conf/html/remote/index.html"}
        result = self.install(files, force=True)
        self.assertEqual(result.conf.removed, ["html/remote/index.html"])
        self.assertFalse((self.layout.conf / "html").exists())
        self.assertFalse(deploy.read_record(self.layout)["boot_checked"])

    def test_needs_a_boot_check_of_that_conf(self):
        files = dict(self.FILES, **{"conf/yaml/a.yaml": blob("untested\n")})
        with self.assertRaisesRegex(deploy.DeployError, "thermite test boot. has not passed"):
            self.install(files)
        self.assertEqual(list(self.layout.conf.iterdir()), [])
        self.install(files, force=True)

    def test_dry_run_writes_nothing(self):
        result = self.install(dry_run=True)
        self.assertEqual(len(result.conf.added), 2)
        self.assertEqual(list(self.layout.conf.iterdir()), [])
        self.assertEqual(deploy.read_record(self.layout), {})

    def test_needs_the_state_directories(self):
        self.layout.atv_app.rmdir()
        with self.assertRaisesRegex(deploy.DeployError, "does not exist"):
            self.install()

    @unittest.skipIf(os.geteuid() == 0, "root writes anywhere")
    def test_needs_write_access_through_the_group(self):
        os.chmod(self.layout.conf, 0o555)
        try:
            with self.assertRaisesRegex(deploy.DeployError, "thermite group"):
                self.install()
        finally:
            os.chmod(self.layout.conf, 0o775)

    def test_without_the_metrics_directories_metrics_are_left_alone(self):
        files = dict(self.FILES, **{"metrics/prometheus/prometheus.yml": blob("a\n")})
        result = self.install(files, post=self.fail)
        self.assertFalse(result.with_metrics)
        self.assertNotIn("metrics", deploy.read_record(self.layout))
        self.assertFalse(self.layout.metrics.exists())


class MetricsDeploy(Install):
    """Install's tests again, on a state with the metrics directories."""

    test_without_the_metrics_directories_metrics_are_left_alone = None
    METRICS = {"metrics/prometheus/prometheus.yml": blob("global: {}\n"),
               "metrics/blackbox/blackbox.yml": blob("modules: {}\n"),
               "metrics/exporter/map.json": blob("{}\n"),
               "metrics/grafana/grafana.ini": blob("[server]\n"),
               "metrics/grafana/dashboards/house.json": blob("{}\n"),
               "lib/oh_metrics/exporter.py": blob("print()\n")}

    def setUp(self):
        super().setUp()
        for mode, paths in self.layout.metrics_directories():
            for path in paths:
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(mode)
        self.posted: list[str] = []
        self.status: int | None = 200

    def post(self, url):
        self.posted.append(url)
        return self.status

    def install(self, files=None, **options):
        # Never the real http_post: it would reach whatever Prometheus answers on this machine.
        host = {**HOST, "uid": os.geteuid(), "gid": os.getegid()}
        return super().install(files, **{"host": host, "post": self.post, **options})

    def deploy(self, **changes):
        files = {**self.FILES, **self.METRICS, **{path: blob(text) for path, text in changes.items()}}
        self.posted.clear()
        return self.install(files)

    def test_a_commit_without_metrics_leaves_them(self):
        self.deploy(**{"metrics/grafana/grafana.ini": "[server]\nold\n"})
        self.layout.metrics_restart.unlink()
        installed = (deploy.read_tree(self.layout.metrics), deploy.read_tree(self.layout.exporter_app))
        recorded = {key: deploy.read_record(self.layout)[key] for key in ("metrics", "exporter_app")}
        without_exporter = {path: data for path, data in {**self.FILES, **self.METRICS}.items()
                            if not path.startswith("lib/oh_metrics/")}
        for files in (self.FILES, without_exporter):
            with self.subTest(files=sorted(files)):
                self.posted.clear()
                result = self.install(files)
                self.assertTrue(result.metrics_kept)
                self.assertEqual((result.metrics, result.exporter_app, result.reloads), (deploy.Changes(),) * 2 + ([],))
                self.assertEqual((deploy.read_tree(self.layout.metrics), deploy.read_tree(self.layout.exporter_app)),
                                 installed)
                record = deploy.read_record(self.layout)
                self.assertEqual({key: record[key] for key in ("metrics", "exporter_app")}, recorded)
                self.assertEqual(self.posted, [])
                self.assertFalse(self.layout.metrics_restart.exists())
        result = self.deploy()
        self.assertFalse(result.metrics_kept)
        self.assertEqual(result.metrics.changed, ["grafana/grafana.ini"])

    def test_installs_metrics_and_the_exporter_and_records_them(self):
        result = self.deploy()
        self.assertTrue(result.with_metrics)
        self.assertEqual(sorted(deploy.read_tree(self.layout.metrics)),
                         ["blackbox/blackbox.yml", "exporter/map.json", "grafana/dashboards/house.json",
                          "grafana/grafana.ini", "prometheus/prometheus.yml"])
        self.assertEqual(sorted(deploy.read_tree(self.layout.exporter_app)), ["lib/oh_metrics/exporter.py", "lib/oh_ports.py"])
        self.assertNotIn("lib/oh_metrics/exporter.py", deploy.read_tree(self.layout.atv_app))
        record = deploy.read_record(self.layout)
        self.assertEqual(sorted(record["exporter_app"]), ["lib/oh_metrics/exporter.py", "lib/oh_ports.py"])
        self.assertEqual(len(record["metrics"]), 5)
        self.assertEqual(self.posted, ["http://127.0.0.1:13606/-/reload", "http://127.0.0.1:13608/-/reload"])
        self.assertTrue(self.layout.metrics_restart.exists())
        self.assertEqual(result.reloads[:2], ["Prometheus reloaded its configuration",
                                              "blackbox_exporter reloaded its configuration"])

    def test_rollback_before_notifications_preserves_installed_stack(self):
        additions = {"metrics/notifications/catalog.json": "{}\n", "metrics/ntfy/server.yml": "listen-http: 127.0.0.1:13612\n",
                     "metrics/alertmanager/alertmanager.yml": "route: {}\n", "lib/oh_metrics/notifications.py": "print()\n"}
        self.deploy(**additions)
        installed = (deploy.read_tree(self.layout.metrics), deploy.read_tree(self.layout.exporter_app))
        self.posted.clear()
        result = self.install({**self.FILES, **self.METRICS})
        self.assertTrue(result.metrics_kept)
        self.assertEqual(self.posted, [])
        self.assertEqual((deploy.read_tree(self.layout.metrics), deploy.read_tree(self.layout.exporter_app)), installed)

    def test_alertmanager_reload_and_adapter_restart_are_distinct(self):
        self.deploy()
        self.layout.metrics_restart.unlink(missing_ok=True)
        result = self.deploy(**{"metrics/alertmanager/alertmanager.yml": "route: {}\n"})
        self.assertEqual(self.posted, ["http://127.0.0.1:13611/-/reload"])
        self.assertIn("Alertmanager reloaded its configuration", result.reloads)
        self.assertFalse(self.layout.metrics_restart.exists())
        self.deploy(**{"metrics/notifications/catalog.json": "{}\n"})
        self.assertTrue(self.layout.metrics_restart.exists())

    def test_an_unchanged_deploy_reloads_and_restarts_nothing(self):
        self.deploy()
        self.layout.metrics_restart.unlink()
        result = self.deploy()
        self.assertEqual((self.posted, result.reloads), ([], []))
        self.assertFalse(self.layout.metrics_restart.exists())

    def test_a_prometheus_change_reloads_only_prometheus(self):
        self.deploy()
        self.layout.metrics_restart.unlink()
        result = self.deploy(**{"metrics/prometheus/prometheus.yml": "global: {scrape_interval: 1m}\n"})
        self.assertEqual(self.posted, ["http://127.0.0.1:13606/-/reload"])
        self.assertEqual(result.reloads, ["Prometheus reloaded its configuration"])
        self.assertFalse(self.layout.metrics_restart.exists())

    def test_a_dashboard_change_needs_no_restart_but_a_setting_or_the_code_does(self):
        self.deploy()
        for path, restarts in (("metrics/grafana/dashboards/house.json", False),
                               ("metrics/exporter/map.json", False),
                               ("metrics/grafana/grafana.ini", True),
                               ("lib/oh_metrics/exporter.py", True)):
            with self.subTest(path=path):
                self.layout.metrics_restart.unlink(missing_ok=True)
                self.deploy(**{path: f"changed for {path}\n"})
                self.assertEqual(self.layout.metrics_restart.exists(), restarts)

    def test_a_service_not_running_yet_or_refusing_is_said(self):
        self.status = None
        self.assertIn("Prometheus is not running yet; it reads the new configuration when it starts",
                      self.deploy().reloads)
        self.status = 400
        result = self.deploy(**{"metrics/blackbox/blackbox.yml": "modules: {a: {}}\n"})
        self.assertEqual(result.reloads, ["blackbox_exporter refused the reload (HTTP 400) and keeps the old "
                                          "configuration: sudo journalctl -u thermite-blackbox.service says why"])

    def test_a_dry_run_posts_and_writes_nothing(self):
        result = self.install({**self.FILES, **self.METRICS}, dry_run=True)
        self.assertEqual(len(result.metrics.added), 5)
        self.assertEqual(self.posted, [])
        self.assertEqual(list(self.layout.metrics.iterdir()), [])


@unittest.skipUnless(shutil.which("git"), "Git is required")
class HostFacts(unittest.TestCase):
    """instance/host.yaml is validated once, completely, before unit text uses it."""

    def refused(self, pattern, **changes):
        host = {**HOST, "metrics": {"units": []}, **changes}
        for key, value in list(host.items()):
            if value is None:
                del host[key]
        with self.assertRaisesRegex(deploy.HostError, pattern):
            deploy.validate_host(host)
        with self.assertRaises(deploy.HostError):
            deploy.render_units(VERSIONS, host)

    def test_valid_hosts_pass_unchanged(self):
        import oh_public
        for host in (HOST, oh_public.demo_host(), {**HOST, "metrics": {
                "units": ["podman.service", "user@1000.service"], "retention": "30d12h", "retention_size": "8GB",
                "grafana_port": 13700, "yoke_targets": ["127.0.0.1:9754"]}}):
            with self.subTest(host=host):
                self.assertIs(deploy.validate_host(host), host)

    def test_missing_and_unknown_settings_are_named(self):
        self.refused("missing timezone", timezone=None)
        self.refused("unknown setting timezon ", timezon="Etc/UTC")
        with self.assertRaisesRegex(deploy.HostError, "must be a mapping"):
            deploy.validate_host(["root"])
        self.refused("unknown metrics setting retension", metrics={"retension": "30d"})
        # The installation root replaced state_root, which has no other meaning now.
        self.refused(r"unknown setting state_root \(allowed: gid, guest_http, http_port, https_port, metrics, root, timezone, uid\)",
                     state_root="/srv/thermite")

    def test_values_cannot_inject_unit_directives(self):
        for zone in ("Etc/UTC\nExecStartPre=/bin/true", "Etc/UTC ", "../etc/passwd", "Etc/%n", 'Etc/"UTC"', 5):
            with self.subTest(zone=zone):
                self.refused("timezone", timezone=zone)
        self.refused("not a known IANA time zone", timezone="Mars/Olympus_Mons")
        for root in ("srv/thermite", "/", "/srv/my thermite", "/srv/thermite\n[Service]", "/srv/a:b", "/srv/%h",
                     "/srv/../etc", "/srv/thermite/", "//srv"):
            with self.subTest(root=root):
                self.refused("root must be", root=root)
        self.refused("metrics.units", metrics={"units": ["x.service\nExecStart=/bin/sh"]})
        self.refused("metrics.retention ", metrics={"retention": 30})
        self.refused("metrics.retention_size", metrics={"retention_size": "12 GB"})
        self.refused("listener port", http_port="13601")
        self.refused("uid and gid", uid=0)
        self.refused("metrics.yoke_targets", metrics={"yoke_targets": ["observer.example"]})

    def test_load_reports_yaml_and_host_problems_with_the_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            (area / "instance").mkdir()
            (area / "versions.json").write_text(json.dumps(VERSIONS))
            path = area / "instance/host.yaml"
            for text, pattern in (("root: [", "not valid YAML"), ("{}", "missing timezone"),
                                  (json.dumps({**HOST, "timezone": "Etc/UTC\nX=1"}), "timezone")):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaisesRegex(deploy.HostError, re.escape(str(path)) + ": " + pattern):
                        deploy.load(area)
            path.write_text(json.dumps(HOST))
            self.assertEqual(deploy.load(area), (VERSIONS, HOST))

    def test_runtime_restarts_are_paced_and_rate_limited(self):
        text = deploy.render_quadlet(VERSIONS, HOST)
        sections, current = {}, None
        for line in text.splitlines():
            if line.startswith("["):
                current = sections.setdefault(line, [])
            elif current is not None and line and not line.startswith("#"):
                current.append(line)
        self.assertIn("StartLimitIntervalSec=300", sections["[Unit]"])
        self.assertIn("StartLimitBurst=10", sections["[Unit]"])
        self.assertIn("Restart=always", sections["[Service]"])
        self.assertIn("RestartSec=15", sections["[Service]"])
        self.assertEqual(text, deploy.render_quadlet(VERSIONS, HOST), "generated units are deterministic")


class PendingRecord(unittest.TestCase):
    """An interrupted deploy's files stay tracked; damaged records fail clearly."""

    FILES = Install.FILES
    setUp, tearDown, install = Install.setUp, Install.tearDown, Install.install

    def interrupted(self, files):
        real = deploy.sync

        def sync(tree, target, previous=(), dry_run=False):
            if Path(target) == self.layout.atv_app:
                raise OSError("interrupted after the configuration was written")
            return real(tree, target, previous, dry_run)

        with mock.patch.object(deploy, "sync", side_effect=sync), \
                self.assertRaisesRegex(OSError, "interrupted"):
            self.install(files, force=True)

    def test_interrupted_deploy_files_are_removed_by_the_next_deploy_or_rollback(self):
        self.install()
        renamed = {**self.FILES, "conf/automation/js/split_rules.js": blob("// new rules\n"),
                   "conf/yaml/b.yaml": blob("b: 1\n")}
        self.interrupted(renamed)
        self.assertTrue((self.layout.conf / "automation/js/split_rules.js").is_file())
        self.assertNotIn("yaml/b.yaml", deploy.read_record(self.layout)["conf"])
        pending = deploy.read_pending(self.layout)
        self.assertIn("automation/js/split_rules.js", pending["conf"])
        self.assertIn("yaml/a.yaml", pending["conf"])
        preview = self.install(force=True, dry_run=True)
        self.assertEqual(preview.conf.removed, ["automation/js/split_rules.js", "yaml/b.yaml"])
        self.assertTrue(self.layout.pending.exists())
        result = self.install(force=True, how="rollback")
        self.assertEqual(result.conf.removed, ["automation/js/split_rules.js", "yaml/b.yaml"])
        self.assertEqual(sorted(deploy.read_tree(self.layout.conf)), ["html/remote/index.html", "yaml/a.yaml"])
        self.assertFalse(self.layout.pending.exists())

    def test_pending_union_is_durable_before_the_first_write(self):
        self.install()
        real, seen = deploy.sync, []

        def sync(tree, target, previous=(), dry_run=False):
            seen.append(json.loads(self.layout.pending.read_text())["trees"])
            return real(tree, target, previous, dry_run)

        files = {**self.FILES, "conf/yaml/b.yaml": blob("b: 1\n")}
        with mock.patch.object(deploy, "sync", side_effect=sync), \
                mock.patch.object(deploy.os, "fsync", wraps=os.fsync) as fsync:
            self.install(files, force=True)
        self.assertEqual(seen[0]["conf"], ["html/remote/index.html", "yaml/a.yaml", "yaml/b.yaml"])
        self.assertEqual(set(seen[0]), {"conf", "atv_app"})
        self.assertGreaterEqual(fsync.call_count, 4, "record and pending contents and their directory are synced")
        self.assertFalse(self.layout.pending.exists())

    def test_unsynced_metrics_paths_remain_pending(self):
        deploy._write_pending(self.layout, "b" * 40, {"metrics": ["prometheus/old.yml"]})
        self.install()
        self.assertEqual(deploy.read_pending(self.layout), {"metrics": ["prometheus/old.yml"]})

    def test_damaged_records_fail_clearly_before_any_write(self):
        for path, content in ((self.layout.record, b"{not json"), (self.layout.record, b"[]"),
                              (self.layout.record, b'{"conf": []}'), (self.layout.record, b"\xff"),
                              (self.layout.pending, b"{not json"), (self.layout.pending, b'{"version": 1}'),
                              (self.layout.pending, b'{"version": 1, "trees": {"conf": [1]}}')):
            with self.subTest(path=path.name, content=content):
                for stale in (self.layout.record, self.layout.pending):
                    stale.unlink(missing_ok=True)
                path.write_bytes(content)
                with self.assertRaisesRegex(deploy.DeployError, f"{path.name} is not .*no deployment files were changed"):
                    self.install()
                self.assertEqual(list(self.layout.conf.iterdir()), [])
                self.assertEqual(path.read_bytes(), content)


class Export(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.area = Path(temporary.name)
        (self.area / "conf/services").mkdir(parents=True)
        (self.area / "conf/services/addons.cfg").write_text("package=minimal\n")
        (self.area / "bin").mkdir()
        (self.area / "bin/thermite").write_text("#!/usr/bin/env python3\n")
        (self.area / "bin/thermite").chmod(0o700)
        for args in (("init", "--initial-branch=fixture"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("config", "commit.gpgsign", "false"), ("add", "."),
                     ("commit", "-m", "reviewed fixture")):
            subprocess.run(["git", "-C", str(self.area), *args], check=True, capture_output=True)

    def test_exports_the_commit_not_the_working_tree(self):
        head = deploy.resolve(self.area, "HEAD")
        (self.area / "conf/services/addons.cfg").write_text("unreviewed change\n")
        files = deploy.export(self.area, head, ("conf/services", "bin/thermite"))
        self.assertEqual(files["conf/services/addons.cfg"].data, b"package=minimal\n")
        self.assertTrue(files["bin/thermite"].program)
        self.assertFalse(files["conf/services/addons.cfg"].program)

    def test_a_path_the_commit_lacks_is_skipped(self):
        head = deploy.resolve(self.area, "HEAD")
        self.assertEqual(deploy.export(self.area, head, ("no-such-dir",)), {})

    def test_unknown_revision(self):
        with self.assertRaises(deploy.DeployError):
            deploy.resolve(self.area, "no-such-revision-anywhere")


class _OpenHAB(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class _Guarded(_OpenHAB):
    """openHAB 5 on /rest/things: only the token oh.abc.123 gets an answer."""

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer oh.abc.123":
            self.send_response(401)
            self.end_headers()
            return
        super().do_GET()


@contextlib.contextmanager
def serving(body: bytes, base: type = _OpenHAB):
    handler = type("Handler", (base,), {"body": body})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class Production(unittest.TestCase):
    def test_openhab_answers_with_its_version(self):
        with serving(json.dumps({"version": "8", "runtimeInfo": {"version": "5.2.3"}}).encode()) as url:
            self.assertEqual(deploy.openhab_version(url), "5.2.3")

    def test_something_else_or_nothing_is_not_openhab(self):
        with serving(b'{"hello": "world"}') as url:
            self.assertIsNone(deploy.openhab_version(url))
        with serving(b"<html>") as url:
            self.assertIsNone(deploy.openhab_version(url))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertIsNone(deploy.openhab_version(f"http://127.0.0.1:{port}", timeout=1))

    def test_a_token_is_accepted_or_refused(self):
        with serving(b"[]", _Guarded) as url:
            self.assertEqual(deploy.token_status(url, "oh.abc.123"), 200)
            self.assertEqual(deploy.token_status(url, "oh.abc.123oh.abc.123"), 401)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.assertIsNone(deploy.token_status(f"http://127.0.0.1:{port}", "oh.abc.123", timeout=1))

    def test_health_names_a_refused_token(self):
        with tempfile.TemporaryDirectory() as tmp, serving(b"{}", _Guarded) as url:
            token = Path(tmp, "api-token")
            token.write_text("oh.abc.999\n")
            args = argparse.Namespace(url=url, token_file=token, timeout=5, allow_offline=None)
            with self.assertRaises(sandbox_lib.SandboxError) as caught:
                health_cli.run(args)
        self.assertEqual(str(caught.exception), f"{url} refused the API token in {token} (HTTP 401)")

    def test_production_is_found_on_this_host(self):
        self.assertEqual(deploy.production_url(HOST), "http://127.0.0.1:8080")
        self.assertEqual(deploy.production_url(HOST, "192.0.2.5"), "http://192.0.2.5:8080")
        self.assertEqual(deploy.token_file("example"), Path("var/example/api-token"))

    def test_live_sandbox_refused_while_production_runs_here(self):
        url = deploy.production_url(deploy.load(AREA)[1])
        for version, state, refused in (("5.2.3", "inactive", f"production openHAB 5.2.3 answers at {url}"),
                                        (None, "activating", "thermite-openhab.service, production openHAB, is up here"),
                                        (None, "inactive", None), (None, "failed", None)):
            with self.subTest(version=version, state=state), \
                    mock.patch.object(deploy, "openhab_version", return_value=version) as asked, \
                    mock.patch.object(deploy, "service_states", return_value={"thermite-openhab.service": state}):
                if refused:
                    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
                        sandbox_cli.refuse_beside_production()
                    self.assertIn(refused, err.getvalue())
                else:
                    sandbox_cli.refuse_beside_production()
                asked.assert_called_once_with(url)

    def test_health_production_reads_this_hosts_token(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(health_cli, "AREA", Path(tmp)), \
                mock.patch.object(deploy, "load", return_value=({}, HOST)):
            token = Path(tmp, "var/example/api-token")
            token.parent.mkdir(parents=True)
            token.write_text("oh.abc.123\n")
            for name, found in (("example", True), ("other", False)):
                args = argparse.Namespace(url=None, token_file=None)
                with self.subTest(name=name), mock.patch.object(deploy, "hostname", return_value=name):
                    if found:
                        health_cli.production(args)
                        self.assertEqual((args.url, args.token_file), ("http://127.0.0.1:8080", token))
                    else:
                        with self.assertRaisesRegex(sandbox_lib.SandboxError, "var/other/api-token: "
                                                    "(bin/)?thermite install saves"):
                            health_cli.production(args)


class HelperVenv(unittest.TestCase):
    def test_venv_follows_the_environment_and_its_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp, "pyatv")
            with mock.patch.dict(os.environ, {"OPENHAB_ATV_VENV": str(venv)}):
                tool = fresh("oh_cli.apple_tv")
            self.assertEqual(tool.VENV, venv)
            self.assertFalse(tool.ready())
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("")
            self.assertFalse(tool.ready())
            (venv / tool.STAMP).write_text(tool.pins() + "\n")
            self.assertTrue(tool.ready())
            (venv / tool.STAMP).write_text("0" * 64 + "\n")
            self.assertFalse(tool.ready())


class CliApproval(unittest.TestCase):
    def test_yes_only_preapproves_ownership_and_does_not_force_or_hide_a_dry_run(self):
        for arguments, approved, dry_run in (([], False, False), (["--yes"], True, False),
                                              (["--yes", "--dry-run"], True, True)):
            with self.subTest(arguments=arguments), \
                    mock.patch.object(deploy_cli.deploy, "load", return_value=(VERSIONS, HOST)), \
                    mock.patch.object(deploy_cli.deploy, "deploy", side_effect=deploy.DeployError("fixture stop")) as apply, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(deploy_cli.main([*arguments, "--_configuration-phase"]), 2)
            self.assertEqual(apply.call_args.kwargs["ownership_approved"], approved)
            self.assertEqual(apply.call_args.kwargs["dry_run"], dry_run)
            self.assertFalse(apply.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
