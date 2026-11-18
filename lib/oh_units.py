"""Host identities owned by Thermite; upstream product names stay unchanged."""

RUNTIME = "thermite-openhab.service"
ATV = "thermite-atv.service"
PROMETHEUS = "thermite-prometheus.service"
NODE_EXPORTER = "thermite-node-exporter.service"
BLACKBOX = "thermite-blackbox.service"
EXPORTER = "thermite-metrics-exporter.service"
GRAFANA = "thermite-grafana.service"
#: Music Assistant, on a host whose instance/host.yaml has music: (oh_music).
MUSIC = "thermite-music.service"

TARGET = "thermite.target"
SLICE = "thermite.slice"
#: Waits for a network share's server name, one instance per server (oh_shares).
SHARE_GATE = "thermite-share-gate@.service"

#: The gateway, its policy worker and its callback relay: host services, not
#: containers (oh_access.staging writes them).
ACCESS = "thermite-access.service"
ACCESS_WORKER = "thermite-access-worker.service"
ACCESS_CALLBACKS = "thermite-access-callbacks.service"

METRICS = (PROMETHEUS, NODE_EXPORTER, BLACKBOX, EXPORTER, GRAFANA)
ACCESS_UNITS = (ACCESS, ACCESS_WORKER, ACCESS_CALLBACKS)
#: The units that run in containers (Quadlet).
CONTAINED = (RUNTIME, ATV, *METRICS, MUSIC)
#: Every long-running Thermite service a host may have.
DAEMONS = (*CONTAINED, *ACCESS_UNITS)
CONTAINERS = {unit: unit.removesuffix(".service") for unit in CONTAINED}
