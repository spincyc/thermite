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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import oh_cli
import oh_logging
import oh_ports
import oh_service
import oh_metrics as metrics

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
QUADLET = "deploy/openhab.container"
ATV_QUADLET = "deploy/openhab-atv.container"
ATV_RESTART_PATH = "deploy/openhab-atv-restart.path"
ATV_RESTART_SERVICE = "deploy/openhab-atv-restart.service"
#: The metrics services' quadlets (oh_metrics.services()), by unit.
METRICS_QUADLETS = {unit: f"deploy/{unit.removesuffix('.service')}.container" for unit in metrics.SERVICE_UNITS}
METRICS_RESTART_PATH = "deploy/openhab-metrics-restart.path"
METRICS_RESTART_SERVICE = "deploy/openhab-metrics-restart.service"
#: What openhab-metrics-restart.service restarts: the services a deploy
#: cannot reload over HTTP.
METRICS_RESTARTED = ("openhab-metrics-exporter.service", "openhab-grafana.service",
                     "openhab-notifications.service", "openhab-ntfy.service")
#: Where the owner installs each generated unit on the host.
INSTALLED = {
    QUADLET: Path("/etc/containers/systemd/openhab.container"),
    ATV_QUADLET: Path("/etc/containers/systemd/openhab-atv.container"),
    ATV_RESTART_PATH: Path("/etc/systemd/system/openhab-atv-restart.path"),
    ATV_RESTART_SERVICE: Path("/etc/systemd/system/openhab-atv-restart.service"),
    **{name: Path("/etc/containers/systemd") / Path(name).name for name in METRICS_QUADLETS.values()},
    METRICS_RESTART_PATH: Path("/etc/systemd/system/openhab-metrics-restart.path"),
    METRICS_RESTART_SERVICE: Path("/etc/systemd/system/openhab-metrics-restart.service"),
}
#: The units of openHAB and its helper, and those of metrics and dashboards.
OPENHAB_UNITS = (QUADLET, ATV_QUADLET, ATV_RESTART_PATH, ATV_RESTART_SERVICE)
METRICS_UNITS = (*METRICS_QUADLETS.values(), METRICS_RESTART_PATH, METRICS_RESTART_SERVICE)
SERVICES = ("openhab.service", "openhab-atv.service", "openhab-atv-restart.path", *metrics.SERVICE_UNITS,
            "openhab-metrics-restart.path")
#: The area's files the Apple TV helper's container runs.
ATV_APP = ("bin/thermite", "lib/oh_cli/__init__.py", "lib/oh_cli/apple_tv.py", "lib/oh_atv.py",
           "lib/requirements-pyatv.txt", "lib/oh_paths.py", "lib/oh_release.py", "lib/oh_ports.py", "lib/oh_service.py")
#: The helper's verb module in commits from before it was apple_tv.py: a
#: deploy of such a commit (a rollback) installs it, or that commit's
#: bin/thermite could not run the helper.
ATV_APP_BEFORE = ("lib/oh_cli/atv.py",)
#: Where a helper quadlet installed before bin/thermite runs the helper from;
#: a deploy keeps a copy of bin/thermite there while that quadlet is installed.
LEGACY_ATV_PROGRAM = "bin/openhab-atv"
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


