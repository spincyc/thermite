"""Actionable alerts and a local-only notification route; no sleeping-device alarms."""
from . import dashboards

# Notification text is finite, so runtime error bodies/labels can never reach ntfy.
CATALOG = {
    "ThermiteControlDegraded": ("Control path needs attention", "Open House rhythm and inspect the active route's device readbacks."),
    "ThermiteControlUnknown": ("Control telemetry is incomplete", "Expected control readbacks have been missing for ten minutes. Check the runtime and exporter."),
    "ThermiteScrapeFailed": ("Monitoring target is unreachable", "Open Prometheus Targets and check the failed local monitoring service."),
    "ThermiteRuntimeUnavailable": ("Runtime API is unavailable", "The metrics exporter cannot read the runtime. Check the runtime service and its API credential."),
    "ThermiteHostDiskLow": ("Host storage is nearly full", "A writable host filesystem has less than ten percent free. Inspect disk use before removing any data."),
    "ThermiteServiceFailed": ("A thermite service failed", "Inspect the failed thermite service and its journal."),
    "ThermiteHandlerPoolSaturated": ("Device dispatch is saturated", "Device dispatch has no idle workers and a ready probe is overdue. Inspect blocked binding connections."),
    "ThermiteHandlerTelemetryUnavailable": ("Device dispatch telemetry is unavailable", "The native scheduler observation is missing, incomplete or stale. Check the runtime telemetry rule."),
    "ThermiteNotificationUnavailable": ("Notification service is unavailable", "Check Alertmanager, the local notification adapter and ntfy. Remote phone delivery is a separate commissioning step."),
    "ThermiteHeartbeatMissing": ("Local notification heartbeat is late", "The local Prometheus to ntfy heartbeat has not completed for fifteen minutes. Inspect the notification services."),
    "ThermiteWatchdog": ("Local monitoring heartbeat", "The local alert evaluation and delivery path completed. This does not prove phone delivery."),
}


def rules(mapping):
    def alert(name, expr, duration, severity="warning"):
        title, detail = CATALOG[name]
        return {"alert": name, "expr": expr, "for": duration, "labels": {"severity": severity},
                "annotations": {"summary": title, "description": detail}}
    return {"groups": [{"name": "thermite", "rules": [
        {"record": "thermite:control_health", "expr": dashboards.control_health(mapping)},
        alert("ThermiteControlDegraded", "thermite:control_health == 1", "5m"),
        alert("ThermiteControlUnknown", "thermite:control_health == -1", "10m"),
        alert("ThermiteScrapeFailed", 'up{job=~"openhab|thermite|node|blackbox|alertmanager|notifications|ntfy"} == 0', "3m"),
        alert("ThermiteRuntimeUnavailable", "thermite_openhab_up == 0", "3m"),
        alert("ThermiteHostDiskLow", '(node_filesystem_avail_bytes{job="node",fstype!~"tmpfs|overlay|squashfs"} / node_filesystem_size_bytes < 0.10) and (node_filesystem_readonly == 0)', "10m"),
        alert("ThermiteServiceFailed", 'node_systemd_unit_state{job="node",name=~"openhab.*\\\\.service",state="failed"} == 1', "3m"),
        *([alert("ThermiteHandlerPoolSaturated", '(thermite_handler_pool_active_threads >= thermite_handler_pool_core_threads) and (thermite_handler_pool_probe_overdue_seconds > 30)', "2m"),
           alert("ThermiteHandlerTelemetryUnavailable", 'thermite_handler_pool_up == 0', "5m")]
          if mapping.get("handler_pool") else []),
        alert("ThermiteNotificationUnavailable", 'probe_success{job="alerting_http"} == 0', "3m"),
        alert("ThermiteHeartbeatMissing", "thermite_notification_heartbeat_age_seconds > 900", "5m"),
        alert("ThermiteWatchdog", "vector(1)", "0s", "heartbeat"),
    ]}]}


def alertmanager(port):
    return {"global": {"resolve_timeout": "5m"},
            "route": {"receiver": "incidents", "group_by": ["alertname"], "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
                      "routes": [{"matchers": ['alertname="ThermiteWatchdog"'], "receiver": "heartbeat", "group_wait": "0s", "group_interval": "1m", "repeat_interval": "5m"}]},
            "receivers": [{"name": name, "webhook_configs": [{"url": f"http://127.0.0.1:{port}/{path}", "send_resolved": True, "max_alerts": 64,
                            "http_config": {"follow_redirects": False, "proxy_from_environment": False}}]}
                          for name, path in (("incidents", "alerts"), ("heartbeat", "heartbeat"))]}


def ntfy(port):
    # Explicitly no Firebase/APNs/upstream. This listener never opens to the LAN.
    return {"base-url": f"http://127.0.0.1:{port}", "listen-http": f"127.0.0.1:{port}",
            "cache-file": "/var/lib/ntfy/cache.db", "cache-duration": "48h", "enable-metrics": True,
            "upstream-base-url": "", "behind-proxy": False}
