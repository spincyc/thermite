"""Everything about one device, in one place.

    thermite device ID [--json] [--house FILE] [--timeout SECONDS]

A filter, never a judge. What the house says of the device is read from the
validated house file and the configuration it generates, through the
functions that decide health for the installation (OWNERS names each); what
holds now is what the installation already worked out, read from its
Prometheus on loopback. No rule here says whether a device is well: a check's
state is thermite:check's, an alert is Prometheus', and a value with no
reading is said as unknown.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import sys
import textwrap
import time
import urllib.parse

import oh_cli
import oh_deploy as deploy
import oh_inventory as inventory
import oh_metrics as metrics
import oh_render as render
import oh_units
from oh_conf import House
from oh_features import reboots, remote_diagnostics
from oh_integrations import _commands
from oh_metrics import alerts, dashboards, exporter

DESCRIPTION = """\
Everything thermite knows about one device, without knowing where each
fact lives.

What it is: its label, room, kind and address, the interfaces that observe
and command it, the Things and Items generated for it, and where it sits on
rooms' paths. What watches it: each reading that says whether it answers,
each health check built on one and when a room needs that check, or plainly
that nothing watches it. Both come from the house file alone and need no
runtime.

What holds now: each reading's value and how long ago it was read, each
check's state and whether a room needs it now, the alerts about it, the
problem its rules last met, the commands they took and what routing failed
at it. This is read from the installation's Prometheus on this host
(127.0.0.1, GET only, no credentials), as thermite health --production
reads it. On a host without the monitoring services it says that this part
needs the installation. While Prometheus does not answer, or has no check
results, what holds now is unknown and is said so: never fine.

Where to look next: the commands for its log lines and the pages for its
board and Things, with this device filled in.

Nothing here decides whether the device is well. Every state is the
installation's own: the checks that alert, as they stand.
"""

EXAMPLES = (
    ("thermite device den_tv", "everything about den_tv"),
    ("thermite device den_tv --json", "the same facts as data"),
    ("thermite device den_tv --house other.yaml", "what another house file makes of it"),
)

NOTES = """\
exit status:
  0  shown; no check a room needs now is failing and no alert about it fires
     (also on a host without the installation, where only the house file
     was read)
  1  a check a room needs now is failing, an alert about it fires, or
     Prometheus has no check results at all
  2  no such device (the nearest id is named), a broken house file, or the
     installation is here and its Prometheus did not answer: what holds now
     is unknown
