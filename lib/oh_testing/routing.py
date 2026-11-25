"""thermite test routing: the room Source selectors end to end, against
simulated matrices."""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import oh_conf
import oh_integrations as integrations
import oh_matrix_sim as sim
import oh_cli
import oh_render as render
import oh_sandbox as lib
import oh_trace
from oh_features import all_off, remote, routing, threadpools
from oh_features.routing import hops
from oh_integrations import _instr as instr

SUMMARY = "the room Source selectors end to end, against simulated matrices"

DESCRIPTION = """\
Prove the room Source selectors work, end to end, without touching the
house. Generate conf/ from the house file, keep only what routing needs
(the model, the matrix and HDMI switch Things, rules and Items, the Source
selectors and routing.js, All off), point those Things and rules at
simulated matrices on 127.0.0.1 (lib/oh_matrix_sim.py), simulate the rooms'
receivers, displays, source power and actions with plain Items that take
what they are sent, boot a test runtime on that as thermite test boot does,
and check that:

1. each room's Source reads back what its HDMI switch and HDMI output show,
   and runtime wrote nothing to a matrix while it started;
2. choosing every source a room offers (all rooms at once, round by round)
   routes the room's HDMI output (and the scaler of its audio output, under
   hdmi_audio_scaler), HDMI switch and USB device ports as the routing says,
   wakes the chosen source, powers its receiver and display (off for Off)
   and puts them on their inputs, sends its actions, has each
   source's input present the EDID of a room showing it (the idle EDID
   once none does), and a choice the room does not offer sends nothing;
3. a switch made at the matrix, at a room's HDMI switch (a keypad, the
   front panel, a web page) or at its receiver or display shows up in that
   room's Source, an input the room does not offer shows as UNDEF, a
   receiver or display turned off there shows as Off, and runtime writes
   nothing for any of them;
4. with two rooms on one source, the last to choose it wins, the first
   gets it back when that room turns off, and the idle EDID follows once
   neither shows it; a room whose receiver zone follows the other's
   (sync_with) takes the other's zone input instead of its own and claims
   no EDID while it does;
5. the rooms go UNDEF while the HDMI matrix, or their HDMI switch, does not
   answer, and recover without a write;
6. the remote page (web/remote) is served at /static/remote/ and, with no
   login, as a phone uses it, reads a room's Source, sends a choice and
   hears it read back on the event stream;
7. All off, sent as the page sends it, turns every room Off as a choice
   of Off would (all_off.js; devices outside the routing are omitted);
8. the simulated matrices heard only the comheads each Thing or rule may send (no
   fixed setting such as HDR or HDCP) and refused none;
9. openhab.log holds no ERROR and no model problem.

It uses the test runtime of thermite test boot (127.0.0.1:13619) and its
warmed userdata, so it cannot run beside it.
"""

EXAMPLES = (
    ("thermite test routing", "every room's Source selector, against simulated matrices on 127.0.0.1"),
    ("thermite test routing --timeout 600", "give a slow host longer to boot"),
)

NOTES = """\
exit status:
  0  every step passed
  1  a step failed
  2  runtime could not be booted or queried
"""

STATE = lib.VAR / "routing-check"
#: The plain Items that simulate receivers, displays, source power and actions.
STAND_INS = "yaml/routing-check.yaml"
#: A command in events.log, as runtime logs every one.
COMMAND = re.compile(r"Item '([^']+)' received command (.*?)(?: \(source: .*\))?$")
STOP_TIMEOUT = 3
SIGNAL_WAIT = 30
#: Seconds a step may take: a poll of a matrix Thing or HDMI switch rule and
#: its timeout, a command and its re-read.
STEP = max((instr.REFRESH, *(mod.POLL_S for mod in integrations.defining("POLL_S")))) + 10
EXECUTOR_QUIET = 0.25
#: The room Sources' updates, as the remote page follows them when openHAB
#: serves it (web/remote/backend.js NATIVE_TOPICS) and the gateway's hub does.
REMOTE_EVENTS = "/rest/events?topics=openhab/items/loc_*_source/stateupdated"


#: The executor status: its counts, then the quarantined actor keys.
ACTIVITY_BYTES = 8192
ACTOR_KEY = re.compile(r"[a-z][a-z0-9_]*")  # oh_inventory.ID


def executor_activity(value: str | None) -> dict[str, int] | None:
    """Only bounded, sanitized executor counts can establish test quiescence.

    The status may also name the quarantined actor keys (sorted, distinct);
    they are checked and dropped, since only the counts decide quiescence.
    """
    try:
        row = json.loads(value) if isinstance(value, str) and len(value) <= ACTIVITY_BYTES else None
    except ValueError:
        return None
    fields = ("pending", "active", "quarantined", "eventsQueued")
    if (not isinstance(row, dict) or not set(fields) <= set(row) <= {*fields, "quarantinedKeys"}
            or any(type(row[name]) is not int or not 0 <= row[name] <= 1048576 for name in fields)):
        return None
    keys = row.get("quarantinedKeys", [])
    if (not isinstance(keys, list) or len(keys) > row["quarantined"]
            or not all(isinstance(key, str) and ACTOR_KEY.fullmatch(key) for key in keys)
            or keys != sorted(set(keys))):
        return None
    return {name: row[name] for name in fields}


def signal_problems(expected: set[str], states: dict[str, str], since: float) -> dict[str, tuple[str, str]]:
    """Native transform evidence, shared with the controls checker.

    Check the snapshot envelope and this run's acquisition time, rather than
    duplicating any provider's field parser. Never print an invalid payload.
    """
    wrong = {}
    for name in sorted(expected):
        value = states.get(name)
        reason = "missing or undefined"
        try:
            data = json.loads(value) if isinstance(value, str) and len(value) <= 65536 else None
        except ValueError:
            data = None
            reason = "not transformed JSON"
        if isinstance(data, dict):
            observed = data.get("observedAt")
            acquired = (isinstance(observed, (int, float)) and not isinstance(observed, bool)
                        and since <= observed <= time.time() * 1000 + 5000)
            if (type(data.get("version")) is int and data["version"] == 1 and acquired
                    and isinstance(data.get("values"), dict) and data["values"]):
                continue
            reason = "invalid envelope or acquisition time"
        wrong[name] = ("a transformed signal snapshot acquired in this run", reason)
    return wrong