def load(area: Path) -> tuple[dict, dict]:
    # Here, not at the top: the install imports this module before it has
    # installed PyYAML.
    import yaml

    versions = json.loads((Path(area) / "versions.json").read_text(encoding="utf-8"))
    host = yaml.safe_load((Path(area) / "instance/host.yaml").read_text(encoding="utf-8"))
    oh_service.validate_identity(host["uid"], host["gid"])
    oh_ports.effective(host)
    return versions, host


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
    """The state subvolume's layout (instance/host.yaml state_root)."""

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
    def history(self) -> Path:
        return self.records / "history"

    @property
    def backups(self) -> Path:
        """Where thermite upgrade writes a tar of conf and userdata before it changes anything."""
        return self.root / "backups"

    @property
    def atv_restart(self) -> Path:
        """Written by a deploy that changes the helper; openhab-atv-restart.path watches it."""
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
    def metrics_restart(self) -> Path:
        """Written by a deploy that changes the exporter's code or Grafana's
        settings; openhab-metrics-restart.path watches it."""
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
            (DIR_MODE, [self.metrics, self.exporter_app]),
            (MONITORING_MODE, [self.prometheus_data, self.grafana_data, self.root / metrics.STATE_ALERTMANAGER,
                               self.root / metrics.STATE_NTFY]),
            (SECRETS_MODE, [self.monitoring_secrets]),
        ]

    def with_metrics(self) -> bool:
        """Whether thermite install or upgrade has made the directories a
        deploy installs metrics/ and the exporter into."""
        return self.metrics.is_dir() and self.exporter_app.is_dir()


def state(host: dict) -> State:
    return State(Path(host["state_root"]))


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
    root = host["state_root"]
    return ("podman", "run", "--rm", "--runtime=runc", "--network=host", "--user", f"{host['uid']}:{host['gid']}",
            "--umask=0077", "--cap-drop=all", "--security-opt=no-new-privileges", "--entrypoint=/bin/sh",
            "--env", f"TZ={host['timezone']}",
            "--volume", f"{root}/conf:/openhab/conf:Z",
            "--volume", f"{root}/userdata:/openhab/userdata:Z",
            "--volume", f"{root}/addons:/openhab/addons:Z",
            image(versions), "-ec", openhab_prepare())


