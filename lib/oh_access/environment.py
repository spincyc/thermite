"""The Python environment of the access services, which run in containers.

The pinned Python image builds it with its own interpreter, at the path its
containers see it at, from a wheelhouse alone and without a network: the
host's Python has no part in it, so a host that moves to another Python
leaves these services as they were. The wheelhouse is filled before an
install transaction begins (oh_install.steps.GatewayWheels), by the same
image's pip, and every wheel is checked against the hashes in
lib/requirements-site.txt each time one is used.

This module only says what to run. Each command is a `podman run` for the
caller to hand to root's podman, which holds the image. The process that
fills the wheelhouse or builds the environment is the calling operator, not
root: it writes the operator's wheelhouse and the release being staged, both
the operator's until the custody handoff. A service command
(service_command) runs after that handoff, as the service account.
"""

import hashlib
import os
from pathlib import Path
import re

from . import AccessError
from .files import STATED_IDENTITY

#: The versions.json name of the pinned Python image: the Apple TV helper's
#: and the metrics exporter's too, so every site already pins and pulls it.
IMAGE = "atv_helper"
#: The hash-pinned requirements, below the application and below a release's
#: program.
REQUIREMENTS = "lib/requirements-site.txt"
#: A release's environment.
DIRECTORY = "env"
#: The operator's cache of wheelhouses, below the site.
STORE = Path("build/sandbox/downloads/wheels")
#: What the services import that carries compiled code or is their server:
#: loaded once in the image when the environment is built, so a wheel that
#: installs but cannot run there fails the stage, not a service's start.
IMPORTS = ("argon2", "django", "h2", "httpx", "hypercorn", "prometheus_client", "yaml")

_PLAIN = re.compile(r"/[A-Za-z0-9_@+=,./-]+")
_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", re.MULTILINE)
_PIP = "-I -m pip --isolated --no-cache-dir --disable-pip-version-check"
_OFFLINE = "--no-index --find-links /wheels --require-hashes --only-binary :all:"


def plain(path) -> str:
    """A path as a volume and a unit can name it: podman splits a volume at
    ':' and systemd reads '%', '$', quotes and spaces in a generated unit."""
    text = str(path)
    if not _PLAIN.fullmatch(text) or ".." in Path(text).parts:
        raise AccessError(f"a containerized access service cannot use the path {text!r}: only letters, digits "
                          "and _@+=,./- are safe in a volume and a unit")
    return text


def pins(requirements: bytes) -> int:
    """How many distributions the requirements pin."""
    return len(_PIN.findall(requirements.decode()))


def wheelhouse(area, image: str, requirements: bytes) -> Path:
    """The directory that holds exactly these pins' wheels for this image's
    Python: named by both, so neither a changed pin nor another Python
    finds a set made for the other."""
    key = hashlib.sha256(image.encode() + b"\0" + requirements).hexdigest()[:16]
    return Path(area) / STORE / key


def _run(image: str, network: str, volumes: list[str], script: str, *arguments: str) -> list[str]:
    return ["podman", "run", "--rm", "--runtime=runc", f"--network={network}", "--read-only",
            "--user", f"{os.geteuid()}:{os.getegid()}", "--umask=0077", "--cap-drop=all",
            "--security-opt=no-new-privileges", "--entrypoint=/bin/sh", "--env", "HOME=/tmp",
            *(part for volume in volumes for part in ("--volume", volume)),
            image, "-euc", script, "thermite-environment", *arguments]


def fetch_command(image: str, wheels, requirements: str = "requirements-site.txt") -> list[str]:
    """Download into `wheels` what `requirements` pins and it does not hold yet.
    The one command here that uses a network."""
    return _run(image, "host", [f"{plain(wheels)}:/wheels:z"],
                f"exec python {_PIP} download --require-hashes --only-binary :all: --dest /wheels "
                f"-r /wheels/{requirements}")


def verify_command(image: str, wheels, requirements: str = "requirements-site.txt") -> list[str]:
    """Resolve the pins from `wheels` alone, as a stage will, installing
    nothing: pip checks each wheel it would use against the pinned hashes
    and that it suits this image's Python."""
    return _run(image, "none", [f"{plain(wheels)}:/wheels:ro,z"],
                f"exec python {_PIP} install --dry-run --ignore-installed --target /tmp/unused {_OFFLINE} "
                f"-r /wheels/{requirements}")


def build_command(image: str, release, wheels) -> list[str]:
    """Build `release`/env from the release's own requirements
    and `wheels` alone. --copies: the environment holds its interpreter's
    launcher, never a link out of the release (custody refuses those)."""
    release = Path(plain(release))
    target = release / DIRECTORY
    return _run(image, "none",
                [f"{target}:{target}:z", f"{release / 'program' / REQUIREMENTS}:/requirements.txt:ro,z",
                 f"{plain(wheels)}:/wheels:ro,z"],
                'python -m venv --copies "$1"; '
                f'"$1/bin/python" {_PIP} install {_OFFLINE} -r /requirements.txt; '
                f'"$1/bin/python" {_PIP} check; '
                f'exec "$1/bin/python" -I -c "import {", ".join(IMPORTS)}"', str(target))


def service_command(image: str, release, identity, verb: str, *, state) -> list[str]:
    """Run `thermite access VERB` of `release` once, as its services run:
    in the pinned image, as the service account by number, which the
    command states as a unit does (files.require_service_account), with a
    read-only root and no capability. It sees the release, read-only, and
    the identity state, and has no network: this is for the commands an
    install runs before the services start, which use none."""
    release, (uid, gid) = Path(plain(release)), identity
    state = plain(state)
    return ["podman", "run", "--rm", "--runtime=runc", "--network=none", "--read-only", "--user", f"{uid}:{gid}",
            "--umask=0077", "--cap-drop=all", "--security-opt=no-new-privileges",
            "--env", f"{STATED_IDENTITY}={uid}:{gid}", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--volume", f"{release}:{release}:ro,z", "--volume", f"{state}:{state}:z", f"--workdir={release}",
            image, str(release / DIRECTORY / "bin/python"), str(release / "program/bin/thermite"), "access", verb,
            "--descriptor", str(release / "config.json")]
