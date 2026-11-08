"""Actionable alerts and a local-only notification route; no sleeping-device alarms.

Control health is recorded per check: thermite:check{room, zone, device,
check, required} is 1 failing, 0 OK or -1 unknown, for

    runtime   the exporter reads openHAB (room house)
    scrape    a core Prometheus job answers (room house; device is the job)
    service   a local HTTP service answers (room house; device is the service)
    systemd   node_exporter reads systemd's unit states (room house)
    source    a routing zone's source readback is known (device empty)
    thing     a device's control Thing is ONLINE
    port      a device's control port accepts connections
    problem   a device reports no problem
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
reads -1, never 0. thermite:control_health folds them into today's one
number: 1 when a required check fails, else -1 when one is unknown (or the
check series are incomplete), else 0.

Severity says how an alert should reach a person once phone delivery is
commissioned: critical ones push, warning ones wait for a digest (and repeat
every 12 hours instead of 4). Delivery itself is unchanged: every alert
goes to the local ntfy adapter.
"""
from __future__ import annotations

import re

import oh_units

from .exporter import HOUSE_ROOM

#: The Prometheus jobs and local HTTP services control depends on.
CORE_JOBS = ("openhab", "thermite", "node", "prometheus", "blackbox")
SERVICES = ("openhab", "apple_tv_helper", "grafana")
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
    "problem": "Reports a problem",
    "power": "Off while in use",
}
CHECK = "thermite:check"
HEALTH = "thermite:control_health"
#: A browser opens at most six HTTP/1.1 connections to one origin.
HTTP1_CONNECTIONS = 6
TIMEOUT_KINDS = ("policy_timeout", "locks_timeout", "command_timeout", "snapshot_timeout")
#: Unallocated btrfs space below which a new metadata chunk may not fit.
BTRFS_UNALLOCATED = 5 * 2**30

