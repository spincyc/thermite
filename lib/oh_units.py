"""Host identities owned by Thermite; upstream product names stay unchanged."""

import dataclasses

import oh_service

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

#: Bounds.user: the state's uid and gid (host.yaml's uid: and gid:), which own
#: the runtime state and so read every secret store a unit can reach.
STATE_USER = "state"
#: Bounds.network: the host's network namespace, and so whatever the host
#: reaches. Nothing narrower exists yet; a unit given one says so here.
HOST_NETWORK = "host"
#: Bounds.secrets: every secret store of the runtime state, for a unit that
#: can reach all of it.
EVERY_SECRET = tuple(sorted(oh_service.PRIVATE))
#: Bounds.secrets of the access services, below the installation root: the
#: gateway's state (accounts, sessions, keys) and its release (the runtime
#: token and the TLS key), which each of them reads as the state's user.
ACCESS_SECRETS = ("access/state", "access/release")


@dataclasses.dataclass(frozen=True)
class Bounds:
    """What one service runs as, may use, reaches and is given.

    No field has a default: a service states each one, the renderers write
    its unit from them, and tests/test_unit_invariants.py holds every
    rendered unit of every fixture house to its record.
    """

    #: STATE_USER.
    user: str
    #: systemd's MemoryHigh= and MemoryMax= for the unit's cgroup, page cache
    #: included: reclaimed and slowed from the first, killed at the second.
    #: docs/service-account.md gives the basis of each.
    memory_high: str
    memory_max: str
    #: HOST_NETWORK.
    network: str
    #: The secret stores it can read: those of the runtime state by their
    #: path below it, as oh_service.PRIVATE names them (a path below one
    #: is that file alone), and ACCESS_SECRETS. Its unit reaches no other.
    secrets: tuple[str, ...]


#: The services outside monitoring; oh_metrics.services() carries the others'.
#: The access services are host services that run as the state's user: their
#: units put an empty directory in place of the runtime state, which none of
#: them needs, with the metrics textfile directory alone inside.
BOUNDS = {
    RUNTIME: Bounds(STATE_USER, "2560M", "3G", HOST_NETWORK, ("secrets", "userdata")),
    ATV: Bounds(STATE_USER, "256M", "512M", HOST_NETWORK, ("atv/secrets",)),
    MUSIC: Bounds(STATE_USER, "1536M", "2560M", HOST_NETWORK, ("music",)),
    ACCESS: Bounds(STATE_USER, "256M", "512M", HOST_NETWORK, ACCESS_SECRETS),
    ACCESS_WORKER: Bounds(STATE_USER, "256M", "512M", HOST_NETWORK, ACCESS_SECRETS),
    ACCESS_CALLBACKS: Bounds(STATE_USER, "128M", "256M", HOST_NETWORK, ACCESS_SECRETS),
}
