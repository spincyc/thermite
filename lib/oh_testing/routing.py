"""thermite test routing: the room Source selectors end to end, against
simulated matrices."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import oh_conf
import oh_inventory as inventory
import oh_matrix_sim as sim
import oh_cli
import oh_render as render
import oh_sandbox as lib
from oh_features import all_off, remote, routing

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
   routes the room's HDMI output, HDMI switch and USB device ports as the
   routing says, wakes the chosen source, powers its receiver and display
   (off for Off) and puts them on their inputs, sends its actions, has each
   source's input present the EDID of a room showing it (the idle EDID
   once none does), and a choice the room does not offer sends nothing;
3. a switch made at the matrix, at a room's HDMI switch (a keypad, the
   front panel, a web page) or at its receiver or display shows up in that
   room's Source, an input the room does not offer shows as UNDEF, a
   receiver or display turned off there shows as Off, and runtime writes
   nothing for any of them;
4. with two rooms on one source, the last to choose it wins, the first
   gets it back when that room turns off, and the idle EDID follows once
   neither shows it;
5. the rooms go UNDEF while the HDMI matrix, or their HDMI switch, does not
   answer, and recover without a write;
6. the remote page (web/remote) is served at /static/remote/ and, with no
   login, as a phone uses it, reads a room's Source, sends a choice and
   hears it read back on the event stream;
7. All off, sent as the page sends it, turns every room Off as a choice
   of Off would (all_off.js; devices outside the routing are omitted);
8. the simulated matrices heard only the comheads each Thing or rule may send (no
   fixed setting such as scaler or HDCP) and refused none;
9. openhab.log holds no ERROR and no model problem.

It uses the test runtime of thermite test boot (127.0.0.1:18081) and its
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
#: Seconds a step may take: a poll (2s), a command and its re-read.
STEP = 15
#: The event stream the remote page listens to (web/remote/backend.js).
REMOTE_EVENTS = "/rest/events?topics=openhab/items/loc_*_source/stateupdated"


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


class Check:
    def __init__(self, data: dict, log=print) -> None:
        self.data = data
        self.log = log
        self.routing = data["routing"]
        full = routing.table(data)
        self.table = full["rooms"]
        self.edid = full["edid"]
        self.devices = {d["id"]: d for d in data["devices"]}
        ids = [self.routing["hdmi_matrix"]]
        ids += [m for e in self.routing["rooms"].values() for m in e.get("usb") or {} if m not in ids]
        #: HDMI switch id (an HDMI switch, or a matrix a room's switch is an
        #: output of) -> the room it is in front of
        self.switches = {e["hdmi_switch"]["device"]: room for room, e in self.routing["rooms"].items()
                         if "hdmi_switch" in e}
        self.modules = {i: routing.switcher(self.devices[i]) for i in dict.fromkeys(ids)}
        self.modules.update({e["hdmi_switch"]["device"]: routing.switch_module(self.devices, e["hdmi_switch"])
                             for e in self.routing["rooms"].values() if "hdmi_switch" in e})
        self.signal_since = time.time() * 1000
        self.signal_items = set()
        for ident, module in self.modules.items():
            if hasattr(module, "diagnostics"):
                self.signal_items.update((module.diagnostics(self.devices[ident]) or {}).get("items", []))
        self.sims = {i: sim.SIMS[m.NAME]() for i, m in self.modules.items()}
        self.servers = {i: sim.serve(s) for i, s in self.sims.items()}
        #: port Item -> (matrix or switch id, port); an HDMI switch's port is
        #: its output 0, the one its room's display is on, a matrix's the
        #: output the room's display is on
        self.ports = {}
        for ident, module in self.modules.items():
            if ident in self.switches:
                continue
            for port in range(1, module.ports(self.devices[ident])[1] + 1):
                self.ports[module.switch_item(self.devices[ident], port)] = (ident, port)
        for room, entry in self.routing["rooms"].items():
            if "hdmi_switch" in entry:
                self.ports[self.table[room]["switch"]["item"]] = (entry["hdmi_switch"]["device"],
                                                                  entry["hdmi_switch"].get("output", 0))
        #: a receiver's or display's input Item -> the inputs it takes
        self.inputs = {stage["input"]: stage["inputs"] for room in self.table
                       for _, stage in routing.stages(data, room)}
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
        self.failures: list[str] = []
        self.http: lib.Http | None = None
        #: the instance's events.log, once it boots
        self.events: Path | None = None

    # ------------------------------------------------------------ conf

    def conf(self) -> dict[str, str | bytes]:
        """The generated routing files and the remote page, with the matrices
        at their simulators."""
        files = render.render(self.data)
        keep = {"services/addons.cfg", "yaml/model.yaml", routing.ITEMS, routing.RULE, all_off.ITEMS, all_off.RULE,
                *(m.FILE for m in self.modules.values()), *(m.RULE for m in self.modules.values() if self.ruled(m)),
                *(f"{remote.TARGET}/{name}" for name in remote.FILES)}
        house = oh_conf.House(self.data)
        for module in {m.NAME: m for m in self.modules.values()}.values():
            keep |= set(getattr(module, "ASSETS", {}))
            if hasattr(module, "files"):
                devices = [device for device in self.data["devices"] if oh_conf.uses(device, module.PHASE, module.NAME)]
                keep |= set(module.files(devices, house))
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
        """Every room off, but the first room on its first source, whose
        input has that room's EDID and every other source's the idle one;
        HDMI switches on the matrix; USB parked."""
        hdmi = self.sims[self.routing["hdmi_matrix"]]
        for entry in self.routing["rooms"].values():
            if "hdmi_output" in entry:
                hdmi.set(entry["hdmi_output"], self.routing["hdmi_off_input"])
            switch = entry.get("hdmi_switch")
            if switch is not None and "matrix_input" in switch:
                self.sims[switch["device"]].set(switch.get("output", 0), switch["matrix_input"])
        room = next(r for r in self.table if self.table[r]["hdmi"] and self.matrix_choices(r))
        source = self.number(self.matrix_choices(room)[0])
        hdmi.set(self.routing["rooms"][room]["hdmi_output"], source)
        if self.edid is not None:
            for number in self.edid["inputs"]:
                if self.edid["idle"] is not None:
                    hdmi.set_edid(int(number), self.edid["idle"])
            hdmi.set_edid(source, self.code(room))
        for ident, matrix in self.sims.items():
            if ident != self.routing["hdmi_matrix"] and ident not in self.switches:
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
        return {item: self.sims[ident].get(port) for item, (ident, port) in self.ports.items()}

    def sources(self, rooms: dict[str, str]) -> bool:
        return self.wait("Source", {self.table[r]["item"]: v for r, v in rooms.items()}, self.states)

    def update(self, item: str, value: str) -> None:
        """Change an Item's state as its device reporting it would."""
        self.http.request("PUT", f"/rest/items/{urllib.parse.quote(item)}/state", value.encode(),
                          {"Content-Type": "text/plain"})

    def expect_routed(self, what: str, choices: dict[str, str]) -> bool:
        want = {item: value for room, choice in choices.items() for item, value in self.table[room]["choices"][choice]}
        routed = self.wait(f"{what}: matrix ports", want, self.matrices)
        sent = {item: str(value) for room, choice in choices.items()
                for key in ("power", "inputs", "after") for item, value in self.table[room][key][choice]}
        devices = self.wait(f"{what}: receivers, displays and actions", sent, self.states)
        return self.sources(choices) and routed and devices and self.expect_edids(what)

    def edid_problems(self) -> dict[str, tuple]:
        """Source inputs whose EDID is neither the code of a room showing
        them nor, when no room does, the idle code."""
        hdmi = self.sims[self.routing["hdmi_matrix"]]
        codes: dict[str, set[int]] = {}
        for item, code in self.edid["outputs"].items():
            ident, port = self.ports[item]
            codes.setdefault(str(self.sims[ident].get(port)), set()).add(code)
        wrong = {}
        for number, item in self.edid["inputs"].items():
            want = codes.get(number) or ({self.edid["idle"]} if self.edid["idle"] is not None else None)
            got = hdmi.edid(int(number))
            if want is not None and got not in want:
                wrong[item] = (sorted(want), got)
        return wrong

    def expect_edids(self, what: str) -> bool:
        return self.edid is None or self.wait_for(f"{what}: EDIDs", self.edid_problems)

    # ------------------------------------------------------------ steps

    def readback_choice(self, room: str, silent: frozenset = frozenset(), states: dict | None = None) -> str:
        """The Source a room should read back, with the matrices and
        switches in `silent` not answering; `states`, the Items' states,
        are read when the room has a receiver or display and none are given."""
        stages = self.table[room]["stages"]
        if stages and states is None:
            states = self.states()

        def reported(item: str) -> str | None:
            value = (states or {}).get(item)
            return None if value in (None, "NULL", "UNDEF") else value

        if any(reported(stage["power"]) == "OFF" for stage in stages):
            return routing.OFF
        for stage in stages:
            value = reported(stage["input"])
            if value is None:
                return "UNDEF"
            if value in stage["readback"]:
                return stage["readback"][value]
            if value != stage["feed"]:
                return "UNDEF"
        switch = self.table[room]["switch"]
        if switch is not None:
            ident, port = self.ports[switch["item"]]
            current = None if ident in silent else str(self.sims[ident].get(port))
            if current in switch["readback"]:
                return switch["readback"][current]
            if current != switch["matrix"]:
                return "UNDEF"
        if self.table[room]["hdmi"] is None or self.routing["hdmi_matrix"] in silent:
            return "UNDEF"
        output = self.routing["rooms"][room]["hdmi_output"]
        current = self.sims[self.routing["hdmi_matrix"]].get(output)
        return self.table[room]["readback"].get(str(current), "UNDEF")

    def uid(self, ident: str) -> str:
        return self.modules[ident].thing_uid(self.devices[ident])

    @staticmethod
    def ruled(module) -> bool:
        """Whether a rule of the integration's own talks to the matrix, not a Thing."""
        return hasattr(module, "aim")

    def switch_inputs(self, ident: str):
        """The input numbers of an HDMI switch, or of a matrix one is an output of."""
        module = self.modules[ident]
        return range(1, module.ports(self.devices[ident])[0] + 1) if hasattr(module, "ports") else module.INPUTS

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
        room = next(iter(self.table))
        writes = {i: self._writes(i) for i in self.sims}
        self.command(self.table[room]["item"], "no_such_source")
        time.sleep(3)
        for ident in self.sims:
            if self._writes(ident) != writes[ident]:
                self.failures.append(f"an unoffered choice in {room} reached {ident}")

    def _writes(self, ident: str) -> int:
        reads = {c for c in self.modules[ident].COMHEADS if c.startswith("get")}
        return sum(1 for c in self.sims[ident].comheads() if c not in reads)

    def expect_edid(self, what: str, number: int, code: int | None) -> bool:
        """Wait until the source on input `number` presents exactly `code`."""
        if self.edid is None or code is None:
            return True
        hdmi = self.sims[self.routing["hdmi_matrix"]]
        item = self.edid["inputs"][str(number)]

        def wrong():
            got = hdmi.edid(number)
            return {} if got == code else {item: (code, got)}
        return self.wait_for(f"{what}: EDID", wrong)

    def code(self, room: str) -> int | None:
        return self.edid["outputs"][self.table[room]["hdmi"]] if self.edid else None

    def number(self, source: str) -> int | None:
        """The source's matrix input (None for one cabled to a room alone)."""
        return self.routing["sources"][source].get("hdmi_input")

    def matrix_choices(self, room: str) -> list[str]:
        """The room's sources that the HDMI matrix routes to it."""
        local = routing._local(self.routing["rooms"][room])
        return [s for s in self.table[room]["choices"] if s != routing.OFF and s not in local
                and self.number(s) is not None]

    def show_feed(self, room: str) -> None:
        """The room's receiver and display on, and on the signal before
        them, as if switched there: the Source then follows the matrix."""
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
        hdmi = self.sims[self.routing["hdmi_matrix"]]
        room = max((r for r in self.table if self.table[r]["hdmi"]),
                   key=lambda r: (self.table[r]["switch"] is not None, len(self.matrix_choices(r))))
        self.turn_off([r for r in self.table if r != room])
        switch = self.table[room]["switch"]
        writes = {ident: self._writes(ident) for ident in self.sims}
        self.show_feed(room)
        if switch is not None:
            ident, port = self.ports[switch["item"]]
            self.sims[ident].set(port, int(switch["matrix"]))
        output = self.routing["rooms"][room]["hdmi_output"]
        source = next(s for s in self.matrix_choices(room) if self.number(s) != hdmi.get(output))
        hdmi.set(output, self.number(source))
        self.sources({room: source})
        offered = {int(k) for k in self.table[room]["readback"]}
        inputs = self.modules[self.routing["hdmi_matrix"]].ports(self.devices[self.routing["hdmi_matrix"]])[0]
        stranger = next((i for i in range(1, inputs + 1) if i not in offered), None)
        if stranger is not None:
            hdmi.set(output, stranger)
            self.sources({room: "UNDEF"})
        unfed = [r for r in self.table if self.table[r]["switch"] is not None and self.table[r]["hdmi"] is None]
        for other in [room, *unfed]:
            if other != room:
                self.show_feed(other)
            if self.table[other]["switch"] is not None:
                self.switched_outside(other)
        time.sleep(3)
        for ident in self.sims:
            if self._writes(ident) != writes[ident]:
                self.failures.append(f"a switch made at the matrix or an HDMI switch made runtime write to {ident}")

    def switched_outside(self, room: str) -> None:
        """Each input of the room's HDMI switch, set at the switch, shows as
        its source, and one the room does not offer as UNDEF; then the
        switch goes back to the matrix, or where it was."""
        switch = self.table[room]["switch"]
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
        """Switches made at a room's receiver or display (its front panel or
        its own remote) are shown and answered with no write, and so is one
        turned off there."""
        staged = [room for room, entry in self.table.items() if entry["stages"]]
        self.turn_off(staged)
        for room in staged:
            entry = self.table[room]
            # A source on the matrix, from Off, moves its ports: the choice is
            # read back only once the matrices have taken all its writes.
            choice = (self.matrix_choices(room) or [c for c in entry["choices"] if c != routing.OFF])[0]
            self.away(room, choice)
            self.command(entry["item"], choice)
            self.expect_routed(f"{entry['label']} before a switch at its devices", {room: choice})
            writes = {ident: self._writes(ident) for ident in self.sims}
            sent = dict(entry["inputs"][choice])
            commands = self.device_commands()
            for stage in entry["stages"]:
                for value, source in stage["readback"].items():
                    self.update(stage["input"], value)
                    self.sources({room: source})
                known = {*stage["readback"], stage["feed"]}
                other = next((v for v in self.inputs[stage["input"]] if v not in known), None)
                if other is not None:
                    self.update(stage["input"], other)
                    self.sources({room: "UNDEF"})
                if stage["input"] in sent:
                    self.update(stage["input"], str(sent[stage["input"]]))
                self.update(stage["power"], "OFF")
                self.sources({room: routing.OFF})
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

    def away(self, room: str, choice: str) -> None:
        """Off leaves a switch the master matrix does not feed as it is, so
        it may already show `choice`, and the choice would read back before
        its write lands: put it on another input first, as if switched
        there while the room is off."""
        switch = self.table[room]["switch"]
        if switch is None or switch["matrix"] is not None:
            return
        wanted = dict(self.table[room]["choices"][choice]).get(switch["item"])
        ident, port = self.ports[switch["item"]]
        if wanted is not None and self.sims[ident].get(port) == int(wanted):
            self.sims[ident].set(port, next(n for n in self.switch_inputs(ident) if n != int(wanted)))

    def step_hand_over(self) -> None:
        """Two rooms on one source: the last to choose it wins; when it
        turns off, the source gets the first room's EDID back, and once
        neither shows it, the idle EDID."""
        pairs = ((a, b, s) for a in self.table for b in self.table if a != b
                 for s in self.table[a]["choices"]
                 if s != routing.OFF and s in self.table[b]["choices"] and self.number(s) is not None)
        found = next(pairs, None)
        if self.edid is None or found is None:
            return
        first, second, source = found
        self.turn_off(list(self.table))
        number = self.number(source)
        for room, choice, code, what in (
                (first, source, self.code(first), "the first room to choose it"),
                (second, source, self.code(second), "the last room to choose it wins"),
                (second, routing.OFF, self.code(first), "handed back when that room turns off"),
                (first, routing.OFF, self.edid["idle"], "the idle EDID once no room shows it")):
            self.command(self.table[room]["item"], choice)
            self.expect_routed(what, {room: choice})
            self.expect_edid(what, number, code)

    def step_silent(self) -> None:
        """The HDMI matrix, then each HDMI switch, stops answering and comes back."""
        for ident in [self.routing["hdmi_matrix"], *self.switches]:
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
            _, body = anonymous.request("GET", remote.URL + name)
            if body != self.remote_files[name]:
                self.failures.append(f"{remote.URL}{name} differs from the generated test conf")
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
        oh_cli.trace_http("GET", self.http.base + REMOTE_EVENTS)
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
            refused = [r for r, a in zip(matrix.requests, matrix.replies) if a and a.get("result") == 0]
            if refused:
                self.failures.append(f"{ident} refused {len(refused)} request(s), first {refused[0]}")
            if matrix.unknown:
                self.failures.append(f"{ident} got unknown comheads {sorted(set(matrix.unknown))}")

    def step_log(self, inst: lib.Instance) -> None:
        text = inst.openhab_log.read_text(encoding="utf-8", errors="replace") if inst.openhab_log.exists() else ""
        problems = lib.log_problems(lib.parse_log(text))
        for record in problems["model"] + problems["errors"]:
            self.failures.append(f"openhab.log:{record.lineno} [{record.level}] [{record.logger}] "
                                 f"{record.message.splitlines()[0] if record.message else ''}")


def run_check(args, rt: lib.Runtime, log) -> int:
    data = inventory.load(args.house)
    problems = render.validate(data)
    if problems or data.get("routing") is None:
        raise lib.SandboxError("; ".join(problems) or "instance/devices.yaml has no routing")
    check = Check(data, log=log)
    conf = STATE / "conf"
    inst = lib.boot_check_instance(conf)
    started = time.monotonic()
    deadline = started + args.timeout
    try:
        for server in check.servers.values():
            server.start()
        lib.wipe_dir(conf)
        render.write(conf, check.conf())
        check.seed()
        with lib.InstanceLock(inst):
            if lib.running_pgid(inst):
                raise lib.SandboxError(f"a kept test instance is still running (pid file {inst.pid_file})")
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
    oh_cli.add_house(parser, render.INVENTORY)


def run(args) -> int:
    try:
        rt = lib.fetch(lib.load_versions(), log=print)
        return run_check(args, rt, print)
    except lib.SandboxError as exc:
        lib.die(str(exc))
    return 2
