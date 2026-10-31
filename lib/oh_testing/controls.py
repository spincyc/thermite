"""thermite test controls: remote and routing controls end to end,
against simulated devices."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from pathlib import Path

import oh_adcp_sim as adcp_sim
import oh_atv_sim as atv_sim
import oh_av_sim as av_sim
import oh_googletv_sim as googletv_sim
import oh_integrations as integrations
import oh_inventory as inventory
import oh_cli
import oh_render as render
import oh_sandbox as lib
import oh_sony_sim as sony_sim
import oh_sonos_sim as sonos_sim
from oh_cli import pair as pair_cli
from oh_conf import House, YamlFile, uses
from oh_features import all_off
from oh_testing.routing import SIGNAL_WAIT, signal_problems

SUMMARY = "receiver, streamer, projector and TV controls, plus Sonos Item contracts, against stand-ins"

DESCRIPTION = """\
Prove the remote and routing controls work, end to end,
without touching the house. Generate conf/ from the house file, keep only
the Things, Items and rules of the devices runtime controls (every
integration's remote(), plus its stage() input where the page has no input
selector), point each at a simulated device on 127.0.0.1 with
the integration's aim(), boot a test runtime on that as thermite test boot
does, and check that:

1. the kept conf names no address of a real device;
2. every Thing is ONLINE, every control Item reads what its simulated
   device shows, and runtime sent them no command while it started;
3. power, every input, a volume, mute and every remote key reach the
   simulated device as that device's own command, and are read back;
4. a volume above the configured limit is held to it. A full-range Sonos
   Dimmer reaches 100 and rejects 110 at REST without a device write;
   an unknown key sends nothing;
5. each device's Mute (Input on a projector), pressed as the page presses
   it, reaches its simulated device within REACH seconds and is read back on the
   page's event stream within its integration's READ_BACK plus SLACK; the
   log shows every time and the CPU runtime used meanwhile;
6. with no login, as a phone uses it, a command is accepted and its
   read-back heard on the remote page's event stream (all its topics);
7. All off, pressed as the page presses it, turns every device with a
   power control off (all_off.js; the rooms' Source Items are not in this
   conf, so it names them as missing);
8. openhab.log holds no ERROR and no model problem.

One Sony TV is also paired again through the CLI and its pairing Items.
Its result must acknowledge that new command, and neither native log may
record the pairing command or its PIN.

Sonos uses an explicitly labelled Item/rule-contract replay over loopback
HTTP, with fake UDNs. This checks profiles, keys, limits and UI read-back;
it does not simulate or claim native Sonos UPnP/SOAP protocol coverage.
Native discovery/channel status and idempotent real-binding validation are
separate deployment checks.

It uses the test runtime of thermite test boot (127.0.0.1:18081) and its
warmed userdata, so it cannot run beside thermite test boot or thermite
test routing.
"""

EXAMPLES = (
    ("thermite test controls", "every control the remote page drives, against simulated devices on 127.0.0.1"),
    ("thermite test controls --timeout 600", "give a slow host longer to boot"),
)

NOTES = """\
exit status:
  0  every step passed
  1  a step failed
  2  runtime could not be booted or queried
"""

STATE = lib.VAR / "av-check"
STOP_TIMEOUT = 3
#: Seconds a step may take: a poll (15s for the projector), a command and its read-back.
STEP = 20
#: How long an unknown key is given to reach a stand-in it must not reach.
QUIET = 3
#: Seconds a pressed command may take to reach its stand-in, and its
#: read-back to reach the event stream beyond its integration's READ_BACK.
REACH = 0.5
SLACK = 1.0
#: Presses timed per device.
PRESSES = 2
FIELDS = ("power", "volume", "mute", "input")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
UPDATE_TOPIC = re.compile(r"openhab/items/([^/]+)/stateupdated")


class ProjectorStandIn:
    """lib/oh_adcp_sim.py's projector, as the control Items see it."""

    def __init__(self, module, device: dict) -> None:
        self.sim = adcp_sim.Projector()
        self.server = adcp_sim.Server(self.sim)
        self.codes = {code: button for button, code in module.KEYS.items()}

    def state(self) -> dict[str, str]:
        on = self.sim.power_status in ("startup", "on")
        return {"power": "ON" if on else "OFF", "input": self.sim.input}

    def keys(self) -> list[str]:
        return [self.codes.get(line[5:-1], line) for line in self.sim.lines() if line.startswith('key "')]

    def writes(self) -> list[str]:
        return self.sim.sets()


