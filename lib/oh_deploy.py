"""Deploy openHAB on this host: generate its systemd units, install a commit's
conf/, metrics/, Apple TV helper and metrics exporter into the state, and
report what runs. The install
itself is oh_install. Nothing here names a host: production is wherever
bin/thermite install ran, and its name and address are found at run time.

The openHAB quadlet has the shape of the factory services role's (ADR 0054)
so that role can adopt openHAB unchanged once it supports more than one
volume.

A deploy installs one commit, never the working tree. Trusted operators
write shared configuration through the thermite group. Before any write,
an operator explicitly approves the finite administrative ownership handoff;
successful deployment leaves runtime state owned by thermite:thermite.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import getpass
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import oh_cli
import oh_logging
import oh_ports
import oh_service
import oh_units
import oh_metrics as metrics
from oh_access import guest

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
QUADLET = f"deploy/{oh_units.CONTAINERS[oh_units.RUNTIME]}.container"
ATV_QUADLET = f"deploy/{oh_units.CONTAINERS[oh_units.ATV]}.container"
ATV_RESTART_PATH = f"deploy/{oh_units.ATV_RESTART_PATH}"
ATV_RESTART_SERVICE = f"deploy/{oh_units.ATV_RESTART_SERVICE}"
#: The metrics services' quadlets (oh_metrics.services()), by unit.
METRICS_QUADLETS = {unit: f"deploy/{unit.removesuffix('.service')}.container" for unit in metrics.SERVICE_UNITS}
METRICS_RESTART_PATH = f"deploy/{oh_units.METRICS_RESTART_PATH}"
METRICS_RESTART_SERVICE = f"deploy/{oh_units.METRICS_RESTART_SERVICE}"
TARGET = f"deploy/{oh_units.TARGET}"
SLICE = f"deploy/{oh_units.SLICE}"
CONTAINER_DROPINS = {unit: f"deploy/{unit}.d/50-container-lifecycle.conf" for unit in oh_units.CONTAINED}
#: What thermite-metrics-restart.service restarts: the services a deploy
#: cannot reload over HTTP.
METRICS_RESTARTED = (oh_units.EXPORTER, oh_units.GRAFANA, oh_units.NOTIFICATIONS, oh_units.NTFY)
#: Where the owner installs each generated unit on the host.
INSTALLED = {
    QUADLET: Path("/etc/containers/systemd") / Path(QUADLET).name,
    ATV_QUADLET: Path("/etc/containers/systemd") / Path(ATV_QUADLET).name,
    ATV_RESTART_PATH: Path("/etc/systemd/system") / oh_units.ATV_RESTART_PATH,
    ATV_RESTART_SERVICE: Path("/etc/systemd/system") / oh_units.ATV_RESTART_SERVICE,
    **{name: Path("/etc/containers/systemd") / Path(name).name for name in METRICS_QUADLETS.values()},
    METRICS_RESTART_PATH: Path("/etc/systemd/system") / oh_units.METRICS_RESTART_PATH,
    METRICS_RESTART_SERVICE: Path("/etc/systemd/system") / oh_units.METRICS_RESTART_SERVICE,
    TARGET: Path("/etc/systemd/system") / oh_units.TARGET,
    SLICE: Path("/etc/systemd/system") / oh_units.SLICE,
    **{source: Path("/etc/systemd/system") / Path(source).relative_to("deploy")
       for source in CONTAINER_DROPINS.values()},
}
#: The units of openHAB and its helper, and those of metrics and dashboards.
METRICS_UNITS = (*METRICS_QUADLETS.values(), METRICS_RESTART_PATH, METRICS_RESTART_SERVICE)
SERVICES = (oh_units.RUNTIME, oh_units.ATV, oh_units.ATV_RESTART_PATH, *metrics.SERVICE_UNITS,
            oh_units.METRICS_RESTART_PATH, oh_units.TARGET)
#: The area's files the Apple TV helper's container runs.
ATV_APP = ("bin/thermite", "lib/oh_cli/__init__.py", "lib/oh_cli/apple_tv.py", "lib/oh_atv.py",
           "lib/requirements-pyatv.txt", "lib/oh_paths.py", "lib/oh_release.py", "lib/oh_ports.py", "lib/oh_service.py",
           "lib/oh_units.py")
#: Inside the helper's container.
ATV_VENV = "/venv/pyatv"
DIR_MODE = oh_service.SHARED_DIR
FILE_MODE = oh_service.SHARED_FILE
PROGRAM_MODE = oh_service.SHARED_PROGRAM
#: Credential custody belongs to the service identity alone.
SECRETS_MODE = oh_service.PRIVATE_DIR
#: Prometheus' and Grafana's stores and the metrics secrets: written only by
#: the services as thermite; credentials remain private to the service.
MONITORING_MODE = oh_service.RUNTIME_DIR
SECRET_FILE_MODE = oh_service.PRIVATE_FILE
#: Where the openHAB container sees the secrets directory, as $OPENHAB_SECRETS.
SECRETS_MOUNT = "/secrets"
#: Ports besides HTTP and HTTPS that openHAB or the helper listen on.
HELPER_PORT = oh_ports.HELPER_PORT
KARAF_PORT = oh_ports.KARAF_PORT
LSP_PORT = oh_ports.LSP_PORT


class DeployError(Exception):
    """A deploy that must not go ahead, with the reason for the owner."""


class HostError(DeployError, ValueError):
    """instance/host.yaml is unusable; generated units are never written from it."""


#: Everything an installation keeps on the host's state filesystem lives in
#: this one root-owned directory unless instance/host.yaml names another root
#: (Installation).
DEFAULT_ROOT = "/srv/thermite"
#: Installation's two fixed children.
RUNTIME = "runtime"
ACCESS = "access"
#: The installation root's mode; root owns it.
ROOT_MODE = 0o755
HOST_REQUIRED = ("timezone", "http_port", "https_port", "uid", "gid")
HOST_KEYS = frozenset((*HOST_REQUIRED, "root", "gateway", "metrics", "guest_http"))
METRICS_KEYS = frozenset(("units", "retention", "retention_size", "yoke_targets",
                          *(name + "_port" for name in oh_ports.METRICS)))
#: Host values are pasted into unit files, container arguments and shell
#: words: no whitespace, quotes, systemd specifiers or volume separators.
_ROOT = re.compile(r"/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*")
_TIMEZONE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")
_UNIT = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_DURATION = re.compile(r"0|(?=\d)(?:\d+y)?(?:\d+w)?(?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?:\d+ms)?")
_SIZE = re.compile(r"\d+(?:B|KB|MB|GB|TB|PB|EB)")


def validate_host(host: object, where: str = "instance/host.yaml") -> dict:
    """The site's host facts, checked completely before anything uses them."""
    def fail(problem: str):
        raise HostError(f"{where}: {problem}")

    if not isinstance(host, dict):
        fail("must be a mapping")
    missing = [key for key in HOST_REQUIRED if key not in host]
    unknown = sorted(str(key) for key in host if key not in HOST_KEYS)
    if missing:
        fail("missing " + ", ".join(missing))
    if unknown:
        fail("unknown setting " + ", ".join(unknown) + " (allowed: " + ", ".join(sorted(HOST_KEYS)) + ")")
    root = host.get("root", DEFAULT_ROOT)
    if (not isinstance(root, str) or not _ROOT.fullmatch(root) or os.path.normpath(root) != root):
        fail("root must be a normalized absolute path below / of letters, digits and ._@+-")
    zone = host["timezone"]
    if not isinstance(zone, str) or not _TIMEZONE.fullmatch(zone) or ".." in zone.split("/"):
        fail("timezone must be an IANA time zone name such as Etc/UTC")
    import zoneinfo
    try:
        zoneinfo.ZoneInfo(zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        fail(f"timezone {zone!r} is not a known IANA time zone")
    overrides = host.get("metrics")
    if overrides is not None:
        if not isinstance(overrides, dict):
            fail("metrics must be a mapping")
        unknown = sorted(str(key) for key in overrides if key not in METRICS_KEYS)
        if unknown:
            fail("unknown metrics setting " + ", ".join(unknown))
        units = overrides.get("units", [])
        if not isinstance(units, list) or not all(isinstance(unit, str) and _UNIT.fullmatch(unit) for unit in units):
            fail("metrics.units must be a list of systemd unit names")
        if "retention" in overrides and not (isinstance(overrides["retention"], str)
                                             and _DURATION.fullmatch(overrides["retention"])):
            fail("metrics.retention must be a Prometheus duration such as 395d")
        if "retention_size" in overrides and not (isinstance(overrides["retention_size"], str)
                                                  and _SIZE.fullmatch(overrides["retention_size"])):
            fail("metrics.retention_size must be a Prometheus size such as 12GB")
    try:
        guest.settings(host.get("guest_http"))
        oh_service.validate_identity(host["uid"], host["gid"])
        oh_ports.effective(host)
        metrics.yoke_targets(host)
    except ValueError as exc:
        fail(str(exc))
    return host


def load_host(source: Path | bytes, where: str | None = None) -> dict:
    """Parse and validate host facts from a file or committed bytes."""
    # Here, not at the top: the install imports this module before it has
    # installed PyYAML.
    import yaml

    if isinstance(source, bytes):
        where, data = where or "instance/host.yaml", source
    else:
        where, data = where or str(source), Path(source).read_bytes()
    try:
        host = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise HostError(f"{where}: not valid YAML ({str(exc).splitlines()[0]})") from None
    return validate_host(host, where)


def load(area: Path) -> tuple[dict, dict]:
    versions = json.loads((Path(area) / "versions.json").read_text(encoding="utf-8"))
    return versions, load_host(Path(area) / "instance/host.yaml")


def image(versions: dict, name: str = "openhab") -> str:
    pinned = versions[name]["image"]
    digest = pinned["digest"]
    if not DIGEST.match(digest):
        raise ValueError(f"versions.json {name} image digest '{digest}' is not sha256")
    repository = pinned["ref"].rsplit(":", 1)[0]
    return f"{repository}@{digest}"


# --------------------------------------------------------------------------
# the state on the host


@dataclasses.dataclass(frozen=True)
class State:
    """The runtime state's layout: Installation.runtime, the service
    account's home. Every path below is service-owned."""

    root: Path

    @property
    def conf(self) -> Path:
        return self.root / "conf"

    @property
    def userdata(self) -> Path:
        return self.root / "userdata"

    @property
    def addons(self) -> Path:
        return self.root / "addons"

    @property
    def atv(self) -> Path:
        return self.root / "atv"

    @property
    def atv_app(self) -> Path:
        return self.atv / "app"

    @property
    def atv_venv(self) -> Path:
        return self.atv / "venv"

    @property
    def atv_secrets(self) -> Path:
        return self.atv / "secrets"

    @property
    def secrets(self) -> Path:
        """Keys openHAB's rules may read, mounted read-only at SECRETS_MOUNT;
        administrative installation preserves service-only custody. No rule reads one today: the
        Sony TVs' registrations are in openHAB's userdata."""
        return self.root / "secrets"

    @property
    def records(self) -> Path:
        return self.root / "deploy"

    @property
    def record(self) -> Path:
        return self.records / "deployed.json"

    @property
    def pending(self) -> Path:
        """Every path a deploy may have written, saved before its first write
        and removed once deployed.json records the result; an interrupted
        deploy's files stay tracked for the next deploy or rollback."""
        return self.records / "deployed.pending.json"

    @property
    def history(self) -> Path:
        return self.records / "history"

    @property
    def backups(self) -> Path:
        """Where thermite upgrade writes a tar of conf and userdata before it changes anything."""
        return self.root / "backups"

    @property
    def atv_restart(self) -> Path:
        """Written by a deploy that changes the helper; thermite-atv-restart.path watches it."""
        return self.records / "atv-restart"

    @property
    def metrics(self) -> Path:
        """The deployed copy of metrics/: Prometheus', blackbox's, Grafana's and
        the exporter's configuration, written by a deploy through the group."""
        return self.root / metrics.STATE_CONFIG

    @property
    def monitoring(self) -> Path:
        return self.root / metrics.STATE_DATA

    @property
    def prometheus_data(self) -> Path:
        """Prometheus' samples: large (up to the retention size), never backed up."""
        return self.root / metrics.STATE_PROMETHEUS

    @property
    def grafana_data(self) -> Path:
        """Grafana's database: its admin user and preferences; small."""
        return self.root / metrics.STATE_GRAFANA

    @property
    def monitoring_secrets(self) -> Path:
        """The openHAB API token Prometheus and the exporter send, and Grafana's
        admin password, mounted read-only at oh_metrics.SECRETS_MOUNT."""
        return self.root / metrics.STATE_SECRETS

    @property
    def exporter(self) -> Path:
        return self.exporter_app.parent

    @property
    def exporter_app(self) -> Path:
        return self.root / metrics.STATE_EXPORTER_APP

    @property
    def textfile(self) -> Path:
        """node_exporter's textfile directory: each access service (running as
        thermite) rewrites its own <unit>.prom here (oh_access.metrics), and
        a deploy, through the group, its DEPLOY_TEXTFILE."""
        return self.root / metrics.STATE_TEXTFILE

    @property
    def metrics_restart(self) -> Path:
        """Written by a deploy that changes the exporter's code or Grafana's
        settings; thermite-metrics-restart.path watches it."""
        return self.records / "metrics-restart"

    def directories(self) -> list[tuple[int, list[Path]]]:
        """Service-owned directories; operators write the shared ones."""
        return [
            (MONITORING_MODE, [self.root, self.addons, self.atv, self.atv_venv]),
            (DIR_MODE, [self.conf, self.records, self.atv_app]),
            (0o700, [self.userdata, self.atv_secrets]),
            (SECRETS_MODE, [self.secrets]),
        ]

    def metrics_directories(self) -> list[tuple[int, list[Path]]]:
        """(mode, directories) the metrics services need, owned by thermite:
        the deploying user writes the 2770 ones through the group; the stores
        and the secrets are the services' own, which the group only reads."""
        return [
            (MONITORING_MODE, [self.monitoring, self.exporter]),
            (DIR_MODE, [self.metrics, self.exporter_app, self.textfile]),
            (MONITORING_MODE, [self.prometheus_data, self.grafana_data, self.root / metrics.STATE_ALERTMANAGER,
                               self.root / metrics.STATE_NTFY]),
            (SECRETS_MODE, [self.monitoring_secrets]),
        ]

    def with_metrics(self) -> bool:
        """Whether thermite install or upgrade has made the directories a
        deploy installs metrics/ and the exporter into."""
        return self.metrics.is_dir() and self.exporter_app.is_dir()


@dataclasses.dataclass(frozen=True)
class Installation:
    """Everything an installation keeps on the host: one directory
    (instance/host.yaml root, DEFAULT_ROOT by default) that root owns with
    ROOT_MODE, a btrfs subvolume where its parent filesystem is btrfs: the
    unit for snapshots, backups and removal. It holds only its two fixed
    children, neither the other's ancestor, so the gateway's custody checks
    meet only root- and operator-owned parents (oh_access.staging)."""

    root: Path

    @property
    def runtime(self) -> Path:
        """The runtime state (State), owned by thermite."""
        return self.root / RUNTIME

    @property
    def access(self) -> Path:
        """The gateway root (oh_install.access.Gateway): operator-owned, with
        releases/, the identity state/, backups/ and tls-cert.pem."""
        return self.root / ACCESS

    @property
    def state(self) -> State:
        return State(self.runtime)


def installation(host: dict) -> Installation:
    return Installation(Path(host.get("root", DEFAULT_ROOT)))


def state(host: dict) -> State:
    return installation(host).state


# --------------------------------------------------------------------------
# generated units

HEADER = [
    "# Generated by bin/thermite generate from versions.json and",
    "# instance/host.yaml: change those, then generate again.",
]

# The pinned image's own /entrypoint creates accounts, edits /etc and chowns
# /openhab as root. Use only its unprivileged volume initialization at startup.
# Verified against image revision 86fb030402531b52bbdc80329d841f96f5d7896e
# (the config of versions.json's linux/amd64 image), not a moving image tag.
OPENHAB_UPDATE_SHA256 = "a6f09a576ae7ad4c6187ecd7ebcdd00d60b6b06ff073c8e52261bf8ca3129386"
NATIVE_PRIME_MARKER = ".thermite-prime-incomplete"
NATIVE_PRIME_WAIT = 180
NATIVE_PRIME_REQUEST_WAIT = 30
NATIVE_PRIME_STOP_WAIT = 60
NATIVE_PRIME_POLL = 1
#: The runtime's restart pacing. Installer starts are minutes apart, far
#: below the burst; only a runtime that fails within seconds reaches it.
RUNTIME_RESTART_SECONDS = 15
RUNTIME_START_INTERVAL = 300
RUNTIME_START_BURST = 10
#: The runtime JVM's heap bound. Without -Xmx the JVM may take a quarter of
#: the host's memory (3.97 GB on the reference host); two days of a live site
#: peaked at 684 MB used and 964 MB committed, with 294 MB live. A heap that
#: is still exhausted exits, so systemd restarts openHAB rather than leaving
#: it half-working after an OutOfMemoryError. The sandbox uses the same bound.
JAVA_HEAP_OPTS = "-Xmx1536m -XX:+ExitOnOutOfMemoryError"


def _openhab_initialize(*, track_fresh: bool = False) -> str:
    return " ".join((
        'test "$(id -u)" -ne 0 || { echo "openHAB must run as thermite, never root" >&2; exit 1; };',
        'umask 0077;',
        *(['thermite_new_userdata=0;'] if track_fresh else []),
        'for name in conf userdata; do',
        'target="$OPENHAB_HOME/$name";',
        'if test -z "$(ls -A "$target")"; then',
        'cp -R --no-preserve=ownership "$OPENHAB_HOME/dist/$name/." "$target/";',
        *(['if test "$name" = userdata; then thermite_new_userdata=1; fi;'] if track_fresh else []),
        'if test "$name" = conf; then chmod -R ug+rwX,o-rwx "$target"; fi;',
        'fi;',
        'done;',
        'test -f "$OPENHAB_USERDATA/etc/version.properties" ||',
        '{ echo "Incomplete native userdata; run thermite install" >&2; exit 1; };',
    ))


def _openhab_prime_guard() -> str:
    return (f'if test -e "$OPENHAB_USERDATA/{NATIVE_PRIME_MARKER}" || '
            f'test -L "$OPENHAB_USERDATA/{NATIVE_PRIME_MARKER}"; then '
            'echo "Incomplete fresh native cache priming; inspect the retained marker and logs before explicit recovery. No cache or credentials were removed." >&2; '
            'exit 1; fi;')


def openhab_start() -> str:
    """The entire container payload, including initialization, is non-root."""
    return " ".join((
        _openhab_initialize(),
        _openhab_prime_guard(),
        'cmp -s "$OPENHAB_USERDATA/etc/version.properties" "$OPENHAB_HOME/dist/userdata/etc/version.properties" ||',
        '{ echo "Native userdata needs offline preparation; run thermite upgrade" >&2; exit 1; };',
        'rm -f "$OPENHAB_USERDATA/tmp/instances/instance.properties";',
        'export JAVA_HOME="$(find /usr/lib/jvm -mindepth 1 -maxdepth 1 -type d | head -n 1)";',
        # A service-owned Java security override avoids editing the image's /etc.
        'printf "crypto.policy=unlimited\\n" > "$OPENHAB_USERDATA/etc/thermite-java.security";',
        'export EXTRA_JAVA_OPTS="$EXTRA_JAVA_OPTS -Djava.security.properties=$OPENHAB_USERDATA/etc/thermite-java.security";',
        'exec tini -s ./start.sh server',
    ))


def _openhab_prime_cache() -> str:
    """Prime fresh, authority-free userdata without upstream's overlapping JVMs.

    Established restores intentionally omit cache. They use normal startup and
    authenticated readiness instead: even empty file conf would load managed
    rules/Things from their jsondb. Never launch or force-stop those in a prepare
    command. A failed fresh primer retains its cache, lock, logs and marker for
    explicit recovery; neither preparation nor normal startup may reuse it.
    """
    return " ".join((
        'thermite_prime_cache() {',
        'if test -d "$OPENHAB_USERDATA/cache/org.eclipse.osgi"; then return 0; fi;',
        'if test "$thermite_new_userdata" != 1; then',
        'echo "Skipping offline cache priming for existing userdata without cache; normal owner-approved startup and authenticated readiness will validate it.";',
        'return 0; fi;',
        'if test -d "$OPENHAB_USERDATA/jsondb"; then',
        'managed="$(find "$OPENHAB_USERDATA/jsondb" -type f -name \'*.json\' -print -quit)";',
        'if test -n "$managed"; then echo "Skipping offline cache priming: managed jsondb state exists."; return 0; fi; fi;',
        'if test -n "$(ls -A "$OPENHAB_HOME/addons")"; then',
        'echo "Skipping offline cache priming: local add-on files exist."; return 0; fi;',
        'mkdir -p "$OPENHAB_USERDATA/tmp";',
        'prime="$(mktemp -d "$OPENHAB_USERDATA/tmp/thermite-prime.XXXXXX")";',
        f'prime_marker="$OPENHAB_USERDATA/{NATIVE_PRIME_MARKER}";',
        '(set -C; printf "Fresh native cache priming incomplete; logs: %s\\n" "$prime" > "$prime_marker");',
        'lock="$OPENHAB_USERDATA/cache-refresh-lock";',
        '(set -C; : > "$lock") || { echo "Native cache refresh lock already exists; inspect before recovery." >&2; return 1; };',
        'mkdir -p "$prime/conf/services" "$prime/logs";',
        'printf "# no add-ons while the fresh OSGi cache is primed\\n" > "$prime/conf/services/addons.cfg";',
        'custom="$OPENHAB_USERDATA/etc/custom.properties";',
        'cp -p -- "$custom" "$prime/custom.properties.before";',
        # Port zero must have a private, unique port file: otherwise Stop may
        # fall back to PID signaling, which confined Ubuntu processes can deny.
        'printf "\\n\\nkaraf.shutdown.port = 0\\nkaraf.shutdown.host = 127.0.0.1\\nkaraf.shutdown.port.file = %s\\n" "$prime/shutdown.port" >> "$custom";',
        # The normal listener step follows preparation. Keep the temporary
        # authority-free server on loopback with ephemeral ports meanwhile.
        'shell_cfg="$OPENHAB_USERDATA/etc/org.apache.karaf.shell.cfg";',
        "sed -i '/^[[:space:]]*sshHost[[:space:]]*=/d; /^[[:space:]]*sshPort[[:space:]]*=/d' \"$shell_cfg\";",
        'printf "\\nsshHost = 127.0.0.1\\nsshPort = 0\\n" >> "$shell_cfg";',
        'rm -f "$OPENHAB_USERDATA/tmp/instances/instance.properties";',
        'printf "0\\n" > "$OPENHAB_USERDATA/openhab-start-level";',
        'prime_pid=""; stop_pid=""; prime_cleanup_attempted=0; prime_cleanup_status=1;',
        'thermite_prime_run() {',
        'export KARAF_EXEC=exec OPENHAB_CONF="$prime/conf" OPENHAB_LOGDIR="$prime/logs";',
        'export OPENHAB_HTTP_ADDRESS=127.0.0.1 OPENHAB_HTTP_PORT=0 OPENHAB_HTTPS_PORT=0;',
        'export EXTRA_JAVA_OPTS="${EXTRA_JAVA_OPTS:-} -Duser.home=$OPENHAB_USERDATA -Declipse.stateSaveDelayInterval=1000";',
        'exec "$@";',
        '};',
        # kill -0 can fail with EPERM while a child is alive. Both children
        # have this process's UID and PID namespace; inspect their proc state
        # and reap only an absent or dead child, never wait on a live process.
        'thermite_prime_child_running() {',
        'test -d "/proc/$1" || return 1;',
        'child_stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 0;',
        'child_state="${child_stat##*) }"; child_state="${child_state%% *}";',
        'case "$child_state" in Z|X|x) return 1 ;; *) return 0 ;; esac;',
        '};',
        'thermite_prime_wait_child() {',
        'wait_pid=$1; left=$2;',
        'while thermite_prime_child_running "$wait_pid" && test "$left" -gt 0; do',
        f'sleep {NATIVE_PRIME_POLL}; left=$((left - 1)); done;',
        '! thermite_prime_child_running "$wait_pid";',
        '};',
        'thermite_stop_prime() {',
        'test -n "$prime_pid" || return 0;',
        'test "$prime_cleanup_attempted" = 0 || return "$prime_cleanup_status";',
        'prime_cleanup_attempted=1;',
        # A repeated termination request must not restart a cleanup budget.
        "trap '' INT HUP TERM;",
        'if thermite_prime_child_running "$prime_pid"; then',
        'stop_port="$(cat "$prime/shutdown.port" 2>/dev/null)" || { echo "Native primer shutdown port file is missing or unreadable; retaining incomplete cache. Temporary processes may remain until preparation container cleanup." >&2; return 1; };',
        'case "$stop_port" in ""|*[!0-9]*) echo "Native primer shutdown port is invalid; refusing the PID fallback and retaining incomplete cache. Temporary processes may remain until preparation container cleanup." >&2; return 1 ;; esac;',
        'if ! test "$stop_port" -gt 0 || ! test "$stop_port" -le 65535; then echo "Native primer shutdown port is invalid; refusing the PID fallback and retaining incomplete cache. Temporary processes may remain until preparation container cleanup." >&2; return 1; fi;',
        'thermite_prime_run "$OPENHAB_HOME/runtime/bin/stop" > "$prime/stop.log" 2>&1 & stop_pid=$!;',
        f'if ! thermite_prime_wait_child "$stop_pid" {NATIVE_PRIME_REQUEST_WAIT}; then',
        'echo "Native primer shutdown client did not exit within its allowance; temporary processes may remain until preparation container cleanup. Incomplete cache retained." >&2; return 1; fi;',
        'stop_exit=0; wait "$stop_pid" || stop_exit=$?; stop_pid="";',
        'if test "$stop_exit" != 0; then echo "Native primer shutdown request failed; inspect retained stop.log. Temporary processes may remain until preparation container cleanup." >&2; return 1; fi;',
        # Stop's zero status confirms a delivered request, not JVM exit. If
        # either child remains alive, return with all evidence retained; the
        # preparation container's PID 1 exit delegates namespace teardown to
        # its runtime rather than signaling through the AppArmor boundary.
        f'if ! thermite_prime_wait_child "$prime_pid" {NATIVE_PRIME_STOP_WAIT}; then',
        'echo "Native primer JVM did not exit after Karaf shutdown; temporary processes may remain until preparation container cleanup. Incomplete cache retained." >&2; return 1; fi;',
        'fi;',
        'prime_exit=0; wait "$prime_pid" || prime_exit=$?; prime_pid="";',
        'if test "$prime_exit" != 0; then echo "Native cache primer exited unsuccessfully; incomplete cache retained." >&2; return 1; fi;',
        'prime_cleanup_status=0;',
        '};',
        "trap 'prime_result=$?; trap - EXIT; thermite_stop_prime || prime_result=1; exit \"$prime_result\"' EXIT;",
        "trap 'exit 130' INT HUP; trap 'exit 143' TERM;",
        'thermite_prime_run "$OPENHAB_HOME/runtime/bin/karaf" server > "$prime/console.log" 2>&1 & prime_pid=$!;',
        f'left={NATIVE_PRIME_WAIT}; ready=0;',
        'while thermite_prime_child_running "$prime_pid" && test "$left" -gt 0; do',
        'level="$(cat "$OPENHAB_USERDATA/openhab-start-level" 2>/dev/null || :)";',
        'case "$level" in ""|*[!0-9]*) ;; *) if test "$level" -ge 10; then ready=1; break; fi ;; esac;',
        f'sleep {NATIVE_PRIME_POLL}; left=$((left - 1)); done;',
        'if test "$ready" != 1; then echo "Fresh native cache primer did not reach start level 10; inspect retained logs." >&2; return 1; fi;',
        'thermite_stop_prime || return 1;',
        'test -d "$OPENHAB_USERDATA/cache/org.eclipse.osgi" || { echo "Native primer exited without an OSGi cache." >&2; return 1; };',
        'cat "$prime/custom.properties.before" > "$custom";',
        'printf "0\\n" > "$OPENHAB_USERDATA/openhab-start-level";',
        'rm -f "$lock";',
        'rm -rf -- "$prime";',
        'rm -f "$prime_marker";',
        'trap - EXIT INT HUP TERM;',
        'echo "Fresh native OSGi cache primed; the temporary JVM has fully exited.";',
        '}; thermite_prime_cache;',
    ))


def openhab_prepare() -> str:
    """Upgrade offline, then prime only newly initialized authority-free userdata.

    Its only root requirement is an initial identity guard. All mutable paths
    belong to thermite, so remove that exact guard from a hash-checked copy
    and run the remaining updater as thermite. A new upstream updater needs
    review before its hash is accepted here.
    """
    return " ".join((
        _openhab_initialize(track_fresh=True),
        _openhab_prime_guard(),
        'export JAVA_HOME="$(find /usr/lib/jvm -mindepth 1 -maxdepth 1 -type d | head -n 1)";',
        'if ! cmp -s "$OPENHAB_USERDATA/etc/version.properties" "$OPENHAB_HOME/dist/userdata/etc/version.properties"; then (',
        f'printf "{OPENHAB_UPDATE_SHA256}  /openhab/runtime/bin/update\\n" | sha256sum -c -;',
        'mkdir -p "$OPENHAB_BACKUPS" "$OPENHAB_LOGDIR";',
        'backup="$(mktemp "$OPENHAB_BACKUPS/thermite-before-$(date -u +%Y%m%dT%H%M%S)-XXXXXX.tar")";',
        'tar --exclude=./backup -cf "$backup" -C "$OPENHAB_USERDATA" .;',
        'prepared="$(mktemp /tmp/thermite-update.XXXXXX)";',
        'trap \'rm -f "$prepared"\' EXIT;',
        "sed '/^  # Ask to run as root/,/^  fi$/d' /openhab/runtime/bin/update > \"$prepared\";",
        '! grep -Fq \'Please run this script as root!\' "$prepared";',
        'sh -e "$prepared";',
        'cmp -s "$OPENHAB_USERDATA/etc/version.properties" "$OPENHAB_HOME/dist/userdata/etc/version.properties"',
        '); fi;',
        _openhab_prime_cache(),
    ))


def prepare_openhab_argv(versions: dict, host: dict) -> tuple[str, ...]:
    """Owner-approved Podman management; the offline payload remains non-root."""
    oh_service.validate_identity(host["uid"], host["gid"])
    root = state(host).root
    return ("podman", "run", "--rm", "--runtime=runc", "--network=host", "--user", f"{host['uid']}:{host['gid']}",
            "--umask=0077", "--cap-drop=all", "--security-opt=no-new-privileges", "--entrypoint=/bin/sh",
            "--env", f"TZ={host['timezone']}",
            "--volume", f"{root}/conf:/openhab/conf:Z",
            "--volume", f"{root}/userdata:/openhab/userdata:Z",
            "--volume", f"{root}/addons:/openhab/addons:Z",
            image(versions), "-ec", openhab_prepare())


def render_quadlet(versions: dict, host: dict) -> str:
    oh_service.validate_identity(host["uid"], host["gid"])
    root = state(host).root
    version = versions["openhab"]["version"]
    lines = [
        f"# openHAB {version}. Generated by bin/thermite generate",
        "# from versions.json and instance/host.yaml: change those, then generate again.",
        "[Unit]",
        f"Description=openHAB {version} home automation",
        f"PartOf={oh_units.TARGET}",
        "Wants=network-online.target",
        "After=network-online.target",
        # Wants=, not Requires=: restarting the boundary (a deploy that
        # changes its rules) must not restart openHAB. ExecStartPre below
        # loads the same rules, so openHAB still never starts without them.
        "Wants=thermite-native-boundary.service",
        "After=thermite-native-boundary.service",
        f"# A runtime failing within seconds stops after {RUNTIME_START_BURST} starts in "
        f"{RUNTIME_START_INTERVAL // 60} minutes;",
        "# a slower crash keeps restarting. systemctl reset-failed clears the limit.",
        f"StartLimitIntervalSec={RUNTIME_START_INTERVAL}",
        f"StartLimitBurst={RUNTIME_START_BURST}",
        "",
        "[Container]",
        f"ContainerName={oh_units.CONTAINERS[oh_units.RUNTIME]}",
        f"Image={image(versions)}",
        "# UPnP, mDNS and SDDP discovery and the Sonos event callbacks do not",
        "# traverse a container network.",
        "Network=host",
        f"User={host['uid']}",
        f"Group={host['gid']}",
        "NoNewPrivileges=true",
        "DropCapability=all",
        # Avoid crun/AppArmor profile stacking under no-new-privileges.
        "PodmanArgs=--runtime=runc",
        # Quadlet adds --replace; this later value preserves foreign namesakes.
        "PodmanArgs=--replace=false",
        "PodmanArgs=--cidfile=%t/%N.cid",
        "PodmanArgs=--umask=0077",
        # Quadlet 4.9 (Ubuntu 24.04) predates the Entrypoint key.
        "PodmanArgs=--entrypoint=/bin/sh",
        "# Karaf needs time to flush persistent state before removal.",
        "PodmanArgs=--stop-timeout=60",
        f"Volume={root}/conf:/openhab/conf:Z",
        f"Volume={root}/userdata:/openhab/userdata:Z",
        f"Volume={root}/addons:/openhab/addons:Z",
        "# Credentials remain outside shared configuration.",
        f"Volume={root}/secrets:{SECRETS_MOUNT}:ro,Z",
        f"Environment=OPENHAB_SECRETS={SECRETS_MOUNT}",
        f"Environment=USER_ID={host['uid']}",
        f"Environment=GROUP_ID={host['gid']}",
        "Environment=HOME=/openhab/userdata",
        "Environment=CRYPTO_POLICY=unlimited",
        f"Environment=TZ={host['timezone']}",
        f'Environment="EXTRA_JAVA_OPTS=-Duser.timezone={host["timezone"]} -Duser.home=/openhab/userdata {JAVA_HEAP_OPTS}"',
        f"Environment=OPENHAB_HTTP_PORT={host['http_port']}",
        f"Environment=OPENHAB_HTTPS_PORT={host['https_port']}",
        "# The HTTPS gateway is the only browser/API ingress.",
        "Environment=OPENHAB_HTTP_ADDRESS=127.0.0.1",
        "Exec=-ec " + exec_word(openhab_start()),
        "",
        "[Service]",
        "# Reapply the native boundary before every runtime start, including after firewall reloads.",
        "ExecStartPre=/usr/bin/env nft -f /etc/thermite-native.nft",
        f"Slice={oh_units.SLICE}",
        "UMask=0077",
        "Restart=always",
        "# Not systemd's 100 ms: a crashing JVM must not thrash userdata.",
        f"RestartSec={RUNTIME_RESTART_SECONDS}",
        "TimeoutStartSec=300",
        "TimeoutStopSec=90",
        "",
        "[Install]",
        f"WantedBy={oh_units.TARGET}",
        "",
    ]
    return "\n".join(lines)


def render_atv_quadlet(versions: dict, host: dict) -> str:
    oh_service.validate_identity(host["uid"], host["gid"])
    layout = state(host)
    lines = [
        "# The Apple TV helper for openHAB (bin/thermite apple-tv, docs/deploy.md).",
        *HEADER,
        "[Unit]",
        "Description=Apple TV helper for openHAB",
        f"PartOf={oh_units.TARGET}",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Container]",
        f"ContainerName={oh_units.CONTAINERS[oh_units.ATV]}",
        f"Image={image(versions, 'atv_helper')}",
        f"# openHAB reaches it on 127.0.0.1:{HELPER_PORT}; it has no login, so it",
        "# listens there only.",
        "Network=host",
        f"User={host['uid']}",
        f"Group={host['gid']}",
        "NoNewPrivileges=true",
        "DropCapability=all",
        # Avoid crun/AppArmor profile stacking under no-new-privileges.
        "PodmanArgs=--runtime=runc",
        "PodmanArgs=--replace=false",
        "PodmanArgs=--cidfile=%t/%N.cid",
        "PodmanArgs=--umask=0077",
        "# Its code, as the last deploy installed it.",
        f"Volume={layout.atv_app}:/app:ro,Z",
        f"Volume={layout.atv_venv}:/venv:Z",
        f"Volume={layout.atv_secrets}:/secrets:Z",
        f"Environment=OPENHAB_ATV_VENV={ATV_VENV}",
        "Environment=HOME=/secrets",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        f"Environment=TZ={host['timezone']}",
        f"# The first start builds {ATV_VENV} from the hash-pinned wheels in",
        "# lib/requirements-pyatv.txt (from PyPI); later starts reuse it until",
        "# those pins change.",
        "Exec=python3 /app/bin/thermite apple-tv serve --setup --secrets /secrets",
        "",
        "[Service]",
        f"Slice={oh_units.SLICE}",
        "UMask=0077",
        "Restart=always",
        "# Unhurried, so a boot without PyPI does not exhaust systemd's start limit.",
        "RestartSec=10",
        "TimeoutStartSec=300",
        "",
        "[Install]",
        f"WantedBy={oh_units.TARGET}",
        "",
    ]
    return "\n".join(lines)


