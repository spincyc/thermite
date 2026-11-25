"""Every unit Thermite installs belongs to thermite.target and thermite.slice,
and every service keeps to its record of what it may use and see.

Stopping Thermite is `systemctl stop thermite.target thermite.slice`, and
starting it at boot is enabling the target. Both hold only while each unit is
part of the target, runs in the slice and is wanted by the target alone.

Each service has a record (oh_units.Bounds) of the user it runs as, its memory
ceiling, its network and the secrets it is given. Bounds walks every service
unit rendered for every fictional house and fails when one says anything its
record does not.
"""
from __future__ import annotations

import dataclasses
import functools
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_host  # noqa: E402
import oh_metrics  # noqa: E402
import oh_service  # noqa: E402
import oh_units  # noqa: E402
from oh_access import staging  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_install import ingress  # noqa: E402
from tests import test_generated_output  # noqa: E402
from tests.test_deploy import MUSIC_HOST, VERSIONS  # noqa: E402

#: A fictional house using every optional helper service (ATV + climate).
ALL_HELPERS_HOUSE = House({
    "version": 2,
    "rooms": {"den": {"label": "Den", "tag": "Room"}, "bath": {"label": "Bath", "tag": "Bathroom"}},
    "devices": [
        {"id": "den_atv", "room": "den", "kind": "streamer", "make": "Apple", "model": "AppleTV14,1",
         "ip": "192.0.2.40", "controls": {"control": ["pyatv_http"]}},
        {"id": "bath_floor", "room": "bath", "kind": "thermostat", "make": "NuHeat", "model": "Signature",
         "controls": {"control": ["nuheat_http"]}},
    ],
})

TARGET, SLICE = oh_units.TARGET, oh_units.SLICE
#: Directives that make systemd start a unit along with another.
INSTALL = ("WantedBy", "RequiredBy", "UpheldBy", "Alias", "Also")


def directives(text: str) -> dict[tuple[str, str], list[str]]:
    """{(section, key): every value, in order}; repeated keys are kept."""
    found: dict[tuple[str, str], list[str]] = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        key, value = line.split("=", 1)
        found.setdefault((section, key), []).append(value)
    return found


def account(host: dict) -> dict:
    """The pinned Python image and the state's numbers, as a stage hands
    them to the access services' units."""
    return {"image": deploy.image(VERSIONS, "atv_helper"), "identity": (host["uid"], host["gid"])}


def rendered() -> dict[str, str]:
    """{unit name: text} of every unit Thermite renders and installs, on a
    host with all of them."""
    units = {}
    for name, text in deploy.render_units(VERSIONS, MUSIC_HOST, ALL_HELPERS_HOUSE).items():
        if name in deploy.CONTAINER_DROPINS.values():
            continue
        unit = Path(name).name
        # Quadlet turns NAME.container into NAME.service.
        units[unit.removesuffix(".container") + ".service" if unit.endswith(".container") else unit] = text
    units.update(staging.units(Path("/srv/thermite-fixture/access/releases/fixture"),
                               state=Path("/srv/thermite-fixture/access/state"), callbacks=True,
                               textfile=Path("/srv/thermite-fixture/runtime/textfile"), **account(MUSIC_HOST)))
    units[ingress.UNIT] = ingress.unit_text()
    return units


#: The units that differ, and why. Everything else is a member.
EXCEPTIONS = {
    # The boot entry point: enabling it is what starts Thermite at boot.
    TARGET: "the target itself, wanted by multi-user.target",
    # Stopped by name with the target; its members pull it in.
    SLICE: "the slice itself, which runs nothing of its own",
    # Only loads the native-port firewall. It stays enabled and outside the
    # target and slice so that stopping Thermite (or a boot with the target
    # disabled) never removes protection from the native ports; it has no
    # ExecStop, so stopping it would leave the rules loaded anyway.
    ingress.UNIT: "the native boundary, which loads firewall rules only",
}