# Notification text is finite, so runtime error bodies/labels can never reach ntfy.
CATALOG = {
    "ThermiteCheckFailing": ("A room's control path needs attention", "A device a room is using now, or routing infrastructure, has failed a control check for five minutes. House health lists the room, device and check under Needs attention now."),
    "ThermiteControlUnknown": ("Control telemetry is incomplete", "Expected control readbacks have been missing for ten minutes. Check the runtime and exporter."),
    "ThermiteScrapeFailed": ("Monitoring target is unreachable", "Open Prometheus Targets and check the failed local monitoring service."),
    "ThermiteRuntimeUnavailable": ("Runtime API is unavailable", "The metrics exporter cannot read the runtime. Check the runtime service and its API credential."),
    "ThermiteHostDiskLow": ("Host storage is nearly full", "A writable host filesystem has less than ten percent free. Inspect disk use before removing any data."),
    "ThermiteBtrfsTrouble": ("Host btrfs needs attention", "A btrfs device has reported new I/O or checksum errors, or its unallocated space is nearly gone. Check btrfs device stats and filesystem usage before removing any data."),
    "ThermiteServiceFailed": ("A thermite service failed", "Inspect the failed thermite service and its journal."),
    "ThermiteServiceDown": ("A thermite service is not running", "A long-running thermite service has not been active for three minutes. Inspect it with systemctl status and its journal."),
    "ThermiteServiceFlapping": ("A thermite service keeps restarting", "A thermite service restarted three or more times in thirty minutes. Its journal says why; the Host dashboard shows restarts."),
    "ThermiteGatewayDown": ("The remote gateway is not answering", "The local HTTPS check of the gateway has failed for three minutes. Check thermite-access.service and its journal. If the gateway answers browsers, run thermite deploy so the check has the gateway's trust certificate."),
    "ThermiteRemoteStarved": ("A remote holds too many HTTP/1.1 streams", "One client holds six or more event streams over HTTP/1.1, the browser's connection limit, so its controls can stall. Close extra remote pages on that device. Switching & remotes shows streams by HTTP version."),
    "ThermiteRemoteTimeouts": ("Remotes are timing out", "Remote pages reported repeated timeouts waiting for the gateway in the last fifteen minutes. Switching & remotes shows which requests and how long they waited."),
    "ThermiteRoutingTelemetryUnavailable": ("Routing telemetry is unreadable", "For five minutes the routing executor's status or routing's counts in the runtime have not been valid, so the dashboards cannot show switching, device failures or quarantined devices. Check the runtime's log for the routing rule and the routing executor."),
    "ThermiteRoutingQuarantined": ("Routing commands are quarantined", "The routing executor has refused commands to a device for five minutes after an uncertain outcome, and its recovery reads have not succeeded. Devices shows which device; check that it is powered and on its network."),
    "ThermitePolicyWorkerStalled": ("Room schedules are not being applied", "The room policy worker has not completed a pass for three minutes, so schedules and locks are not enforced. Check thermite-access-worker.service and its journal."),
    "ThermitePolicyUnenforced": ("A room's lock is not being enforced", "For ten minutes a locked room's lock could not be held: its policy cannot be read, it conflicts with another room over shared hardware, its devices cannot be read, or a change on them has not been put back. Switching & remotes shows locked rooms by state; check the room's devices and thermite-access-worker.service's journal."),
    "ThermiteHandlerPoolSaturated": ("Device dispatch is saturated", "Device dispatch has no idle workers and a ready probe is overdue. Inspect blocked binding connections."),
    "ThermiteHandlerTelemetryUnavailable": ("Device dispatch telemetry is unavailable", "The native scheduler observation is missing, incomplete or stale. Check the runtime telemetry rule."),
    "ThermiteMonitoringBroken": ("Monitoring is not working fully", "Prometheus failed to evaluate a rule or to hand alerts over, Alertmanager failed a delivery, or node_exporter could not read a metrics textfile. Check their journals."),
    "ThermiteNotificationUnavailable": ("Notification service is unavailable", "Check Alertmanager, the local notification adapter and ntfy. Remote phone delivery is a separate commissioning step."),
    "ThermiteHeartbeatMissing": ("Local notification heartbeat is late", "The local Prometheus to ntfy heartbeat has not completed for fifteen minutes. Inspect the notification services."),
    "ThermiteWatchdog": ("Local monitoring heartbeat", "The local alert evaluation and delivery path completed. This does not prove phone delivery."),
}
#: Alerts that should push to a phone once delivery is commissioned.
CRITICAL = frozenset({"ThermiteCheckFailing", "ThermiteRuntimeUnavailable", "ThermiteGatewayDown",
                      "ThermiteRemoteStarved", "ThermiteServiceDown", "ThermiteRoutingQuarantined",
                      "ThermitePolicyWorkerStalled", "ThermitePolicyUnenforced", "ThermiteHandlerPoolSaturated"})
#: (source, targets): while the source fires, the targets would only repeat it.
INHIBITS = (
    ("ThermiteRuntimeUnavailable", ("ThermiteCheckFailing", "ThermiteControlUnknown", "ThermiteHandlerPoolSaturated",
                                    "ThermiteHandlerTelemetryUnavailable", "ThermiteRoutingQuarantined",
                                    "ThermiteRoutingTelemetryUnavailable", "ThermitePolicyUnenforced")),
    ("ThermiteGatewayDown", ("ThermiteRemoteStarved", "ThermiteRemoteTimeouts")),
    # A stalled worker's last textfile keeps its last states.
    ("ThermitePolicyWorkerStalled", ("ThermitePolicyUnenforced",)),
)


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


def _demand(demands: list[dict]) -> str:
    """Whether a zone's source uses the device now (one item per call)."""
    sources = sorted({demand["source"] for demand in demands})
    item = demands[0]["item"]
    return (f'(count(thermite_item_info{{item={_q(item)},value=~{_q("|".join(re.escape(s) for s in sources))}}}) > 0)')


def _zone_room(mapping: dict, item: str) -> str:
    return str(((mapping.get("items") or {}).get(item) or {}).get("room") or item)


def _zone(mapping: dict, item: str) -> str:
    """The routing zone id of a zone's source Item."""
    return str(((mapping.get("paths") or {}).get(item) or {}).get("zone") or item)