def render_atv_restart_path(host: dict) -> str:
    lines = [
        "# Restarts the Apple TV helper when a deploy changes its code",
        "# (bin/thermite deploy writes the watched file).",
        *HEADER,
        "[Unit]",
        "Description=Restart the Apple TV helper after a deploy changes it",
        f"PartOf={oh_units.TARGET}",
        "",
        "[Path]",
        f"PathChanged={state(host).atv_restart}",
        f"Unit={oh_units.ATV_RESTART_SERVICE}",
        "",
        "[Install]",
        f"WantedBy={oh_units.TARGET}",
        "",
    ]
    return "\n".join(lines)


def render_atv_restart_service(host: dict) -> str:
    lines = [
        "# Started by thermite-atv-restart.path.",
        *HEADER,
        "[Unit]",
        "Description=Restart the Apple TV helper after a deploy changed it",
        f"PartOf={oh_units.TARGET}",
        "",
        "[Service]",
        f"Slice={oh_units.SLICE}",
        "Type=oneshot",
        f"ExecStart=/usr/bin/systemctl try-restart {oh_units.ATV}",
        # The journal names this unit, not the program it runs (systemctl).
        f"SyslogIdentifier={oh_units.ATV_RESTART_SERVICE.removesuffix('.service')}",
        "",
    ]
    return "\n".join(lines)