class Membership(unittest.TestCase):
    def setUp(self):
        self.units = rendered()

    def test_every_unit_is_part_of_the_target_in_its_slice_and_wanted_only_by_it(self):
        self.assertLessEqual(set(EXCEPTIONS), set(self.units))
        for name, text in self.units.items():
            if name in EXCEPTIONS:
                continue
            found = directives(text)
            with self.subTest(unit=name):
                self.assertEqual(found.get(("Unit", "PartOf")), [TARGET])
                self.assertTrue(name.endswith(".service"), name)
                self.assertEqual(found.get(("Service", "Slice")), [SLICE])
                install = {key: values for (section, key), values in found.items() if section == "Install"}
                self.assertEqual(install, {"WantedBy": [TARGET]})
                for key in ("Requires", "BindsTo", "Requisite", "Upholds"):
                    self.assertNotIn(("Unit", key), found, "only the target and slice bring members along")

    def test_the_targets_members_are_every_daemon(self):
        members = {name for name, text in self.units.items()
                   if directives(text).get(("Install", "WantedBy")) == [TARGET]}
        self.assertEqual(members, set(oh_units.DAEMONS))
        self.assertEqual(set(self.units) - members - set(EXCEPTIONS), set())

    def test_target_and_slice_pull_in_nothing_themselves(self):
        target, slice_ = directives(self.units[TARGET]), directives(self.units[SLICE])
        self.assertEqual({key: values for (section, key), values in target.items() if section == "Install"},
                         {"WantedBy": ["multi-user.target"]})
        for found in (target, slice_):
            self.assertFalse({key for (section, key) in found if section == "Unit"} &
                             {"Wants", "Requires", "BindsTo", "PartOf"})
        self.assertFalse(any(section == "Install" for section, _ in slice_))

    def test_the_boundary_stays_outside_thermite_so_stopping_keeps_the_filter(self):
        found = directives(self.units[ingress.UNIT])
        self.assertEqual(found[("Install", "WantedBy")], ["multi-user.target"])
        self.assertNotIn(("Unit", "PartOf"), found)
        self.assertNotIn(("Service", "Slice"), found)
        self.assertNotIn(("Service", "ExecStop"), found)

    def test_dropins_change_how_a_member_stops_never_its_membership(self):
        for name, text in deploy.render_units(VERSIONS, MUSIC_HOST).items():
            if name in deploy.CONTAINER_DROPINS.values():
                with self.subTest(dropin=name):
                    self.assertEqual({section for section, _ in directives(text)}, {"Service"})
                    self.assertFalse({key for _, key in directives(text)} & {"Slice", *INSTALL})


ACCESS_UNITS = dict(release=Path("/srv/thermite/access/releases/fixture"), state=Path("/srv/thermite/access/state"))
MEMORY = re.compile(r"[1-9][0-9]*[MG]")


def size(value: str) -> int:
    return int(value[:-1]) * {"M": 2 ** 20, "G": 2 ** 30}[value[-1]]


def within(path: str, other: str) -> bool:
    """Whether `path` is `other` or lies below it."""
    return path == other or path.startswith(other.rstrip("/") + "/")


def access_problems(found: dict, record: oh_units.Bounds, host: dict, release: Path, state: Path) -> list[str]:
    """What an access service's quadlet shows its container that
    its record does not: its mounts are all it sees of the host, so each is
    held to the release, read-only, the identity state and the textfile
    directory, and to the secret stores of those that the record names."""
    problems = []
    if "--read-only" not in found.get(("Container", "PodmanArgs"), []):
        problems.append("its root file system is writable")
    stated = [value for value in found.get(("Container", "Environment"), [])
              if value.startswith("THERMITE_SERVICE_IDENTITY=")]
    if stated != [f"THERMITE_SERVICE_IDENTITY={host['uid']}:{host['gid']}"]:
        problems.append(f"states the account {stated}")
    secrets = f"{release}/secrets"
    given = [str(state) if name == oh_units.ACCESS_STATE else secrets + name.removeprefix(oh_units.ACCESS_RELEASE)
             for name in record.secrets if name.startswith("access/")]
    sources = []
    for value in found.get(("Container", "Volume"), []):
        source, target, *options = value.split(":")
        sources.append(source)
        if target != source:
            problems.append(f"mounts {source} at another path")
        if within(source, str(release)):
            if "ro" not in ",".join(options).split(","):
                problems.append(f"mounts {source} of its release writable")
        elif not within(source, str(state)) and source != str(deploy.state(host).textfile):
            problems.append(f"mounts {source}, which is neither its release, its state nor the textfile directory")
        for store in (str(state), secrets):
            if within(source, store) and not any(within(source, path) for path in given):
                problems.append(f"mounts {source}, which its record does not declare")
            elif within(store, source) and not any(within(store, path) for path in given):
                problems.append(f"reaches {store} through {source}, which its record does not declare")
    for path in given:
        if not any(within(path, source) or within(source, path) for source in sources):
            problems.append(f"declares {path}, which it does not reach")
    return problems