def checks(mapping: dict) -> tuple[list[dict], int]:
    """(thermite:check recording rules, how many required series always exist)."""
    rules: list[dict] = []
    always_required = 0

    def record(expr: str, room: str, device: str, check: str, required: bool, zone: str = "") -> None:
        labels = {"room": room, "zone": zone, "device": device, "check": check,
                  "required": "true" if required else "false"}
        rules.append({"record": CHECK, "expr": expr, "labels": {k: v for k, v in labels.items() if v}})

    record("max(thermite_openhab_up == bool 0) or vector(-1)", HOUSE_ROOM, "openhab", "runtime", True)
    for job in CORE_JOBS:
        record(f"max(up{{job={_q(job)}}} == bool 0) or vector(-1)", HOUSE_ROOM, job, "scrape", True)
    for service in SERVICES:
        record(f'max(probe_success{{job="service_http",service={_q(service)}}} == bool 0) or vector(-1)',
               HOUSE_ROOM, service, "service", True)
    record('max(node_scrape_collector_success{job="node",collector="systemd"} == bool 0) or vector(-1)',
           HOUSE_ROOM, "node", "systemd", True)
    always_required += 2 + len(CORE_JOBS) + len(SERVICES)
    for item in sorted((mapping.get("paths") or {})):
        record(f'(count(thermite_item_info{{item={_q(item)},value!="other"}}) * 0) or vector(-1)',
               _zone_room(mapping, item), "", "source", True, _zone(mapping, item))
        always_required += 1
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
            room, zone, used = _zone_room(mapping, item), _zone(mapping, item), _demand(demanded["demands"])
            if group["optional"]:
                record(value, room, device, name, False, zone)
                continue
            record(f"({value}) and on() {used}", room, device, name, True, zone)
            record(f"({value}) unless on() {used}", room, device, name, False, zone)
    return rules, always_required


def control_health(required_series: int) -> str:
    """thermite:control_health from thermite:check: today's one number."""
    required = f'{CHECK}{{required="true"}}'
    return (f"clamp_max(count({required} == 1), 1)"
            f" or ((vector(0) unless on() ({required} == -1)) and on() (count({required}) >= {required_series}))"
            " or vector(-1)")