class ReceiverStandIn:
    """lib/oh_av_sim.py's receiver, as the control Items see it."""

    def __init__(self, module, device: dict) -> None:
        self.sim = av_sim.Receiver(model=device.get("model") or "CINEMA 60")
        self.server = av_sim.Server(self.sim)
        self.codes = {code: button for button, code in module.KEYS.items()}

    def state(self) -> dict[str, str]:
        sim = self.sim
        return {"power": "ON" if sim.power else "OFF", "volume": f"{sim.volume:g}",
                "mute": "ON" if sim.mute else "OFF", "input": sim.source}

    def keys(self) -> list[str]:
        return [self.codes[command] for command in self.sim.commands if command in self.codes]

    def writes(self) -> list[str]:
        return [command for command in self.sim.commands if not command.endswith("?")]


class PlayerStandIn:
    """lib/oh_av_sim.py's WiiM, as the control Items see it."""

    def __init__(self, module, device: dict) -> None:
        self.sim = av_sim.Player()
        self.server = av_sim.Server(self.sim)
        self.modes = module.MODES
        self.codes = {code: button for button, code in module.KEYS.items()}

    def state(self) -> dict[str, str]:
        sim = self.sim
        return {"volume": str(sim.volume), "mute": "ON" if sim.mute else "OFF",
                "input": self.modes.get(sim.mode, "wifi")}

    def keys(self) -> list[str]:
        return [self.codes[command[len("setPlayerCmd:"):]] for command in self.sim.commands
                if command[len("setPlayerCmd:"):] in self.codes]

    def writes(self) -> list[str]:
        return [command for command in self.sim.commands if command.startswith("setPlayerCmd:")]


class SonosStandIn:
    """Sonos Item/rule contract only; actual UPnP binding checked separately."""
    def __init__(self, module, device: dict) -> None:
        self.sim = sonos_sim.Player(module.simulation_udn(device))
        self.server = sonos_sim.Server(self.sim)

    def state(self) -> dict[str, str]:
        return {"volume": str(self.sim.volume), "mute": self.sim.mute}

    def keys(self) -> list[str]:
        return list(self.sim.presses)

    def writes(self) -> list:
        return list(self.sim.commands)