"""

#: Every fact the report holds and the function, table or series that owns
#: it. The verb only looks these up: tests/test_device_verb.py fails for a
#: field with no owner here, so no second derivation can be added in
#: passing. A `now.` fact is a series of the installation's Prometheus.
OWNERS = {
    "id": "the house file (oh_conf.House.by_id)",
    "is.label": "oh_conf.House.label()",
    "is.room": "the house file, and oh_conf.House.room_label()",
    "is.kind": "the house file",
    "is.address": "the house file (ip)",
    "is.via": "the house file",
    "is.monitor": "the house file (monitor: optional keeps it out of control health)",
    "is.observe": "the house file (controls.observe)",
    "is.control": "the house file (controls.control)",
    "is.things": "oh_metrics.device_things() over the generated configuration",
    "is.items": "oh_conf.Conf.points(); `kept` is oh_metrics.exporter_map()'s point for it",
    "is.paths": "oh_metrics.health_paths() for membership, oh_features.remote_diagnostics.manifest() for the role",
    "watched.signals": "oh_metrics.signals(), in oh_metrics.signal_words()' words",
    "watched.checks": "oh_metrics.health_paths() (the exporter map's health_checks), failing as "
                      "oh_metrics.alerts.CHECK_WORDS",
    "watched.notes": "oh_metrics.unobserved(), as oh_metrics.unobserved_text()",
    "watched.problem_item": "oh_integrations._commands.problem_item(), where the exporter map keeps it",
    "watched.tallies": "oh_integrations._commands.tallies() (the exporter map's commands)",
    "now.state": "whether this host has the monitoring services (oh_deploy.metrics_installed()) and its "
                 "Prometheus answered",
    "now.text": "words for now.state",
    "now.url": "oh_deploy.prometheus_url()",
    "now.at": "the instant Prometheus was asked for",
    "now.runtime": "thermite:check{check=\"runtime\"}: whether the exporter reads the runtime",
    "now.deployed": "thermite_device_info: whether the running installation has this device",
    "now.signals": "each signal's own series, against oh_metrics.Signal.bad; a Thing's status is "
                   "thermite_thing_status_info's",
    "now.readings": "thermite_item_value, thermite_item_info, thermite_item_unknown and thermite_item_device_off "
                    "of each Item the exporter map keeps",
    "now.checks": "thermite:check{device}: its value is the state, its required label whether a room needs it now",
    "now.alerts": "ALERTS{device} and ALERTS_FOR_STATE, in oh_metrics.alerts.CATALOG's and CHECK_WORDS' words",
    "now.problem": "thermite_device_problem and thermite_device_problem_info, in "
                   "oh_integrations._commands.CODES' words; when it last read clear is the gauge's own history",
    "now.commands": "thermite_device_commands_total",
    "now.quarantined": "thermite_routing_device_quarantined",
    "now.routing_failures": "thermite_routing_endpoint_failures_total",
    "next": "oh_units, oh_deploy's addresses and paths, oh_metrics.dashboards.UIDS, oh_features.reboots.table()",
}

#: What the series the exporter does not own say (exporter.FAMILIES has the
#: others'), so that --json names no series without its meaning.
MEANING = {
    "probe_success": "1 when the TCP connection Prometheus' blackbox exporter last tried to the port opened.",
    alerts.CHECK: "A health check's result: 1 failing, 0 OK, -1 unknown. `required` says whether a room needs "
                  "it now.",
    "ALERTS": "An alert Prometheus has pending or firing.",
}
#: A check's states by thermite:check's value (oh_metrics.alerts: 1 failing,
#: 0 OK, -1 unknown). What Prometheus has no result for is unknown too.
FAILING, OK, UNKNOWN = "failing", "ok", "unknown"
CHECK_STATES = {1.0: FAILING, 0.0: OK, -1.0: UNKNOWN}
#: A speaker's playing values in words (exporter PLAYING_*).
PLAYING = {exporter.PLAYING_IDLE: "idle", exporter.PLAYING_SOUND: "sound, no track named",
           exporter.PLAYING_TRACK: "a track"}
#: How far back Prometheus looks for a series' latest sample: its own
#: default, which an instant query applies. The second query reads the same
#: window raw, for when each sample was taken.
LOOKBACK = "5m"
#: How far back, and how finely, the problem gauge's history is searched for
#: when it last read clear.
CLEAR_WINDOW, CLEAR_STEP = "14d", "5m"
#: now.state.
READ, ELSEWHERE, UNREACHABLE = "read", "elsewhere", "unreachable"
#: Wall-clock seconds; the tests' seam.
clock = time.time


class Unanswered(Exception):
    """Prometheus did not answer a query usably."""


@dataclasses.dataclass(frozen=True)
class Sample:
    """One series of an instant answer: its latest value, and the Unix time
    that sample was taken (None when the raw window did not hold it)."""

    name: str
    labels: dict
    value: float
    at: float | None


@dataclasses.dataclass(frozen=True)
class Known:
    """What the house file and the configuration it generates say."""

    house: House
    device: dict
    conf: object
    mapping: dict
    signals: list
    manifest: dict


def known(house: House, ident: str) -> Known:
    data = house.data
    conf = render.build(copy.deepcopy(data))
    mapping = metrics.exporter_map(copy.deepcopy(data), conf)
    found, _ = metrics.probes(data, conf)
    device = house.by_id[ident]
    return Known(house, device, conf, mapping, metrics.signals(device, conf, found),
                 remote_diagnostics.manifest(House(copy.deepcopy(data)), conf))


def series(metric: str, labels) -> dict:
    return {"metric": metric, "labels": dict(labels),
            "meaning": MEANING.get(metric) or exporter.FAMILIES[metric][1]}


def paths(facts: Known) -> list[dict]:
    """Each room's source whose path uses the device, and as what."""
    ident = facts.device["id"]
    routed = (facts.house.data.get("routing") or {}).get("rooms") or {}
    out = []
    for entry in facts.mapping["paths"].values():
        zone = entry["zone"]
        for source, used in entry["sources"].items():
            if ident not in used:
                continue
            path = facts.manifest["rooms"][zone]["sources"][source]
            nodes = [node for node in [*path["nodes"], *path.get("audioNodes", [])] if node["device"] == ident]
            # The device a room's Volume and Mute drive is in use with every
            # source, though no signal passes through it (health_paths()).
            if not nodes and (routed.get(zone) or {}).get("audio") == ident:
                nodes = [{"role": "volume"}]
            for node in nodes:
                out.append({"zone": zone, "room": entry["label"], "source": source,
                            "source_label": facts.mapping["sources"].get(source) or path.get("label") or source,
                            "role": node["role"], **{key: node[key] for key in ("input", "output") if key in node}})
    return out