def simulated_lifecycle(contract: dict, resources: set[str], *, endpoints: bool = True) -> dict:
    """Project ownership onto this check's actual, simulated device actors.

    Plain stage/source stand-ins have no terminal executor and cannot prove
    lifecycle readiness or authorize source standby. Matrix I/O remains real
    actor I/O, pointed at loopback by the existing provider aim() methods.
    Controls-only checks retain the manual-command arbiter without route Items.
    A subsequent configure() gives controller and actors one matching epoch.
    """
    result = copy.deepcopy(contract)
    if "lifecycle" not in result:
        return result
    descriptor = result["lifecycle"]
    descriptor.pop("io", None)
    descriptor["resources"] = {name: row for name, row in descriptor["resources"].items() if name in resources}
    selected = set(descriptor["resources"])
    for row in descriptor["resources"].values():
        row["requires"] = {value: {name: target for name, target in dependencies.items() if name in selected}
                           for value, dependencies in row.get("requires", {}).items()}
    for row in descriptor["sources"].values():
        row.update(power=None, idle="preserve", capable=False)
    if endpoints:
        for endpoint in descriptor["endpoints"].values():
            for choice in endpoint["choices"].values():
                for field in ("requirements", "release", "deactivate"):
                    choice[field] = {name: value for name, value in choice.get(field, {}).items() if name in selected}
                if "leases" in choice:
                    choice["leases"] = {name: value for name, value in choice["leases"].items() if name in selected}
                choice["preserve"] = [name for name in choice.get("preserve", []) if name in selected]
    else:
        descriptor["endpoints"] = {}
        result["rooms"] = {}
        result["edid"] = None
    if "consumers" in descriptor:
        descriptor["consumers"] = [
            {**row, "requirements": {name: value for name, value in row["requirements"].items() if name in selected}}
            for row in descriptor["consumers"] if any(name in selected for name in row["requirements"])
        ]
    return result


