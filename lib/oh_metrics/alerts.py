"""Actionable alerts, which the thermite app's Home shows; no sleeping-device alarms.

Control health is recorded per check: thermite:check{room, zone, device,
check, required, scope} is 1 failing, 0 OK or -1 unknown, for

    runtime   the exporter reads openHAB (room house)
    scrape    a core Prometheus job answers (room house; device is the job)
    service   a local HTTP service answers (room house; device is the service)
    systemd   node_exporter reads systemd's unit states (room house)
    source    a routing zone's source readback is known (device empty)
    thing     a device's control Thing is ONLINE
    port      a device's control port accepts connections
    reads     a device's own rule can read it: it answers and is paired
    power     a device on the active path is on

`zone` is the routing zone's id, as in thermite_route_seconds and
thermite_zone_info; `room` is the room the check is for: the zone's room
for a zone's checks, the device's own room for infrastructure, and house
for the house-wide ones. A device's room may differ from the room using
it, so device facts (thermite_device_info) join on(device) alone.

Device checks come from the exporter map's health_checks: routing
infrastructure always, every other device only on a zone's active path
(required="true" while the zone's source uses it, "false" otherwise, and
always "false" for a device marked `monitor: optional`). Missing telemetry
reads -1, never 0.

A zone's source is thermite:zone_source: its readback while that is known,
else what the readback last said, for HELD at most. A device that stops
answering takes its zone's readback with it, so demand read from the
readback alone would end with the very failure it is for. A zone that
reports Off releases its devices at once, and one whose readback has not
been known within HELD (a fresh start) requires none. The zone's own
source check is required the same way: while its source, as held, is a
source and not Off.

`scope` is control for what controlling a room depends on and monitoring
for what only watches it: every scrape, node_exporter's reading of systemd
and Grafana. A monitoring check never counts as control health or alerts as
a room: a failed scrape is ThermiteScrapeFailed's and the others are
ThermiteMonitoringBroken's.

ThermiteCheckFailing and ThermiteControlUnknown fire per required control
check, so each carries its room, zone, device and check. No alert reads
thermite:control_health, which folds the required control checks into the
House board's one number: 1 when one fails, else -1 when one is unknown (or
the series that always exist are incomplete), else 0.

Severity says how much an alert matters: a critical one means a room cannot
be controlled as asked, or soon will not be; a warning can wait. Nothing is
sent anywhere. The gateway reads the firing alerts from Prometheus and the
thermite app's Home shows them, critical ones first (oh_identity.alerts),
holding back what INHIBITS and ROOM_INHIBITS say another alert explains.
"""
from __future__ import annotations

import re

import oh_units

from .exporter import HOUSE_ROOM

