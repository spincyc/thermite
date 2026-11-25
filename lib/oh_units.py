"""Host identities owned by Thermite; upstream product names stay unchanged."""

import dataclasses

RUNTIME = "thermite-openhab.service"
ATV = "thermite-atv.service"
#: Localhost climate helper, on a house whose devices use a climate helper integration.
CLIMATE = "thermite-climate.service"
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

#: The gateway, its policy worker and its callback relay. oh_access.staging
#: writes their units for each release: containers of the pinned Python
#: image (ACCESS_CONTAINED).
ACCESS = "thermite-access.service"
ACCESS_WORKER = "thermite-access-worker.service"
ACCESS_CALLBACKS = "thermite-access-callbacks.service"

METRICS = (PROMETHEUS, NODE_EXPORTER, BLACKBOX, EXPORTER, GRAFANA)
ACCESS_UNITS = (ACCESS, ACCESS_WORKER, ACCESS_CALLBACKS)
#: The units that run in containers (Quadlet).
CONTAINED = (RUNTIME, ATV, CLIMATE, *METRICS, MUSIC)
#: Every long-running Thermite service a host may have.
DAEMONS = (*CONTAINED, *ACCESS_UNITS)
CONTAINERS = {unit: unit.removesuffix(".service") for unit in CONTAINED}
#: The access services that run in containers, and their containers. Their
#: quadlets name a release, so a release's stage writes them and the gateway
#: step installs them: `thermite generate` and deploy/ know nothing of them.
ACCESS_CONTAINED = ACCESS_UNITS
ACCESS_CONTAINERS = {unit: unit.removesuffix(".service") for unit in ACCESS_CONTAINED}

#: Bounds.user: the state's uid and gid (host.yaml's uid: and gid:), which own
#: the runtime state and so read every secret store a unit can reach.
STATE_USER = "state"
#: Bounds.user: a uid and gid of the unit's own, an account that owns no file
#: and is in no other group. Of the runtime state (0750, the state's) it
#: reads only what is mounted for it by name. node_exporter's is the only
#: one (oh_metrics.node_exporter_id()).
OWN_USER = "own"
#: Bounds.network: the host's network namespace, and so whatever the host
#: reaches. Nothing narrower exists yet; a unit given one says so here.
HOST_NETWORK = "host"
#: Bounds.secrets of the access services, below the installation root: the
#: gateway's state (accounts, sessions, keys) and its release's secrets (the
#: runtime token and the TLS key). A container reads what its quadlet
#: mounts.
ACCESS_STATE = "access/state"
ACCESS_RELEASE = "access/release/secrets"
ACCESS_SECRETS = (ACCESS_STATE, ACCESS_RELEASE)
#: Of a release's secrets, the runtime token alone.
ACCESS_RUNTIME_TOKEN = ACCESS_RELEASE + "/runtime-bearer"


@dataclasses.dataclass(frozen=True)
class Bounds:
    """What one service runs as, may use, reaches and is given.

    No field has a default: a service states each one, the renderers write
    its unit from them, and tests/test_unit_invariants.py holds every
    rendered unit of every fixture house to its record.
    """

    #: STATE_USER or OWN_USER.
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
    #: is that file alone), and those of ACCESS_SECRETS by theirs. Its unit
    #: reaches no other.
    secrets: tuple[str, ...]


#: The services outside monitoring; oh_metrics.services() carries the others'.
#: The access services run as the state's user, in containers that see only
#: what their quadlets mount: of the runtime state, which none of them
#: needs, the metrics textfile directory alone. The gateway sees its release
#: and the state; the worker and the relay, of a release, the program, its
#: environment and the one descriptor each reads; the worker also the state
#: and the runtime token, the relay no secret at all.
BOUNDS = {
    RUNTIME: Bounds(STATE_USER, "2560M", "3G", HOST_NETWORK, ("secrets", "userdata")),
    ATV: Bounds(STATE_USER, "256M", "512M", HOST_NETWORK, ("atv/secrets",)),
    CLIMATE: Bounds(STATE_USER, "128M", "256M", HOST_NETWORK, ("climate/secrets",)),
    MUSIC: Bounds(STATE_USER, "1536M", "2560M", HOST_NETWORK, ("music",)),
    ACCESS: Bounds(STATE_USER, "256M", "512M", HOST_NETWORK, ACCESS_SECRETS),
    ACCESS_WORKER: Bounds(STATE_USER, "256M", "512M", HOST_NETWORK, (ACCESS_STATE, ACCESS_RUNTIME_TOKEN)),
    ACCESS_CALLBACKS: Bounds(STATE_USER, "128M", "256M", HOST_NETWORK, ()),
}