class Check:
    def __init__(self, house: oh_conf.House, log=print) -> None:
        self.house = house
        self.log = log
        self.routing = house.data["routing"]
        self.graph = hops.compile_house(house)
        full = routing.table(house)
        self.executor_enabled = "lifecycle" in full
        self.table = full["rooms"]
        self.edid = full["edid"]
        self.devices = house.by_id
        self.hdmi_matrix: str | None = self.routing.get("hdmi_matrix")
        matrix_ids: list[str] = [self.hdmi_matrix] if self.hdmi_matrix is not None else []
        #: HDMI switch id (an HDMI switch, or a downstream matrix a room's
        #: switch is an output of) -> a room it is in front of
        self.switches: dict[str, str] = {}
        self.modules = {}
        for room, paths in self.graph.rooms.items():
            off_path = paths[routing.OFF]
            for uhop in off_path.usb_hops():
                if uhop.device not in matrix_ids:
                    matrix_ids.append(uhop.device)
            for swhop in off_path.switch_hops():
                self.switches.setdefault(swhop.device, room)
        for ident in dict.fromkeys(matrix_ids):
            module = routing.switcher(self.devices[ident])
            if module is not None:
                self.modules[ident] = module
        for entry in self.routing["rooms"].values():
            for sw in routing.room_switches(entry):
                module = routing.switch_module(self.devices, sw)
                if module is not None:
                    self.modules[sw["device"]] = module
        self.signal_since = time.time() * 1000
        self.signal_items = set()
        for ident, module in self.modules.items():
            if module.diagnostics:
                self.signal_items.update((module.diagnostics(self.devices[ident]) or {}).get("items", []))
        self.sims = {i: sim.SIMS[m.NAME]() for i, m in self.modules.items()}
        self.servers = {i: sim.serve(s) for i, s in self.sims.items()}
        #: port Item -> (matrix or switch id, port); an HDMI switch's port is
        #: its output 0, the one its room's display is on, a matrix's the
        #: output the room's display is on
        self.ports: dict[str, tuple[str, int]] = {}
        for ident, module in self.modules.items():
            if ident in self.switches:
                continue
            for port in range(1, module.ports(self.devices[ident])[1] + 1):
                self.ports[module.switch_item(self.devices[ident], port)] = (ident, port)
        for paths in self.graph.rooms.values():
            for swhop in paths[routing.OFF].switch_hops():
                if swhop.item:
                    self.ports[swhop.item] = (swhop.device, swhop.output if swhop.output is not None else 0)
        #: scaler Item -> (HDMI matrix id, output) of each output whose
        #: scaler the routing sets (hdmi_audio_scaler)
        self.scalers: dict[str, tuple[str, int]] = {
            mhop.scaler_item: (mhop.device, mhop.output)
            for paths in self.graph.rooms.values()
            for mhop in paths[routing.OFF].matrix_hops()
            if mhop.scaler_item
        }
        #: a stage's input Item -> the inputs it takes (receivers, soundbars, displays)
        self.inputs: dict[str, list[str]] = {
            stage.input_item: list(stage.inputs_allowed)
            for paths in self.graph.rooms.values()
            for stage in paths[routing.OFF].stage_hops()
            if not stage.passive and stage.input_item
        }
        #: stand-in Item -> its type
        self.stand_in_items: dict[str, str] = {}
        for room in self.table.values():
            for stage in room["stages"]:
                self.stand_in_items.update({stage["power"]: "Switch", stage["input"]: "String"})
            for steps in room["power"].values():
                for item, _ in steps:
                    self.stand_in_items.setdefault(item, "Switch")
            for steps in room["after"].values():
                for item, _ in steps:
                    self.stand_in_items.setdefault(item, "String")
        self.remote_files: dict[str, bytes] = {}
        #: each room's last choice, as routing.js remembers it for zone sync
        self.chosen: dict[str, str] = {}
        self.failures: list[str] = []
        self.http: lib.Http | None = None
        #: the instance's events.log, once it boots
        self.events: Path | None = None

    # ------------------------------------------------------------ conf

    def conf(self) -> dict[str, str | bytes]:
        """The generated routing files and the remote page, with the matrices
        at their simulators."""
        house = self.house
        built = render.build(house)
        contract = simulated_lifecycle(routing.table(house), set(self.ports) | set(self.scalers))
        if "lifecycle" in contract:
            from oh_features.routing import lifecycle_io
            # Regenerate custom matrix actor adapters with the projected epoch;
            # never leave an original full-house adapter beside the projection.
            for module in {m.NAME: m for m in self.modules.values()}.values():
                if module.files:
                    devices = [self.devices[ident] for ident, owner in self.modules.items() if owner is module]
                    built.files.update(module.files(devices, house))
            built.yaml[lifecycle_io.ITEMS].items.clear()
            lifecycle_io.configure(house, built, contract["lifecycle"])
            built.files[routing.RULE] = routing.rule(house, contract)
        files = built.render()
        # threadpools: production's thread pools, as the controls check runs with.
        keep = {"services/addons.cfg", "yaml/model.yaml", routing.ITEMS, routing.RULE, all_off.ITEMS, all_off.RULE,
                threadpools.FILE, *(m.FILE for m in self.modules.values()), *(m.RULE for m in self.modules.values() if self.ruled(m)),
                *(f"{remote.TARGET}/{name}" for name in remote.FILES)}
        for module in {m.NAME: m for m in self.modules.values()}.values():
            keep |= set(module.ASSETS)
            if module.files:
                devices = [device for device in house.devices if oh_conf.uses(device, module.PHASE, module.NAME)]
                keep |= set(module.files(devices, house))
        if "lifecycle" in contract:
            keep |= {lifecycle_io.ITEMS, lifecycle_io.MODULE,
                     "automation/js/node_modules/thermite-routing-lifecycle/index.js"}
            keep |= {f"automation/js/thermite_routing_io_{key}.js" for key in contract["lifecycle"]["io"]["actors"]}
        out = {path: text for path, text in files.items() if path in keep}
        for ident, module in self.modules.items():
            if self.ruled(module):
                out = module.aim(out, self.devices[ident], self.servers[ident].address)
                continue
            real = module.url(self.devices[ident]["ip"])
            if out[module.FILE].count(real) != 1:
                raise lib.SandboxError(f"{module.FILE}: expected {real} once")
            out[module.FILE] = out[module.FILE].replace(real, module.url(self.servers[ident].address))
        out[STAND_INS] = self.stand_ins()
        leaks = sorted({f"{path}: {device['id']} ({device['ip']})" for path, text in out.items()
                        if isinstance(text, str) for device in house.devices
                        if device.get("ip") and re.search(rf"(?<![\d.]){re.escape(device['ip'])}(?![\d.])", text)})
        if leaks:
            raise lib.SandboxError("the conf would reach a real device: " + "; ".join(leaks))
        self.remote_files = {name: value if isinstance(value, bytes) else value.encode("utf-8")
                             for name in remote.FILES for value in [out[f"{remote.TARGET}/{name}"]]}
        return out

    def stand_ins(self) -> str:
        """Plain Items, autoupdate on, for the power and input of every
        room's receiver, display and source, and for every action Item: each takes
        what it is sent, as a device that obeys would report it."""
        items = {name: {"type": kind, "label": f"{name} (simulated)"} for name, kind in self.stand_in_items.items()}
        header = ["Generated by bin/thermite test routing: the rooms' receivers, displays,",
                  "source power and actions, simulated (lib/oh_testing/routing.py)."]
        return oh_conf.dump(header, {"items": items})

    def device_commands(self) -> list[tuple[str, str]]:
        """(Item, command) of each command a stand-in got, from events.log."""
        if self.events is None or not self.events.exists():
            raise lib.SandboxError(f"no events.log to read the receivers' and displays' commands from: {self.events}")
        out = []
        for line in self.events.read_text(encoding="utf-8", errors="replace").splitlines():
            found = COMMAND.search(line)
            if found and found[1] in self.stand_in_items:
                out.append((found[1], found[2]))
        return out

    def seed(self) -> None:
        """Every room off, but the first matrix-fed room on its first source,
        whose input has that room's EDID and every other source's the idle one;
        HDMI switches on their upstream feed; USB parked."""
        hdmi_id = self.routing.get("hdmi_matrix")
        for paths in self.graph.rooms.values():
            off_path = paths[routing.OFF]
            for mhop in off_path.matrix_hops():
                self.sims[mhop.device].set(mhop.output, mhop.park_value)
            for swhop in off_path.switch_hops():
                if swhop.park_value is not None:
                    self.sims[swhop.device].set(swhop.output if swhop.output is not None else 0, swhop.park_value)
        room = next((r for r in self.table if self.table[r]["hdmi"] and self.matrix_choices(r)), None)
        if hdmi_id is not None and room is not None:
            hdmi = self.sims[hdmi_id]
            first_choice = self.matrix_choices(room)[0]
            source = self.number(first_choice)
            for mhop in self.graph.path(room, first_choice).matrix_hops():
                self.sims[mhop.device].set(mhop.output, mhop.input_value)
            if self.edid is not None and source is not None:
                for number in self.edid["inputs"]:
                    if self.edid["idle"] is not None:
                        hdmi.set_edid(int(number), self.edid["idle"])
                hdmi.set_edid(source, self.code(room, source))
        if "usb_off_host" in self.routing:
            for ident, matrix in self.sims.items():
                if ident != hdmi_id and ident not in self.switches:
                    for port in range(1, self.modules[ident].ports(self.devices[ident])[1] + 1):
                        matrix.set(port, self.routing["usb_off_host"])

    # ------------------------------------------------------------ runtime

    def states(self) -> dict[str, str]:
        _, rows = self.http.try_get_json("/rest/items?recursive=false&fields=name,state")
        return {row["name"]: row.get("state") for row in rows or []}

    def things(self) -> dict[str, str]:
        _, rows = self.http.try_get_json("/rest/things?summary=true")
        return {row["UID"]: row.get("statusInfo", {}).get("status") for row in rows or []}

    def command(self, item: str, value: str) -> None:
        self.http.request("POST", f"/rest/items/{urllib.parse.quote(item)}", value.encode(),
                          {"Content-Type": "text/plain"})

    def wait(self, what: str, expected: dict[str, object], actual, timeout: float = STEP) -> bool:
        """Wait until actual() agrees with expected; record a failure if it never does."""
        def wrong():
            seen = actual()
            return {k: (v, seen.get(k)) for k, v in expected.items() if seen.get(k) != v}
        return self.wait_for(what, wrong, timeout)

    def wait_for(self, what: str, problems, timeout: float = STEP) -> bool:
        """Wait until problems() ({key: (want, got)}) is empty; record a failure if it never is."""
        end = time.monotonic() + timeout
        while True:
            wrong = problems()
            if not wrong:
                return True
            if time.monotonic() >= end:
                detail = ", ".join(f"{k} {got!r} (want {want!r})" for k, (want, got) in sorted(wrong.items()))
                self.failures.append(f"{what}: {detail}")
                self.log(f"FAIL    {what}: {detail}")
                return False
            time.sleep(0.05)

    def matrices(self) -> dict[str, int]:
        """What each port Item, and each routed scaler Item, should read."""
        out = {item: self.sims[ident].get(port) for item, (ident, port) in self.ports.items()}
        out.update({item: self.sims[ident].scaler(port) for item, (ident, port) in self.scalers.items()})
        return out

    def executor_idle(self, what: str, timeout: float = STEP) -> bool:
        """Let queued writes retire before a passive fixture change/baseline.

        This is test-only REST observation at the existing wait cadence. It
        proves neither route readiness nor the absence of external controllers.
        A short stable window also lets already-posted native Item commands
        reach their actor before a simulator is muted or moved externally.
        """
        if not getattr(self, "executor_enabled", False):
            return True
        from oh_features.routing.lifecycle_io import STATUS_ITEM
        quiet_since = None

        def wrong():
            nonlocal quiet_since
            activity = executor_activity(self.states().get(STATUS_ITEM))
            if activity is None:
                quiet_since = None
                return {STATUS_ITEM: ("idle activity counts", "missing or invalid snapshot")}
            busy = {name: (0, count) for name, count in activity.items() if count}
            if busy:
                quiet_since = None
                return busy
            now = time.monotonic()
            if quiet_since is None:
                quiet_since = now
            return {} if now - quiet_since >= EXECUTOR_QUIET else {STATUS_ITEM: ("stable idle", "settling")}

        return self.wait_for(f"{what}: executor idle", wrong, timeout)

    def sources(self, rooms: dict[str, str]) -> bool:
        return self.wait("Source", {self.table[r]["item"]: v for r, v in rooms.items()}, self.states)

    def update(self, item: str, value: str) -> None:
        """Change an Item's state as its device reporting it would."""
        self.http.request("PUT", f"/rest/items/{urllib.parse.quote(item)}/state", value.encode(),
                          {"Content-Type": "text/plain"})

    def synced(self, room: str, choice: str) -> dict | None:
        """The synced route `choice` takes in a follower room while its
        leader's last choice is the same source (routing.js syncs()), else
        None. Rooms chosen together end synced: the follower syncs once the
        leader's output reads back."""
        sync = self.table[room].get("sync")
        if not sync or choice not in sync["choices"] or self.chosen.get(sync["leader"]) != choice:
            return None
        return sync["choices"][choice]

    def expect_routed(self, what: str, choices: dict[str, str]) -> bool:
        self.chosen.update(choices)
        want = {item: value for room, choice in choices.items() for item, value in self.table[room]["choices"][choice]}
        routed = self.wait(f"{what}: matrix ports", want, self.matrices)
        synced = {room: self.synced(room, choice) for room, choice in choices.items()}
        sent = {item: str(value) for room, choice in choices.items() for key in ("power", "inputs", "after")
                for item, value in (synced[room]["inputs"] if key == "inputs" and synced[room]
                                    else self.table[room][key][choice])}
        devices = self.wait(f"{what}: receivers, displays and actions", sent, self.states)
        return self.sources(choices) and routed and devices and self.expect_edids(what)

    def edid_problems(self) -> dict[str, tuple]:
        """Source inputs whose EDID is neither the code of a room showing
        them nor, when no room does, the idle code."""
        hdmi_id = self.routing.get("hdmi_matrix")
        if self.edid is None or hdmi_id is None:
            return {}
        hdmi = self.sims[hdmi_id]
        codes: dict[str, set[int]] = {}
        overrides: dict[str, set[int]] = {}
        for room, raw in self.table.items():
            output = raw["hdmi"]
            if output is None:
                continue
            ident, port = self.ports[output]
            number = str(self.sims[ident].get(port))
            choice = self.chosen.get(room)
            if choice and self.synced(room, choice):
                continue
            codes.setdefault(number, set()).add(self.edid["outputs"][output])
            preset = self.edid.get("overrides", {}).get(number, {}).get(output)
            if preset is not None:
                overrides.setdefault(number, set()).add(preset)
        wrong = {}
        for number, item in self.edid["inputs"].items():
            want = overrides.get(number) or codes.get(number) or (
                {self.edid["idle"]} if self.edid["idle"] is not None else None)
            got = hdmi.edid(int(number))
            if want is not None and got not in want:
                wrong[item] = (sorted(want), got)
        return wrong

    def expect_edids(self, what: str) -> bool:
        return self.edid is None or self.wait_for(f"{what}: EDIDs", self.edid_problems)

    # ------------------------------------------------------------ steps

    def readback_choice(self, room: str, silent: frozenset = frozenset(), states: dict | None = None) -> str:
        """The Source a room should read back, derived from the canonical hop
        readback evaluator with the matrices and switches in `silent` muted."""
        raw = self.table[room]
        if raw["stages"] and states is None:
            states = self.states()

        def reported(item: str) -> str | None:
            if item in self.ports:
                ident, port = self.ports[item]
                if ident in silent:
                    return None
                value = self.sims[ident].get(port)
                return None if value is None else str(value)
            value = (states or {}).get(item)
            return None if value in (None, "NULL", "UNDEF") else str(value)

        return hops.evaluate_readback(raw, reported)

    def uid(self, ident: str) -> str:
        return self.modules[ident].thing_uid(self.devices[ident])

    @staticmethod
    def ruled(module) -> bool:
        """Whether a rule of the integration's own talks to the matrix, not a Thing."""
        return module.aim is not None

    def switch_inputs(self, ident: str):
        """The input numbers of an HDMI switch, or of a matrix one is an output of."""
        module = self.modules[ident]
        return range(1, module.ports(self.devices[ident])[0] + 1) if module.ports else module.INPUTS

    def answering(self, idents, answering: bool = True, timeout: float = 60) -> bool:
        """Wait until each matrix or switch answers, or does not: a Thing is
        ONLINE or OFFLINE; a matrix its rule reads has its port Items show
        the stand-in's ports, or UNDEF."""
        things = {self.uid(i): "ONLINE" if answering else "OFFLINE" for i in idents
                  if not self.ruled(self.modules[i])}
        ports = {item: str(self.sims[i].get(port)) if answering else "UNDEF" for item, (i, port) in self.ports.items()
                 if i in idents and self.ruled(self.modules[i])}
        what = "answering" if answering else "silent"
        return ((not things or self.wait(f"{what} matrix and switch Things", things, self.things, timeout)) and
                (not ports or self.wait(f"{what} matrices their rule reads", ports, self.states, timeout)))

    def step_online(self) -> None:
        self.answering(list(self.modules))

    def readbacks(self, silent: frozenset = frozenset()) -> dict[str, str]:
        states = self.states()
        return {room: self.readback_choice(room, silent, states) for room in self.table}

    def step_readback(self) -> None:
        self.sources(self.readbacks())
        self.expect_edids("initial read-back")
        for ident in self.sims:
            if self._writes(ident):
                self.failures.append(f"runtime wrote to {ident} while it started: "
                                     f"{[c for c in self.sims[ident].comheads() if not c.startswith('get')]}")

    def step_diagnostics(self) -> None:
        if self.signal_items:
            self.wait_for("read-only signal snapshots", lambda: signal_problems(
                self.signal_items, self.states(), self.signal_since), timeout=SIGNAL_WAIT)

    def step_rounds(self) -> int:
        lists = {room: list(entry["choices"]) for room, entry in self.table.items()}
        rounds = max(map(len, lists.values()))
        for index in range(1, rounds + 1):
            choices = {room: names[index % len(names)] for room, names in lists.items()}
            for room, choice in choices.items():
                self.command(self.table[room]["item"], choice)
            self.expect_routed(f"round {index}", choices)
        return rounds

    def step_refused_choice(self) -> None:
        if not self.executor_idle("before an unoffered choice"):
            return
        room = next(iter(self.table))
        writes = {i: self._writes(i) for i in self.sims}
        self.command(self.table[room]["item"], "no_such_source")
        time.sleep(3)
        for ident in self.sims:
            if self._writes(ident) != writes[ident]:
                self.failures.append(f"an unoffered choice in {room} reached {ident}")

    def _writes(self, ident: str) -> int:
        module = self.modules[ident]
        reads = {c for c in module.COMHEADS if c.startswith("get")} | set(module.SETUP_COMHEADS)
        return sum(1 for c in self.sims[ident].comheads() if c not in reads)

    def expect_edid(self, what: str, number: int, code: int | None) -> bool:
        """Wait until the source on input `number` presents exactly `code`."""
        hdmi_id = self.routing.get("hdmi_matrix")
        if self.edid is None or code is None or hdmi_id is None:
            return True
        hdmi = self.sims[hdmi_id]
        item = self.edid["inputs"][str(number)]

        def wrong():
            got = hdmi.edid(number)
            return {} if got == code else {item: (code, got)}
        return self.wait_for(f"{what}: EDID", wrong)

    def code(self, room: str, number: int) -> int | None:
        if self.edid is None:
            return None
        output = self.table[room]["hdmi"]
        return self.edid.get("overrides", {}).get(str(number), {}).get(output, self.edid["outputs"][output])

    def number(self, source: str) -> int | None:
        """The source's matrix input (None for one cabled to a room alone)."""
        return self.routing["sources"][source].get("hdmi_input")

    def matrix_choices(self, room: str) -> list[str]:
        """The room's sources that the HDMI matrix routes to it."""
        local = hops.local_sources(self.routing["rooms"][room])
        return [s for s in self.table[room]["choices"] if s != routing.OFF and s not in local
                and self.number(s) is not None]

    def show_feed(self, room: str) -> None:
        """The room's receiver, soundbar, and displays on, and on the signal
        before them, as if switched there: the Source then follows the matrix."""
        for stage in self.table[room]["stages"]:
            if stage["feed"] is not None:
                self.update(stage["power"], "ON")
                self.update(stage["input"], stage["feed"])

    def turn_off(self, rooms) -> None:
        for room in rooms:
            self.command(self.table[room]["item"], routing.OFF)
        self.expect_routed("rooms off", {room: routing.OFF for room in rooms})

    def step_outside(self) -> None:
        """Switches made at the matrix or at an HDMI switch are shown and
        answered with no write."""
        hdmi_id = self.routing.get("hdmi_matrix")
        matrix_rooms = [r for r in self.table if self.table[r]["hdmi"] and self.matrix_choices(r)]
        room = (
            max(matrix_rooms, key=lambda r: (bool(hops._switches_of(self.table[r])), len(self.matrix_choices(r))))
            if hdmi_id is not None and matrix_rooms
            else None
        )
        self.turn_off([r for r in self.table if r != room])
        if not self.executor_idle("before external matrix switches"):
            return
        writes = {ident: self._writes(ident) for ident in self.sims}
        if room is not None and hdmi_id is not None:
            hdmi = self.sims[hdmi_id]
            self.show_feed(room)
            for sw in hops._switches_of(self.table[room]):
                if sw["matrix"] is not None:
                    ident, port = self.ports[sw["item"]]
                    self.sims[ident].set(port, int(sw["matrix"]))
            output = self.routing["rooms"][room]["hdmi_output"]
            source = next(s for s in self.matrix_choices(room) if self.number(s) != hdmi.get(output))
            hdmi.set(output, self.number(source))
            if "hdmi_audio_output" in self.routing["rooms"][room]:
                hdmi.set(self.routing["rooms"][room]["hdmi_audio_output"], self.number(source))
            self.sources({room: source})
            offered = {int(k) for k in self.table[room]["readback"]}
            inputs = self.modules[hdmi_id].ports(self.devices[hdmi_id])[0]
            stranger = next((i for i in range(1, inputs + 1) if i not in offered), None)
            if stranger is not None:
                hdmi.set(output, stranger)
                self.sources({room: "UNDEF"})
        switched = [r for r in self.table if hops._switches_of(self.table[r])]
        ordered = ([room] if room in switched else []) + [r for r in switched if r != room]
        for other in ordered:
            if other != room:
                self.show_feed(other)
            self.switched_outside(other)
        time.sleep(3)
        for ident in self.sims:
            if self._writes(ident) != writes[ident]:
                self.failures.append(f"a switch made at the matrix or an HDMI switch made runtime write to {ident}")

    def switched_outside(self, room: str) -> None:
        """Each input of each of the room's HDMI switches, set at the switch,
        shows as its source, and one the room does not offer as UNDEF; then
        each switch goes back to its upstream feed, or where it was."""
        switches = hops._switches_of(self.table[room])
        for index, switch in enumerate(switches):
            for downstream in switches[:index]:
                if downstream["matrix"] is not None:
                    d_ident, d_port = self.ports[downstream["item"]]
                    self.sims[d_ident].set(d_port, int(downstream["matrix"]))
            ident, port = self.ports[switch["item"]]
            before = self.sims[ident].get(port)
            for number, local in switch["readback"].items():
                self.sims[ident].set(port, int(number))
                self.sources({room: local})
            known = {*map(int, switch["readback"]), *([int(switch["matrix"])] if switch["matrix"] is not None else [])}
            other = next((n for n in self.switch_inputs(ident) if n not in known), None)
            if other is not None:
                self.sims[ident].set(port, other)
                self.sources({room: "UNDEF"})
            self.sims[ident].set(port, int(switch["matrix"]) if switch["matrix"] is not None else before)
            self.sources({room: self.readback_choice(room)})

    def step_devices(self) -> None:
        """Switches made at a room's receiver, soundbar, or display (its front
        panel or its own remote) are shown and answered with no write, and so
        is one turned off there."""
        staged = [room for room, entry in self.table.items() if entry["stages"]]
        self.turn_off(staged)
        if not self.executor_idle("before external device switches"):
            return
        for room in staged:
            entry = self.table[room]
            # A source on the matrix, from Off, moves its ports: the choice is
            # read back only once the matrices have taken all its writes.
            choice = (self.matrix_choices(room) or [c for c in entry["choices"] if c != routing.OFF])[0]
            self.away(room, choice)
            self.command(entry["item"], choice)
            self.expect_routed(f"{entry['label']} before a switch at its devices", {room: choice})
            if not self.executor_idle(f"before {entry['label']} front-panel switches"):
                return
            writes = {ident: self._writes(ident) for ident in self.sims}
            sent = dict(entry["inputs"][choice])
            commands = self.device_commands()
            sync = entry.get("sync")
            for stage in entry["stages"]:
                for value, source in stage["readback"].items():
                    self.external_source(room, stage["input"], value, source)
                known = {*stage["readback"], stage["feed"]}
                if sync and stage["input"] == sync["input"]:
                    # A follower's zone switched to its leader's input.
                    for value in dict.fromkeys(route["value"] for route in sync["choices"].values()):
                        self.update(stage["input"], value)
                        self.sources({room: self.readback_choice(room)})
                        known.add(value)
                other = next((v for v in self.inputs[stage["input"]] if v not in known), None)
                if other is not None:
                    self.update(stage["input"], other)
                    self.sources({room: "UNDEF"})
                for item, value in sent.items():
                    self.update(item, str(value))
                self.update(stage["power"], "OFF")
                self.sources({room: self.readback_choice(room)})
                self.update(stage["power"], "ON")
                self.sources({room: self.readback_choice(room)})
            if len(entry["stages"]) > 1:
                for stage in entry["stages"]:
                    self.update(stage["power"], "OFF")
                self.sources({room: routing.OFF})
                for stage in entry["stages"]:
                    self.update(stage["power"], "ON")
                self.sources({room: self.readback_choice(room)})
            time.sleep(3)
            later = self.device_commands()
            if later != commands:
                self.failures.append(f"a switch made at {entry['label']}'s receiver or display made runtime send "
                                     f"{later[len(commands):]}")
            for ident in self.sims:
                if self._writes(ident) != writes[ident]:
                    self.failures.append(f"a switch made at {entry['label']}'s receiver or display made runtime "
                                         f"write to {ident}")

    def external_source(self, room: str, item: str, value: str, source: str) -> None:
        """A local receiver source can require a different input on its display.

        Check the partial physical path first, then simulate the other device
        reports needed for that source. These are readbacks, never commands.
        """
        self.update(item, value)
        self.sources({room: self.readback_choice(room)})
        for other, expected in self.table[room]["inputs"][source]:
            if other != item:
                self.update(other, str(expected))
        self.sources({room: source})

    def away(self, room: str, choice: str) -> None:
        """Off leaves a switch the master matrix does not feed as it is, so
        it may already show `choice`, and the choice would read back before
        its write lands: put it on another input first, as if switched
        there while the room is off."""
        planned = dict(self.table[room]["choices"][choice])
        for switch in hops._switches_of(self.table[room]):
            if switch["matrix"] is not None:
                continue
            wanted = planned.get(switch["item"])
            ident, port = self.ports[switch["item"]]
            if wanted is not None and self.sims[ident].get(port) == int(wanted):
                self.sims[ident].set(port, next(n for n in self.switch_inputs(ident) if n != int(wanted)))

    def step_hand_over(self) -> None:
        """Two rooms on one source: the last to choose it wins; when it
        turns off, the source gets the first room's EDID back, and once
        neither shows it, the idle EDID. A second room whose zone follows
        the first's takes the first's zone input (expect_routed) and claims
        no EDID; a first room that follows the second gets its own back as
        its sync ends."""
        pairs = ((a, b, s) for a in self.table for b in self.table if a != b
                 for s in self.table[a]["choices"]
                 if s != routing.OFF and s in self.table[b]["choices"] and self.number(s) is not None)
        found = next(pairs, None)
        if self.edid is None or found is None:
            return
        first, second, source = found
        self.turn_off(list(self.table))
        number = self.number(source)
        sync = self.table[second].get("sync")
        follows = bool(sync) and sync["leader"] == first and source in sync["choices"]
        first_code = self.code(first, number)
        follower = self.table[first].get("sync")
        first_syncs = follower and follower["leader"] == second and source in follower["choices"]
        active_overrides = self.edid.get("overrides", {}).get(str(number), {})
        shared = self.code(second, number)
        if follows or (not first_syncs and self.table[first]["hdmi"] in active_overrides):
            shared = first_code
        for room, choice, code, what in (
                (first, source, first_code, "the first room to choose it"),
                (second, source, shared, "the EDID while both rooms show it"),
                (second, routing.OFF, first_code, "handed back when that room turns off"),
                (first, routing.OFF, self.edid["idle"], "the idle EDID once no room shows it")):
            self.command(self.table[room]["item"], choice)
            self.expect_routed(what, {room: choice})
            self.expect_edid(what, number, code)

    def step_silent(self) -> None:
        """The HDMI matrix (if any), then each HDMI switch, stops answering and comes back."""
        hdmi_id = self.routing.get("hdmi_matrix")
        targets = ([hdmi_id] if hdmi_id is not None else []) + list(self.switches)
        for ident in targets:
            if not self.executor_idle(f"before {ident} stops answering"):
                return
            writes = {i: self._writes(i) for i in self.sims}
            self.sims[ident].muted = True
            self.answering([ident], False, STEP)
            self.sources(self.readbacks(frozenset({ident})))
            self.sims[ident].muted = False
            self.answering([ident])
            self.sources(self.readbacks())
            time.sleep(3)
            for other in self.sims:
                if self._writes(other) != writes[other]:
                    self.failures.append(f"runtime wrote to {other} when {ident} stopped answering or came back")

    def step_remote(self) -> None:
        """The remote page as a phone uses it: its files from runtime's web
        server, then, with no login, a room's Source read, a choice sent and
        its read-back heard on the event stream (web/remote/backend.js)."""
        anonymous = lib.Http(self.http.base, timeout=STEP, allow_write=True)
        for name in remote.FILES:
            _, body = anonymous.request("GET", remote.ASSET_URL + name)
            if body != self.remote_files[name]:
                self.failures.append(f"{remote.ASSET_URL}{name} differs from the generated test conf")
        room = next(iter(self.table))
        item = self.table[room]["item"]
        _, shown = anonymous.request("GET", f"/rest/items/{item}/state", headers={"Accept": "text/plain"})
        choice = next(c for c in self.table[room]["choices"] if c != shown.decode("utf-8").strip())
        opened, heard = threading.Event(), {}
        listener = threading.Thread(target=self._listen, daemon=True,
                                    args=(f"openhab/items/{item}/stateupdated", choice, opened, heard))
        listener.start()
        opened.wait(3)
        anonymous.request("POST", f"/rest/items/{item}", choice.encode(), {"Content-Type": "text/plain"})
        self.expect_routed("a choice from the remote", {room: choice})
        listener.join(STEP)
        if not heard.get("value"):
            self.failures.append(f"the event stream never showed {item} = {choice}: {heard.get('error', 'no event')}")

    def step_all_off(self) -> None:
        """Every room on a source (round 1's), then All off with no login."""
        choices = {room: list(entry["choices"])[1 % len(entry["choices"])] for room, entry in self.table.items()}
        for room, choice in choices.items():
            self.command(self.table[room]["item"], choice)
        self.expect_routed("every room on before All off", choices)
        anonymous = lib.Http(self.http.base, timeout=STEP, allow_write=True)
        anonymous.request("POST", f"/rest/items/{all_off.ITEM}", b"ON", {"Content-Type": "text/plain"})
        self.expect_routed("All off", {room: routing.OFF for room in self.table})

    def _listen(self, topic: str, value: str, opened: threading.Event, heard: dict) -> None:
        """Read the remote's event stream until `topic` carries `value`."""
        oh_trace.http("GET", self.http.base + REMOTE_EVENTS)
        try:
            with urllib.request.urlopen(self.http.base + REMOTE_EVENTS, timeout=STEP) as stream:
                opened.set()
                for raw in stream:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    if event.get("topic") == topic and json.loads(event["payload"]).get("value") == value:
                        heard["value"] = value
                        return
        except (OSError, ValueError, KeyError) as exc:
            heard["error"] = str(exc)
        finally:
            opened.set()

    def step_audit(self) -> None:
        for ident, matrix in self.sims.items():
            allowed = self.modules[ident].COMHEADS
            heard = sorted(set(matrix.comheads()) - allowed)
            if heard:
                self.failures.append(f"{ident} heard {', '.join(heard)}")
            # A request the stand-in is answering at this moment has no reply yet.
            refused = [r for r, a in zip(matrix.requests, matrix.replies, strict=False) if a and a.get("result") == 0]
            if refused:
                self.failures.append(f"{ident} refused {len(refused)} request(s), first {refused[0]}")
            if matrix.unknown:
                self.failures.append(f"{ident} got unknown comheads {sorted(set(matrix.unknown))}")
            # Setup (SETUP_COMHEADS) is no port write, so _writes() does not
            # bound it: a connection asks it once, and a set that changes
            # nothing (getattr: a stand-in that records it) is a rule
            # re-sending what the device already holds.
            for comhead in matrix.repeated(set(self.modules[ident].SETUP_COMHEADS)):
                self.failures.append(f"{ident} heard {comhead} more than once on one connection")
            redundant = list(getattr(matrix, "redundant", ()))
            if redundant:
                self.failures.append(f"{ident} was sent {redundant[0]}, which changed nothing, {len(redundant)} time(s)")

    def step_log(self, inst: lib.Instance) -> None:
        text = inst.openhab_log.read_text(encoding="utf-8", errors="replace") if inst.openhab_log.exists() else ""
        problems = lib.log_problems(lib.parse_log(text))
        for record in problems["model"] + problems["errors"]:
            self.failures.append(f"openhab.log:{record.lineno} [{record.level}] [{record.logger}] "
                                 f"{record.message.splitlines()[0] if record.message else ''}")