#: The Prometheus jobs and local HTTP services with a house-wide check.
CORE_JOBS = ("openhab", "thermite", "node", "prometheus", "blackbox")
SERVICES = ("openhab", "apple_tv_helper", "grafana")
#: The services that only watch the house: control works without them.
MONITORING_SERVICES = ("grafana",)
#: A check's scope (the module docstring).
CONTROL, MONITORING = "control", "monitoring"
GATEWAY_JOB = "gateway_https"
#: Whether this host has the gateway (its unit, as node_exporter sees it).
#: prometheus.yml probes it in every house: whether it is installed is the
#: host's state, which generated files never depend on.
GATEWAY_INSTALLED = f'count(node_systemd_unit_state{{job="node",name="{oh_units.ACCESS}"}}) > 0'
#: Plain words for each check, as dashboards show a failing one.
CHECK_WORDS = {
    "runtime": "Runtime not answering",
    "scrape": "Monitoring target down",
    "service": "Service not answering",
    "systemd": "Unit states unreadable",
    "source": "Source not reported",
    "thing": "Not responding to control",
    "port": "Control port closed",
    "reads": "Cannot be read",
    "power": "Off while in use",
}
CHECK = "thermite:check"
HEALTH = "thermite:control_health"
#: Each routing zone's source, held while its readback is unknown; its
#: value is when the readback last said so.
ZONE_SOURCE = "thermite:zone_source"
#: How long a zone keeps the source its readback last gave. Longer than a
#: failure usually stays unrepaired (a day and a half is not rare), or the
#: alert for a device still broken would resolve by itself; bounded, so a
#: room nobody came back to stops counting as in use.
HELD = "3d"
#: The source value of a zone that is Off (oh_features.routing.OFF).
OFF = "off"
#: Seconds after which a metrics textfile counts as no longer rewritten.
#: Its service rewrites it every 10 (oh_access.metrics.WRITE_SECONDS) and
#: node_exporter is scraped every 30, so a healthy one reads 40 old at most
#: and one whose service is restarting a little over two minutes.
TEXTFILE_STALE = 120
#: A browser opens at most six HTTP/1.1 connections to one origin. The
#: gateway admits one peer at least that many event streams
#: (oh_identity/streams.py PEER_STREAMS), or ThermiteRemoteStarved could
#: never fire: oh_identity/tests/test_stream_limits.py holds the two together.
HTTP1_CONNECTIONS = 6
#: The client events (oh_access.metrics.CLIENT_KINDS) that are a remote
#: giving up on the gateway, and those that are its event stream failing:
#: an SSE drop (as at every gateway restart) or refusal is not giving up.
TIMEOUT_KINDS = ("policy_timeout", "locks_timeout", "command_timeout", "snapshot_timeout")
STREAM_KINDS = ("stream_error", "stream_refused")
#: Thing statuses openHAB leaves once it has started a Thing; one that stays
#: in them never started: its binding failed or its settings are wrong. A
#: device asleep is OFFLINE instead.
STARTING = ("UNINITIALIZED", "INITIALIZING", "UNKNOWN")
#: Unallocated btrfs space below which a new metadata chunk may not fit.
BTRFS_UNALLOCATED = 5 * 2**30
#: Filesystem types excluded from host disk-space alerts and tiles (virtual,
#: read-only image, EFI boot, FUSE and network mounts).
IGNORED_FSTYPES = r"tmpfs|overlay|squashfs|vfat|fuse\..*|cifs|nfs.*"
#: Days before gateway TLS certificate expiry at which the warning fires.
CERT_EXPIRY_DAYS = 30