def what_it_is(facts: Known) -> dict:
    device, ident = facts.device, facts.device["id"]
    controls = device.get("controls") or {}
    kept = facts.mapping["items"]
    return {
        "label": facts.house.label(device),
        "room": {"id": device["room"], "label": facts.house.room_label(device["room"])},
        "kind": device["kind"],
        "address": device.get("ip"),
        "via": device.get("via"),
        "monitor": device.get("monitor"),
        "observe": list(controls.get("observe") or []),
        "control": list(controls.get("control") or []),
        "things": [{"uid": uid, "file": path, "label": facts.conf.yaml[path].things[uid].get("label")}
                   for uid, path in metrics.device_things(facts.conf, ident)],
        "items": [{"name": name, "type": str(item.get("type")), "label": item.get("label"),
                   "kept": (kept.get(name) or {}).get("point")} for name, item in facts.conf.points(ident)],
        "paths": paths(facts),
    }


def what_watches_it(facts: Known) -> dict:
    ident = facts.device["id"]
    mapping = facts.mapping
    by_series = {(signal.metric, signal.labels): signal for signal in facts.signals}
    zones = mapping["paths"]
    items = dict(facts.conf.points(ident))
    checks = []
    for check in mapping["health_checks"]:
        if check["device"] != ident:
            continue
        signal = by_series.get((check["metric"], tuple(check["labels"].items())))
        name = check["labels"].get("item", "")
        needed = []
        for demand in check["required"]:
            if demand.get("playing"):
                needed.append({"playing": demand["item"], "within": alerts.IN_USE})
            else:
                zone = zones[demand["item"]]
                needed.append({"zone": zone["zone"], "room": zone["label"], "source": demand["source"],
                               "source_label": mapping["sources"].get(demand["source"]) or demand["source"]})
        checks.append({
            "check": check["check"], "fails_as": alerts.CHECK_WORDS[check["check"]],
            "what": metrics.signal_words(signal)[0] if signal else f"{(items.get(name) or {}).get('label') or name} "
                                                                    f"({name})",
            "series": series(check["metric"], check["labels"]),
            "always": check["always"], "optional": check["optional"], "needed": needed})
    problem = _commands.problem_item(ident)
    return {
        "signals": [{"kind": signal.kind, "what": metrics.signal_words(signal)[0],
                     "through": list(signal.integrations), "check": signal.control,
                     "series": series(signal.metric, signal.labels)} for signal in facts.signals],
        "checks": checks,
        "notes": [metrics.unobserved_text(*found)
                  for found in metrics.unobserved(facts.house.data, facts.conf) if found[0] == ident],
        "problem_item": problem if (mapping["items"].get(problem) or {}).get("point") == "problem" else None,
        "tallies": sorted(entry["integration"] for entry in (mapping.get("commands") or {}).values()
                          if entry["device"] == ident),
    }


# ---------------------------------------------------------------- what holds now


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask(get, base: str, expr: str, at: float, timeout: float) -> tuple[str, list]:
    """(result type, result) of one instant query of Prometheus."""
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": expr, "time": f"{at:.3f}"})
    code, body = get(url, timeout)
    if code != 200:
        raise Unanswered(f"HTTP {code}" if code else "no answer")
    try:
        answer = json.loads(body)
        if answer["status"] != "success":
            raise ValueError
        return answer["data"]["resultType"], list(answer["data"]["result"])
    except (ValueError, KeyError, TypeError):
        raise Unanswered("an answer that is not Prometheus'") from None


def _key(labels: dict) -> tuple:
    return tuple(sorted(labels.items()))


def elsewhere(facts: Known) -> list[str]:
    """Selectors of the readings of the device that Prometheus keeps under
    another device's name: the Thing an integration sends its commands
    through is named after that device (oh_metrics.signals())."""
    ident = facts.device["id"]
    return [f"{name}{{thing={_q(uid)}}}" for signal in facts.signals if signal.kind == "thing"
            for uid in [dict(signal.labels)["thing"]] if metrics.thing_device(uid) != ident
            for name in (signal.metric, "thermite_thing_status_info")]


def own(ident: str) -> str:
    """The selector of every series that names the device."""
    return f"{{device={_q(ident)}}}"


def everything(ident: str, more: list[str]) -> str:
    """The one question for what exists now: every series that names the
    device, those of `more`, and the runtime's own check."""
    return " or ".join((own(ident), *more, f'{alerts.CHECK}{{check="runtime",room={_q(exporter.HOUSE_ROOM)}}}'))