def bound_problems(text: str, record: oh_units.Bounds, host: dict, access: dict | None = None) -> list[str]:
    """What a rendered service unit says that its record does not, and what
    its record says that is no bound at all. access: for an access service,
    its release and its identity state."""
    if not isinstance(record, oh_units.Bounds):
        return ["no record"]
    found = directives(text)
    problems = [f"{field.name} is not stated" for field in dataclasses.fields(record)
                if getattr(record, field.name) in (None, "")]
    if record.user not in (oh_units.STATE_USER, oh_units.OWN_USER):
        problems.append(f"user {record.user!r} is none a unit is rendered for")
    if record.network != oh_units.HOST_NETWORK:
        problems.append(f"network {record.network!r} is none a unit is rendered for")
    if not (MEMORY.fullmatch(record.memory_high or "") and MEMORY.fullmatch(record.memory_max or "")
            and size(record.memory_high) < size(record.memory_max)):
        problems.append(f"MemoryHigh {record.memory_high} is not below MemoryMax {record.memory_max}")
    for key, value in (("MemoryHigh", record.memory_high), ("MemoryMax", record.memory_max)):
        if found.get(("Service", key)) != [value]:
            problems.append(f"{key} is {found.get(('Service', key))}, not {value}")
    if ("Container", "Image") in found:
        said = {key: found.get(("Container", key)) for key in ("Network", "User", "Group")}
        uid, gid = host["uid"], host["gid"]
        if record.user == oh_units.OWN_USER:
            uid = gid = oh_metrics.node_exporter_id(host)
            if uid in (host["uid"], host["gid"]):
                problems.append(f"its own user {uid} is the state's")
        if said != {"Network": [record.network], "User": [str(uid)], "Group": [str(gid)]}:
            problems.append(f"runs as {said}")
        reached = [value.split(":")[0] for value in found.get(("Container", "Volume"), [])]
        if access is not None:
            problems += access_problems(found, record, host, **access)
    else:
        # A host service would see the host's files as far as its user
        # reads them: every secret store, whatever its record says.
        problems.append("is a host service, not a container")
        reached = []
    root = str(deploy.state(host).root)
    given = [f"{root}/{name}" for name in record.secrets if not name.startswith("access/")]
    def sources(path):
        """What of `reached` leads to `path`: for a user of the unit's own
        nothing from above the runtime state, which only the state's user
        and group enter (0750)."""
        return [source for source in reached if not (record.user == oh_units.OWN_USER and within(root, source))]

    for store in (f"{root}/{name}" for name in oh_service.PRIVATE):
        for source in sources(store):
            if within(source, store) and not any(within(source, path) for path in given):
                problems.append(f"mounts {source}, which its record does not declare")
            elif within(store, source) and not any(within(store, path) for path in given):
                problems.append(f"reaches {store} through {source}, which its record does not declare")
    for path in given:
        if not any(within(path, source) or within(source, path) for source in sources(path)):
            problems.append(f"declares {path}, which it does not reach")
    return problems


@functools.lru_cache(maxsize=None)
def fixture_houses() -> dict[str, tuple[dict, dict[str, str]]]:
    """Name -> (host, {unit: text}): the units `thermite generate` writes for
    each fictional house with the access services' units for it, and the
    rendered units of a host with every service."""
    with tempfile.TemporaryDirectory() as out:
        trees = test_generated_output.generated(Path(out))
    _, host = deploy.load(AREA)
    def access(host):
        return staging.units(ACCESS_UNITS["release"], state=ACCESS_UNITS["state"], callbacks=True,
                             textfile=deploy.state(host).textfile, runtime=deploy.state(host).root, **account(host))

    houses = {name: (host, {**{Path(path).name.removesuffix(".container") + ".service": data.decode("utf-8")
                               for path, data in files.items()
                               if path.startswith("deploy/") and path.endswith((".container", ".service"))},
                            **access(host)})
              for name, files in trees.items()}
    houses["a host with every service"] = (MUSIC_HOST, {**rendered(), **access(MUSIC_HOST)})
    return houses