# An alert's words are finite: a client is given these (oh_identity.alerts),
# never a label or an annotation of Prometheus', so no runtime error body can reach one.
CATALOG = {
    "ThermiteCheckFailing": ("A room's control path needs attention", "A device a room is using (or was using when the room last reported its source), a speaker in use (it played in the last half hour, or was playing when it stopped answering), or routing infrastructure, has failed a control check for five minutes. House health lists the room, device and check under Needs attention now."),
    "ThermiteControlUnknown": ("A room in use cannot be read", "For ten minutes a control check that a room in use, a speaker in use or routing infrastructure needs has had no reading: most often the room's source, when one of its devices stopped reporting. A room counts as in use for three days after it last reported a source; one that reports Off does not. House health lists the room, device and check under Needs attention now."),
    "ThermiteScrapeFailed": ("Monitoring target is unreachable", "Open Prometheus Targets and check the failed local monitoring service."),
    "ThermiteRuntimeUnavailable": ("Runtime API is unavailable", "The metrics exporter cannot read the runtime. Check the runtime service and its API credential."),
    "ThermiteHostDiskLow": ("Host storage is nearly full", "A writable host filesystem has less than ten percent free. Inspect disk use before removing any data."),
    "ThermiteBtrfsTrouble": ("Host btrfs needs attention", "A btrfs device has reported new I/O or checksum errors, or its unallocated space is nearly gone. Check btrfs device stats and filesystem usage before removing any data."),
    "ThermiteServiceFailed": ("A thermite service failed", "Inspect the failed thermite service and its journal."),
    "ThermiteServiceDown": ("A thermite service is not running", "A long-running thermite service has not been active for three minutes. Inspect it with systemctl status and its journal."),
    "ThermiteServiceFlapping": ("A thermite service keeps restarting", "A thermite service restarted three or more times in thirty minutes. Its journal says why; the Host dashboard shows restarts."),
    "ThermiteGatewayDown": ("The remote gateway is not answering", "The local HTTPS check of the gateway has failed for three minutes. Check thermite-access.service and its journal. If the gateway answers browsers, run thermite deploy so the check has the gateway's trust certificate."),
    "ThermiteGatewayCertExpiring": ("The remote gateway's certificate is expiring", "The remote gateway's TLS certificate expires in less than thirty days. Renew or replace the gateway certificate and reload thermite-access.service before browsers refuse the connection."),
    "ThermiteGatewayFeedDown": ("Remotes are not live", "For five minutes the gateway has been reconnecting to the runtime's event feed, so every remote page shows states as they were. Check the runtime and thermite-access.service's journal."),
    "ThermiteRemoteStarved": ("A remote holds too many HTTP/1.1 streams", "One client holds six or more event streams over HTTP/1.1, the browser's connection limit, so its controls can stall. Close extra remote pages on that device. Switching & remotes shows streams by HTTP version."),
    "ThermiteRemoteTimeouts": ("Remotes are timing out", "Remote pages reported repeated timeouts waiting for the gateway in the last fifteen minutes. Switching & remotes shows which requests and how long they waited."),
    "ThermiteThingStuck": ("A device's Thing never started", "For fifteen minutes openHAB has had a device's Thing neither online nor offline, so its states never update and its commands go nowhere; a device that is asleep goes offline instead. A binding that failed as openHAB started does this, and restarting thermite-openhab.service brings it up; missing or wrong settings keep it so until fixed. Things not online, on the Devices dashboard, lists each one."),
    "ThermiteRoutingTelemetryUnavailable": ("Routing telemetry is unreadable", "For five minutes the routing executor's status or routing's counts in the runtime have not been valid, so the dashboards cannot show switching, device failures or quarantined devices. Check the runtime's log for the routing rule and the routing executor."),
    "ThermiteRoutingQuarantined": ("Routing commands are quarantined", "The routing executor has refused commands to a device for five minutes after an uncertain outcome, and its recovery reads have not succeeded. House health (Needs attention now) and Switching & remotes (Quarantined now) show which device; check that it is powered and on its network."),
    "ThermitePolicyWorkerStalled": ("Room schedules are not being applied", "The room policy worker has not completed a pass for three minutes, so schedules and locks are not enforced. Check thermite-access-worker.service and its journal."),
    "ThermitePolicyUnenforced": ("A room's lock is not being enforced", "For ten minutes a locked room's lock could not be held: its policy cannot be read, it conflicts with another room over shared hardware, openHAB does not answer the worker's reads, or a change on the room's devices has not been put back. A reading that is merely old, from a device that reports only when it changes, does not count. Switching & remotes shows locked rooms by state; check the room's devices and thermite-access-worker.service's journal."),
    "ThermiteHandlerPoolSaturated": ("Device dispatch is saturated", "Device dispatch has no idle workers and a ready probe is overdue. Inspect blocked binding connections."),
    "ThermiteHandlerTelemetryUnavailable": ("Device dispatch telemetry is unavailable", "The native scheduler observation is missing, incomplete or stale. Check the runtime telemetry rule."),
    "ThermiteMonitoringBroken": ("Monitoring is not working fully", "Prometheus failed to evaluate a rule, node_exporter could not read a metrics textfile or systemd's unit states, or Grafana does not answer. Rooms can still be controlled. Check their journals."),
    "ThermiteServiceMetricsStale": ("A service's metrics have stopped", "The gateway, the policy worker or the callback relay is running but has not rewritten its metrics file for five minutes, so the dashboards and the alerts that read it show its last values. Its journal says why; restarting the service starts a new writer. Host shows each file's age under Metrics textfiles age."),
}
#: The alerts of severity critical; every other is a warning.
CRITICAL = frozenset({"ThermiteCheckFailing", "ThermiteRuntimeUnavailable", "ThermiteGatewayDown", "ThermiteGatewayFeedDown",
                      "ThermiteRemoteStarved", "ThermiteServiceFailed", "ThermiteServiceDown", "ThermiteRoutingQuarantined",
                      "ThermitePolicyWorkerStalled", "ThermitePolicyUnenforced", "ThermiteHandlerPoolSaturated",
                      "ThermiteThingStuck"})