_PLAIN_WORD = re.compile(r"^[A-Za-z0-9_@+=:,./-]+$")


def exec_word(word: str) -> str:
    """One argument of a quadlet's Exec=, which systemd splits and unescapes
    as it does ExecStart=; % would be a specifier and $ a variable."""
    if _PLAIN_WORD.match(word):
        return word
    escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _volume(service: metrics.Service, volume: metrics.Volume, root: Path, shared: set[str]) -> str:
    options = [option for option in volume.options.split(",") if option and option != "rw"]
    if not volume.source.startswith("/"):
        # Under SELinux: a private label for a directory one container
        # mounts, a shared one for the secrets several mount.
        options.append("z" if volume.source in shared else "Z")
    source = volume.source if volume.source.startswith("/") else str(root / volume.source)
    return f"Volume={source}:{volume.target}" + (f":{','.join(options)}" if options else "")


def _grafana_conditions(host: dict) -> list[str]:
    """A partial-install reboot must not initialize Grafana's default admin."""
    credential = exec_word(str(state(host).monitoring_secrets / metrics.GRAFANA_ADMIN_PASSWORD))
    prefix = (f"/usr/bin/setpriv --reuid={host['uid']} --regid={host['gid']} "
              "--clear-groups --no-new-privs ")
    lines = ["# Check as the payload user before any administrator database can be created."]
    lines += [f"ExecCondition={prefix}/usr/bin/test -{flag} {credential}" for flag in ("f", "r", "s")]
    # run.sh uses export around command substitution, which masks cat failure.
    # Assignment preserves that failure and rejects newline-only input after
    # substitution, without exporting or printing the credential.
    check = 'credential=$(/usr/bin/cat -- "$1") && test -n "$credential"'
    lines.append(f"ExecCondition={prefix}/usr/bin/sh -ec {exec_word(check)} thermite-grafana {credential}")
    return lines