class Bounds(unittest.TestCase):
    def test_every_service_of_every_fixture_house_keeps_to_its_record(self):
        houses = fixture_houses()
        self.assertGreater(len(houses), 3)
        walked = set()
        for house, (host, units) in houses.items():
            records = deploy.bounds(host)
            for name, text in units.items():
                found = directives(text)
                oneshot = found.get(("Service", "Type")) == ["oneshot"]
                if not name.endswith(".service") or oneshot and ("Container", "Image") not in found:
                    # A target, slice, path or mount, or a command that runs once and exits.
                    self.assertNotIn(name, oh_units.DAEMONS)
                    continue
                with self.subTest(house=house, unit=name):
                    self.assertEqual(bound_problems(text, records.get(name), host,
                                                    ACCESS_UNITS if name in oh_units.ACCESS_UNITS else None), [])
                    walked.add(name)
        self.assertEqual(walked, set(oh_units.DAEMONS))
        self.assertEqual(set(deploy.bounds(MUSIC_HOST)), set(oh_units.DAEMONS))

    def test_a_unit_that_strays_from_its_record_is_found(self):
        host, units = fixture_houses()["a host with every service"]
        records = deploy.bounds(host)
        root = deploy.state(host).root

        def strayed(unit, old, new, record=None):
            text = units[unit]
            self.assertIn(old, text)
            return bound_problems(text.replace(old, new), record or records[unit], host)

        grafana, openhab = oh_units.GRAFANA, oh_units.RUNTIME
        self.assertEqual(strayed(grafana, "MemoryMax=1536M\n", ""), ["MemoryMax is None, not 1536M"])
        self.assertEqual(strayed(openhab, "MemoryHigh=2560M\n", "MemoryHigh=infinity\n"),
                         ["MemoryHigh is ['infinity'], not 2560M"])
        self.assertIn("runs as", strayed(grafana, "User=9001\n", "User=0\n")[0])
        self.assertIn("runs as", strayed(grafana, "Network=host\n", "")[0])
        self.assertIn("runs as", strayed(oh_units.ACCESS, "User=9001\n", "User=0\n")[0])
        # A secret store, a file of one, or a directory that holds one, mounted without being declared.
        password = f"Volume={root}/monitoring/secrets/grafana-admin-password"
        for mount, problem in ((f"Volume={root}/monitoring/secrets:/all:ro\n{password}", "mounts"),
                               (f"Volume={root}/userdata/jsondb:/jsondb:ro\n{password}", "mounts"),
                               (f"Volume={root}:/state:ro\n{password}", "reaches")):
            with self.subTest(mount=mount):
                self.assertTrue(all(found.startswith(problem) and found.endswith("which its record does not declare")
                                    for found in strayed(grafana, password, mount)), strayed(grafana, password, mount))
                self.assertTrue(strayed(grafana, password, mount))
        self.assertEqual(strayed(oh_units.BLACKBOX, "[Service]", "[Service]",
                                 dataclasses.replace(records[oh_units.BLACKBOX], secrets=("music",))),
                         [f"declares {root}/music, which it does not reach"])
        # A host service: it would read every store its user does. No daemon is one.
        gateway = oh_units.ACCESS
        self.assertEqual(bound_problems("[Service]\nUser=thermite\nGroup=thermite\nMemoryHigh=256M\nMemoryMax=512M\n",
                                        records[gateway], host), ["is a host service, not a container"])
        # A container that is mounted a store of the runtime state, or the state itself.
        self.assertEqual(strayed(gateway, "[Service]", f"Volume={root}/userdata:{root}/userdata:ro\n[Service]"),
                         [f"mounts {root}/userdata, which its record does not declare"])
        self.assertEqual(strayed(gateway, "[Service]", f"Volume={root}:{root}:ro\n[Service]"),
                         [f"reaches {root}/{name} through {root}, which its record does not declare"
                          for name in oh_service.PRIVATE])
        # A user of its own keeps nothing of the state's: not a store mounted for it by name.
        node = oh_units.NODE_EXPORTER
        self.assertEqual(strayed(node, "[Service]", f"Volume={root}/secrets:/s:ro\n[Service]"),
                         [f"mounts {root}/secrets, which its record does not declare"])
        # A record that bounds nothing, and a service without one.
        loose = dataclasses.replace(records[grafana], memory_high="2G", memory_max="1G")
        self.assertIn("MemoryHigh 2G is not below MemoryMax 1G", strayed(grafana, "[Service]", "[Service]", loose))
        self.assertEqual(bound_problems(units[grafana], None, host), ["no record"])

    def test_an_access_service_sees_only_what_its_record_declares(self):
        host, units = fixture_houses()["a host with every service"]
        records = deploy.bounds(host)
        release, state = ACCESS_UNITS["release"], ACCESS_UNITS["state"]
        gateway, worker, relay = oh_units.ACCESS_UNITS
        self.assertEqual(oh_units.ACCESS_CONTAINED, (gateway, worker, relay))
        self.assertEqual((records[gateway].secrets, records[worker].secrets, records[relay].secrets),
                         (("access/state", "access/release/secrets"),
                          ("access/state", "access/release/secrets/runtime-bearer"), ()))

        def volumes(unit):
            return directives(units[unit])[("Container", "Volume")]

        def strayed(unit, old, new, record=None):
            self.assertIn(old, units[unit])
            return bound_problems(units[unit].replace(old, new), record or records[unit], host, ACCESS_UNITS)

        textfile = str(deploy.state(host).textfile)
        # All each sees of the host: nothing of the runtime state but the textfile directory.
        self.assertEqual(volumes(gateway), [f"{release}:{release}:ro,z", f"{state}:{state}:z", f"{textfile}:{textfile}"])
        self.assertEqual(volumes(worker), [*(f"{release / part}:{release / part}:ro,z" for part in (
            "program", "env", "config.json", "contract.json", "secrets/runtime-bearer")),
            f"{state}:{state}:z", f"{textfile}:{textfile}"])
        self.assertEqual(volumes(relay), [*(f"{release / part}:{release / part}:ro,z" for part in (
            "program", "env", "callbacks.json")), f"{textfile}:{textfile}"])
        for unit in (gateway, worker, relay):
            found = directives(units[unit])
            with self.subTest(unit=unit):
                self.assertEqual((found[("Container", "User")], found[("Container", "Group")]), (["9001"], ["9001"]))
                self.assertEqual((found[("Container", "NoNewPrivileges")], found[("Container", "DropCapability")]),
                                 (["true"], ["all"]))
                self.assertIn(f"--workdir={release}", found[("Container", "PodmanArgs")])
                # The identity the service's own guard reads is the one the container runs as.
                self.assertEqual(strayed(unit, "Environment=THERMITE_SERVICE_IDENTITY=9001:9001\n",
                                         "Environment=THERMITE_SERVICE_IDENTITY=9000:9001\n"),
                                 ["states the account ['THERMITE_SERVICE_IDENTITY=9000:9001']"])
                self.assertEqual(strayed(unit, "PodmanArgs=--read-only\n", ""), ["its root file system is writable"])
                self.assertIn("runs as", strayed(unit, "User=9001\n", "User=0\n")[0])
                part = release if unit == gateway else release / "program"
                program = f"Volume={part}:{part}:ro,z\n"
                self.assertEqual(strayed(unit, program, program.replace(":ro,z", ":z")),
                                 [f"mounts {part} of its release writable"])
                self.assertEqual(strayed(unit, program, program.replace(f":{part}:", ":/program:")),
                                 [f"mounts {part} at another path"])
                self.assertEqual(strayed(unit, program, program + "Volume=/etc:/etc:ro\n"),
                                 ["mounts /etc, which is neither its release, its state nor the textfile directory"])
        # The gateway: its release with both secrets, and the state.
        self.assertEqual(strayed(gateway, f"Volume={state}:{state}:z\n", ""), [f"declares {state}, which it does not reach"])
        self.assertEqual(strayed(gateway, f"Volume={release}:{release}:ro,z\n", f"Volume={release}/program:{release}/program:ro,z\n"),
                         [f"declares {release}/secrets, which it does not reach"])
        # The relay: no state, no secret, and not the release that holds them.
        end = "[Service]"
        self.assertEqual(strayed(relay, end, f"Volume={state}:{state}:z\n{end}"),
                         [f"mounts {state}, which its record does not declare"])
        self.assertEqual(strayed(relay, end, f"Volume={release}/secrets/runtime-bearer:{release}/secrets/runtime-bearer:ro,z\n{end}"),
                         [f"mounts {release}/secrets/runtime-bearer, which its record does not declare"])
        whole = strayed(relay, f"Volume={release}/program:{release}/program:ro,z\n", f"Volume={release}:{release}:ro,z\n")
        self.assertEqual(whole, [f"reaches {release}/secrets through {release}, which its record does not declare"])
        # The worker: the runtime token, never the TLS key or the directory that holds both.
        token = f"Volume={release}/secrets/runtime-bearer:{release}/secrets/runtime-bearer:ro,z\n"
        self.assertEqual(strayed(worker, token, token.replace("runtime-bearer", "tls-key.pem")),
                         [f"mounts {release}/secrets/tls-key.pem, which its record does not declare",
                          f"declares {release}/secrets/runtime-bearer, which it does not reach"])
        self.assertEqual(strayed(worker, token, token.replace("/runtime-bearer", "")),
                         [f"mounts {release}/secrets, which its record does not declare"])
        self.assertEqual(strayed(worker, f"Volume={state}:{state}:z\n", ""),
                         [f"declares {state}, which it does not reach"])
        # A record that names more than the quadlet mounts is found too.
        self.assertEqual(strayed(relay, end, end, dataclasses.replace(records[relay], secrets=records[worker].secrets)),
                         [f"declares {state}, which it does not reach",
                          f"declares {release}/secrets/runtime-bearer, which it does not reach"])

    @unittest.skipUnless(Path("/usr/lib/systemd/system-generators/podman-system-generator").is_file(),
                         "Podman Quadlet generator absent")
    def test_the_quadlet_generator_accepts_the_access_services(self):
        host, units = fixture_houses()["a host with every service"]
        release = ACCESS_UNITS["release"]
        files = staging.unit_files({name: units[name] for name in oh_units.ACCESS_UNITS})
        with tempfile.TemporaryDirectory() as tmp:
            for _, _, destination, text in files:
                if destination.suffix == ".container":
                    Path(tmp, destination.name).write_text(text)
            generated = subprocess.run(["/usr/lib/systemd/system-generators/podman-system-generator", "-dryrun",
                                        "-no-kmsg-log"], env={**os.environ, "QUADLET_UNIT_DIRS": tmp},
                                       capture_output=True, text=True, timeout=60)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        commands = {name: shlex.split(line.removeprefix("ExecStart="))
                    for line in generated.stdout.splitlines() if line.startswith("ExecStart=")
                    for name in oh_units.ACCESS_CONTAINED if f"--name {oh_units.ACCESS_CONTAINERS[name]} " in line
                    or f"--name={oh_units.ACCESS_CONTAINERS[name]} " in line}
        self.assertEqual(set(commands), set(oh_units.ACCESS_CONTAINED), generated.stdout)
        for name, argv in commands.items():
            with self.subTest(unit=name):
                image_at = argv.index(account(host)["image"])
                options, command = argv[:image_at], argv[image_at + 1:]
                for flag in ("--runtime=runc", "--replace=false", "--cidfile=%t/%N.cid", "--umask=0077", "--read-only",
                             "--stop-timeout=30", f"--workdir={release}"):
                    self.assertIn(flag, options)
                # Podman keeps the last value of a repeated boolean flag.
                replace = [word for word in options if word == "--replace" or word.startswith("--replace=")]
                self.assertEqual(replace[-1], "--replace=false")
                self.assertRegex(" ".join(options), r"--user(?:=| )9001(?::9001)? ")
                self.assertEqual(command[:3], [str(release / "env/bin/python"),
                                               str(release / "program/bin/thermite"), "access"])
        # The service systemd is handed keeps its place in Thermite.
        self.assertEqual(generated.stdout.count("Slice=thermite.slice"), 3)
        self.assertEqual(generated.stdout.count("PartOf=thermite.target"), 3)
        self.assertEqual(generated.stdout.count("WantedBy=thermite.target"), 3)

    def test_node_exporter_is_never_the_states_user(self):
        """It has all of / and the host's processes: as the state's user it
        would read every secret store and could signal every service."""
        host, units = fixture_houses()["a host with every service"]
        node, state = oh_units.NODE_EXPORTER, deploy.state(host)
        record, root = deploy.bounds(host)[node], state.root
        self.assertEqual((record.user, record.secrets), (oh_units.OWN_USER, ()))
        found = directives(units[node])
        own = oh_metrics.node_exporter_id(host)
        self.assertEqual((own, host["uid"], host["gid"]), (9000, 9001, 9001))
        self.assertEqual((found[("Container", "User")], found[("Container", "Group")]), ([str(own)], [str(own)]))
        self.assertIn("/:/host:ro,rslave", found[("Container", "Volume")])
        self.assertIn("--pid=host", found[("Container", "PodmanArgs")])
        # What keeps / from leading into the state: its mode, and no group but its own.
        self.assertEqual([mode for mode, paths in state.directories() if root in paths], [0o750])
        self.assertNotIn(("Container", "GroupAdd"), found)
        # The one directory of the state mounted for it, and what makes it readable there.
        self.assertEqual([value for value in found[("Container", "Volume")] if within(value.split(":")[0], str(root))],
                         [f"{root}/textfile:/textfile:ro"])
        self.assertIn("--collector.textfile.directory=/textfile", found[("Container", "Exec")][0].split())
        self.assertEqual([mode for mode, paths in state.metrics_directories() if state.textfile in paths], [0o2775])
        self.assertEqual(deploy.TEXTFILE_FILE_MODE, 0o644)
        # Going back to the state's user, in the record, the unit or both, is found.
        back = dataclasses.replace(record, user=oh_units.STATE_USER)
        as_state = units[node].replace(f"User={own}\nGroup={own}\n", "User=9001\nGroup=9001\n")
        self.assertNotEqual(as_state, units[node])
        reaches = [f"reaches {root}/{name} through /, which its record does not declare" for name in oh_service.PRIVATE]
        self.assertEqual(bound_problems(as_state, back, host), reaches)
        self.assertEqual(bound_problems(units[node], back, host)[1:], reaches)
        self.assertIn("runs as", bound_problems(units[node], back, host)[0])
        self.assertEqual(len(bound_problems(as_state, record, host)), 1)
        self.assertIn("runs as", bound_problems(as_state, record, host)[0])
        # The number: one below the state's uid, or the host's own choice, never the state's.
        chosen = {**host, "metrics": {"node_exporter_uid": 61000}}
        self.assertIn("User=61000\nGroup=61000\n", deploy.render_units(VERSIONS, chosen)[deploy.METRICS_QUADLETS[node]])
        for wrong in (9001, 0, -5, True, "9000", None):
            with self.subTest(node_exporter_uid=wrong), self.assertRaisesRegex(oh_host.HostError, "node_exporter_uid"):
                oh_host.validate({**host, "metrics": {"node_exporter_uid": wrong}})
        with self.assertRaisesRegex(oh_host.HostError, "node_exporter_uid"):
            oh_host.validate({**host, "uid": 9001, "gid": 9000})

    def test_a_record_cannot_leave_a_field_out(self):
        self.assertTrue(all(field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING
                            for field in dataclasses.fields(oh_units.Bounds)))
        self.assertEqual([field.name for field in dataclasses.fields(oh_units.Bounds)],
                         ["user", "memory_high", "memory_max", "network", "secrets"])
        with self.assertRaises(TypeError):
            oh_units.Bounds(oh_units.STATE_USER, "1G", "2G", oh_units.HOST_NETWORK)
        service = oh_metrics.services(MUSIC_HOST)[0]
        values = {field.name: getattr(service, field.name) for field in dataclasses.fields(service)}
        del values["bounds"]
        with self.assertRaises(TypeError):
            oh_metrics.Service(**values)

    def test_openhab_ceiling_leaves_room_above_the_heap(self):
        heap = int(re.search(r"-Xmx(\d+)m", deploy.JAVA_HEAP_OPTS)[1]) * 2 ** 20
        record = oh_units.BOUNDS[oh_units.RUNTIME]
        # Metaspace, the code cache, thread stacks and direct buffers come on top of the heap.
        self.assertGreaterEqual(size(record.memory_high), heap + 1024 * 2 ** 20)
        self.assertGreaterEqual(size(record.memory_max), size(record.memory_high) + 512 * 2 ** 20)


if __name__ == "__main__":
    unittest.main()