def clear_question(ident: str) -> str:
    """When the device's problem gauge last read clear, to CLEAR_STEP,
    within CLEAR_WINDOW: the latest of every series it had there, since a
    device whose room or kind changed has more than one."""
    return (f"max(max_over_time(timestamp(thermite_device_problem{{device={_q(ident)}}} == 0)"
            f"[{CLEAR_WINDOW}:{CLEAR_STEP}]))")


def samples(get, base: str, ident: str, at: float, timeout: float, more: list[str]) -> list[Sample]:
    """The latest sample of everything() and when each was taken.

    The instant answer decides what exists now (a series Prometheus has
    marked stale is gone from it); the raw window gives each sample's own
    time, which an instant answer replaces with the query's."""
    try:
        kind, vector = ask(get, base, everything(ident, more), at, timeout)
        matrix = []
        for selector in (own(ident), *more):
            raw, rows = ask(get, base, f"{selector}[{LOOKBACK}]", at, timeout)
            if raw != "matrix":
                raise ValueError
            matrix += rows
        if kind != "vector":
            raise ValueError
        taken = {_key(row["metric"]): float(row["values"][-1][0]) for row in matrix}
        out = []
        for row in vector:
            labels = dict(row["metric"])
            if not all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items()):
                raise ValueError
            out.append(Sample(labels.pop("__name__", ""), labels, float(row["value"][1]),
                              taken.get(_key(row["metric"]))))
        return out
    except (ValueError, KeyError, TypeError, IndexError):
        raise Unanswered("an answer that is not Prometheus'") from None


def last_clear(get, base: str, ident: str, at: float, timeout: float) -> float | None:
    """clear_question()'s answer; None when the gauge did not read clear in
    the window, or that cannot be read."""
    try:
        kind, vector = ask(get, base, clear_question(ident), at, timeout)
        return float(vector[0]["value"][1]) if kind == "vector" and vector else None
    except (Unanswered, ValueError, KeyError, TypeError, IndexError):
        return None


def find(read: list[Sample], name: str, **labels: str) -> list[Sample]:
    return [sample for sample in read
            if sample.name == name and all(sample.labels.get(key) == value for key, value in labels.items())]


def _age(sample: Sample | None, at: float) -> int | None:
    return None if sample is None or sample.at is None else max(0, round(at - sample.at))


def _number(value: float) -> str:
    return f"{value:g}"