#: The labels by which an alert names what it is about, for the alerts whose
#: series carry them; every other alert is about the house as a whole. The
#: gateway's feed (oh_identity.alerts) reads these labels and no others.
ABOUT = {
    "ThermiteCheckFailing": ("room", "zone", "device", "check"),
    "ThermiteControlUnknown": ("room", "zone", "device", "check"),
    "ThermiteThingStuck": ("room", "device"),
    "ThermitePolicyUnenforced": ("zone",),
}
#: (source, targets): while the source fires, the targets would only repeat it.
INHIBITS = (
    ("ThermiteRuntimeUnavailable", ("ThermiteCheckFailing", "ThermiteControlUnknown", "ThermiteHandlerPoolSaturated",
                                    "ThermiteHandlerTelemetryUnavailable", "ThermiteRoutingQuarantined",
                                    "ThermiteRoutingTelemetryUnavailable", "ThermitePolicyUnenforced",
                                    "ThermiteGatewayFeedDown", "ThermiteThingStuck")),
    ("ThermiteGatewayDown", ("ThermiteGatewayFeedDown", "ThermiteRemoteStarved", "ThermiteRemoteTimeouts")),
    # A stalled worker's last textfile keeps its last states.
    ("ThermitePolicyWorkerStalled", ("ThermitePolicyUnenforced",)),
)
#: The same between alerts of one room: a device that failed reports
#: nothing either, so its room's failure already says what is unknown.
ROOM_INHIBITS = (
    ("ThermiteCheckFailing", ("ThermiteControlUnknown",)),
)


def severity(name: str) -> str:
    """An alert's severity label, and what the gateway's feed says of it."""
    return "critical" if name in CRITICAL else "warning"


