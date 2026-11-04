"""Host identities owned by Thermite; upstream product names stay unchanged."""

RUNTIME = "thermite-openhab.service"
ATV = "thermite-atv.service"
PROMETHEUS = "thermite-prometheus.service"
NODE_EXPORTER = "thermite-node-exporter.service"
BLACKBOX = "thermite-blackbox.service"
EXPORTER = "thermite-metrics-exporter.service"
GRAFANA = "thermite-grafana.service"
ALERTMANAGER = "thermite-alertmanager.service"
NTFY = "thermite-ntfy.service"
NOTIFICATIONS = "thermite-notifications.service"

TARGET = "thermite.target"
SLICE = "thermite.slice"
ATV_RESTART_PATH = "thermite-atv-restart.path"
ATV_RESTART_SERVICE = "thermite-atv-restart.service"
METRICS_RESTART_PATH = "thermite-metrics-restart.path"
METRICS_RESTART_SERVICE = "thermite-metrics-restart.service"

METRICS = (PROMETHEUS, NODE_EXPORTER, BLACKBOX, EXPORTER, GRAFANA,
           ALERTMANAGER, NTFY, NOTIFICATIONS)
DAEMONS = (RUNTIME, ATV, *METRICS)
RESTART_PATHS = (ATV_RESTART_PATH, METRICS_RESTART_PATH)
RESTART_SERVICES = (ATV_RESTART_SERVICE, METRICS_RESTART_SERVICE)
CONTAINERS = {unit: unit.removesuffix(".service") for unit in DAEMONS}