def what_holds(facts: Known, read: list[Sample], at: float, cleared: float | None) -> dict:
    """The device's part of what Prometheus has, in the order of what
    watches it. Nothing is concluded here: each state is a series' value
    in its owner's words, and a series that is not there is unknown."""
    ident = facts.device["id"]
    mapping = facts.mapping
    runtime = find(read, alerts.CHECK, check="runtime", room=exporter.HOUSE_ROOM)
    out: dict = {
        "runtime": CHECK_STATES.get(runtime[0].value, UNKNOWN) if runtime else None,
        "deployed": bool(find(read, "thermite_device_info", device=ident)),
        "signals": [], "readings": [], "checks": [], "alerts": [], "commands": {}}
    taken = set()
    for signal in facts.signals:
        what, well, bad = metrics.signal_words(signal)
        found = find(read, signal.metric, **dict(signal.labels))
        taken.add((signal.metric, dict(signal.labels).get("item")))
        row = {"kind": signal.kind, "what": what, "answers": None, "words": None, "age": None}
        if found:
            row.update(answers=found[0].value != signal.bad, age=_age(found[0], at))
            row["words"] = well if row["answers"] else bad
            if signal.kind == "thing":
                status = find(read, "thermite_thing_status_info", **dict(signal.labels))
                row["status"] = status[0].labels.get("status") if status else None
        out["signals"].append(row)
    labels = dict(facts.conf.points(ident))
    for name, spec in mapping["items"].items():
        if spec.get("device") != ident or spec["point"] == "problem" or ("thermite_item_value", name) in taken:
            continue
        row = {"item": name, "label": (labels.get(name) or {}).get("label"), "point": spec["point"],
               "state": UNKNOWN, "value": None, "age": None}
        value, info = find(read, "thermite_item_value", item=name), find(read, "thermite_item_info", item=name)
        if value:
            number = value[0].value
            row.update(state=READ, age=_age(value[0], at),
                       value={"switch": {1.0: "ON", 0.0: "OFF"}, "playing": PLAYING}.get(spec["convert"], {}).get(
                           number, _number(number)))
        elif info:
            row.update(state=READ, age=_age(info[0], at), value=info[0].labels.get("value"))
        elif any(sample.value == 1 for sample in find(read, "thermite_item_device_off", item=name)):
            row["state"] = "off"
        out["readings"].append(row)
    rooms, zones = mapping["rooms"], {entry["zone"]: entry["label"] for entry in mapping["paths"].values()}
    results = sorted(find(read, alerts.CHECK, device=ident),
                     key=lambda sample: (sample.labels.get("check", ""), sample.labels.get("zone", ""),
                                         sample.labels.get("room", "")))
    for sample in results:
        check, room, zone = (sample.labels.get(key, "") for key in ("check", "room", "zone"))
        out["checks"].append({
            "check": check, "fails_as": alerts.CHECK_WORDS.get(check), "room": rooms.get(room, room) or None,
            "zone": zones.get(zone, zone) or None, "state": CHECK_STATES.get(sample.value, UNKNOWN),
            "needed": sample.labels.get("required") == "true", "result": True, "age": _age(sample, at)})
    reported = {sample.labels.get("check") for sample in results}
    for name in dict.fromkeys(check["check"] for check in mapping["health_checks"] if check["device"] == ident):
        if name not in reported:
            # A check the house file gives it and Prometheus has no result for.
            out["checks"].append({"check": name, "fails_as": alerts.CHECK_WORDS[name], "room": None, "zone": None,
                                  "state": UNKNOWN, "needed": None, "result": False, "age": None})
    for sample in sorted(find(read, "ALERTS", device=ident),
                         key=lambda sample: (sample.labels.get("alertstate") != "firing",
                                             sample.labels.get("alertname", ""), _key(sample.labels))):
        name = sample.labels.get("alertname", "")
        title, text = alerts.CATALOG.get(name, (name, ""))
        place = {key: value for key, value in sample.labels.items() if key != "alertstate"}
        since = [each.value for each in find(read, "ALERTS_FOR_STATE", alertname=name) if each.labels == place]
        check, room, zone = (sample.labels.get(key, "") for key in ("check", "room", "zone"))
        out["alerts"].append({
            "name": name, "state": sample.labels.get("alertstate"), "severity": alerts.severity(name),
            "title": title, "text": text, "check": check or None, "check_text": alerts.CHECK_WORDS.get(check),
            "room": rooms.get(room, room) or None, "zone": zones.get(zone, zone) or None,
            "since": int(since[0]) if since else None})
    gauge = find(read, "thermite_device_problem", device=ident)
    if gauge:
        code = find(read, "thermite_device_problem_info", device=ident)
        holds = gauge[0].value == 1
        named = code[0].labels.get("code") if holds and code else None
        out["problem"] = {"holds": holds, "code": named, "words": _commands.CODES.get(named),
                          "clear_at": int(cleared) if holds and cleared else None,
                          "window": CLEAR_WINDOW if holds else None, "age": _age(gauge[0], at)}
    else:
        out["problem"] = None
    for sample in find(read, "thermite_device_commands_total", device=ident):
        if sample.value:
            out["commands"].setdefault(sample.labels.get("integration", ""), {})[
                sample.labels.get("outcome", "")] = int(sample.value)
    quarantined = find(read, "thermite_routing_device_quarantined", device=ident)
    out["quarantined"] = bool(quarantined[0].value) if quarantined else None
    failed = find(read, "thermite_routing_endpoint_failures_total", device=ident)
    out["routing_failures"] = ({sample.labels.get("reason", ""): int(sample.value) for sample in failed
                                if sample.value} if failed else None)
    return out


def installation() -> tuple[dict | None, bool]:
    """(this site's host facts when they can be read, whether this host
    has the monitoring services): the installation's Prometheus is asked
    only where it is installed."""
    try:
        host = deploy.load_host(oh_cli.AREA / "instance/host.yaml")
    except (deploy.DeployError, OSError, ValueError):
        return None, False
    return host, deploy.metrics_installed()


def now(facts: Known, host: dict | None, installed: bool, timeout: float, *, get=None) -> dict:
    """What holds now, from the installation's Prometheus on loopback: the
    reader thermite health --production uses (oh_deploy.http_get), GET only
    and with no credentials."""
    ident = facts.device["id"]
    if host is None or not installed:
        return {"state": ELSEWHERE, "text": (
            "This host does not run the house's monitoring, so what holds now is not known here. "
            f"Run thermite device {ident} on the host the house is installed on.")}
    base, at = deploy.prometheus_url(host), clock()
    get = get or deploy.http_get
    try:
        read = samples(get, base, ident, at, timeout, elsewhere(facts))
    except Unanswered as error:
        return {"state": UNREACHABLE, "url": base, "text": (
            f"Prometheus on this host did not answer ({error}), so what holds now is unknown: no reading, "
            "check or alert can be shown, and none of them is known to be fine. thermite health --production "
            "says what the monitoring services are doing.")}
    cleared = None
    if any(sample.value == 1 for sample in find(read, "thermite_device_problem", device=ident)):
        cleared = last_clear(get, base, ident, at, timeout)
    return {"state": READ, "url": base, "at": int(at), **what_holds(facts, read, at, cleared)}