class GoogleTVStandIn:
    """lib/oh_googletv_sim.py's TV, as the control Items see it."""

    def __init__(self, module, device: dict) -> None:
        self.sim = googletv_sim.TV()
        self.server = googletv_sim.Server(self.sim)
        self.buttons = {("link" if isinstance(press, str) else "key", press): button
                        for button, press in module.keys(device).items()}

    def state(self) -> dict[str, str]:
        sim = self.sim
        return {"power": "ON" if sim.power else "OFF", "volume": str(sim.volume * 100 // sim.max_volume),
                "mute": "ON" if sim.muted else "OFF"}

    def keys(self) -> list[str]:
        return [self.buttons.get(press, f"{press[0]} {press[1]}") for press in self.sim.presses()]

    def writes(self) -> list[str]:
        return [f"{kind} {value}" for kind, value in self.sim.presses()]


class AppleTVStandIn:
    """lib/oh_atv_sim.py's helper, already paired with its Apple TV, as the control Items see it."""

    def __init__(self, module, device: dict) -> None:
        self.sim = atv_sim.AppleTV()
        self.server = atv_sim.Server(self.sim, paired={device["id"]: os.urandom(32).hex()})
        self.buttons = {f"key {key}": button for button, key in module.PRESSES.items()}
        self.buttons.update({f"app {bundle}": button for button, bundle in module.APPS.items()})

    def state(self) -> dict[str, str]:
        return {"power": "ON" if self.sim.power else "OFF"}

    def keys(self) -> list[str]:
        return [self.buttons.get(command, command) for command in self.sim.commands]

    def writes(self) -> list[str]:
        return self.sim.commands


class SonyTVStandIn:
    """lib/oh_sony_sim.py's TV, paired already, as the control Items see it:
    prepare() puts the registration the rule would keep in the test
    instance's userdata, and cleanup() takes it out again."""

    def __init__(self, module, device: dict) -> None:
        self.module = module
        self.ident = device["id"]
        self.sim = sony_sim.TV(pin="0000", cookie=f"stand-in-{os.urandom(8).hex()}",
                               max_volume=module.MAX_VOLUME)
        self.server = sony_sim.Server(self.sim)
        self.buttons = {code: button for button, code in module.KEYS.items()}
        self.path: Path | None = None

    def prepare(self, inst: lib.Instance) -> None:
        directory = inst.userdata / self.module.USERDATA_DIR
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / f"{self.ident}.json"
        self.path.write_text(json.dumps(self.sim.registration()), encoding="utf-8")
        self.path.chmod(0o600)

    def cleanup(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)

    def state(self) -> dict[str, str]:
        sim = self.sim
        hdmi = sony_sim.HDMI.fullmatch(sim.uri or "")
        return {"power": "ON" if sim.power else "OFF", "volume": str(sim.volume), "mute": "ON" if sim.muted else "OFF",
                "input": f"hdmi{hdmi[1]}" if hdmi else "tv"}

    def keys(self) -> list[str]:
        return [self.buttons.get(code, code) for code in self.sim.keys()]

    def writes(self) -> list[str]:
        return self.sim.sets() + [f"ircc {code}" for code in self.sim.keys()]


#: The stand-in for each integration with a control, by its NAME.
STAND_INS = {"sony_adcp": ProjectorStandIn, "denonmarantz_http": ReceiverStandIn, "wiim_http": PlayerStandIn,
             "androidtv_googletv": GoogleTVStandIn, "pyatv_http": AppleTVStandIn, "sony_ip_control": SonyTVStandIn,
             "sonos": SonosStandIn}


def same(want: object, got: object) -> bool:
    """Item states compare as numbers when both are (a Dimmer reads 40 or 40.0)."""
    if isinstance(want, str) and isinstance(got, str):
        a, b = NUMBER.fullmatch(want.split(" ")[0]), NUMBER.fullmatch(got.split(" ")[0])
        if a and b:
            return float(a.group()) == float(b.group())
    return want == got


def until(condition, since: float, timeout: float) -> float | None:
    """Seconds from `since` until condition() holds; None if it does not within timeout."""
    while not condition():
        if time.monotonic() - since >= timeout:
            return None
        time.sleep(0.005)
    return time.monotonic() - since


def cpu_seconds(pgid: int | None) -> float:
    """The CPU time a process group has used so far, its exited children aside."""
    total = 0
    for pid in lib.group_members(pgid) if pgid else []:
        try:
            # Fields after the parenthesised command: state is the first, utime and stime the 12th and 13th.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        total += int(fields[11]) + int(fields[12])
    return total / os.sysconf("SC_CLK_TCK")


def control_items(control: dict) -> dict[str, str]:
    """field -> Item name of what a control reads (oh_integrations remote())."""
    items = {}
    for field in FIELDS:
        value = control.get(field)
        if value:
            items[field] = value if isinstance(value, str) else value["item"]
    return items


class Check:
    def __init__(self, data: dict, log=print) -> None:
        self.data = data
        self.log = log
        self.devices = {d["id"]: d for d in data["devices"]}
        #: device id -> (its controlling module, its remote and routing controls)
        self.controls: dict[str, tuple] = {}
        for device in data["devices"]:
            for module in integrations.REGISTRY:
                offered = module.remote(device) if hasattr(module, "remote") else None
                if offered:
                    if not offered.get("input") and hasattr(module, "stage"):
                        stage = module.stage(device, None)
                        if stage and stage.get("input"):
                            offered = {**offered, "input": {"item": stage["input"],
                                                           "options": [[value, value] for value in stage["inputs"]]}}
                    self.controls[device["id"]] = (module, offered)
        if not self.controls:
            raise lib.SandboxError("no device in instance/devices.yaml is under control")
        missing = sorted({m.NAME for m, _ in self.controls.values()} - set(STAND_INS))
        if missing:
            raise lib.SandboxError("no simulator for " + ", ".join(missing))
        self.stand_ins = {i: STAND_INS[m.NAME](m, self.devices[i]) for i, (m, _) in self.controls.items()}
        self.signal_since = time.time() * 1000
        self.signal_items = set()
        for ident, (module, _) in self.controls.items():
            if hasattr(module, "diagnostics"):
                self.signal_items.update((module.diagnostics(self.devices[ident]) or {}).get("items", []))
        self.failures: list[str] = []
        self.http: lib.Http | None = None

    # ------------------------------------------------------------ conf

    def conf(self) -> dict[str, str | bytes]:
        """The control modules' rendered files, every controlled device at its stand-in."""
        files = render.render(self.data)
        house = House(self.data)
        modules = {m.NAME: m for m, _ in self.controls.values()}
        keep = {"services/addons.cfg", "yaml/model.yaml", all_off.ITEMS, all_off.RULE}
        for module in modules.values():
            keep.add(module.FILE)
            keep |= set(getattr(module, "ASSETS", {}))
            if hasattr(module, "files"):
                rendered = [d for d in self.data["devices"] if uses(d, module.PHASE, module.NAME)]
                keep |= set(module.files(rendered, house))
        out = {path: files[path] for path in sorted(keep) if path in files}
        # A shared module file can also contain observer-only or unexposed
        # devices. Render only this check's selected controls; a UDN can reach
        # real hardware even when a naive IP-address scan finds no leak.
        for module in modules.values():
            selected = [self.devices[i] for i, (owner, _) in self.controls.items() if owner is module]
            part = YamlFile(tuple(module.HEADER))
            for device in selected:
                module.render(device, house, part)
            out[module.FILE] = part.text()
            if hasattr(module, "files"):
                out.update(module.files(selected, house))
        # A module that needs another's Things and Items (sony_ip_control,
        # sony_rest's power) gets them for its own devices only.
        for module in modules.values():
            for needed in getattr(module, "NEEDS", ()):
                part = YamlFile(tuple(needed.HEADER))
                for ident, (owner, _) in self.controls.items():
                    if owner is module and uses(self.devices[ident], needed.PHASE, needed.NAME):
                        needed.render(self.devices[ident], house, part)
                out[needed.FILE] = part.text()
        for ident, (module, _) in self.controls.items():
            out = module.aim(out, self.devices[ident], self.stand_ins[ident].server.address)
        leaks = sorted({f"{path}: {device['id']} ({device['ip']})" for path, text in out.items()
                        if isinstance(text, str) for device in self.data["devices"]
                        if device.get("ip") and re.search(rf"(?<![\d.]){re.escape(device['ip'])}(?![\d.])", text)})
        leaks += sorted({f"{path}: {device['id']} (real Sonos UDN)" for path, text in out.items()
                         if isinstance(text, str) for device in self.data["devices"]
                         if device.get("sonos_udn") and device["sonos_udn"] in text})
        if leaks:
            raise lib.SandboxError("the conf would reach a real device: " + "; ".join(leaks))
        return out

    # ------------------------------------------------------------ runtime

    def prepare(self, inst: lib.Instance) -> None:
        for stand_in in self.stand_ins.values():
            if hasattr(stand_in, "prepare"):
                stand_in.prepare(inst)

    def states(self) -> dict[str, str]:
        _, rows = self.http.try_get_json("/rest/items?recursive=false&fields=name,state")
        return {row["name"]: row.get("state") for row in rows or []}

    def things(self) -> dict[str, str]:
        _, rows = self.http.try_get_json("/rest/things?summary=true")
        return {row["UID"]: row.get("statusInfo", {}).get("status") for row in rows or []}

    def command(self, item: str, value: str, http: lib.Http | None = None) -> None:
        (http or self.http).request("POST", f"/rest/items/{urllib.parse.quote(item)}", value.encode(),
                                    {"Content-Type": "text/plain"})

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

    def agree(self, what: str, ident: str, want: dict[str, str]) -> bool:
        """Wait until the stand-in shows `want` and the control's Items read it back."""
        items = control_items(self.controls[ident][1])

        def wrong():
            shown, read = self.stand_ins[ident].state(), self.states()
            problems = {f"{ident} {f}": (v, shown.get(f)) for f, v in want.items() if not same(v, shown.get(f))}
            problems.update({items[f]: (v, read.get(items[f])) for f, v in want.items()
                             if not same(v, read.get(items[f]))})
            return problems
        return self.wait_for(what, wrong)

    def each(self, check) -> None:
        """check(ident) for every controlled device at once: each waits on its own device's polls."""
        with ThreadPoolExecutor(len(self.controls)) as pool:
            list(pool.map(check, self.controls))

    # ------------------------------------------------------------ steps

    def step_online(self) -> None:
        """Every controlled device's Thing (a rule-driven one has none), and any other kept Thing."""
        expected = {m.thing_uid(self.devices[i]) for i, (m, _) in self.controls.items() if hasattr(m, "thing_uid")}

        def wrong():
            seen = self.things()
            return {uid: ("ONLINE", seen.get(uid)) for uid in expected | set(seen) if seen.get(uid) != "ONLINE"}
        self.wait_for("Things", wrong, timeout=60)

    def step_readback(self) -> None:
        for ident, (_, control) in self.controls.items():
            shown = self.stand_ins[ident].state()
            self.agree(f"{ident} read at start", ident, {f: shown[f] for f in control_items(control)})
        for ident, stand_in in self.stand_ins.items():
            if stand_in.writes():
                self.failures.append(f"runtime sent {ident} {stand_in.writes()} while it started")

    def step_commands(self) -> None:
        self.each(self._commands)

    def step_diagnostics(self) -> None:
        if self.signal_items:
            self.wait_for("read-only signal snapshots", lambda: signal_problems(
                self.signal_items, self.states(), self.signal_since), timeout=SIGNAL_WAIT)

    def step_pairing(self, inst: lib.Instance) -> None:
        """One Sony PIN round trip through real Items, with no PIN in either native log."""
        tv = next((s for s in self.stand_ins.values() if isinstance(s, SonyTVStandIn)), None)
        if tv is None:
            return
        device = self.devices[tv.ident]
        item = f"{tv.ident}_pairing"
        result = tv.module.pairing_result(device)
        # A distinct synthetic PIN avoids matching ordinary timestamps.
        tv.sim.pin = "8429"
        try:
            before = pair_cli.item_state(self.http, result)
            paired = pair_cli.pair_sony(device, self.http, None, ask=lambda _: tv.sim.pin, log=self.log)
            after = pair_cli.item_state(self.http, result)
            status = pair_cli.item_state(self.http, f"{tv.ident}_pairing_status")
        except pair_cli.OpenHABError as exc:
            self.failures.append(f"Sony PIN round trip: {exc}")
            return
        try:
            acknowledged = json.loads(after)
        except (ValueError, TypeError):
            acknowledged = {}
        if (paired or after == before or not acknowledged.get("id")
                or acknowledged.get("state") != "paired" or status != "paired"):
            self.failures.append("Sony PIN round trip did not produce a fresh paired acknowledgement")
        paths = [inst.openhab_log.parent / "events.log", inst.openhab_log]

        def logged():
            text = paths[0].read_text(encoding="utf-8", errors="replace") if paths[0].exists() else ""
            return {} if result in text else {"pairing result in events.log": ("recorded", "missing")}

        self.wait_for("Sony pairing recorded", logged)
        private_item = re.compile(rf"(?<!\w){re.escape(item)}(?!\w)")
        private_pin = re.compile(rf"(?:received command |with value ['\"]?){re.escape(tv.sim.pin)}(?!\d)")
        for path in paths:
            if not path.exists():
                self.failures.append(f"Sony pairing privacy: no {path.name}")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if private_item.search(text) or private_pin.search(text):
                self.failures.append(f"Sony pairing privacy: a pairing command or PIN reached {path.name}")

    def _commands(self, ident: str) -> None:
        module, control = self.controls[ident]
        if control.get("power"):
            self.command(control["power"], "ON")
            self.agree(f"{ident} power ON", ident, {"power": "ON"})
        # A receiver reads back only at its next poll, so each round sends the
        # next value of every field at once and one poll reads them all.
        fields = self.values(ident)
        for together in zip_longest(*fields.values()):
            sent = {field: entry for field, entry in zip(fields, together) if entry}
            for item, value, _ in sent.values():
                self.command(item, value)
            what = ", ".join(f"{field} {value}" + (f" held to {shown}" if shown != value else "")
                             for field, (_, value, shown) in sent.items())
            self.agree(f"{ident} {what}", ident, {field: shown for field, (_, _, shown) in sent.items()})
        if module.NAME == "sonos" and control.get("volume", {}).get("max") == 100:
            self._reject_sonos_volume(ident)
        if control.get("key"):
            buttons = module.keys(self.devices[ident]) if hasattr(module, "keys") else getattr(module, "KEYS", {})
            for button in buttons:
                self.command(control["key"], button)
                self.wait_for(f"{ident} key {button}", lambda: {} if self.stand_ins[ident].keys()[-1:] == [button]
                              else {ident: (button, (self.stand_ins[ident].keys() or [None])[-1])})

    def values(self, ident: str) -> dict[str, list[tuple[str, str, str]]]:
        """field -> (Item, value sent, value read back) for each value a
        device is sent: every input, ending on the one it showed; half its
        volume limit, 0 and an above-limit value held to the limit; mute
        on and off. Full-range Sonos reaches 100, then a separate check
        proves REST rejects an out-of-range Dimmer command."""
        module, control = self.controls[ident]
        values = {}
        if control.get("input"):
            options = [value for value, _ in control["input"]["options"]]
            first = self.stand_ins[ident].state().get("input")
            order = [o for o in options if o != first] + ([first] if first in options else [])
            values["input"] = [(control["input"]["item"], value, value) for value in order]
        if control.get("volume"):
            spec = control["volume"]
            half = f"{int(spec['max']) // 2}"
            above = min(100, spec["max"] + 10) if module.NAME == "sonos" else spec["max"] + 10
            values["volume"] = [(spec["item"], half, half), (spec["item"], "0", "0"),
                                (spec["item"], f"{above:g}", f"{spec['max']:g}")]
        if control.get("mute"):
            values["mute"] = [(control["mute"], value, value) for value in ("ON", "OFF")]
        return values

    def _reject_sonos_volume(self, ident: str) -> None:
        """A native Dimmer rejects values above 100 before channel profiles."""
        item = self.controls[ident][1]["volume"]["item"]
        stand_in = self.stand_ins[ident]
        before = list(stand_in.writes())
        status = None
        try:
            self.command(item, "110")
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
        if status != 400:
            self.failures.append(f"{ident} volume 110 was not rejected with HTTP 400")
        time.sleep(QUIET)
        if stand_in.writes() != before:
            self.failures.append(f"an out-of-range volume reached {ident}")

    def step_unknown_key(self) -> None:
        self.each(self._unknown_key)

    def _unknown_key(self, ident: str) -> None:
        control, stand_in = self.controls[ident][1], self.stand_ins[ident]
        if control.get("key"):
            before = list(stand_in.writes())
            self.command(control["key"], "warp")
            time.sleep(QUIET)
            if stand_in.writes() != before:
                self.failures.append(f"an unknown key reached {ident}: {stand_in.writes()[len(before):]}")

    def step_latency(self, inst: lib.Instance) -> None:
        """Every device pressed at once, each as the page presses it, timed to its stand-in and
        to its read-back on the page's event stream; and the CPU runtime used meanwhile."""
        items = [item for _, control in self.controls.values() for item in control_items(control).values()]
        heard: list[tuple[float, str | None, str]] = []
        opened, stop = threading.Event(), threading.Event()
        threading.Thread(target=self._record, daemon=True,
                         args=("/rest/events?topics=" + ",".join(f"openhab/items/{i}/stateupdated" for i in items),
                               heard, opened, stop)).start()
        opened.wait(3)
        pgid = lib.running_pgid(inst)
        began, used = time.monotonic(), cpu_seconds(pgid)
        self.each(lambda ident: self._latency(ident, heard))
        took, used = time.monotonic() - began, cpu_seconds(pgid) - used
        stop.set()
        self.log(f"        runtime used {used / took * 100:.1f}% of one core over these {took:.1f}s")

    def _latency(self, ident: str, heard: list) -> None:
        module, control = self.controls[ident]
        stand_in, items = self.stand_ins[ident], control_items(control)
        if "mute" in items:
            field, values = "mute", ["ON", "OFF"]
        elif "input" in items:
            field, values = "input", [value for value, _ in control["input"]["options"]][:2]
        else:
            return
        item, allowed = items[field], module.READ_BACK + SLACK
        for _ in range(PRESSES):
            value = next(v for v in values if not same(v, self.states().get(item)))
            sent = len(stand_in.writes())
            began = time.monotonic()
            self.command(item, value)
            reached = until(lambda: len(stand_in.writes()) > sent, began, REACH + STEP)
            read = until(lambda: any(t >= began and i == item and same(value, v) for t, i, v in list(heard)),
                         began, allowed + STEP)
            shown = [f"{ident} {field} {value}:",
                     "never reached it" if reached is None else f"at the device in {reached * 1000:.0f} ms,",
                     "never read back" if read is None else f"read back on the event stream in {read * 1000:.0f} ms",
                     f"(allowed {REACH:g} s and {allowed:g} s)"]
            self.log("        " + " ".join(shown))
            if reached is None or reached > REACH or read is None or read > allowed:
                self.failures.append(" ".join(shown))

    def _record(self, path: str, heard: list, opened: threading.Event, stop: threading.Event) -> None:
        """(time, Item, value) of every event on the stream, until stop is set."""
        oh_cli.trace_http("GET", self.http.base + path)
        try:
            with urllib.request.urlopen(self.http.base + path, timeout=STEP) as stream:
                opened.set()
                for raw in stream:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    event = json.loads(line[5:]) if line.startswith("data:") else {}
                    # The stream's keep-alive events name no topic.
                    match = UPDATE_TOPIC.fullmatch(event.get("topic", ""))
                    if match:
                        heard.append((time.monotonic(), match.group(1), str(json.loads(event["payload"]).get("value"))))
        except (OSError, ValueError, KeyError) as exc:
            heard.append((time.monotonic(), None, str(exc)))
        finally:
            opened.set()

    def step_remote(self) -> None:
        """A command with no login, heard on the event stream the remote page opens."""
        anonymous = lib.Http(self.http.base, timeout=STEP, allow_write=True)
        items = [item for _, control in self.controls.values() for item in control_items(control).values()]
        topics = ["openhab/items/loc_*_source/stateupdated"] + [f"openhab/items/{i}/stateupdated" for i in items]
        ident, (_, control) = next((i, c) for i, c in self.controls.items() if c[1].get("power"))
        opened, heard = threading.Event(), {}
        listener = threading.Thread(target=self._listen, daemon=True,
                                    args=("/rest/events?topics=" + ",".join(topics),
                                          f"openhab/items/{control['power']}/stateupdated", "OFF", opened, heard))
        listener.start()
        opened.wait(3)
        self.command(control["power"], "OFF", anonymous)
        self.agree(f"{ident} power OFF from the remote", ident, {"power": "OFF"})
        listener.join(STEP)
        if not heard.get("value"):
            self.failures.append(f"the event stream never showed {control['power']} = OFF: "
                                 f"{heard.get('error', 'no event')}")

    def step_all_off(self) -> None:
        """Every powered device on, then All off with no login, as the page sends it."""
        powered = [i for i, (_, control) in self.controls.items() if control.get("power")]
        for ident in powered:
            power = self.controls[ident][1]["power"]
            if self.states().get(power) != "ON":
                self.command(power, "ON")
        self.each(lambda ident: ident in powered and self.agree(f"{ident} power ON before All off", ident, {"power": "ON"}))
        self.command(all_off.ITEM, "ON", lib.Http(self.http.base, timeout=STEP, allow_write=True))
        self.each(lambda ident: ident in powered and self.agree(f"{ident} power OFF from All off", ident, {"power": "OFF"}))

    def _listen(self, path: str, topic: str, value: str, opened: threading.Event, heard: dict) -> None:
        oh_cli.trace_http("GET", self.http.base + path)
        try:
            with urllib.request.urlopen(self.http.base + path, timeout=STEP) as stream:
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

    def step_log(self, inst: lib.Instance) -> None:
        text = inst.openhab_log.read_text(encoding="utf-8", errors="replace") if inst.openhab_log.exists() else ""
        problems = lib.log_problems(lib.parse_log(text))
        for record in problems["model"] + problems["errors"]:
            self.failures.append(f"openhab.log:{record.lineno} [{record.level}] [{record.logger}] "
                                 f"{record.message.splitlines()[0] if record.message else ''}")


def run_check(args, rt: lib.Runtime, log) -> int:
    data = inventory.load(args.house)
    problems = render.validate(data)
    if problems:
        raise lib.SandboxError("; ".join(problems))
    check = Check(data, log=log)
    conf = STATE / "conf"
    inst = lib.boot_check_instance(conf)
    started = time.monotonic()
    deadline = started + args.timeout
    try:
        for stand_in in check.stand_ins.values():
            stand_in.server.start()
        lib.wipe_dir(conf)
        render.write(conf, check.conf())
        with lib.InstanceLock(inst):
            if lib.running_pgid(inst):
                raise lib.SandboxError(f"a kept test instance is still running (pid file {inst.pid_file})")
            inst.console_log.unlink(missing_ok=True)
            booted = lib.boot(rt, inst, lib.read_requested_addons(conf), started, deadline,
                              log=log, before_start=check.prepare)
            if booted.missing:
                raise lib.SandboxError("add-ons not installed: " + ", ".join(sorted(booted.missing)))
            check.http = lib.Http(inst.url, token=booted.token, timeout=30, allow_write=True)
            try:
                steps = [
                    ("Things online", check.step_online),
                    ("initial read-back", check.step_readback),
                    ("read-only signal snapshots transformed by the runtime", check.step_diagnostics),
                    ("Sony PIN pairing and native log privacy", lambda: check.step_pairing(inst)),
                    ("power, inputs, volume and its limit, mute and keys", check.step_commands),
                    ("an unknown key", check.step_unknown_key),
                    ("how long a press takes", lambda: check.step_latency(inst)),
                    ("a command from the remote page, with no login", check.step_remote),
                    ("All off", check.step_all_off),
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
        for stand_in in check.stand_ins.values():
            stand_in.server.stop()
            if hasattr(stand_in, "cleanup"):
                stand_in.cleanup()
    log(f"\n{len(check.controls)} controlled devices ({', '.join(check.controls)}), "
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
