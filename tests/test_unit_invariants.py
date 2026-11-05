"""Every unit Thermite installs belongs to thermite.target and thermite.slice.

Stopping Thermite is `systemctl stop thermite.target thermite.slice`, and
starting it at boot is enabling the target. Both hold only while each unit is
part of the target, runs in the slice and is wanted by the target alone.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy as deploy  # noqa: E402
import oh_units  # noqa: E402
from oh_access import staging  # noqa: E402
from oh_install import ingress  # noqa: E402
from tests.test_deploy import HOST, VERSIONS  # noqa: E402

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


def rendered() -> dict[str, str]:
    """{unit name: text} of every unit Thermite renders and installs."""
    units = {}
    for name, text in deploy.render_units(VERSIONS, HOST).items():
        if name in deploy.CONTAINER_DROPINS.values():
            continue
        unit = Path(name).name
        # Quadlet turns NAME.container into NAME.service.
        units[unit.removesuffix(".container") + ".service" if unit.endswith(".container") else unit] = text
    units.update(staging.units(Path("/srv/thermite-fixture/access/releases/fixture"),
                               state=Path("/srv/thermite-fixture/access/state"), callbacks=True,
                               textfile=Path("/srv/thermite-fixture/runtime/textfile")))
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
                if name.endswith(".service"):
                    self.assertEqual(found.get(("Service", "Slice")), [SLICE])
                else:
                    # A path unit runs no process, and systemd takes Slice=
                    # only for units that do; PartOf= stops it with the target.
                    self.assertTrue(name.endswith(".path"), name)
                    self.assertNotIn(("Path", "Slice"), found)
                install = {key: values for (section, key), values in found.items() if section == "Install"}
                if name in oh_units.RESTART_SERVICES:
                    # Started only by its path unit.
                    self.assertEqual(install, {})
                else:
                    self.assertEqual(install, {"WantedBy": [TARGET]})
                for key in ("Requires", "BindsTo", "Requisite", "Upholds"):
                    self.assertNotIn(("Unit", key), found, "only the target and slice bring members along")

    def test_the_targets_members_are_every_daemon_and_restart_path(self):
        members = {name for name, text in self.units.items()
                   if directives(text).get(("Install", "WantedBy")) == [TARGET]}
        self.assertEqual(members, {*oh_units.DAEMONS, *oh_units.RESTART_PATHS})
        self.assertEqual(set(self.units) - members - set(EXCEPTIONS), set(oh_units.RESTART_SERVICES))

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
        for name, text in deploy.render_units(VERSIONS, HOST).items():
            if name in deploy.CONTAINER_DROPINS.values():
                with self.subTest(dropin=name):
                    self.assertEqual({section for section, _ in directives(text)}, {"Service"})
                    self.assertFalse({key for _, key in directives(text)} & {"Slice", *INSTALL})


if __name__ == "__main__":
    unittest.main()