def render_quadlet(versions: dict, host: dict) -> str:
    oh_service.validate_identity(host["uid"], host["gid"])
    root = host["state_root"]
    version = versions["openhab"]["version"]
    lines = [
        f"# openHAB {version}. Generated by bin/thermite generate",
        "# from versions.json and instance/host.yaml: change those, then generate again.",
        "[Unit]",
        f"Description=openHAB {version} home automation",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Container]",
        "ContainerName=openhab",
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
        f'Environment="EXTRA_JAVA_OPTS=-Duser.timezone={host["timezone"]} -Duser.home=/openhab/userdata"',
        f"Environment=OPENHAB_HTTP_PORT={host['http_port']}",
        f"Environment=OPENHAB_HTTPS_PORT={host['https_port']}",
        "Exec=-ec " + exec_word(openhab_start()),
        "",
        "[Service]",
        "UMask=0077",
        "Restart=always",
        "TimeoutStartSec=300",
        "TimeoutStopSec=90",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
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
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Container]",
        "ContainerName=openhab-atv",
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
        "UMask=0077",
        "Restart=always",
        "# Unhurried, so a boot without PyPI does not exhaust systemd's start limit.",
        "RestartSec=10",
        "TimeoutStartSec=300",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
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
        "",
        "[Path]",
        f"PathChanged={state(host).atv_restart}",
        "Unit=openhab-atv-restart.service",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_atv_restart_service(host: dict) -> str:
    lines = [
        "# Started by openhab-atv-restart.path.",
        *HEADER,
        "[Unit]",
        "Description=Restart the Apple TV helper after a deploy changed it",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=/usr/bin/systemctl try-restart openhab-atv.service",
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
    root = Path(host["state_root"])
    every = metrics.services(host)
    used = [volume.source for other in every for volume in other.volumes]
    shared = {source for source in used if used.count(source) > 1}
    address = service.listen
    where = ("only this host reaches it" if address.startswith(f"{metrics.LOOPBACK}:") else
             "phones and computers on the LAN reach it")
    lines = [
        f"# {service.description}: metrics and dashboards (lib/oh_metrics, docs/deploy.md).",
        *HEADER,
        "[Unit]",
        f"Description={service.description}",
        "Wants=network-online.target",
        "After=network-online.target",
        *(["After=apparmor.service"] if service.unit == "openhab-node-exporter.service" else []),
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
        "PodmanArgs=--umask=0027",
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
        *(["Environment=CONTAINERS_CONF_OVERRIDE=/etc/thermite/node-exporter-containers.conf"]
          if service.unit == "openhab-node-exporter.service" else []),
        "UMask=0027",
        *(_grafana_conditions(host) if service.image == "grafana" else []),
        *(["# node_exporter exits 143 after an ordinary container SIGTERM.", "SuccessExitStatus=143"]
          if service.unit == "openhab-node-exporter.service" else []),
        "Restart=always",
        "RestartSec=10",
        "# The first start pulls the image.",
        "TimeoutStartSec=300",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
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
        "",
        "[Path]",
        f"PathChanged={state(host).metrics_restart}",
        f"Unit={Path(METRICS_RESTART_SERVICE).name}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_metrics_restart_service(host: dict) -> str:
    lines = [
        f"# Started by {Path(METRICS_RESTART_PATH).name}.",
        *HEADER,
        "[Unit]",
        "Description=Restart the metrics applications after a deploy changed them",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStart=/usr/bin/systemctl try-restart {' '.join(METRICS_RESTARTED)}",
        "",
    ]
    return "\n".join(lines)


def render_units(versions: dict, host: dict) -> dict[str, str]:
    """{area path: text} of every unit the owner installs."""
    units = {
        QUADLET: render_quadlet(versions, host),
        ATV_QUADLET: render_atv_quadlet(versions, host),
        ATV_RESTART_PATH: render_atv_restart_path(host),
        ATV_RESTART_SERVICE: render_atv_restart_service(host),
    }
    for service in metrics.services(host):
        units[METRICS_QUADLETS[service.unit]] = render_metrics_quadlet(versions, host, service)
    units[METRICS_RESTART_PATH] = render_metrics_restart_path(host)
    units[METRICS_RESTART_SERVICE] = render_metrics_restart_service(host)
    return units


def unit_version(text: str | None) -> str | None:
    """The openHAB version an openhab.container was generated for."""
    found = re.search(r"^Description=openHAB (\S+)", text or "", re.MULTILINE)
    return found[1] if found else None


def unit_image(text: str | None) -> str | None:
    """The image (repository@digest) a quadlet runs."""
    found = re.search(r"^Image=(\S+)", text or "", re.MULTILINE)
    return found[1] if found else None


def unit_differences(area: Path, commit: str, installed: dict[str, Path] | None = None) -> list[str]:
    """The units (area paths) whose installed file is not the one `commit`'s
    deploy/ holds; one that cannot be read is left out."""
    files = export(area, commit, tuple(installed or INSTALLED))
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


def installed_units(area: Path, installed: dict[str, Path] | None = None) -> list[tuple[str, Path, str]]:
    """(area path, installed path, "current" | "differs" | "missing" |
    "unreadable") for each unit: whether the owner has installed the one
    this checkout generates."""
    out = []
    for name, target in (installed or INSTALLED).items():
        try:
            same = Path(target).read_bytes() == (Path(area) / name).read_bytes()
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


def _replace(target: Path, data: bytes, mode: int) -> None:
    """Write target by renaming a new file over it: needs only the right to
    write its directory, whoever owns the old file."""
    handle, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".deploy", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fchmod(out.fileno(), mode)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


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


def read_record(layout: State) -> dict:
    try:
        return json.loads(layout.record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


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
def ownership_handoff(layout: State, host: dict | None, dry_run: bool, require_logging: bool):
    """Approve before writes; repair ownership even when a write later fails."""
    argv = ownership_argv(layout, host)
    uid, gid = int(argv[argv.index("--uid") + 1]), int(argv[argv.index("--gid") + 1])
    direct = os.geteuid() == 0 or (os.geteuid() == uid and gid in {os.getegid(), *os.getgroups()})
    command = argv if direct else ("sudo", *argv)
    checks = (*command, "--check-pairing-logging")
    if dry_run:
        if require_logging:
            print("pairing logging check: " + shlex.join(checks))
        print("ownership handoff: " + shlex.join(command))
        yield
        return
    if not direct:
        if not sys.stdin.isatty():
            raise DeployError("deployment needs an explicitly approved thermite ownership handoff; "
                              "run interactively (no files were changed): " + shlex.join(command))
        if shutil.which("sudo") is None:
            raise DeployError("sudo is unavailable for the thermite ownership handoff; no files were changed")
        print("Deployment will run these administrative commands:")
        if require_logging:
            print("  " + shlex.join(checks))
        print("  " + shlex.join(command))
        if input("Approve this ownership handoff before deployment writes? [y/N] ").strip().lower() not in ("y", "yes"):
            raise DeployError("ownership handoff declined; no files were changed")
    if require_logging:
        try:
            if direct:
                protected = oh_service.pairing_logging(layout.root)
            else:
                oh_cli.trace_run(checks)
                protected = subprocess.run(checks, check=False).returncode == 0
        except (OSError, ValueError):
            protected = False
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
            raise DeployError("thermite ownership handoff failed; deployment is incomplete. Run "
                              + shlex.join(command) + f": {exc}") from exc


@dataclasses.dataclass
class Deployed:
    commit: str
    subject: str
    conf: Changes
    atv_app: Changes
    record: dict
    dry_run: bool
    legacy_atv: bool = False
    #: Whether the state has the metrics directories (thermite upgrade makes them).
    with_metrics: bool = False
    metrics: Changes = dataclasses.field(default_factory=Changes)
    exporter_app: Changes = dataclasses.field(default_factory=Changes)
    #: What became of each reload and restart the metrics changes asked for, in words.
    reloads: list[str] = dataclasses.field(default_factory=list)
    #: Whether the commit has no metrics/ or no exporter, so the deployed ones were left as they are.
    metrics_kept: bool = False


def deploy(area: Path, layout: State, var: Path, rev: str = "HEAD", *, force: bool = False,
           dry_run: bool = False, how: str = "deploy", host: dict | None = None) -> Deployed:
    """Install commit `rev`'s conf/ into the state's conf and the helper's
    files into atv/app (and, once the state has their directories, its
    metrics/ into metrics and the exporter into exporter/app), then record
    it; host gives the metrics services' ports. Its conf must have passed
    `thermite test boot` on this host unless `force`; deploying HEAD needs
    a clean area, since make check has just tested the working tree.
    `how` (deploy or rollback) goes into the history."""
    commit = resolve(area, rev)
    if commit == resolve(area, "HEAD"):
        dirt = uncommitted(area)
        if dirt:
            listed = "".join(f"\n  {line}" for line in dirt[:10]) + ("\n  ..." if len(dirt) > 10 else "")
            raise DeployError("automation/openhab has changes that are not committed; a deploy installs a commit, "
                              f"and make check has tested the working tree. Commit them first:{listed}")
    files, application = deployment_files(area, commit, var)
    return install(files, commit, subject(area, commit), layout, var, force=force, dry_run=dry_run,
                   legacy_atv=legacy_atv(), how=how, host=host, application=application)


def deployment_files(area: Path, commit: str, var: Path) -> tuple[dict[str, Blob], dict | None]:
    """Site revisions lock code separately; full checkouts retain Git source export."""
    from oh_paths import SITE_FILE
    import oh_release
    files = export(area, commit, ("conf", "metrics", SITE_FILE, *ATV_APP, *ATV_APP_BEFORE,
                                  LEGACY_ATV_PROGRAM, *metrics.EXPORTER_APP))
    locked = files.pop(SITE_FILE, None)
    if locked is None:
        return files, None
    try:
        requirement = json.loads(locked.data)
        if (not isinstance(requirement, dict) or set(requirement) != {"version", "application", "release"}
                or requirement.get("version") != 1 or not isinstance(requirement["application"], str)
                or not oh_release.HEX.fullmatch(str(requirement.get("release")))):
            raise ValueError("invalid site application requirement")
        root = Path(var) / "application-releases" / requirement["release"]
        release = oh_release.manifest(root, requirement["release"])
        if release["application"] != requirement["application"]:
            raise ValueError("cached release version differs from the site requirement")
        names = tuple(dict.fromkeys((*ATV_APP, *metrics.EXPORTER_APP)))
        application = oh_release.blobs(root, names, requirement["release"])
    except (OSError, ValueError) as exc:
        raise DeployError("the committed site requires its exact cached application release; "
                          "install the reviewed wheel and run thermite init --upgrade before deployment") from exc
    # Never take a site's arbitrary lib/ as application authority.
    files = {name: blob for name, blob in files.items() if name.startswith(("conf/", "metrics/"))}
    files.update({name: Blob(data, name.startswith("bin/")) for name, data in application.items()})
    return files, {"version": release["application"], "digest": release["digest"]}


def legacy_atv(installed: Path = INSTALLED[ATV_QUADLET]) -> bool:
    """Whether the installed helper quadlet still runs LEGACY_ATV_PROGRAM;
    one that cannot be read is assumed to."""
    try:
        return f"/app/{LEGACY_ATV_PROGRAM} " in Path(installed).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return True


def install(files: dict[str, Blob], commit: str, title: str, layout: State, var: Path, *,
            force: bool = False, dry_run: bool = False, legacy_atv: bool = False, how: str = "deploy",
            host: dict | None = None, post=None, application: dict | None = None) -> Deployed:
    """deploy() once the commit's files ({area path: Blob}) are exported.
    With legacy_atv the helper also gets bin/thermite as LEGACY_ATV_PROGRAM;
    a commit from before bin/thermite brings its own LEGACY_ATV_PROGRAM,
    which is what its helper quadlet runs."""
    conf = {path[len("conf/"):]: blob for path, blob in files.items() if path.startswith("conf/")}
    app = {path: files[path] for path in (*ATV_APP, *ATV_APP_BEFORE, LEGACY_ATV_PROGRAM) if path in files}
    copied = legacy_atv and "bin/thermite" in app and LEGACY_ATV_PROGRAM not in app
    if copied:
        app[LEGACY_ATV_PROGRAM] = app["bin/thermite"]
    if not conf:
        raise DeployError(f"{commit[:12]} has no automation/openhab/conf/")
    digest = tree_digest(conf)
    if not force and not boot_checked(var, digest):
        raise DeployError(f"`thermite test boot` has not passed on {commit[:12]}'s conf/ (digest {digest[:12]}) on "
                          "this host: run make openhab-test TEST=boot on that commit first, or pass --force to "
                          "install a commit known to be good (thermite rollback needs none for a commit "
                          "deployed here before)")
    preflight(layout)
    with ownership_handoff(layout, host, dry_run, bool(oh_logging.pairing_conf(conf))):
        previous = read_record(layout)
        conf_changes = sync(conf, layout.conf, previous.get("conf", {}), dry_run)
        app_changes = sync(app, layout.atv_app, previous.get("atv_app", {}), dry_run)
        tree = {path[len("metrics/"):]: blob for path, blob in files.items() if path.startswith("metrics/")}
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
        metrics_changes = sync(tree, layout.metrics, previous.get("metrics", {}), dry_run) if synced else Changes()
        exporter_changes = (sync(exporter, layout.exporter_app, previous.get("exporter_app", {}), dry_run)
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
        if not dry_run:
            _replace(layout.record, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"), FILE_MODE)
            entry = {key: record[key] for key in ("deployed_at", "commit", "by", "boot_checked")}
            if application is not None:
                entry["application"] = application
            _shared_write(layout.history, json.dumps({**entry, "how": how, "subject": record["subject"]}) + "\n", append=True)
            if app_changes:
                # A plain write: the path unit reacts to the file being closed.
                _shared_write(layout.atv_restart, f"{record['deployed_at']} {commit}\n")
            if synced:
                reloads = apply_metrics(layout, host or {}, metrics_changes, exporter_changes, record, post or http_post)
        return Deployed(commit, record["subject"], conf_changes, app_changes, record, dry_run, copied, with_metrics,
                        metrics_changes, exporter_changes, reloads, kept)


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
                  post) -> list[str]:
    """Make the running metrics services take a deploy's changes, without
    root: Prometheus and blackbox_exporter reload over HTTP, and the trigger
    file has openhab-metrics-restart.path restart the exporter and Grafana."""
    said = []
    port = metrics.ports(host)
    touched = _touched(metrics_changes)
    for name, prefix, unit, key in (("Prometheus", "prometheus/", "openhab-prometheus.service", "prometheus"),
                                    ("blackbox_exporter", "blackbox/", "openhab-blackbox.service", "blackbox"),
                                    ("Alertmanager", "alertmanager/", "openhab-alertmanager.service", "alertmanager")):
        if not any(path.startswith(prefix) for path in touched):
            continue
        status = post(f"http://{metrics.LOOPBACK}:{port[key]}/-/reload")
        if status is None:
            said.append(f"{name} is not running yet; it reads the new configuration when it starts")
        elif 200 <= status < 300:
            said.append(f"{name} reloaded its configuration")
        else:
            said.append(f"{name} refused the reload (HTTP {status}) and keeps the old configuration: "
                        f"sudo journalctl -u {unit} says why")
    if metrics_restart_needed(metrics_changes, exporter_changes):
        _shared_write(layout.metrics_restart, f"{record['deployed_at']} {record['commit']}\n")
        said.append(f"{Path(METRICS_RESTART_PATH).name} restarts {' and '.join(METRICS_RESTARTED)} "
                    f"({layout.metrics_restart})")
    return said


def history(layout: State, count: int = 10) -> list[str]:
    try:
        return layout.history.read_text(encoding="utf-8").splitlines()[-count:]
    except FileNotFoundError:
        return []


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _history_entry(line: str) -> dict | None:
    """One history line: a JSON object, or, as deploys wrote it before they
    recorded the boot test, `TIME COMMIT USER SUBJECT`."""
    if line.startswith("{"):
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        if not isinstance(entry, dict) or not _COMMIT.match(str(entry.get("commit", ""))):
            return None
    else:
        parts = line.split(" ", 3)
        if len(parts) < 3 or not _COMMIT.match(parts[1]):
            return None
        entry = {"deployed_at": parts[0], "commit": parts[1], "by": parts[2],
                 "subject": parts[3] if len(parts) > 3 else ""}
    return {"deployed_at": str(entry.get("deployed_at", "")), "commit": entry["commit"],
            "by": str(entry.get("by", "")), "boot_checked": entry.get("boot_checked"),
            "how": entry.get("how") or "deploy", "subject": str(entry.get("subject", ""))}


def deploys(layout: State) -> list[dict]:
    """Every deploy in the history, oldest first: number (from 1),
    deployed_at, commit, by, boot_checked (None when an old line does not
    say and deployed.json is not about that deploy), how (deploy or
    rollback), subject, and current: the one deployed now."""
    try:
        lines = layout.history.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    record = read_record(layout)
    entries = [entry for entry in map(_history_entry, lines) if entry]
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
    answering on its HTTP port, or openhab.service up."""
    url = production_url(host)
    version = openhab_version(url)
    if version:
        return f"production openHAB {version} answers at {url}"
    if service_states(("openhab.service",)).get("openhab.service") in ("active", "activating", "reloading"):
        return "openhab.service, production openHAB, is up here"
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
                   "is 0): sudo journalctl -u openhab-node-exporter.service says why" if self.systemd is False else "")
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