# ---------------------------------------------------------------- where to look next


def look_next(facts: Known, host: dict | None, here: bool) -> list[dict]:
    """The commands and pages for more, with this device filled in. A page's
    address is given only on the installation's own host, which knows it."""
    ident = facts.device["id"]
    out = []
    if (facts.mapping.get("commands") or {}) and any(
            entry["device"] == ident for entry in facts.mapping["commands"].values()):
        out.append({"what": "What became of each command its rules took",
                    "run": f"journalctl -u {oh_units.RUNTIME} -g 'command device={ident} '"})
    if ident in ((facts.mapping.get("routing") or {}).get("executors") or ()):
        out.append({"what": "Each step the routing executor sent it for a room's source choice",
                    "run": f"journalctl -u {oh_units.RUNTIME} -g 'routing ticket .*resource={ident}_'"})
    if host is not None:
        out.append({"what": "Every command to its Items and every change of their states",
                    "run": f"grep -h '{ident}_' {deploy.state(host).logs / 'events.log'}"})
    if here and host is not None:
        address = deploy.lan_address() or deploy.hostname()
        out.append({"what": "Its row on the Devices board, and its room's history",
                    "open": f"{deploy.grafana_url(host, address)}d/{dashboards.UIDS[dashboards.DEVICES]}"
                            f"?var-{dashboards.ROOM}={facts.device['room']}"})
        # Its own Things, and one its commands go through under another's name.
        things = dict.fromkeys([*(uid for uid, _ in metrics.device_things(facts.conf, ident)),
                                *(dict(signal.labels)["thing"] for signal in facts.signals if signal.kind == "thing")])
        out += [{"what": f"Thing {uid} on the runtime's own pages, with its reason when it is not online",
                 "open": f"{deploy.production_url(host, address)}/settings/things/{urllib.parse.quote(uid, safe=':')}"}
                for uid in things]
    out.append({"what": "Every Thing's status in the runtime, with its reason",
                "run": "thermite health --production"})
    if any(entry["id"] == ident for entry in reboots.table(facts.house)["devices"]):
        out.append({"what": "Restart it as the nightly reboots do", "run": f"thermite reboot {ident}"})
    out.append({"what": "What the log lines' fields mean", "read": "docs/installation.md, Trace a command"})
    return out


def report(house: House, ident: str, host: dict | None, installed: bool, timeout: float, *, get=None) -> dict:
    facts = known(house, ident)
    return {"id": ident, "is": what_it_is(facts), "watched": what_watches_it(facts),
            "now": now(facts, host, installed, timeout, get=get), "next": look_next(facts, host, installed)}


def status(found: dict) -> int:
    """The exit status: read from the checks' and alerts' own states."""
    held = found["now"]
    if held["state"] == UNREACHABLE:
        return 2
    if held["state"] != READ:
        return 0
    failing = any(check["state"] == FAILING and check["needed"] for check in held["checks"])
    firing = any(alert["state"] == "firing" for alert in held["alerts"])
    return 1 if failing or firing or held["runtime"] is None else 0


# ---------------------------------------------------------------- as text