def _q(value: str) -> str:
    """A PromQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _units(names) -> str:
    return "|".join(re.escape(name) for name in names)


def _check_value(selectors: list[str], bad: int) -> str:
    """1 when any series is `bad`, else 0 when all are there, else -1."""
    union = selectors[0] if len(selectors) == 1 else "(" + " or ".join(selectors) + ")"
    failing = f"max({union} == bool {bad})"
    if len(selectors) == 1:
        return f"{failing} or vector(-1)"
    return f"({failing} == 1) or ({failing} and on() (count({union}) == {len(selectors)})) or vector(-1)"


#: How long a speaker counts as in use after it was last read playing, so
#: a pause is still use; one left idle never is.
IN_USE = "30m"
#: How long a speaker that went unread while playing stays in use: its
#: failure lasts until it answers again, as a display's does while its room
#: still has the source, at most this long.
LAST_HEARD = "1d"


def _playing(item: str) -> str:
    """Whether a speaker is in use (its playing Item): it played within
    IN_USE, or was playing when last read, within LAST_HEARD."""
    series = f'thermite_item_value{{item={_q(item)},point="playing"}}'
    return (f"((max(max_over_time({series}[{IN_USE}])) >= 1)"
            f" or (max(last_over_time({series}[{LAST_HEARD}])) >= 1))")


def zone_source() -> str:
    """thermite:zone_source: what each zone's source readback says, and
    while it says nothing, what it said last within HELD. Its value is the
    time of that reading. `other`, a state the zone does not offer, is no
    reading, as for the zone's source check.

    The readback's own history is the memory, so a zone unknown when these
    rules are first loaded is held too. The subquery steps every 2 m (well
    inside Prometheus's 5 m lookback delta), four times cheaper than stepping
    at the 30 s evaluation interval with the same coverage."""
    read = 'max by (item, zone, value) (timestamp(thermite_item_info{point="source",value!="other"}))'
    return f"{read} or on (item) topk by (item) (1, max_over_time(({read})[{HELD}:2m]))"


def _demand(demands: list[dict]) -> str:
    """Whether a zone's source, as held, uses the device (one item per call)."""
    sources = sorted({demand["source"] for demand in demands})
    item = demands[0]["item"]
    return f'(count({ZONE_SOURCE}{{item={_q(item)},value=~{_q("|".join(re.escape(s) for s in sources))}}}) > 0)'


def _in_use(item: str) -> str:
    """Whether a zone's source, as held, is a source: not Off, and not
    unknown since before HELD."""
    return f'(count({ZONE_SOURCE}{{item={_q(item)},value!={_q(OFF)}}}) > 0)'


def _zone_room(mapping: dict, item: str) -> str:
    return str(((mapping.get("items") or {}).get(item) or {}).get("room") or item)


def _zone(mapping: dict, item: str) -> str:
    """The routing zone id of a zone's source Item."""
    return str(((mapping.get("paths") or {}).get(item) or {}).get("zone") or item)


def checks(mapping: dict) -> tuple[list[dict], int]:
    """(thermite:check recording rules, how many required control series always exist)."""
    rules: list[dict] = []
    always_required = 0

    def record(expr: str, room: str, device: str, check: str, required: bool, zone: str = "",
               scope: str = CONTROL) -> None:
        labels = {"room": room, "zone": zone, "device": device, "check": check,
                  "required": "true" if required else "false", "scope": scope}
        rules.append({"record": CHECK, "expr": expr, "labels": {k: v for k, v in labels.items() if v}})

    def by_demand(value: str, used: str, room: str, device: str, check: str, zone: str) -> None:
        """A check required only while `used` holds: one series either way."""
        record(f"({value}) and on() {used}", room, device, check, True, zone)
        record(f"({value}) unless on() {used}", room, device, check, False, zone)

    record("max(thermite_openhab_up == bool 0) or vector(-1)", HOUSE_ROOM, "openhab", "runtime", True)
    for job in CORE_JOBS:
        record(f"max(up{{job={_q(job)}}} == bool 0) or vector(-1)", HOUSE_ROOM, job, "scrape", True, scope=MONITORING)
    for service in SERVICES:
        record(f'max(probe_success{{job="service_http",service={_q(service)}}} == bool 0) or vector(-1)',
               HOUSE_ROOM, service, "service", True, scope=MONITORING if service in MONITORING_SERVICES else CONTROL)
    record('max(node_scrape_collector_success{job="node",collector="systemd"} == bool 0) or vector(-1)',
           HOUSE_ROOM, "node", "systemd", True, scope=MONITORING)
    always_required += 1 + len(SERVICES) - len(MONITORING_SERVICES)
    for item in sorted((mapping.get("paths") or {})):
        by_demand(f'(count(thermite_item_info{{item={_q(item)},value!="other"}}) * 0) or vector(-1)', _in_use(item),
                  _zone_room(mapping, item), "", "source", _zone(mapping, item))
    # A zone's check reads only the series its own demands added: two rooms
    # on different zones of one receiver each check their own zone's power.
    groups: dict[tuple[str, str], dict] = {}
    for check in mapping.get("health_checks") or []:
        device, name = check.get("device", ""), check.get("check", check["metric"])
        group = groups.setdefault((device, name), {"selectors": [], "bad": check["bad"], "always": False,
                                                   "zones": {}, "optional": bool(check.get("optional"))})
        selector = ",".join(f"{key}={_q(value)}" for key, value in check["labels"].items())
        selector = f"{check['metric']}{{{selector}}}"
        group["selectors"].append(selector)
        group["always"] |= bool(check["always"])
        for demand in check["required"]:
            zone = group["zones"].setdefault(demand["item"], {"selectors": [], "demands": []})
            if selector not in zone["selectors"]:
                zone["selectors"].append(selector)
            if demand not in zone["demands"]:
                zone["demands"].append(demand)
    devices = mapping.get("devices") or {}
    for (device, name), group in sorted(groups.items()):
        if group["always"]:
            room = str((devices.get(device) or {}).get("room") or HOUSE_ROOM)
            record(_check_value(group["selectors"], group["bad"]), room, device, name, not group["optional"])
            always_required += not group["optional"]
            continue
        for item, demanded in sorted(group["zones"].items()):
            value = _check_value(demanded["selectors"], group["bad"])
            # A speaker's own demand: its room, no zone, in use while it plays.
            speaker = any(demand.get("playing") for demand in demanded["demands"])
            room, zone = _zone_room(mapping, item), "" if speaker else _zone(mapping, item)
            used = _playing(item) if speaker else _demand(demanded["demands"])
            if group["optional"]:
                record(value, room, device, name, False, zone)
                continue
            by_demand(value, used, room, device, name, zone)
    return rules, always_required


def control_health(required_series: int) -> str:
    """thermite:control_health from the required control checks: the House
    board's one number."""
    required = f'{CHECK}{{required="true",scope="{CONTROL}"}}'
    return (f"clamp_max(count({required} == 1), 1)"
            f" or ((vector(0) unless on() ({required} == -1)) and on() (count({required}) >= {required_series}))"
            " or vector(-1)")


def stale_textfiles() -> str:
    """The access services that run while their metrics textfile is no
    longer rewritten, each with its unit as `name`. A service that is not
    running is ThermiteServiceDown's or ThermiteServiceFailed's, and one
    this host does not have has no unit: the file each leaves behind says
    nothing new. A textfile is named after the unit that writes it."""
    files = _q(".*/(" + _units(unit.removesuffix(".service") for unit in oh_units.ACCESS_UNITS) + ")\\.prom")
    stale = (f'label_replace(time() - node_textfile_mtime_seconds{{job="node"}} > {TEXTFILE_STALE}, '
             f'"name", "$1.service", "file", {files})')
    return f'{stale} and on (name) (node_systemd_unit_state{{job="node",state="active"}} == 1)'


def rules(mapping):
    def alert(name, expr, duration, *, labels=None):
        title, detail = CATALOG[name]
        return {"alert": name, "expr": expr, "for": duration,
                "labels": {"severity": severity(name), **(labels or {})},
                "annotations": {"summary": title, "description": detail}}
    recorded, required_series = checks(mapping)
    held = [{"record": ZONE_SOURCE, "expr": zone_source()}] if mapping.get("paths") else []
    required = f'{CHECK}{{required="true",scope="{CONTROL}"}}'
    daemons = _units(oh_units.DAEMONS)
    timeouts = "|".join(TIMEOUT_KINDS)
    return {"groups": [
        # Sequential within the group: the checks read this evaluation's zone
        # sources, and control_health this evaluation's checks.
        {"name": "thermite_checks", "rules": [*held, *recorded,
                                               {"record": HEALTH, "expr": control_health(required_series)}]},
        {"name": "thermite", "rules": [
            # Per check: each alert carries the check's room, zone, device and check.
            alert("ThermiteCheckFailing", f"{required} == 1", "5m"),
            alert("ThermiteControlUnknown", f"{required} == -1", "10m"),
            # Every job with a scrape check, so each failed one has this alert.
            alert("ThermiteScrapeFailed", f'up{{job=~"{"|".join(CORE_JOBS)}"}} == 0', "3m"),
            alert("ThermiteRuntimeUnavailable", "thermite_openhab_up == 0", "3m"),
            alert("ThermiteHostDiskLow", f'(node_filesystem_avail_bytes{{job="node",fstype!~{_q(IGNORED_FSTYPES)}}} / node_filesystem_size_bytes < 0.10) and (node_filesystem_readonly == 0)', "10m"),
            alert("ThermiteBtrfsTrouble", f'(sum by (uuid) (increase(node_btrfs_device_errors_total{{job="node"}}[1h])) > 0) or (min by (uuid) (node_btrfs_device_unused_bytes{{job="node"}}) < {BTRFS_UNALLOCATED})', "5m"),
            alert("ThermiteServiceFailed", 'node_systemd_unit_state{job="node",name=~"thermite-.*\\\\.service",state="failed"} == 1', "3m"),
            # A failed unit is ThermiteServiceFailed's.
            alert("ThermiteServiceDown", f'(node_systemd_unit_state{{job="node",name=~{_q(daemons)},state="active"}} == 0) unless on(name) (node_systemd_unit_state{{job="node",state="failed"}} == 1)', "3m"),
            alert("ThermiteServiceFlapping", 'increase(node_systemd_service_restart_total{job="node",name=~"thermite-.*"}[30m]) >= 3', "1m"),
            # Only where the gateway's unit exists: a house without one has nothing to probe.
            alert("ThermiteGatewayDown", f'(probe_success{{job="{GATEWAY_JOB}"}} == 0) and on() ({GATEWAY_INSTALLED})', "3m"),
            alert("ThermiteGatewayCertExpiring", f'(probe_ssl_earliest_cert_expiry{{job="{GATEWAY_JOB}"}} - time() < {CERT_EXPIRY_DAYS} * 86400) and (probe_ssl_earliest_cert_expiry{{job="{GATEWAY_JOB}"}} > 0) and on() ({GATEWAY_INSTALLED})', "1h"),
            # The gateway's one subscription to the runtime's events; `closed`
            # is a gateway with no client stream, which needs none.
            alert("ThermiteGatewayFeedDown", f'(thermite_gateway_upstream_state{{state="connecting"}} == 1) and on() ({GATEWAY_INSTALLED})', "5m"),
            alert("ThermiteRemoteStarved", f'thermite_gateway_sse_peer_streams_max{{http_version="1.1"}} >= {HTTP1_CONNECTIONS}', "1m"),
            alert("ThermiteRemoteTimeouts", f'sum(increase(thermite_client_events_total{{kind=~"{timeouts}"}}[15m])) >= 5', "5m"),
            # Per Thing, so each one stuck says so, while another alert fires; optional devices excluded.
            alert("ThermiteThingStuck", f'(thermite_thing_status_info{{status=~"{"|".join(STARTING)}"}} == 1) and on(device) thermite_device_info{{required="true"}}', "15m"),
            alert("ThermiteRoutingQuarantined", "thermite_routing_quarantined > 0", "5m"),
            alert("ThermiteRoutingTelemetryUnavailable",
                  "(thermite_routing_executor_up == 0) or (thermite_routing_metrics_up == 0)", "5m"),
            alert("ThermitePolicyWorkerStalled", f'(time() - thermite_policy_loop_timestamp_seconds > 120) and on() (node_systemd_unit_state{{job="node",name={_q(oh_units.ACCESS_WORKER)},state="active"}} == 1)', "1m"),
            alert("ThermitePolicyUnenforced", 'thermite_policy_room_state{state="unavailable"} == 1', "10m"),
            *([alert("ThermiteHandlerPoolSaturated", '(thermite_handler_pool_active_threads >= thermite_handler_pool_core_threads) and (thermite_handler_pool_probe_overdue_seconds > 30)', "2m"),
               alert("ThermiteHandlerTelemetryUnavailable", 'thermite_handler_pool_up == 0', "5m")]
              if mapping.get("handler_pool") else []),
            # A failed scrape is ThermiteScrapeFailed's.
            alert("ThermiteMonitoringBroken", '(increase(prometheus_rule_evaluation_failures_total[10m]) > 0) or (node_textfile_scrape_error == 1)'
                  f' or ({CHECK}{{scope="{MONITORING}",check!="scrape"}} == 1)', "5m"),
            alert("ThermiteServiceMetricsStale", stale_textfiles(), "5m"),
        ]},
    ]}
