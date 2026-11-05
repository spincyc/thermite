"""Trusted, stdlib-only administrative helpers for the unified installer.

Execute the operator-owned copy with system Python, never a service-owned
interpreter. Reading old configuration does not import Django or initialize
site authority. The install engine sets the identity state aside before a
migration; nothing here archives it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import grp
import http.client
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import time
from urllib.parse import urlsplit


#: Native REST statuses retried during reloads. This helper runs standalone
#: under `python3 -I -S`, so it mirrors oh_http.TRANSIENT_STATUSES; failed
#: connections are retried too. Tests keep both rules identical.
TRANSIENT_STATUSES = frozenset({404, 429, 500, 502, 503, 504})


class DeploymentError(ValueError):
    pass


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise DeploymentError("deployment paths must be absolute, non-root paths without traversal")
    return path


@contextlib.contextmanager
def directory(value):
    path = absolute(value)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read(value, *, maximum=2 * 1024 * 1024, owner=None, private=False):
    path = absolute(value)
    with directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum
                    or owner is not None and info.st_uid != owner
                    or private and stat.S_IMODE(info.st_mode) != 0o600):
                raise DeploymentError("deployment input must be a bounded regular file with expected custody")
            content = source.read(maximum + 1)
            if len(content) > maximum:
                raise DeploymentError("deployment input exceeds its bound")
            return content


def _write(parent, name, content, uid, gid):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(descriptor, "wb") as destination:
        os.fchmod(destination.fileno(), 0o600)
        os.fchown(destination.fileno(), uid, gid)
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())


def adopt(config, destination, *, uid, gid, service_uid):
    """Export only bounded gateway inputs to a new operator-owned directory.

    The service controls these data bytes, so none are interpreted as commands,
    executable paths, unit sources, or ownership instructions.
    """
    config, destination = absolute(config), absolute(destination)
    original = json.loads(read(config, maximum=65536, owner=service_uid))
    fields = {"version", "state", "contract_file", "runtime_url", "runtime_token_file", "allowed_hosts",
              "bind_host", "port", "tls_cert", "tls_key", "timezone"}
    # Upgrades adopt the installed release's configuration, so version 1 is
    # read until a release with version 2 (the Guest listener) is installed.
    # guest_http is not adopted: host.yaml supplies it afresh.
    known = {1: fields, 2: fields | {"guest_http"}}
    if not isinstance(original, dict) or type(original.get("version")) is not int or known.get(original["version"]) != set(original):
        raise DeploymentError("installed gateway configuration is not recognized")
    state = absolute(original["state"])
    if state == config.parent or config.parent in state.parents:
        raise DeploymentError("installed authority must be separate from its release")
    material = {"tls-cert.pem": read(original["tls_cert"], maximum=65536, owner=service_uid),
                "tls-key.pem": read(original["tls_key"], maximum=65536, owner=service_uid)}
    callback_path = config.parent / "callbacks.json"
    try:
        material["callbacks.json"] = read(callback_path, maximum=256 * 1024, owner=service_uid)
    except FileNotFoundError:
        pass
    # The installer derives the hosts, bind address and timezone from the host
    # each run; the port is returned only to say when it moves.
    result = {key: original[key] for key in ("state", "runtime_url", "port")}
    result.update(previous_release=str(config.parent), callbacks="callbacks.json" in material,
                  tls_cert=str(destination / "tls-cert.pem"), tls_key=str(destination / "tls-key.pem"))
    with directory(destination.parent) as parent:
        parent_info = os.fstat(parent)
        if parent_info.st_uid != uid or stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise DeploymentError("adoption parent must be private to the operator")
        os.mkdir(destination.name, 0o700, dir_fd=parent)
        child = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            os.fchmod(child, 0o700)
            # Keep the directory root-owned until every input is durable.
            for name, content in material.items():
                _write(child, name, content, uid, gid)
            _write(child, "adopted.json", (json.dumps(result) + "\n").encode(), uid, gid)
            os.fchown(child, uid, gid)
            os.fsync(child)
            os.fsync(parent)
        finally:
            os.close(child)
    return result


def live(release, pids):
    expected = str(absolute(release))
    for pid in pids:
        if not pid.isdecimal() or int(pid) <= 0 or os.readlink("/proc/" + pid + "/cwd") != expected:
            raise DeploymentError("gateway is not running the expected release")


def upstream(config, expectations, *, service=None, connection_factory=http.client.HTTPConnection,
             clock=time.monotonic, sleep=time.sleep):
    """Prove the actual staged bearer works, without Django or root networking.

    Read the operator's expectations before dropping privileges. All service
    data and native HTTP are then accessed as thermite, using trusted stdlib
    code already loaded from the operator-owned helper.
    """
    expected = json.loads(read(expectations, maximum=65536, private=True))
    if not isinstance(expected, dict) or set(expected) != {"runtime_url", "runtime_credential_sha256"}:
        raise DeploymentError("invalid staged upstream expectations")
    if service is None:
        account, group = pwd.getpwnam("thermite"), grp.getgrnam("thermite")
        if account.pw_gid != group.gr_gid:
            raise DeploymentError("thermite service identity has changed")
        service = account.pw_uid, group.gr_gid
    uid, gid = service
    if uid <= 0 or gid <= 0:
        raise DeploymentError("staged upstream checks require a non-root service identity")
    if os.geteuid() != uid:
        if os.geteuid() != 0:
            raise DeploymentError("cannot assume the staged gateway service identity")
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    if os.getegid() != gid:
        raise DeploymentError("staged gateway primary group differs")
    data = json.loads(read(config, maximum=65536, owner=uid, private=True))
    if data.get("runtime_url") != expected["runtime_url"]:
        raise DeploymentError("staged gateway native origin differs from this deployment")
    origin = urlsplit(expected["runtime_url"])
    if (origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "::1"}
            or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise DeploymentError("staged upstream must be an explicit loopback HTTP origin")
    raw = read(data["runtime_token_file"], maximum=16384, owner=uid, private=True)
    if hashlib.sha256(raw).hexdigest() != expected["runtime_credential_sha256"]:
        raise DeploymentError("staged gateway runtime credential differs; restage the current deployment")
    token = raw.decode("utf-8").strip()
    if not token or len(token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise DeploymentError("staged gateway runtime credential is invalid")
    # Configuration reloads can briefly unregister REST resources after the
    # boundary check. Retry only transient reads; custody and auth failures
    # never become permission to replace credentials or accept a bad response.
    deadline = clock() + 30
    while clock() < deadline:
        connection = None
        try:
            connection = connection_factory(origin.hostname, origin.port or 80, timeout=min(5, deadline - clock()))
            headers = {"Accept": "application/json"}
            headers["Authorization"] = "Bearer " + token
            connection.request("GET", "/rest/items?recursive=false&fields=name", headers=headers)
            response = connection.getresponse()
            body = response.read(256 * 1024 + 1)
            if response.status == 200:
                try:
                    valid = len(body) <= 256 * 1024 and isinstance(json.loads(body), list)
                except ValueError:
                    valid = False
                if not valid:
                    raise DeploymentError("staged gateway authenticated native readiness failed")
                if clock() < deadline:
                    return
            elif response.status not in TRANSIENT_STATUSES:
                raise DeploymentError("staged gateway authenticated native readiness failed")
        except (OSError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        sleep(min(1, max(0, deadline - clock())))
    raise DeploymentError("staged gateway authenticated native readiness failed after 30 seconds; native REST did not settle")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("adopt")
    command.add_argument("config")
    command.add_argument("destination")
    for name in ("uid", "gid", "service-uid"):
        command.add_argument("--" + name, type=int, required=True)
    command = sub.add_parser("live")
    command.add_argument("release")
    command.add_argument("pids", nargs="+")
    command = sub.add_parser("upstream")
    command.add_argument("config")
    command.add_argument("expectations")
    args = vars(parser.parse_args(argv))
    action = args.pop("action")
    try:
        globals()[action](**args)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("thermite gateway deployment: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