def _ago(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _when(moment: int | None) -> str:
    return f"{datetime.datetime.fromtimestamp(moment).astimezone():%Y-%m-%d %H:%M}" if moment else ""


def _or(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _table(rows: list[tuple], indent: str = "    ") -> list[str]:
    """Rows in columns as wide as their widest cell; empty trailing cells dropped."""
    rows = [tuple(str(cell) for cell in row) for row in rows]
    widths = [max(len(row[index]) for row in rows if index < len(row)) for index in range(max(map(len, rows)))]
    return [(indent + "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))).rstrip() for row in rows]


def _wrapped(text: str, indent: str) -> list[str]:
    return textwrap.wrap(text, width=96, initial_indent=indent, subsequent_indent=indent)


def _needed(check: dict) -> str:
    if check["optional"]:
        return "shown, never needed (monitor: optional)"
    if check["always"]:
        return "needed always: routing depends on it"
    rooms: dict[str, list[str]] = {}
    playing = None
    for demand in check["needed"]:
        if "playing" in demand:
            playing = demand["within"]
        else:
            rooms.setdefault(demand["room"], []).append(demand["source_label"])
    said = [f"while {room} uses {_or(sources)}" for room, sources in rooms.items()]
    if playing:
        said.append(f"while it plays, or did in the last {playing}")
    return "needed " + "; ".join(said) if said else "never needed: no room's path uses it"


def text(found: dict) -> str:
    """The report for a person: only what `found` holds."""
    what, watched, held = found["is"], found["watched"], found["now"]
    lines = [f"{what['label']} ({found['id']})",
             f"  {what['kind']} in {what['room']['label']}"
             + (f", at {what['address']}" if what["address"] else ", with no address")
             + (f", reached through {what['via']}" if what["via"] else "")]
    if what["monitor"]:
        lines.append(f"  monitor: {what['monitor']} (shown, and kept out of control health and alerts)")
    lines.append("  observed through " + (", ".join(what["observe"]) or "nothing")
                 + "; commanded through " + (", ".join(what["control"]) or "nothing"))
    lines += ["", "On rooms' paths"]
    if what["paths"]:
        places: dict[tuple, list[str]] = {}
        for use in what["paths"]:
            port = ", ".join(f"{key} {use[key]}" for key in ("input", "output") if key in use)
            places.setdefault((use["room"], use["role"]), []).append(
                use["source_label"] + (f" ({port})" if port else ""))
        lines += [f"  {room}: {role} for {', '.join(sources)}" for (room, role), sources in places.items()]
    else:
        lines.append("  none: no room's source passes through it")
    lines += ["", f"Things generated for it ({len(what['things'])})"]
    if what["things"]:
        lines += _table([(thing["uid"], thing["label"] or "", f"conf/{thing['file']}") for thing in what["things"]],
                        "  ")
    own_things = {thing["uid"] for thing in what["things"]}
    lines += [f"  Its commands go through {uid}, a Thing named after another device."
              for uid in dict.fromkeys(signal["series"]["labels"]["thing"] for signal in watched["signals"]
                                       if signal["kind"] == "thing") if uid not in own_things]
    lines += ["", f"Items generated for it ({len(what['items'])}; kept: what the dashboards and checks keep of it)"]
    if what["items"]:
        lines += _table([(item["name"], item["type"], item["label"] or "",
                          f"kept as {item['kept']}" if item["kept"] else "") for item in what["items"]], "  ")

    lines += ["", "What watches it", "  Readings that say whether it answers"]
    if watched["signals"]:
        lines += _table([(signal["what"], "through " + ", ".join(signal["through"]),
                          "a check is built on it" if signal["check"] else "a reading only")
                         for signal in watched["signals"]])
    else:
        lines.append("    none")
    lines.append("  Health checks (an alert follows one a room needs)")
    if watched["checks"]:
        for check in watched["checks"]:
            lines.append(f"    {check['check']}: {check['what']}. Failing, it is shown as \"{check['fails_as']}\".")
            lines += _wrapped(_needed(check), "      ")
    else:
        lines.append("    none: nothing alerts when this device stops answering")
    for note in watched["notes"]:
        lines += _wrapped("Note: " + note, "  ")
    lines += _wrapped("Its problem Item " + (
        f"({watched['problem_item']}) holds what its rules last met: a read's problem now, else a command's, "
        "which stays until the device takes a command and so may be old. No check reads it."
        if watched["problem_item"] else "does not exist: no rule of thermite's commands it."), "  ")
    if watched["tallies"]:
        lines.append("  Commands are counted by the rules of " + ", ".join(watched["tallies"]) + ".")

    lines.append("")
    if held["state"] != READ:
        lines += ["What holds now", *_wrapped(held["text"], "  ")]
    else:
        lines.append(f"What holds now (Prometheus on this host, {_when(held['at'])}; "
                     "each age is since that reading was taken)")
        if held["runtime"] is None:
            lines += _wrapped("Prometheus has no check results at all: its rules are not loaded or are not being "
                              "evaluated. Every check and alert below is unknown, not fine.", "  ")
        elif held["runtime"] != OK:
            lines += _wrapped(f"The exporter's reading of the runtime is {held['runtime']} (the house-wide runtime "
                              "check), so readings below may be missing for that reason.", "  ")
        if not held["deployed"]:
            lines += _wrapped("The running installation reports no such device: this house file has it and what "
                              "is deployed may not yet (thermite status compares them).", "  ")
        lines.append("  Readings")
        rows = []
        for signal in held["signals"]:
            words = signal["words"] or "no reading: unknown"
            if signal.get("status") and not signal["answers"]:
                words += f" ({signal['status']})"
            rows.append((signal["what"], words, _ago(signal["age"])))
        for reading in held["readings"]:
            value = {READ: reading["value"], "off": "none: the device is off"}.get(reading["state"],
                                                                                 "no reading: unknown")
            rows.append((f"{reading['label'] or reading['point']} ({reading['item']})", value, _ago(reading["age"])))
        lines += _table(rows) if rows else ["    none"]
        lines.append("  Checks")
        rows = []
        always = {check["check"] for check in watched["checks"] if check["always"]}
        for check in held["checks"]:
            where = " and ".join(dict.fromkeys(filter(None, (check["zone"], check["room"]))))
            where = "" if check["check"] in always or not where else f" by {where}"
            needed = ("no result in Prometheus: the deployed rules do not have it, or are not evaluated"
                      if not check["result"] else
                      f"needed now{where}" if check["needed"] else f"not needed now{where}")
            if check["state"] == FAILING and check["needed"]:
                # A check the deployed rules have and this checkout does not has no words here.
                state = "FAILING" + (f" ({check['fails_as']})" if check["fails_as"] else "")
            elif check["state"] == FAILING:
                # As House health has it: a sleeping device no room uses is never a failure.
                state, needed = "failing", needed + ", so no failure"
            else:
                state = "OK" if check["state"] == OK else "unknown"
            rows.append((check["check"], state, needed, _ago(check["age"])))
        lines += _table(rows) if rows else ["    none"]
        lines.append("  Alerts about it")
        if not held["alerts"]:
            lines.append("    none firing or pending" if held["runtime"] is not None else "    unknown")
        for alert in held["alerts"]:
            place = ", ".join(filter(None, (alert["check_text"], alert["zone"] or alert["room"])))
            lines.append(f"    {alert['state'].upper()} ({alert['severity']})"
                         + (f" since {_when(alert['since'])}" if alert["since"] else "")
                         + f": {alert['title']}" + (f" ({place})" if place else ""))
            lines += _wrapped(alert["text"], "      ")
        lines.append("  Problem")
        problem = held["problem"]
        if problem is None:
            lines.append("    no reading: unknown" if watched["problem_item"] else "    it has no problem Item")
        elif not problem["holds"]:
            lines.append(f"    none ({_ago(problem['age'])})")
        else:
            said = (f"{what['label']} {problem['words']} (code {problem['code']})" if problem["words"] else
                    "it holds a problem code this installation's exporter does not name yet")
            since = (f"it last read clear {_when(problem['clear_at'])}" if problem["clear_at"] else
                     f"it has not read clear in the last {problem['window']}")
            lines += _wrapped(f"{said}; {since}. A command's problem stays until the device takes a command, so "
                              "this may be old: the readings and checks above say what holds now.", "    ")
        lines.append("  Commands its rules took, since each rule last loaded")
        if held["commands"]:
            lines += _table([(integration, ", ".join(f"{outcome} {count}" for outcome, count in sorted(counts.items())))
                             for integration, counts in sorted(held["commands"].items())])
        else:
            lines.append("    none counted" if watched["tallies"] else "    no rule counts commands for it")
        if held["routing_failures"] is not None:
            lines += ["  Routing operations that failed at it, by reason, since the routing script last loaded",
                      "    " + (", ".join(f"{reason.replace('_', ' ')} {count}"
                                          for reason, count in sorted(held["routing_failures"].items())) or "none")]
        if held["quarantined"]:
            lines.append("  Routing has quarantined its commands after an uncertain outcome.")

    lines += ["", "Where to look next" + ("" if held["state"] != ELSEWHERE else
                                         " (the commands run on the host the house is installed on)")]
    for entry in found["next"]:
        lines.append(f"  {entry['what']}:")
        lines.append("    " + (entry.get("run") or entry.get("open") or entry.get("read")))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("device", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("device", metavar="ID", help="the device's id in the house file")
    oh_cli.add_house(parser, render.INVENTORY)
    oh_cli.add_timeout(parser, 5, "each answer of Prometheus")
    oh_cli.add_json(parser)
    args = parser.parse_args(argv)

    house = House.load(args.house)
    if args.device not in house.by_id:
        print(f"thermite device: {args.device} is not a device id in {args.house}"
              + (inventory.hint(args.device, house.by_id) or f"; its devices are {', '.join(house.by_id) or 'none'}"),
              file=sys.stderr)
        return 2
    host, installed = installation()
    found = report(house, args.device, host, installed, args.timeout)
    if args.json:
        oh_cli.emit(found)
    else:
        sys.stdout.write(text(found))
    return status(found)