def rules(mapping):
    def alert(name, expr, duration, *, labels=None):
        title, detail = CATALOG[name]
        return {"alert": name, "expr": expr, "for": duration,
                "labels": {"severity": "heartbeat" if name == "ThermiteWatchdog" else
                           "critical" if name in CRITICAL else "warning", **(labels or {})},
                "annotations": {"summary": title, "description": detail}}
    recorded, required_series = checks(mapping)
    daemons = _units(oh_units.DAEMONS)
    timeouts = "|".join(TIMEOUT_KINDS)
    return {"groups": [
        # Sequential within the group: control_health reads this evaluation's checks.
        {"name": "thermite_checks", "rules": [*recorded, {"record": HEALTH, "expr": control_health(required_series)}]},
        {"name": "thermite", "rules": [
            alert("ThermiteCheckFailing", f'{CHECK}{{required="true"}} == 1', "5m"),
            alert("ThermiteControlUnknown", f"{HEALTH} == -1", "10m"),
            alert("ThermiteScrapeFailed", 'up{job=~"openhab|thermite|node|blackbox|alertmanager|notifications|ntfy"} == 0', "3m"),
            alert("ThermiteRuntimeUnavailable", "thermite_openhab_up == 0", "3m"),
            alert("ThermiteHostDiskLow", '(node_filesystem_avail_bytes{job="node",fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes < 0.10) and (node_filesystem_readonly == 0)', "10m"),
            alert("ThermiteBtrfsTrouble", f'(sum by (uuid) (increase(node_btrfs_device_errors_total{{job="node"}}[1h])) > 0) or (sum by (uuid) (node_btrfs_device_unused_bytes{{job="node"}}) < {BTRFS_UNALLOCATED})', "5m"),
            alert("ThermiteServiceFailed", 'node_systemd_unit_state{job="node",name=~"thermite-.*\\\\.service",state="failed"} == 1', "3m"),
            # A failed unit is ThermiteServiceFailed's.
            alert("ThermiteServiceDown", f'(node_systemd_unit_state{{job="node",name=~{_q(daemons)},state="active"}} == 0) unless on(name) (node_systemd_unit_state{{job="node",state="failed"}} == 1)', "3m"),
            alert("ThermiteServiceFlapping", 'increase(node_systemd_service_restart_total{job="node",name=~"thermite-.*"}[30m]) >= 3', "0s"),
            # Only where the gateway's unit exists: a house without one has nothing to probe.
            alert("ThermiteGatewayDown", f'(probe_success{{job="{GATEWAY_JOB}"}} == 0) and on() ({GATEWAY_INSTALLED})', "3m"),
            alert("ThermiteRemoteStarved", f'thermite_gateway_sse_peer_streams_max{{http_version="1.1"}} >= {HTTP1_CONNECTIONS}', "1m"),
            alert("ThermiteRemoteTimeouts", f'sum(increase(thermite_client_events_total{{kind=~"{timeouts}"}}[15m])) >= 5', "5m"),
            alert("ThermiteRoutingQuarantined", "thermite_routing_quarantined > 0", "5m"),
            alert("ThermiteRoutingTelemetryUnavailable",
                  "(thermite_routing_executor_up == 0) or (thermite_routing_metrics_up == 0)", "5m"),
            alert("ThermitePolicyWorkerStalled", "time() - thermite_policy_loop_timestamp_seconds > 120", "1m"),
            alert("ThermitePolicyUnenforced", 'thermite_policy_room_state{state="unavailable"} == 1', "10m"),
            *([alert("ThermiteHandlerPoolSaturated", '(thermite_handler_pool_active_threads >= thermite_handler_pool_core_threads) and (thermite_handler_pool_probe_overdue_seconds > 30)', "2m"),
               alert("ThermiteHandlerTelemetryUnavailable", 'thermite_handler_pool_up == 0', "5m")]
              if mapping.get("handler_pool") else []),
            alert("ThermiteMonitoringBroken", '(increase(prometheus_rule_evaluation_failures_total[10m]) > 0) or (increase(prometheus_notifications_dropped_total[10m]) > 0) or (increase(alertmanager_notifications_failed_total[10m]) > 0) or (node_textfile_scrape_error == 1)', "5m"),
            alert("ThermiteNotificationUnavailable", 'probe_success{job="alerting_http"} == 0', "3m"),
            alert("ThermiteHeartbeatMissing", "thermite_notification_heartbeat_age_seconds > 900", "5m"),
            alert("ThermiteWatchdog", "vector(1)", "0s"),
        ]},
    ]}


def alertmanager(port):
    inhibit = [{"source_matchers": [f'alertname="{source}"'],
                "target_matchers": [f'alertname=~"{"|".join(targets)}"']} for source, targets in INHIBITS]
    return {"global": {"resolve_timeout": "5m"},
            "route": {"receiver": "incidents", "group_by": ["alertname", "room"], "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
                      "routes": [{"matchers": ['alertname="ThermiteWatchdog"'], "receiver": "heartbeat", "group_wait": "0s", "group_interval": "1m", "repeat_interval": "5m"},
                                 # Digest-class alerts: the same delivery, repeated less often.
                                 {"matchers": ['severity="warning"'], "receiver": "incidents", "repeat_interval": "12h"}]},
            "inhibit_rules": inhibit,
            "receivers": [{"name": name, "webhook_configs": [{"url": f"http://127.0.0.1:{port}/{path}", "send_resolved": True, "max_alerts": 64,
                            "http_config": {"follow_redirects": False, "proxy_from_environment": False}}]}
                          for name, path in (("incidents", "alerts"), ("heartbeat", "heartbeat"))]}


def ntfy(port):
    # Explicitly no Firebase/APNs/upstream. This listener never opens to the LAN.
    return {"base-url": f"http://127.0.0.1:{port}", "listen-http": f"127.0.0.1:{port}",
            "cache-file": "/var/lib/ntfy/cache.db", "cache-duration": "48h", "enable-metrics": True,
            "upstream-base-url": "", "behind-proxy": False, "log-level": "warn"}
