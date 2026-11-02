"""Deploy openHAB on this host: generate its systemd units, install a commit's
conf/, metrics/, Apple TV helper and metrics exporter into the state, and
report what runs. The install
itself is oh_install. Nothing here names a host: production is wherever
bin/thermite install ran, and its name and address are found at run time.

The openHAB quadlet has the shape of the factory services role's (ADR 0054)
so that role can adopt openHAB unchanged once it supports more than one
volume.

A deploy installs one commit, never the working tree, and runs as the
owner's user, a member of the openhab group, without root. The openHAB
image's entrypoint chowns everything below its home, conf/ included, to
openhab at every start but leaves modes alone. So a deploy creates
directories 2775 and files 0664 (0775 for a program), and it only ever
creates entries, renames a new file over an old one and deletes entries:
it never chmods or chowns one it did not just create, which that user may
no longer own.
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
import shutil
import socket
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import oh_cli
import oh_logging
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
           "lib/requirements-pyatv.txt", "lib/oh_paths.py", "lib/oh_release.py")
#: The helper's verb module in commits from before it was apple_tv.py: a
#: deploy of such a commit (a rollback) installs it, or that commit's
#: bin/thermite could not run the helper.
ATV_APP_BEFORE = ("lib/oh_cli/atv.py",)
#: Where a helper quadlet installed before bin/thermite runs the helper from;
#: a deploy keeps a copy of bin/thermite there while that quadlet is installed.
LEGACY_ATV_PROGRAM = "bin/openhab-atv"
#: Inside the helper's container.
ATV_VENV = "/venv/pyatv"
DIR_MODE = 0o2775
FILE_MODE = 0o664
PROGRAM_MODE = 0o775
#: The secrets directory: the openhab group reads and writes it, no one else
#: enters it.
SECRETS_MODE = 0o2770
#: Prometheus' and Grafana's stores and the metrics secrets: written only by
#: the services (as openhab), read by the group.
MONITORING_MODE = 0o750
SECRET_FILE_MODE = 0o640
#: Where the openHAB container sees the secrets directory, as $OPENHAB_SECRETS.
SECRETS_MOUNT = "/secrets"
#: Ports besides HTTP and HTTPS that openHAB or the helper listen on.
HELPER_PORT = 8099
KARAF_PORT = 8101
LSP_PORT = 5007


class DeployError(Exception):
    """A deploy that must not go ahead, with the reason for the owner."""


def load(area: Path) -> tuple[dict, dict]:
    # Here, not at the top: the install imports this module before it has
    # installed PyYAML.
    import yaml

    versions = json.loads((Path(area) / "versions.json").read_text(encoding="utf-8"))
    host = yaml.safe_load((Path(area) / "instance/host.yaml").read_text(encoding="utf-8"))
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
        the owner writes them through the group. No rule reads one today: the
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
        """(mode, directories) the install creates, owned by openhab. The
        deploying user writes the 2775 ones through the group."""
        return [
            (0o755, [self.userdata, self.addons, self.atv, self.atv_venv]),
            (DIR_MODE, [self.conf, self.records, self.atv_app]),
            (0o700, [self.atv_secrets]),
            (SECRETS_MODE, [self.secrets]),
        ]

    def metrics_directories(self) -> list[tuple[int, list[Path]]]:
        """(mode, directories) the metrics services need, owned by openhab:
        the deploying user writes the 2775 ones through the group; the stores
        and the secrets are the services' own, which the group only reads."""
        return [
            (0o755, [self.monitoring, self.exporter]),
            (DIR_MODE, [self.metrics, self.exporter_app]),
            (MONITORING_MODE, [self.prometheus_data, self.grafana_data, self.root / metrics.STATE_ALERTMANAGER,
                               self.root / metrics.STATE_NTFY, self.monitoring_secrets]),
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


def render_quadlet(versions: dict, host: dict) -> str:
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
        f"Volume={root}/conf:/openhab/conf:Z",
        f"Volume={root}/userdata:/openhab/userdata:Z",
        f"Volume={root}/addons:/openhab/addons:Z",
        "# Keys the rules may read: outside /openhab, which the entrypoint",
        "# chowns, and never in conf.",
        f"Volume={root}/secrets:{SECRETS_MOUNT}:ro,Z",
        f"Environment=OPENHAB_SECRETS={SECRETS_MOUNT}",
        f"Environment=USER_ID={host['uid']}",
        f"Environment=GROUP_ID={host['gid']}",
        "Environment=CRYPTO_POLICY=unlimited",
        f"Environment=TZ={host['timezone']}",
        f"Environment=EXTRA_JAVA_OPTS=-Duser.timezone={host['timezone']}",
        f"Environment=OPENHAB_HTTP_PORT={host['http_port']}",
        f"Environment=OPENHAB_HTTPS_PORT={host['https_port']}",
        "",
        "[Service]",
        "Restart=always",
        "TimeoutStartSec=300",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)


def render_atv_quadlet(versions: dict, host: dict) -> str:
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
        "# Its code, as the last deploy installed it.",
        f"Volume={layout.atv_app}:/app:ro,Z",
        f"Volume={layout.atv_venv}:/venv:Z",
        f"Volume={layout.atv_secrets}:/secrets:Z",
        f"Environment=OPENHAB_ATV_VENV={ATV_VENV}",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        f"Environment=TZ={host['timezone']}",
        f"# The first start builds {ATV_VENV} from the hash-pinned wheels in",
        "# lib/requirements-pyatv.txt (from PyPI); later starts reuse it until",
        "# those pins change.",
        "Exec=python3 /app/bin/thermite apple-tv serve --setup --secrets /secrets",
        "",
        "[Service]",
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


def render_metrics_quadlet(versions: dict, host: dict, service: metrics.Service) -> str:
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
    for path in sorted(files):
        destination = target / _relative(path)
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
    for path in sorted(set(previous) - set(files)):
        destination = target / _relative(path)
        if destination.is_symlink() or destination.is_file():
            changes.removed.append(path)
            if not dry_run:
                destination.unlink()
                _prune(target, destination.parent)
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
        if not os.access(directory, os.W_OK | os.X_OK):
            raise DeployError(f"{user} cannot write {directory}: join the openhab group, then log in again "
                              "(`id -nG` lists your groups; docs/deploy.md)")


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
    if oh_logging.pairing_conf(conf) and not oh_logging.ready(layout.userdata):
        script = Path(__file__).resolve().with_name("oh_logging.py")
        port = (host or {}).get("http_port", 8080)
        raise DeployError("pairing credentials would enter openHAB's native logs; before deploying, stop openHAB, "
                          f"run `sudo python3 {script} {layout.userdata} --http-port {port}`, "
                          "then start openHAB to load the filter and deploy again (docs/deploy.md)")
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
    kept = with_metrics and (not (tree and exporter) or (notifications_installed and not notifications_present))
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
        with open(layout.history, "a", encoding="utf-8") as log:
            log.write(json.dumps({**entry, "how": how, "subject": record["subject"]}) + "\n")
        if app_changes:
            # A plain write: the path unit reacts to the file being closed.
            with open(layout.atv_restart, "w", encoding="utf-8") as trigger:
                trigger.write(f"{record['deployed_at']} {commit}\n")
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
        with open(layout.metrics_restart, "w", encoding="utf-8") as trigger:
            trigger.write(f"{record['deployed_at']} {record['commit']}\n")
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
        return self.total is not None and self.up == self.total

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