def render_metrics_quadlet(versions: dict, host: dict, service: metrics.Service) -> str:
    oh_service.validate_identity(host["uid"], host["gid"])
    root = state(host).root
    every = metrics.services(host)
    used = [volume.source for other in every for volume in other.volumes]
    shared = {source for source in used if used.count(source) > 1}
    address = service.listen
    # Python as a container's PID 1 installs no SIGTERM handler, and PID 1
    # ignores the signals it does not handle: podman waited out its stop
    # timeout and killed it, exit 137, on every stop. An init process
    # (podman --init, catatonit) forwards SIGTERM; Python dies of it at once
    # and the container reports 143, which counts as a clean stop.
    python = service.command[:1] == ("python3",)
    where = ("only this host reaches it" if address.startswith(f"{metrics.LOOPBACK}:") else
             "phones and computers on the LAN reach it")
    lines = [
        f"# {service.description}: metrics and dashboards (lib/oh_metrics, docs/deploy.md).",
        *HEADER,
        "[Unit]",
        f"Description={service.description}",
        f"PartOf={oh_units.TARGET}",
        "Wants=network-online.target",
        "After=network-online.target",
        *(["After=apparmor.service"] if service.unit == oh_units.NODE_EXPORTER else []),
        "",
        "[Container]",
        f"ContainerName={service.container}",
        f"Image={image(versions, service.image)}",
        f"# Listens on {address}: {where}.",
        "Network=host",
        f"User={host['uid']}",
        f"Group={host['gid']}",
        "NoNewPrivileges=true",
        "DropCapability=all",
        # Avoid crun/AppArmor profile stacking under no-new-privileges.
        "PodmanArgs=--runtime=runc",
        "PodmanArgs=--replace=false",
        "PodmanArgs=--cidfile=%t/%N.cid",
        "PodmanArgs=--umask=0027",
        *(["# Forwards SIGTERM to Python, which ignores it as PID 1.", "PodmanArgs=--init"] if python else []),
        *(f"# {note}" for note in service.notes),
        *(_volume(service, volume, root, shared) for volume in service.volumes),
        *(f"Environment={key}={value}" for key, value in service.environment),
        f"Environment=TZ={host['timezone']}",
        *(f"PodmanArgs={argument}" for argument in service.podman_args),
    ]
    words = [*service.command, *service.args]
    if words:
        lines.append("Exec=" + " ".join(exec_word(word) for word in words))
    lines += [
        "",
        "[Service]",
        f"Slice={oh_units.SLICE}",
        *(["Environment=CONTAINERS_CONF_OVERRIDE=/etc/thermite/node-exporter-containers.conf"]
          if service.unit == oh_units.NODE_EXPORTER else []),
        "UMask=0027",
        *(_grafana_conditions(host) if service.image == "grafana" else []),
        *(["# node_exporter exits 143 after an ordinary container SIGTERM.", "SuccessExitStatus=143"]
          if service.unit == oh_units.NODE_EXPORTER else []),
        *(["# Python killed by its init's forwarded SIGTERM exits 143.", "SuccessExitStatus=143"] if python else []),
        "Restart=always",
        "RestartSec=10",
        "# The first start pulls the image.",
        "TimeoutStartSec=300",
        "",
        "[Install]",
        f"WantedBy={oh_units.TARGET}",
        "",
    ]
    return "\n".join(lines)


def render_metrics_restart_path(host: dict) -> str:
    lines = [
        "# Restarts the metrics applications when a deploy changes their",
        "# programs, Grafana settings, adapter catalog or ntfy settings (deploy writes",
        "# the watched file). Prometheus, Alertmanager and blackbox reload over HTTP.",
        *HEADER,
        "[Unit]",
        "Description=Restart the metrics applications after a deploy changes them",
        f"PartOf={oh_units.TARGET}",
        "",
        "[Path]",
        f"PathChanged={state(host).metrics_restart}",
        f"Unit={Path(METRICS_RESTART_SERVICE).name}",
        "",
        "[Install]",
        f"WantedBy={oh_units.TARGET}",
        "",
    ]
    return "\n".join(lines)


def render_metrics_restart_service(host: dict) -> str:
    lines = [
        f"# Started by {Path(METRICS_RESTART_PATH).name}.",
        *HEADER,
        "[Unit]",
        "Description=Restart the metrics applications after a deploy changed them",
        f"PartOf={oh_units.TARGET}",
        "",
        "[Service]",
        f"Slice={oh_units.SLICE}",
        "Type=oneshot",
        f"ExecStart=/usr/bin/systemctl try-restart {' '.join(METRICS_RESTARTED)}",
        f"SyslogIdentifier={oh_units.METRICS_RESTART_SERVICE.removesuffix('.service')}",
        "",
    ]
    return "\n".join(lines)