def run_check(args, rt: lib.Runtime, log) -> int:
    house = oh_conf.House.load(args.house)
    if house.data.get("routing") is None:
        raise lib.SandboxError("instance/devices.yaml has no routing")
    check = Check(house, log=log)
    conf = STATE / "conf"
    inst = lib.boot_check_instance(conf)
    started = time.monotonic()
    deadline = started + args.timeout
    try:
        # conf/ is rewritten only under the lock, so a run the lock refuses
        # leaves the holder's conf/ alone.
        with lib.InstanceLock(inst):
            if lib.running_pgid(inst):
                raise lib.SandboxError(f"a kept test instance is still running (pid file {inst.pid_file})")
            for server in check.servers.values():
                server.start()
            lib.wipe_dir(conf)
            render.write(conf, check.conf())
            check.seed()
            inst.console_log.unlink(missing_ok=True)
            booted = lib.boot(rt, inst, lib.read_requested_addons(conf), started, deadline, log=log)
            if booted.missing:
                raise lib.SandboxError("add-ons not installed: " + ", ".join(sorted(booted.missing)))
            check.http = lib.Http(inst.url, token=booted.token, timeout=30, allow_write=True)
            check.events = inst.openhab_log.parent / "events.log"
            try:
                steps = [
                    ("matrix and switch Things online", check.step_online),
                    ("initial read-back", check.step_readback),
                    ("read-only signal snapshots transformed by the runtime", check.step_diagnostics),
                    ("every choice in every room", check.step_rounds),
                    ("an unoffered choice", check.step_refused_choice),
                    ("a switch made at the matrix or an HDMI switch", check.step_outside),
                    ("a switch made at a receiver or display", check.step_devices),
                    ("two rooms on one source", check.step_hand_over),
                    ("a matrix or switch that stops answering", check.step_silent),
                    ("the remote page, with no login", check.step_remote),
                    ("All off", check.step_all_off),
                    ("comheads the matrices heard", check.step_audit),
                    ("openhab.log", lambda: check.step_log(inst)),
                ]
                for name, step in steps:
                    began, failed = time.monotonic(), len(check.failures)
                    step()
                    verdict = "ok  " if len(check.failures) == failed else "FAIL"
                    log(f"{verdict}    {name} ({time.monotonic() - began:.1f}s)")
            finally:
                lib.stop_instance(inst, timeout=STOP_TIMEOUT, log=log)
    finally:
        for server in check.servers.values():
            server.stop()
    requests = sum(len(s.requests) for s in check.sims.values())
    log(f"\n{len(check.table)} rooms, {len(check.sims)} simulated matrices and switches, {requests} requests, "
        f"{time.monotonic() - started:.0f}s")
    for failure in check.failures:
        log(f"  {failure}")
    log("RESULT: " + ("FAIL" if check.failures else "ok"))
    return 1 if check.failures else 0


def add_arguments(parser) -> None:
    oh_cli.add_timeout(parser, 300, "the boot")
    oh_cli.add_house(parser)


def run(args) -> int:
    try:
        rt = lib.fetch(lib.load_versions(), log=print)
        return run_check(args, rt, print)
    except lib.SandboxError as exc:
        lib.die(str(exc))
    return 2