def render_target() -> str:
    """One boot entry point; members declare their own target membership."""
    return "\n".join([
        *HEADER,
        "[Unit]",
        "Description=Thermite home control and monitoring",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def render_slice() -> str:
    return "\n".join([
        *HEADER,
        "[Unit]",
        "Description=Thermite services",
        "",
        "[Slice]",
        # No CPUAccounting=: cgroup v2 always accounts CPU, and current
        # systemd ignores the option with a warning at every unit load.
        "MemoryAccounting=yes",
        "TasksAccounting=yes",
        "",
    ])


def render_container_dropin() -> str:
    """Override generator cleanup after generation, retaining only our CID."""
    return "\n".join([
        *HEADER,
        "[Service]",
        "ExecStartPre=/usr/bin/rm -f -- %t/%N.cid",
        "ExecStop=",
        "ExecStop=/usr/bin/podman rm -v -f -i --cidfile=%t/%N.cid",
        "ExecStopPost=",
        "ExecStopPost=-/usr/bin/podman rm -v -f -i --cidfile=%t/%N.cid",
        "",
    ])


def render_units(versions: dict, host: dict) -> dict[str, str]:
    """{area path: text} of every unit and service drop-in the owner installs."""
    # Every host value below lands in unit text; never render unchecked facts.
    validate_host(host)
    units = {
        QUADLET: render_quadlet(versions, host),
        ATV_QUADLET: render_atv_quadlet(versions, host),
        ATV_RESTART_PATH: render_atv_restart_path(host),
        ATV_RESTART_SERVICE: render_atv_restart_service(host),
        TARGET: render_target(),
        SLICE: render_slice(),
    }
    for service in metrics.services(host):
        units[METRICS_QUADLETS[service.unit]] = render_metrics_quadlet(versions, host, service)
    units[METRICS_RESTART_PATH] = render_metrics_restart_path(host)
    units[METRICS_RESTART_SERVICE] = render_metrics_restart_service(host)
    units.update({source: render_container_dropin() for source in CONTAINER_DROPINS.values()})
    return units


def unit_version(text: str | None) -> str | None:
    """The openHAB version a thermite-openhab.container was generated for."""
    found = re.search(r"^Description=openHAB (\S+)", text or "", re.MULTILINE)
    return found[1] if found else None


def unit_image(text: str | None) -> str | None:
    """The image (repository@digest) a quadlet runs."""
    found = re.search(r"^Image=(\S+)", text or "", re.MULTILINE)
    return found[1] if found else None


def unit_differences(area: Path, commit: str, installed: dict[str, Path] | None = None) -> list[str]:
    """The units (area paths) whose installed file is not the one `commit`'s
    deploy/ holds; one that cannot be read is left out."""
    from oh_paths import SITE_FILE
    locked = export(area, commit, (SITE_FILE,)).get(SITE_FILE)
    try:
        derived = locked is not None and _site_requirement(locked.data)["version"] == 2
    except (TypeError, ValueError) as exc:
        raise DeployError("invalid committed application requirement") from exc
    files = (prepared_files(area, commit, Path(area) / "var")[0] if derived
             else export(area, commit, tuple(installed or INSTALLED)))
    differ = []
    for name, target in (installed or INSTALLED).items():
        try:
            have = Path(target).read_bytes()
        except FileNotFoundError:
            have = None
        except PermissionError:
            continue
        want = files.get(name)
        if have != (want.data if want else None):
            differ.append(name)
    return differ


def installed_units(area: Path, installed: dict[str, Path] | None = None, *, rev: str = "HEAD") -> list[tuple[str, Path, str]]:
    """(area path, installed path, "current" | "differs" | "missing" |
    "unreadable") for each unit: whether the owner has installed the one
    the selected committed schema-2 site derives. Legacy checkouts retain
    comparison with their local generated units."""
    from oh_paths import SITE_FILE
    files = None
    try:
        commit = resolve(area, rev)
        locked = export(area, commit, (SITE_FILE,)).get(SITE_FILE)
    except DeployError:
        # Older callers also inspect generated directories outside Git.
        # A private site must still have a verifiable committed requirement.
        if (Path(area) / SITE_FILE).exists():
            raise
    else:
        if locked is not None:
            try:
                derived = _site_requirement(locked.data)["version"] == 2
            except (TypeError, ValueError) as exc:
                raise DeployError("invalid committed application requirement") from exc
            if derived:
                files = prepared_files(area, commit, Path(area) / "var")[0]
    out = []
    for name, target in (installed or INSTALLED).items():
        try:
            if files is not None and name not in files:
                raise FileNotFoundError
            want = files[name].data if files is not None else (Path(area) / name).read_bytes()
            same = Path(target).read_bytes() == want
            out.append((name, Path(target), "current" if same else "differs"))
        except FileNotFoundError:
            out.append((name, Path(target), "missing"))
        except PermissionError:
            out.append((name, Path(target), "unreadable"))
    return out


# --------------------------------------------------------------------------
# trees of files


@dataclasses.dataclass(frozen=True)
class Blob:
    data: bytes
    program: bool = False


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_digest(files: dict[str, Blob]) -> str:
    """One digest of a tree: every path and its content (not modes)."""
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(f"{path}\0{sha256(files[path].data)}\n".encode("utf-8"))
    return digest.hexdigest()


def read_tree(root: Path) -> dict[str, Blob]:
    root = Path(root)
    if not root.is_dir():
        raise DeployError(f"{root} is not a directory")
    return {p.relative_to(root).as_posix(): Blob(p.read_bytes(), bool(p.stat().st_mode & 0o111))
            for p in sorted(root.rglob("*")) if p.is_file()}


# --------------------------------------------------------------------------
# boot test stamps


def passed_dir(var: Path) -> Path:
    return Path(var) / "boot-check" / "passed"


def record_boot_check(var: Path, digest: str) -> Path:
    """Note that `thermite test boot` passed on the conf with this digest."""
    stamp = passed_dir(var) / digest
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(_now() + "\n", encoding="utf-8")
    return stamp


def boot_checked(var: Path, digest: str) -> bool:
    return (passed_dir(var) / digest).is_file()


# --------------------------------------------------------------------------
# git


def _git(area: Path, *args: str) -> bytes:
    oh_cli.trace_run(["git", "-C", str(area), *args])
    try:
        return subprocess.run(["git", "-C", str(area), *args], check=True, capture_output=True).stdout
    except FileNotFoundError:
        raise DeployError("git is not installed") from None
    except subprocess.CalledProcessError as exc:
        raise DeployError(f"git {' '.join(args)}: {exc.stderr.decode(errors='replace').strip()}") from None


def resolve(area: Path, rev: str) -> str:
    return _git(area, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}").decode().strip()


def subject(area: Path, commit: str) -> str:
    return _git(area, "log", "-1", "--format=%s", commit).decode().strip()


def uncommitted(area: Path) -> list[str]:
    """git status lines for the area: changes and untracked files."""
    out = _git(area, "status", "--porcelain", "--untracked-files=all", "--", ".").decode()
    return [line for line in out.splitlines() if line]


def ahead(area: Path, old: str, new: str) -> int:
    return int(_git(area, "rev-list", "--count", f"{old}..{new}").decode().strip())


def pushed(area: Path, commit: str) -> bool:
    return bool(_git(area, "branch", "--remotes", "--contains", commit).strip())


def export(area: Path, commit: str, paths: Iterable[str]) -> dict[str, Blob]:
    """The files below `paths` (relative to the area) in `commit`, as they
    are in Git: what a deploy installs, whatever the working tree holds."""
    top = Path(_git(area, "rev-parse", "--show-toplevel").decode().strip())
    prefix = _git(area, "rev-parse", "--show-prefix").decode().strip().rstrip("/")
    tree = f"{commit}:{prefix}" if prefix else commit
    paths = list(paths)
    present = _git(top, "ls-tree", "-r", "--name-only", tree, "--", *paths).decode().splitlines()
    # A commit from before a path existed has none of it; git archive would refuse.
    wanted = [p for p in paths if any(f == p or f.startswith(p + "/") for f in present)]
    if not wanted:
        return {}
    raw = _git(top, "archive", "--format=tar", tree, "--", *wanted)
    files: dict[str, Blob] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar:
            if member.isdir() or member.type == tarfile.XGLTYPE:
                continue
            if not member.isfile():
                raise DeployError(f"{commit[:12]}: {member.name} is not a regular file; deploys copy only files")
            handle = tar.extractfile(member)
            files[member.name] = Blob(handle.read(), bool(member.mode & 0o111))
    return files


# --------------------------------------------------------------------------
# installing a tree


@dataclasses.dataclass
class Changes:
    added: list[str] = dataclasses.field(default_factory=list)
    changed: list[str] = dataclasses.field(default_factory=list)
    removed: list[str] = dataclasses.field(default_factory=list)
    unchanged: int = 0

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    def summary(self) -> str:
        return (f"{len(self.added)} added, {len(self.changed)} changed, {len(self.removed)} removed, "
                f"{self.unchanged} unchanged")


def _relative(path: str) -> str:
    parts = path.split("/")
    if not path or path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise DeployError(f"refusing path {path!r}")
    return path


def _same(target: Path, blob: Blob) -> bool:
    if target.is_symlink() or not target.is_file():
        return False
    return bool(target.stat().st_mode & 0o111) == blob.program and target.read_bytes() == blob.data


def _make_dirs(root: Path, directory: Path) -> None:
    missing = []
    while directory != root and not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir()
        os.chmod(path, DIR_MODE)


def _safe_parents(root: Path, directory: Path) -> None:
    """A deployed relative path must never walk a link out of its tree."""
    descriptor = None
    try:
        descriptor = oh_service._open_root(root)
        for part in directory.relative_to(root).parts:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                return
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise DeployError(f"refusing non-directory or symlink deployment parent {directory}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace(target: Path, data: bytes, mode: int) -> None:
    """Write target by renaming a new file over it: needs only the right to
    write its directory, whoever owns the old file. Both the contents and the
    rename are durable before this returns."""
    handle, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".deploy", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    _fsync_directory(target.parent)


def _shared_write(target: Path, data: str, *, append: bool = False) -> None:
    """Keep path-unit close events and give a newly created record mode0660."""
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    except FileExistsError:
        descriptor = os.open(target, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_APPEND if append else 0))
    else:
        os.fchmod(descriptor, FILE_MODE)
    with os.fdopen(descriptor, "a" if append else "w", encoding="utf-8") as output:
        info = os.fstat(output.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeployError(f"refusing non-regular or hard-linked deployment record {target}")
        if not append:
            os.ftruncate(output.fileno(), 0)
        output.write(data)


def _prune(root: Path, directory: Path) -> None:
    """Remove directories a removal left empty, up to (not including) root."""
    while directory != root and root in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def sync(files: dict[str, Blob], target: Path, previous: Iterable[str] = (), dry_run: bool = False) -> Changes:
    """Install files into target: write what differs, and delete what an
    earlier deploy installed (`previous`) that files no longer hold. A file
    no deploy installed is left alone."""
    target = Path(target)
    changes = Changes()
    obsolete = sorted(set(previous) - set(files))

    def remove(path: str) -> None:
        destination = target / _relative(path)
        _safe_parents(target, destination.parent)
        if destination.is_symlink() or destination.is_file():
            changes.removed.append(path)
            if not dry_run:
                destination.unlink()
                _prune(target, destination.parent)

    # A renamed/split script must not coexist on disk with its replacement:
    # both define command rules. This also orders the reverse rollback safely.
    scripts = {path for path in obsolete if path.startswith("automation/js/") and path.endswith(".js")}
    for path in sorted(scripts):
        remove(path)
    for path in sorted(files):
        destination = target / _relative(path)
        _safe_parents(target, destination.parent)
        blob = files[path]
        if _same(destination, blob):
            changes.unchanged += 1
            continue
        if destination.is_dir() and not destination.is_symlink():
            raise DeployError(f"{destination} is a directory; the commit has a file there")
        (changes.changed if destination.exists() or destination.is_symlink() else changes.added).append(path)
        if not dry_run:
            _make_dirs(target, destination.parent)
            _replace(destination, blob.data, PROGRAM_MODE if blob.program else FILE_MODE)
    for path in obsolete:
        if path not in scripts:
            remove(path)
    changes.removed.sort()
    return changes


def drift(target: Path, recorded: dict[str, str]) -> dict[str, list[str]]:
    """How target differs from what the last deploy recorded ({path: sha256})."""
    target = Path(target)
    present = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} if target.is_dir() else set()
    report: dict[str, list[str]] = {"modified": [], "missing": [], "foreign": sorted(present - set(recorded))}
    for path, digest in sorted(recorded.items()):
        if path not in present:
            report["missing"].append(path)
        elif sha256((target / path).read_bytes()) != digest:
            report["modified"].append(path)
    return report


# --------------------------------------------------------------------------
# deploying a commit


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


#: The record's per-tree file maps ({path: sha256}), in deployment order.
RECORDED_TREES = ("conf", "atv_app", "metrics", "exporter_app")


def _damaged(path: Path, problem: str) -> DeployError:
    return DeployError(f"{path} {problem}; no deployment files were changed. Inspect it, then restore it from a "
                       "backup or move it aside: without it the next deploy cannot remove files an earlier "
                       "deploy installed")


def read_record(layout: State) -> dict:
    try:
        record = json.loads(layout.record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (UnicodeError, ValueError):
        raise _damaged(layout.record, "is not valid JSON") from None
    if not isinstance(record, dict) or any(not isinstance(record.get(key, {}), dict) for key in RECORDED_TREES):
        raise _damaged(layout.record, "is not a deployment record")
    return record


def read_pending(layout: State) -> dict[str, list[str]]:
    """Paths an interrupted deploy may have written, by tree; {} when none."""
    try:
        value = json.loads(layout.pending.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (UnicodeError, ValueError):
        raise _damaged(layout.pending, "is not valid JSON") from None
    trees = value.get("trees") if isinstance(value, dict) and value.get("version") == 1 else None
    if (not isinstance(trees, dict) or not set(trees) <= set(RECORDED_TREES)
            or any(not isinstance(paths, list) or not all(isinstance(path, str) for path in paths)
                   for paths in trees.values())):
        raise _damaged(layout.pending, "is not a pending deployment record")
    return trees


def _write_pending(layout: State, commit: str, trees: dict[str, Iterable[str]]) -> None:
    value = {"version": 1, "commit": commit, "trees": {key: sorted(set(paths)) for key, paths in trees.items()}}
    _replace(layout.pending, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"), FILE_MODE)


def _settle_pending(layout: State, commit: str, remaining: dict[str, list[str]]) -> None:
    """Keep only what this deploy did not resolve, durably."""
    if remaining:
        _write_pending(layout, commit, remaining)
        return
    with contextlib.suppress(FileNotFoundError):
        layout.pending.unlink()
        _fsync_directory(layout.records)


def preflight(layout: State) -> None:
    user = getpass.getuser()
    extra = (layout.metrics, layout.exporter_app) if layout.with_metrics() else ()
    for directory in (layout.conf, layout.atv_app, layout.records, *extra):
        if not directory.is_dir():
            raise DeployError(f"{directory} does not exist; bin/thermite install creates it")
        _safe_parents(directory, directory)
        if not os.access(directory, os.W_OK | os.X_OK):
            raise DeployError(f"{user} cannot write {directory}: join the thermite group, then log in again "
                              "(`id -nG` lists your groups; docs/deploy.md)")


def ownership_argv(layout: State, host: dict | None = None) -> tuple[str, ...]:
    """The finite administrative helper, independent of an operator's home."""
    from oh_paths import PROGRAM
    uid = host["uid"] if host else layout.root.stat().st_uid
    gid = host["gid"] if host else layout.root.stat().st_gid
    oh_service.validate_identity(uid, gid)
    return ("/usr/bin/python3", str(PROGRAM / "lib/oh_service.py"), str(layout.root),
            "--uid", str(uid), "--gid", str(gid), "--deployment")


@contextlib.contextmanager
def ownership_handoff(layout: State, host: dict | None, dry_run: bool, require_logging: bool, *, approved: bool = False):
    """Approve before writes; repair ownership even when a write later fails."""
    argv = ownership_argv(layout, host)
    uid, gid = int(argv[argv.index("--uid") + 1]), int(argv[argv.index("--gid") + 1])
    direct = os.geteuid() == 0 or (os.geteuid() == uid and gid in {os.getegid(), *os.getgroups()})
    command = argv if direct else ("sudo", "-n", *argv)
    checks = (*command, "--check-pairing-logging")
    if dry_run:
        if require_logging:
            print("pairing logging check: " + shlex.join(checks))
        print("ownership handoff: " + shlex.join(command))
        yield
        return
    if not direct:
        if not approved and not sys.stdin.isatty():
            raise DeployError("deployment needs an explicitly approved thermite ownership handoff; "
                              "run interactively (no files were changed): " + shlex.join(command))
        if shutil.which("sudo") is None:
            raise DeployError("sudo is unavailable for the thermite ownership handoff; no files were changed")
        print("Deployment will run these administrative commands:")
        if require_logging:
            print("  " + shlex.join(checks))
        print("  " + shlex.join(command))
        if not approved and input("Approve this ownership handoff before deployment writes? [y/N] ").strip().lower() not in ("y", "yes"):
            raise DeployError("ownership handoff declined; no files were changed")
        # Validate in this process's terminal/parent context, before writes.
        # Parent approval is not authentication, and a preceding test or group
        # refresh may have outlived or changed the parent's sudo timestamp.
        validation = ("sudo", "-v") if sys.stdin.isatty() else ("sudo", "-n", "-v")
        try:
            oh_cli.trace_run(validation)
            authenticated = subprocess.run(validation, check=False).returncode == 0
        except OSError:
            authenticated = False
        if not authenticated:
            raise DeployError("sudo authentication failed before deployment; no deployment files were changed. "
                              "Rerun the install or deployment from a terminal")
    if require_logging:
        try:
            if direct:
                protected = oh_service.pairing_logging(layout.root)
            else:
                oh_cli.trace_run(checks)
                result = subprocess.run(checks, check=False).returncode
                if result not in (0, oh_service.PAIRING_LOGGING_UNPROTECTED):
                    raise DeployError("could not verify native pairing-log protection; the administrative check failed. "
                                      "No deployment files were changed")
                protected = result == 0
        except (OSError, ValueError) as exc:
            raise DeployError("could not verify native pairing-log protection; the administrative check could not read "
                              "its configuration. No deployment files were changed") from exc
        if not protected:
            raise DeployError("pairing credentials would enter openHAB's native logs; run thermite install "
                              "to protect native logging before deploying")
    try:
        yield
    finally:
        try:
            if direct:
                oh_service.normalize_state(layout.root, uid, gid, deployment=True)
            else:
                oh_cli.trace_run(command)
                subprocess.run(command, check=True)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            repair = argv if direct else ("sudo", *argv)
            raise DeployError("thermite ownership handoff failed; deployment is incomplete. Run "
                              + shlex.join(repair) + f": {exc}") from exc


@dataclasses.dataclass
class Deployed:
    commit: str
    subject: str
    conf: Changes
    atv_app: Changes
    record: dict
    dry_run: bool
    #: Whether the state has the metrics directories (thermite upgrade makes them).
    with_metrics: bool = False
    metrics: Changes = dataclasses.field(default_factory=Changes)
    exporter_app: Changes = dataclasses.field(default_factory=Changes)
    #: What became of each reload and restart the metrics changes asked for, in words.
    reloads: list[str] = dataclasses.field(default_factory=list)
    #: Whether the commit has no metrics/ or no exporter, so the deployed ones were left as they are.
    metrics_kept: bool = False
    #: The history entry this deploy appended (or, dry run, would append): history_entry.
    history: dict = dataclasses.field(default_factory=dict)


def deploy(area: Path, layout: State, var: Path, rev: str = "HEAD", *, force: bool = False,
           dry_run: bool = False, how: str = "deploy", host: dict | None = None,
           ownership_approved: bool = False, expected_conf_digest: str | None = None,
           backup: str | None = None, marker=None) -> Deployed:
    """Install commit `rev`'s conf/ into the state's conf and the helper's
    files into atv/app (and, once the state has their directories, its
    metrics/ into metrics and the exporter into exporter/app), then record
    it; host gives the metrics services' ports. Its conf must have passed
    `thermite test boot` on this host unless `force`; deploying HEAD needs
    a clean area, since make check has just tested the working tree.
    `how` (deploy, install, upgrade or rollback) and `backup` (the state
    backup the upgrade took first) go into the history.

    `marker(line, priority)` receives one start and one end line for the
    journal (journal_marker; the CLI passes it, tests and dry runs do not)."""
    commit = resolve(area, rev)
    mark = None if dry_run else marker
    if mark is not None:
        mark(marker_line("start", commit=commit, how=how, by=getpass.getuser()), "info")
    try:
        result = _deploy(area, layout, var, commit, force=force, dry_run=dry_run, how=how, host=host,
                         ownership_approved=ownership_approved, expected_conf_digest=expected_conf_digest,
                         backup=backup)
    except BaseException as exc:
        if mark is not None:
            mark(marker_line("end", commit=commit, how=how, outcome="failed", error=type(exc).__name__), "warning")
        raise
    if mark is not None:
        mark(end_marker(result), "info")
    return result


def _deploy(area: Path, layout: State, var: Path, commit: str, *, force: bool, dry_run: bool, how: str,
            host: dict | None, ownership_approved: bool, expected_conf_digest: str | None,
            backup: str | None) -> Deployed:
    if commit == resolve(area, "HEAD"):
        dirt = uncommitted(area)
        if dirt:
            listed = "".join(f"\n  {line}" for line in dirt[:10]) + ("\n  ..." if len(dirt) > 10 else "")
            raise DeployError("automation/openhab has changes that are not committed; a deploy installs a commit, "
                              f"and make check has tested the working tree. Commit them first:{listed}")
    files, application = deployment_files(area, commit, var)
    if expected_conf_digest is not None:
        conf = {name.removeprefix("conf/"): blob for name, blob in files.items() if name.startswith("conf/")}
        if tree_digest(conf) != expected_conf_digest:
            raise DeployError("the final configuration differs from the verified historical deployment; "
                              "no deployment files were changed")
    if application is not None:
        from oh_paths import SITE_FILE
        locked = export(area, commit, (SITE_FILE,)).get(SITE_FILE)
        if locked is not None and _site_requirement(locked.data)["version"] == 2:
            host = committed_inputs(area, commit)[1]
    return install(files, commit, subject(area, commit), layout, var, force=force, dry_run=dry_run,
                   how=how, host=host, application=application,
                   ownership_approved=ownership_approved, backup=backup)


def deployment_files(area: Path, commit: str, var: Path) -> tuple[dict[str, Blob], dict | None]:
    """Resolve a committed configuration and application, including derived sites."""
    return prepared_files(area, commit, var)


def _site_requirement(data: bytes) -> dict:
    import oh_release
    requirement = json.loads(data)
    if (not isinstance(requirement, dict) or set(requirement) != {"version", "application", "release"}
            or type(requirement.get("version")) is not int or requirement["version"] not in (1, 2)
            or not isinstance(requirement["application"], str)
            or not oh_release.HEX.fullmatch(str(requirement.get("release")))):
        raise ValueError("invalid site application requirement")
    return requirement


def committed_inputs(area: Path, commit: str) -> tuple[dict, dict, dict | None]:
    """Native pins, host facts and application requirement from Git, never cache."""
    from oh_paths import SITE_FILE
    files = export(area, commit, ("versions.json", "instance/host.yaml", SITE_FILE))
    try:
        versions = json.loads(files["versions.json"].data)
        data = files["instance/host.yaml"].data
        if not isinstance(versions, dict):
            raise ValueError("invalid committed site inputs")
        requirement = _site_requirement(files[SITE_FILE].data) if SITE_FILE in files else None
    except (KeyError, TypeError, ValueError) as exc:
        raise DeployError("the committed site has invalid host, version or application inputs") from exc
    return versions, load_host(data, f"instance/host.yaml at {commit[:12]}"), requirement


def prepared_files(area: Path, commit: str, var: Path) -> tuple[dict[str, Blob], dict | None]:
    """Resolve all generated trees and helper code for one immutable site revision.

    Schema 2 renders only committed data with its exact verified release. A
    missing cache may use the same installed release without writing a cache.
    Schema 1 and full source checkouts retain their committed-tree behavior.
    Rendering uses a cleaned temporary workspace, including during dry-run.
    """
    from oh_paths import SITE_FILE
    import oh_release
    files = export(area, commit, ("conf", "metrics", "deploy", "instance", "versions.json", SITE_FILE, *ATV_APP,
                                  *metrics.EXPORTER_APP))
    locked = files.pop(SITE_FILE, None)
    if locked is None:
        return {name: blob for name, blob in files.items() if not name.startswith("instance/") and name != "versions.json"}, None
    try:
        requirement = _site_requirement(locked.data)
        root = oh_release.resolve(Path(var) / "application-releases", requirement["release"],
                                  requirement["application"])
        release = oh_release.manifest(root, requirement["release"])
        names = tuple(dict.fromkeys((*ATV_APP, *metrics.EXPORTER_APP)))
        application = oh_release.blobs(root, names, requirement["release"])
    except (OSError, ValueError) as exc:
        raise DeployError("the committed site's exact application release is unavailable or failed verification; "
                          "restore that release cache or install its exact reviewed wheel; "
                          "a different release requires an explicit site upgrade") from exc
    if requirement["version"] == 2:
        import oh_site_snapshot
        # A config-only site's launcher is inert scaffolding. Application
        # directories are not inputs and cannot be used as an alternate codebase.
        if export(area, commit, ("lib", "web", "tui", "iso", "site", "pyproject.toml")):
            raise DeployError("a schema-2 private site must not contain application source directories")
        inputs = {name: blob.data for name, blob in files.items() if name.startswith("instance/") or name == "versions.json"}
        inputs[SITE_FILE] = locked.data
        try:
            # A fresh clone has no ignored var/. Use the existing private site
            # for the temporary render rather than creating persistent state
            # during planning. Existing unsafe var paths remain errors.
            work = Path(var)
            if not work.exists() and not work.is_symlink():
                work = Path(area)
            rendered = oh_site_snapshot.render(inputs, root, work, requirement["release"])
        except (OSError, ValueError) as exc:
            raise DeployError("could not derive committed site configuration from its pinned application") from exc
        files = {name: Blob(data) for name, data in rendered.items()}
        # The gateway compiles its own contract from the very same committed
        # inventory, including provider data. Never consult the working tree.
        files.update({name: Blob(data) for name, data in inputs.items() if name.startswith("instance/")})
    else:
        # Never take a schema-1 site's arbitrary lib/ as application authority.
        files = {name: blob for name, blob in files.items() if name.startswith(("conf/", "metrics/", "deploy/"))}
    files.update({name: Blob(data, name.startswith("bin/")) for name, data in application.items()})
    return files, {"version": release["application"], "digest": release["digest"]}


#: The deploy's own metrics, for the dashboards' deploy annotations.
DEPLOY_TEXTFILE = "thermite-deploy.prom"


def deploy_metrics(layout: State, how: str, clock=time.time) -> bool:
    """Tell node_exporter's textfile collector when the last deploy or
    rollback finished; a metrics failure never fails a deploy."""
    directory = layout.textfile
    text = ("# HELP thermite_deploy_timestamp_seconds When the last thermite deploy or rollback finished.\n"
            "# TYPE thermite_deploy_timestamp_seconds gauge\n"
            f'thermite_deploy_timestamp_seconds{{how="{"rollback" if how == "rollback" else "deploy"}"}} '
            f"{clock():.3f}\n")
    temporary = directory / f".{DEPLOY_TEXTFILE}.{os.getpid()}.tmp"
    try:
        if not directory.is_dir() or directory.is_symlink():
            return False
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), FILE_MODE)
            output.write(text)
        os.replace(temporary, directory / DEPLOY_TEXTFILE)
        return True
    except OSError:
        temporary.unlink(missing_ok=True)
        return False


#: The gateway's public trust certificate in its root (oh_install.access).
ACCESS_TRUST = "tls-cert.pem"
TRUST_BYTES = 65536


def gateway_trust(host: dict | None) -> Blob | None:
    """The gateway's private authority certificate, which every client
    trusts, when this host's gateway is installed: a deploy installs it with
    metrics/ as oh_metrics.GATEWAY_TRUST for the HTTPS probe. It is public."""
    if host is None:
        return None
    path = installation(host).access / ACCESS_TRUST
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > TRUST_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return Blob(data) if data.startswith(b"-----BEGIN CERTIFICATE-----") else None


def install(files: dict[str, Blob], commit: str, title: str, layout: State, var: Path, *,
            force: bool = False, dry_run: bool = False, how: str = "deploy",
            host: dict | None = None, post=None, application: dict | None = None,
            ownership_approved: bool = False, backup: str | None = None) -> Deployed:
    """deploy() once the commit's files ({area path: Blob}) are exported."""
    conf = {path[len("conf/"):]: blob for path, blob in files.items() if path.startswith("conf/")}
    app = {path: files[path] for path in ATV_APP if path in files}
    if not conf:
        raise DeployError(f"{commit[:12]} has no automation/openhab/conf/")
    digest = tree_digest(conf)
    if not force and not boot_checked(var, digest):
        raise DeployError(f"`thermite test boot` has not passed on {commit[:12]}'s conf/ (digest {digest[:12]}) on "
                          "this host: run make openhab-test TEST=boot on that commit first, or pass --force to "
                          "install a commit known to be good (thermite rollback needs none for a commit "
                          "deployed here before)")
    preflight(layout)
    with ownership_handoff(layout, host, dry_run, bool(oh_logging.pairing_conf(conf)), approved=ownership_approved):
        previous = read_record(layout)
        pending = read_pending(layout)
        tree = {path[len("metrics/"):]: blob for path, blob in files.items() if path.startswith("metrics/")}
        trust = gateway_trust(host) if tree else None
        if trust is not None:
            # blackbox_exporter's gateway probe checks the gateway's certificate against it.
            tree[metrics.GATEWAY_TRUST] = trust
        exporter = {path: files[path] for path in metrics.EXPORTER_APP if path in files}
        with_metrics = layout.with_metrics()
        # A commit from before the metrics (rolling back past them) has neither: syncing its nothing would delete
        # what the running services read, so they keep what the last deploy installed.
        # Installed notification units must retain their program/config on a
        # rollback to an older revision that has no notification stack.
        notifications_installed = (layout.metrics / "notifications/catalog.json").is_file()
        notifications_present = ("notifications/catalog.json" in tree and "alertmanager/alertmanager.yml" in tree
                                 and "ntfy/server.yml" in tree and metrics.EXPORTER_APP[1] in exporter)
        kept = with_metrics and (not (tree and metrics.EXPORTER_APP[0] in exporter) or (notifications_installed and not notifications_present))
        synced = with_metrics and not kept
        wanted = {"conf": conf, "atv_app": app, **({"metrics": tree, "exporter_app": exporter} if synced else {})}
        # What an earlier deploy recorded, or an interrupted one may have
        # written, is this deploy's to remove if the commit no longer has it.
        installed = {key: {*previous.get(key, {}), *pending.get(key, ())} for key in RECORDED_TREES}
        if not dry_run:
            # Durable before the first write: an interruption leaves every
            # possibly written path known to the next deploy or rollback.
            _write_pending(layout, commit, {**pending, **{key: installed[key] | set(wanted[key]) for key in wanted}})
        conf_changes = sync(conf, layout.conf, installed["conf"], dry_run)
        app_changes = sync(app, layout.atv_app, installed["atv_app"], dry_run)
        metrics_changes = sync(tree, layout.metrics, installed["metrics"], dry_run) if synced else Changes()
        exporter_changes = (sync(exporter, layout.exporter_app, installed["exporter_app"], dry_run)
                            if synced else Changes())
        record = {
            "commit": commit,
            "subject": title,
            "deployed_at": _now(),
            "by": getpass.getuser(),
            "conf_digest": digest,
            "boot_checked": boot_checked(var, digest),
            "conf": {path: sha256(blob.data) for path, blob in conf.items()},
            "atv_app": {path: sha256(blob.data) for path, blob in app.items()},
        }
        if application is not None:
            record["application"] = application
        if synced:
            record["metrics"] = {path: sha256(blob.data) for path, blob in tree.items()}
            record["exporter_app"] = {path: sha256(blob.data) for path, blob in exporter.items()}
        elif kept:
            record.update({key: previous[key] for key in ("metrics", "exporter_app") if key in previous})
        reloads: list[str] = []
        outcomes: list[dict] = []
        changes = {"conf": conf_changes, "atv_app": app_changes,
                   **({"metrics": metrics_changes, "exporter_app": exporter_changes} if synced else {})}
        entry = history_entry(record, how, changes, outcomes, metrics_kept=kept, backup=backup)
        if not dry_run:
            _replace(layout.record, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"), FILE_MODE)
            _settle_pending(layout, commit, {key: paths for key, paths in pending.items() if key not in wanted})
            try:
                if app_changes:
                    # A plain write: the path unit reacts to the file being closed.
                    _shared_write(layout.atv_restart, f"{record['deployed_at']} {commit}\n")
                    outcomes.append({"unit": oh_units.ATV_RESTART_PATH, "outcome": "triggered",
                                     "restarts": [oh_units.ATV]})
                if synced:
                    reloads = apply_metrics(layout, host or {}, metrics_changes, exporter_changes, record,
                                            post or http_post, outcomes)
            except BaseException as exc:
                outcomes.append({"outcome": "failed", "error": type(exc).__name__})
                raise
            finally:
                # Once the reloads are asked for, so the entry says what became
                # of them; and even when one failed, since deployed.json
                # already names this commit.
                _shared_write(layout.history, json.dumps(entry, sort_keys=True) + "\n", append=True)
                deploy_metrics(layout, how)
        return Deployed(commit, record["subject"], conf_changes, app_changes, record, dry_run, with_metrics,
                        metrics_changes, exporter_changes, reloads, kept, entry)


def _touched(changes: Changes) -> list[str]:
    return [*changes.added, *changes.changed, *changes.removed]


def metrics_restart_needed(metrics_changes: Changes, exporter_changes: Changes) -> bool:
    """Whether a deploy's changes need the exporter and Grafana restarted:
    the exporter's code, or Grafana's settings and provisioning (it rereads
    its dashboards and the exporter its map by themselves)."""
    return bool(exporter_changes) or any(
        path.startswith(("notifications/", "ntfy/")) or (path.startswith("grafana/") and not path.startswith(f"{metrics.GRAFANA_DASHBOARDS}/"))
        for path in _touched(metrics_changes))


def apply_metrics(layout: State, host: dict, metrics_changes: Changes, exporter_changes: Changes, record: dict,
                  post, outcomes: list[dict] | None = None) -> list[str]:
    """Make the running metrics services take a deploy's changes, without
    root: Prometheus and blackbox_exporter reload over HTTP, and the trigger
    file has thermite-metrics-restart.path restart the exporter and Grafana.
    Says what became of each, in words; `outcomes` also gets each as it
    happens, for the history: {unit, outcome, and status or restarts}."""
    said = []
    outcomes = [] if outcomes is None else outcomes
    port = metrics.ports(host)
    touched = _touched(metrics_changes)
    for name, prefix, unit, key in (("Prometheus", "prometheus/", oh_units.PROMETHEUS, "prometheus"),
                                    ("blackbox_exporter", "blackbox/", oh_units.BLACKBOX, "blackbox"),
                                    ("Alertmanager", "alertmanager/", oh_units.ALERTMANAGER, "alertmanager")):
        if not any(path.startswith(prefix) for path in touched):
            continue
        status = post(f"http://{metrics.LOOPBACK}:{port[key]}/-/reload")
        if status is None:
            outcomes.append({"unit": unit, "outcome": "not-running"})
            said.append(f"{name} is not running yet; it reads the new configuration when it starts")
        elif 200 <= status < 300:
            outcomes.append({"unit": unit, "outcome": "reloaded", "status": status})
            said.append(f"{name} reloaded its configuration")
        else:
            outcomes.append({"unit": unit, "outcome": "refused", "status": status})
            said.append(f"{name} refused the reload (HTTP {status}) and keeps the old configuration: "
                        f"sudo journalctl -u {unit} says why")
    if metrics_restart_needed(metrics_changes, exporter_changes):
        _shared_write(layout.metrics_restart, f"{record['deployed_at']} {record['commit']}\n")
        outcomes.append({"unit": oh_units.METRICS_RESTART_PATH, "outcome": "triggered",
                         "restarts": list(METRICS_RESTARTED)})
        said.append(f"{Path(METRICS_RESTART_PATH).name} restarts {' and '.join(METRICS_RESTARTED)} "
                    f"({layout.metrics_restart})")
    return said


# --------------------------------------------------------------------------
# the history: <state>/deploy/history, one JSON object a line (thermite history)

#: The most paths a history entry names for each kind of change in each
#: tree; its counts stay exact, and truncated says when it names fewer.
HISTORY_PATHS = 50
#: What a deploy's history line holds besides when, what, who, how and the
#: boot test, in deploys written since the history recorded them.
HISTORY_DETAILS = ("conf_digest", "application", "changes", "metrics_kept", "reloads", "backup")
#: A completed gateway activation's line, written by the install or upgrade
#: that activated it (oh_install.access.Gateway): {"event": "gateway", ...}.
GATEWAY_EVENT = ("at", "by", "commit", "application", "release", "previous_release", "backup")
#: The journal identifier of a deploy's start and end lines: journalctl -t thermite-deploy.
JOURNAL_TAG = "thermite-deploy"


def change_entry(changes: Changes, limit: int = HISTORY_PATHS) -> dict:
    """One tree's changes as the history keeps them: the paths, at most
    `limit` of each kind, and the exact counts."""
    kinds = ("added", "changed", "removed")
    entry: dict = {kind: getattr(changes, kind)[:limit] for kind in kinds}
    entry["counts"] = {**{kind: len(getattr(changes, kind)) for kind in kinds}, "unchanged": changes.unchanged}
    entry["truncated"] = any(len(getattr(changes, kind)) > limit for kind in kinds)
    return entry


def history_entry(record: dict, how: str, changes: dict[str, Changes], reloads: list[dict], *,
                  metrics_kept: bool = False, backup: str | None = None) -> dict:
    """A deploy's history line: deployed.json's who, when and what, how it
    came (deploy, install, upgrade or rollback), each synced tree's changes,
    what became of the reloads and restarts they asked for (apply_metrics),
    and the state backup an upgrade took before it."""
    entry = {key: record[key] for key in ("deployed_at", "commit", "by", "boot_checked", "conf_digest")}
    entry.update(how=how, subject=record["subject"])
    if "application" in record:
        entry["application"] = record["application"]
    entry["changes"] = {tree: change_entry(found) for tree, found in changes.items()}
    if metrics_kept:
        entry["metrics_kept"] = True
    entry["reloads"] = reloads
    if backup is not None:
        entry["backup"] = str(backup)
    return entry


def changed_paths(entry: dict) -> int:
    """How many paths a deploy's history entry says it added, changed or removed."""
    return sum(tree["counts"][kind] for tree in entry.get("changes", {}).values()
               for kind in ("added", "changed", "removed"))


_MARKER_VALUE = re.compile(r"[^A-Za-z0-9._@:+,/-]")


def marker_line(event: str, **fields: object) -> str:
    """`deploy EVENT key=value ...`, one line, its values without spaces or
    `=`, so journal searches can match it whole."""
    return " ".join(("deploy", event, *(f"{key}={_MARKER_VALUE.sub('_', str(value)) or '-'}"
                                        for key, value in fields.items())))


def end_marker(result: Deployed) -> str:
    entry = result.history
    reloads = ",".join(f"{outcome.get('unit', '-')}:{outcome['outcome']}" for outcome in entry.get("reloads", ()))
    return marker_line("end", commit=result.commit, how=entry.get("how", "deploy"), outcome="ok",
                       changes=changed_paths(entry), reloads=reloads or "none")


def journal_marker(line: str, priority: str = "info") -> None:
    """Send one line to the journal as thermite-deploy. Best effort: a host
    without systemd-cat, or a journal that does not take the line within
    seconds, never fails or holds up a deploy. The lines carry commit
    hashes, counts and unit names only."""
    program = shutil.which("systemd-cat")
    if program is None:
        return
    try:
        subprocess.run((program, "-t", JOURNAL_TAG, "-p", priority), input=line + "\n", text=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def _optional_path(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("/") or any(ord(c) < 32 for c in value):
        raise DeployError(f"the gateway {what} is not an absolute path")
    return value


def record_gateway(layout: State, event: dict) -> dict:
    """Append a completed gateway activation to the history: the site
    commit and application it serves, its release, the release it
    replaced (None on the first install) and the complete identity backup
    taken before migrating. thermite history shows it with that commit's
    latest deploy."""
    unknown = set(event) - {"commit", "application", "release", "previous_release", "backup"}
    if unknown or not _COMMIT.match(str(event.get("commit", ""))):
        raise DeployError("a gateway history event names a site commit and the gateway's release, "
                          "previous release, backup and application only")
    application = event.get("application")
    if application is not None and not (isinstance(application, dict)
                                        and all(isinstance(value, str) for value in application.values())):
        raise DeployError("the gateway's application is not a mapping of names")
    line = {"event": "gateway", "at": _now(), "by": getpass.getuser(), "commit": event["commit"],
            "application": application,
            "release": _optional_path(event.get("release"), "release"),
            "previous_release": _optional_path(event.get("previous_release"), "previous release"),
            "backup": _optional_path(event.get("backup"), "backup")}
    if line["release"] is None:
        raise DeployError("a gateway history event needs the activated release")
    _shared_write(layout.history, json.dumps(line, sort_keys=True) + "\n", append=True)
    return line


def history(layout: State, count: int = 10) -> list[str]:
    try:
        return layout.history.read_text(encoding="utf-8").splitlines()[-count:]
    except FileNotFoundError:
        return []


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _history_line(line: str) -> dict | None:
    """One history line: a JSON object, or, as deploys wrote it before they
    recorded the boot test, `TIME COMMIT USER SUBJECT`."""
    if line.startswith("{"):
        try:
            value = json.loads(line)
        except ValueError:
            return None
        if not isinstance(value, dict) or not _COMMIT.match(str(value.get("commit", ""))):
            return None
        return value
    parts = line.split(" ", 3)
    if len(parts) < 3 or not _COMMIT.match(parts[1]):
        return None
    return {"deployed_at": parts[0], "commit": parts[1], "by": parts[2],
            "subject": parts[3] if len(parts) > 3 else ""}


def _history_entry(value: dict) -> dict:
    entry = {"deployed_at": str(value.get("deployed_at", "")), "commit": value["commit"],
             "by": str(value.get("by", "")), "boot_checked": value.get("boot_checked"),
             "how": value.get("how") or "deploy", "subject": str(value.get("subject", ""))}
    entry.update({key: value[key] for key in HISTORY_DETAILS if key in value})
    return entry


def deploys(layout: State) -> list[dict]:
    """Every deploy in the history, oldest first: number (from 1),
    deployed_at, commit, by, boot_checked (None when an old line does not
    say and deployed.json is not about that deploy), how (deploy, install,
    upgrade or rollback), subject, current: the one deployed now, and what
    newer lines recorded: HISTORY_DETAILS, and gateway, the gateway
    activations (GATEWAY_EVENT) recorded after that commit's deploy."""
    try:
        lines = layout.history.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    record = read_record(layout)
    entries: list[dict] = []
    for value in filter(None, map(_history_line, lines)):
        if "event" not in value:
            entries.append(_history_entry(value))
        elif value["event"] == "gateway":
            owner = next((entry for entry in reversed(entries) if entry["commit"] == value["commit"]), None)
            if owner is not None:
                owner.setdefault("gateway", []).append({key: value.get(key) for key in GATEWAY_EVENT})
    now = (record.get("commit"), record.get("deployed_at"))
    current = next((i for i in range(len(entries) - 1, -1, -1)
                    if (entries[i]["commit"], entries[i]["deployed_at"]) == now), None)
    if current is None:
        current = next((i for i in range(len(entries) - 1, -1, -1) if entries[i]["commit"] == now[0]), None)
    for number, entry in enumerate(entries, 1):
        entry["number"] = number
        entry["current"] = number - 1 == current
        if entry["boot_checked"] is None and (entry["commit"], entry["deployed_at"]) == now:
            entry["boot_checked"] = record.get("boot_checked")
    return entries


def hostname() -> str:
    return socket.gethostname().split(".")[0]


def lan_address(probe: str = "192.0.2.1") -> str | None:
    """The address this host reaches the LAN from. Connecting a UDP socket
    only picks a route; nothing is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect((probe, 9))
            return sock.getsockname()[0]
        except OSError:
            return None


def production_url(host: dict, address: str = "127.0.0.1") -> str:
    """Production openHAB on this host: as this host reaches it, or at the
    address (or name) a phone or another computer uses."""
    return f"http://{address}:{host['http_port']}"


def token_file(name: str) -> Path:
    """Where, relative to the area, bin/thermite install saves the API token
    of production on host `name`, and bin/thermite health --production reads it."""
    return Path("var") / name / "api-token"


def running_here(host: dict) -> str | None:
    """How production openHAB shows itself on this host, if it does:
    answering on its HTTP port, or thermite-openhab.service up."""
    url = production_url(host)
    version = openhab_version(url)
    if version:
        return f"production openHAB {version} answers at {url}"
    if service_states((oh_units.RUNTIME,)).get(oh_units.RUNTIME) in ("active", "activating", "reloading"):
        return "thermite-openhab.service, production openHAB, is up here"
    return None


def openhab_version(url: str, timeout: float = 2.0) -> str | None:
    """The version of the openHAB answering at url, or None when nothing,
    or something other than openHAB, answers."""
    oh_cli.trace_http("GET", url.rstrip("/") + "/rest/")
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/rest/", timeout=timeout) as response:
            body = json.load(response)
    except (OSError, ValueError):
        return None
    info = body.get("runtimeInfo") if isinstance(body, dict) else None
    return str(info["version"]) if isinstance(info, dict) and info.get("version") else None


def token_status(url: str, token: str, timeout: float = 5.0) -> int | None:
    """The HTTP status openHAB at url answers a request that only an API
    token gets through (/rest/things): 200 when it accepts token, 401 when
    it refuses it, None when nothing answers."""
    request = urllib.request.Request(url.rstrip("/") + "/rest/things?summary=true",
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    oh_cli.trace_http("GET", request.full_url)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except OSError:
        return None


def http_post(url: str, timeout: float = 5.0) -> int | None:
    """POST with no body: the HTTP status, or None when nothing answers."""
    oh_cli.trace_http("POST", url)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"), timeout=timeout) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except OSError:
        return None


def http_get(url: str, timeout: float = 5.0) -> tuple[int | None, bytes]:
    """(HTTP status or None when nothing answers, body)."""
    oh_cli.trace_http("GET", url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, b""
    except OSError:
        return None, b""


def metrics_installed(installed: dict[str, Path] | None = None) -> bool:
    """Whether any metrics quadlet is installed on this host."""
    return any(Path((installed or INSTALLED)[name]).exists() for name in METRICS_QUADLETS.values())


def prometheus_url(host: dict) -> str:
    return f"http://{metrics.LOOPBACK}:{metrics.ports(host)['prometheus']}"


def grafana_url(host: dict, address: str | None = None) -> str:
    """Grafana as a phone or another computer on the LAN reaches it."""
    return f"http://{address or hostname()}:{metrics.ports(host)['grafana']}/"


@dataclasses.dataclass(frozen=True)
class MetricsHealth:
    """What the metrics services answer: Prometheus' /-/ready and its
    scrape targets (up, total, the ones down), and Grafana's /api/health;
    systemd is whether node_exporter's systemd collector, which reads the
    units over D-Bus, last succeeded (None: Prometheus has no answer yet)."""

    prometheus: str
    ready: int | None
    up: int | None
    total: int | None
    down: tuple[str, ...]
    grafana: str
    grafana_status: int | None
    systemd: bool | None = None

    @property
    def prometheus_ok(self) -> bool:
        return self.ready == 200

    @property
    def grafana_ok(self) -> bool:
        return self.grafana_status == 200

    @property
    def targets_ok(self) -> bool:
        return self.total is not None and self.total > 0 and self.up == self.total

    def describe(self) -> str:
        if not self.prometheus_ok:
            prometheus = f"Prometheus {self.prometheus} " + (
                "not answering" if self.ready is None else f"not ready (HTTP {self.ready})")
        elif self.total is None:
            prometheus = f"Prometheus {self.prometheus} ready, its targets unreadable"
        else:
            prometheus = f"Prometheus {self.prometheus} ready, targets {self.up}/{self.total} up"
        grafana = f"Grafana {self.grafana} " + ("answers" if self.grafana_ok else
                                                "not answering" if self.grafana_status is None else
                                                f"answers HTTP {self.grafana_status}")
        systemd = ("; node_exporter's systemd collector fails (node_scrape_collector_success{collector=\"systemd\"} "
                   "is 0): sudo journalctl -u thermite-node-exporter.service says why" if self.systemd is False else "")
        return f"{prometheus}{systemd}; {grafana}"


def metrics_health(host: dict, address: str | None = None, get=http_get) -> MetricsHealth:
    prometheus = prometheus_url(host)
    ready, _ = get(f"{prometheus}/-/ready")
    up = total = None
    down: list[str] = []
    if ready == 200:
        code, body = get(f"{prometheus}/api/v1/targets?state=active")
        try:
            targets = json.loads(body)["data"]["activeTargets"] if code == 200 else None
        except (ValueError, KeyError, TypeError):
            targets = None
        if isinstance(targets, list):
            total = len(targets)
            up = sum(1 for target in targets if target.get("health") == "up")
            down = sorted(f"{(target.get('labels') or {}).get('job', '?')} "
                          f"{(target.get('labels') or {}).get('instance', '?')}"
                          for target in targets if target.get("health") != "up")
        systemd = collector_success(get, prometheus, "systemd")
    else:
        systemd = None
    grafana = grafana_url(host, address)
    status, _ = get(f"{grafana}api/health")
    return MetricsHealth(prometheus, ready, up, total, tuple(down), grafana, status, systemd)


def collector_success(get, prometheus: str, collector: str) -> bool | None:
    """Whether node_exporter's collector last succeeded, as Prometheus has it
    (None when it has no such series yet, or cannot be read)."""
    query = urllib.parse.urlencode({"query": f'node_scrape_collector_success{{collector="{collector}"}}'})
    code, body = get(f"{prometheus}/api/v1/query?{query}")
    try:
        values = [sample["value"][1] for sample in json.loads(body)["data"]["result"]] if code == 200 else []
    except (ValueError, KeyError, TypeError, IndexError):
        values = []
    return all(value == "1" for value in values) if values else None


def service_states(services: Iterable[str] = SERVICES) -> dict[str, str]:
    if not shutil.which("systemctl"):
        return {}
    services = list(services)
    oh_cli.trace_run(["systemctl", "is-active", *services])
    out = subprocess.run(["systemctl", "is-active", *services], capture_output=True, text=True, check=False)
    return dict(zip(services, out.stdout.split()))
