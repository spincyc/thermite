"""The gateway lifecycle inside the one approved installation workflow."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl

import oh_ports
from oh_access import AccessError, staging
from oh_access.files import atomic_private, json_private, private_directory, read_private, service_identity
from oh_install.model import Check, Command, Step, Task


class _UnitAuthorityError(AccessError):
    """An unknown unit override must not be overwritten as a routine repair."""


@contextmanager
def site_inputs(root):
    """Bind legacy renderer modules to the same immutable input snapshot."""
    import oh_paths
    from oh_features import remote
    from oh_integrations import globalcache_ir
    previous = oh_paths.SITE, remote.INSTANCE, globalcache_ir.INSTANCE
    oh_paths.SITE, remote.INSTANCE, globalcache_ir.INSTANCE = root, root / "instance", root / "instance"
    try:
        yield
    finally:
        oh_paths.SITE, remote.INSTANCE, globalcache_ir.INSTANCE = previous


def installed_release(system):
    """Discover opaque service state through the installed, root-owned unit."""
    origin = system.unit_origin("thermite-access.service")
    if not origin.known:
        raise AccessError("cannot determine the installed gateway unit; inspect systemd before retrying")
    if origin.load_state == "not-found":
        # A partially replaced unit set is not a fresh site. In particular an
        # older worker may still point at a nondefault identity directory.
        for name in staging.ACCESS_UNITS[1:]:
            sibling = system.unit_origin(name)
            if not sibling.known or sibling.load_state != "not-found":
                raise AccessError("gateway unit is missing while related access units remain; restore its protected unit before adopting state")
        return None
    if origin.drop_ins or origin.transient or origin.source or not origin.fragment:
        raise AccessError("installed gateway has unexpected overrides or unit provenance")
    unit = system.unit_file(Path(origin.fragment))
    if unit is None or unit.uid != 0 or unit.mode & 0o022 or not unit.ancestors_trusted:
        raise AccessError("installed gateway unit is not protected by root custody")
    values = {}
    section = None
    for line in unit.content.decode().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line and not line.startswith(("#", ";")):
            key, value = line.split("=", 1)
            if key in values:
                raise AccessError("installed gateway has ambiguous service directives")
            values[key] = value
    root = Path(values.get("WorkingDirectory", "").replace("%%", "%"))
    if (values.get("User") != "thermite" or values.get("Group") != "thermite"
            or not root.is_absolute() or ".." in root.parts or root == Path("/")):
        raise AccessError("installed gateway does not identify its persistent release safely")
    return root


class Gateway(Step):
    """Prepare, migrate and activate a release, retaining complete rollback data.

    ``boundary`` is an optional installer hook called after inputs are adopted.
    It receives (system, metadata, existing_callback_path) and returns a private
    callback configuration path or None. Existing callbacks cannot be discarded.
    """

    title = "HTTPS gateway and identity state"
    always = True
    stop_on_failure = True

    def __init__(self, context, *, boundary=None, callbacks=None, options=None):
        self.context = context
        self.boundary = boundary
        self.callbacks = callbacks
        self.options = options or {}
        import oh_release
        application = context.application or {"version": oh_release.VERSION, "source_commit": context.head}
        self.versions = {"site": context.head, "application": application}
        identifier = hashlib.sha256(json.dumps({**self.versions, "configuration": context.conf_digest}, sort_keys=True).encode()).hexdigest()[:24]
        self.identifier = identifier
        self.root = Path(self.options.get("root", str(context.layout.root) + "-access"))
        self.directory = context.var / "gateway" / identifier
        self.journal_path = self.directory / "deployment.json"
        self.helper = self.directory / "deployment.py"

    def check(self, system):
        try:
            old = installed_release(system)
            detail = "upgrade the gateway and preserve its identity, TLS and callbacks" if old else "install the persistent HTTPS gateway and identity state"
            return Check(False, detail)
        except (OSError, ValueError, AccessError) as exc:
            return Check(False, "", str(exc))

    def actions(self, system):
        return [Task("prepare gateway, stop writers, back up identity, migrate, activate and verify HTTPS", self.perform)]

    def _run(self, system, *argv):
        from oh_install.runner import perform
        argv = tuple(str(value) for value in argv)
        root = argv[0] == "sudo"
        code = perform(Command(argv[1:] if root else argv, root=root, terminal=root), system, self.context.area)
        if code is None or code:
            raise AccessError("gateway deployment command failed; rerun the same command to resume")

    def _helper(self, system, *argv):
        staging._trusted_ancestors(self.directory, "Gateway plan")
        self._run(system, "sudo", "/usr/bin/python3", "-I", "-S", self.helper, *argv)

    def _save(self, journal, **changes):
        journal.update(changes)
        json_private(self.journal_path, journal)

    def _load(self):
        try:
            result = json.loads(read_private(self.journal_path))
        except FileNotFoundError:
            return None
        if not isinstance(result, dict) or result.get("version") != 1 or result.get("versions") != self.versions:
            raise AccessError("gateway deployment journal does not match this application and site")
        return result

    def _credentials(self, system, journal):
        """Pin staging to the current token, independently of Git revisions."""
        content = read_private(self.context.area / self.context.token_file, 16384)
        fingerprint = hashlib.sha256(content).hexdigest()
        if journal.get("runtime_credential_sha256") == fingerprint and journal.get("runtime_token_file"):
            try:
                previous = read_private(journal["runtime_token_file"], 16384)
            except FileNotFoundError:
                previous = None
            if previous is not None and hashlib.sha256(previous).hexdigest() == fingerprint:
                return
        # Retain every earlier receipt and rollback location. Neither a token
        # rotation nor adopting a pre-fingerprint journal can reuse its release.
        json_private(self.directory / ("history-" + secrets.token_hex(8) + ".json"), journal)
        current = installed_release(system)
        previous = str(current) if current else journal.get("previous_release")
        metadata = journal.get("metadata")
        snapshot = self.directory / ("runtime-input-" + secrets.token_hex(8)) / "bearer"
        atomic_private(snapshot, content)
        journal.clear()
        self._save(journal, version=1, versions=self.versions, previous_release=previous,
                   **({"metadata": metadata} if metadata else {}), phase="inputs" if metadata else "planned",
                   runtime_credential_sha256=fingerprint, runtime_token_file=str(snapshot),
                   backup_suffix="-" + secrets.token_hex(6))

    def _metadata(self, system, journal):
        if journal.get("metadata"):
            return journal["metadata"]
        previous = journal["previous_release"]
        if previous:
            destination = self.directory / ("adopted-" + secrets.token_hex(6))
            uid, _ = service_identity(required=True)
            self._helper(system, "adopt", Path(previous) / "config.json", destination,
                         "--uid", os.geteuid(), "--gid", os.getegid(), "--service-uid", uid)
            metadata = json.loads(read_private(destination / "adopted.json"))
            metadata["callback_file"] = str(destination / "callbacks.json") if metadata["callbacks"] else None
            origin = system.unit_origin("thermite-access-callbacks.service")
            if not origin.known or origin.load_state != "not-found" and not metadata["callbacks"]:
                raise AccessError("installed callback service has no recoverable exact callback map; refusing to retire it")
        else:
            inputs = private_directory(self.directory / "inputs")
            hosts = list(dict.fromkeys([self.context.hostname, *([self.context.address] if self.context.address else []), "localhost", "127.0.0.1"]))
            if any(not re.fullmatch(r"[A-Za-z0-9.:-]+", host) for host in hosts):
                raise AccessError("gateway TLS requires literal hostname/address inputs")
            certificate, key = inputs / "tls-cert.pem", inputs / "tls-key.pem"
            if not certificate.exists() or not key.exists():
                names = []
                for host in hosts:
                    try:
                        ipaddress.ip_address(host)
                        kind = "IP"
                    except ValueError:
                        kind = "DNS"
                    names.append(kind + ":" + host)
                self._run(system, "openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
                          "-days", "3650", "-subj", "/CN=" + self.context.hostname,
                          "-addext", "subjectAltName=" + ",".join(names), "-keyout", key, "-out", certificate)
                key.chmod(0o600)
                certificate.chmod(0o600)
            metadata = {"state": str(self.root / "state"), "allowed_hosts": hosts,
                        "bind_host": "0.0.0.0", "port": oh_ports.ACCESS_PORT,
                        "timezone": self.context.host["timezone"], "tls_cert": str(certificate),
                        "tls_key": str(key), "callbacks": False, "callback_file": None}
        self._save(journal, metadata=metadata, phase="inputs")
        return metadata

    def _stage(self, system, journal, metadata):
        if journal.get("stage"):
            return journal["stage"]
        # A completed plan survives interruption immediately after stage().
        if journal.get("plan"):
            try:
                receipt = json.loads(read_private(Path(journal["plan"]) / "stage.json"))
            except FileNotFoundError:
                receipt = None
            if receipt and receipt.get("destination") == journal["release"] and receipt.get("persistent_state") == metadata["state"]:
                self._save(journal, stage=receipt, phase="staged")
                return receipt
        attempt = self.identifier + "-" + secrets.token_hex(6)
        release, plan = self.root / "releases" / attempt, self.directory / ("plan-" + attempt)
        # Parent authority remains with the operator. Only the finite staged
        # release/state inventory passes to the service account.
        self._run(system, "sudo", "install", "-d", "-o", os.geteuid(), "-g", os.getegid(), "-m", "0755", self.root, self.root / "releases")
        self._run(system, "sudo", "install", "-d", "-o", "root", "-g", "root", "-m", "0700", self.root / "backups")
        self._save(journal, release=str(release), plan=str(plan), phase="preparing")
        callback = metadata["callback_file"] or self.callbacks
        house = Path(self.options.get("house", self.context.configuration / "instance/devices.yaml"))
        assets = Path(self.options.get("assets", self.context.configuration / "conf/html/remote"))
        assets = self._assets(assets)
        with site_inputs(self.context.configuration):
            receipt = staging.stage(release, state=metadata["state"], house=house, assets=assets,
                                    runtime_url=self.context.url, runtime_token_file=journal["runtime_token_file"],
                                    tls_cert=metadata["tls_cert"], tls_key=metadata["tls_key"],
                                    allowed_hosts=metadata["allowed_hosts"], bind_host=metadata["bind_host"],
                                    port=metadata["port"], timezone=metadata["timezone"],
                                    area=self.context.area, plan_directory=plan, callbacks=callback,
                                    wheelhouse=self.options.get("wheelhouse"), host_ports=oh_ports.effective(self.context.host))
        self._save(journal, stage=receipt, phase="staged")
        return receipt

    def _assets(self, source):
        """Keep gateway transport independent of a legacy native preview mode.

        Database security mode decides whether credentials are required. Even
        when it is off, the HTTPS client must use the gateway's ordinary API.
        """
        import oh_access_mode
        from oh_features.remote import FILES, asset_versions
        content = {}
        for name in FILES:
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise AccessError("gateway private assets are incomplete or unsafe")
            content["html/remote/" + name] = path.read_bytes()
        content["html/remote/access-config.js"] = oh_access_mode.script({"access": {"mode": "authenticated"}}).encode()
        metadata = json.loads(content["html/remote/system.json"])
        for tool in metadata["tools"]:
            if tool.get("id") == "openhab":
                tool["localOnly"] = True
        content["html/remote/system.json"] = (json.dumps(metadata, separators=(",", ":")) + "\n").encode()
        versions = asset_versions(content)
        def version(match):
            name = match[1]
            return "/static/remote/" + name + ("?v=" + versions[name] if name in versions else "")
        destination = private_directory(self.directory / "gateway-assets")
        for name in FILES:
            value = content["html/remote/" + name]
            if name.endswith(".html"):
                value = re.sub(r"/static/remote/([A-Za-z0-9._-]+)(?:\?v=[0-9a-f]+)?", version, value.decode()).encode()
            atomic_private(destination / name, value)
        return destination

    def _boundary(self, system, journal, metadata):
        callback = metadata["callback_file"] or self.callbacks
        if self.boundary:
            selected = self.boundary(system, metadata, Path(callback) if callback else None)
        else:
            import oh_inventory
            from oh_access.native_boundary import configure
            house = Path(self.options.get("house", self.context.configuration / "instance/devices.yaml"))
            result = configure(self.context.url, self.context.area / self.context.token_file,
                               oh_inventory.load(house), self.context.address,
                               existing_callbacks=Path(callback) if callback else None)
            selected = None
            if result["callbacks"] is not None:
                selected = self.directory / "callbacks.json"
                json_private(selected, result["callbacks"])
        if callback and selected is None:
            raise AccessError("existing callback routes must be preserved during gateway upgrade")
        metadata["callback_file"] = str(selected) if selected else None
        metadata["callbacks"] = bool(selected)
        self._save(journal, metadata=metadata)

    def _stop(self, system):
        origins = system.unit_origins(staging.ACCESS_UNITS)
        for name, origin in origins.items():
            if not origin.known:
                raise AccessError("cannot determine whether every gateway writer is stopped")
            if origin.load_state != "not-found":
                self._run(system, "sudo", "systemctl", "stop", name)
        for name, origin in system.unit_origins(staging.ACCESS_UNITS).items():
            if not origin.known or origin.load_state != "not-found" and origin.active not in {"inactive", "failed"}:
                raise AccessError("gateway and worker must both be stopped before identity backup")

    def _live(self, system, receipt):
        expected = {Path(command[-1]).name: (Path(command[-2]), Path(command[-1]))
                    for command in receipt["unit_install_commands"]}
        if set(expected) != set(receipt["units"]):
            raise _UnitAuthorityError("gateway unit plan does not cover every service")
        pids = []
        for name in receipt["units"]:
            source, destination = expected[name]
            origin = system.unit_origin(name)
            if (destination != Path("/etc/systemd/system") / name or not origin.known
                    or origin.drop_ins or origin.transient or origin.source
                    or origin.load_state not in {"loaded", "not-found"}
                    or origin.load_state == "loaded" and origin.fragment != str(destination)):
                raise _UnitAuthorityError("gateway service has unknown unit provenance or overrides")
            if origin.load_state == "not-found":
                raise AccessError("gateway service unit is missing")
            installed = system.unit_file(destination)
            if (installed is None or installed.uid != 0 or installed.mode & 0o022
                    or not installed.ancestors_trusted):
                raise _UnitAuthorityError("gateway service unit has lost protected root custody")
            if installed.content != read_private(source):
                raise AccessError("gateway service unit differs from its staged plan")
            code, output = system.capture(["systemctl", "show", name, "--property=ActiveState,MainPID,UnitFileState,NeedDaemonReload"])
            values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            if (code or values.get("ActiveState") != "active" or values.get("UnitFileState") != "enabled"
                    or values.get("NeedDaemonReload") != "no"):
                raise AccessError("gateway units have not reached enabled, active state")
            pid = values.get("MainPID", "")
            if not pid.isdecimal() or int(pid) <= 0:
                raise AccessError("gateway unit has no live process")
            pids.append(pid)
        self._helper(system, "live", receipt["destination"], *pids)
        for name, pid in zip(receipt["units"], pids):
            code, value = system.capture(["systemctl", "show", name, "--property=MainPID", "--value"])
            if code or value.strip() != pid:
                raise AccessError("gateway process changed during live release verification")

    def _https(self, metadata):
        # Trust only the retained/generated certificate and validate its name.
        host = metadata["allowed_hosts"][0].strip("[]")
        shown_host = "[" + host + "]" if ":" in host else host
        url = "https://" + shown_host + ":" + str(metadata["port"])
        context = ssl.create_default_context(cafile=metadata["tls_cert"])
        address = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(metadata["bind_host"], metadata["bind_host"])
        connection = http.client.HTTPSConnection(host, metadata["port"], context=context, timeout=5)
        try:
            # Connect locally while retaining certificate name and HTTP Host
            # validation, even when the public DNS name has no local route.
            connection.sock = context.wrap_socket(socket.create_connection((address, metadata["port"]), timeout=5), server_hostname=host)
            connection.request("GET", "/api/v1/security", headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise AccessError("gateway HTTPS security response exceeds its bound")
            value = json.loads(body)
            if (response.status != 200 or not isinstance(value, dict)
                    or type(value.get("enabled")) is not bool or type(value.get("revision")) is not int):
                raise AccessError("gateway HTTPS security endpoint returned an unexpected response")
        finally:
            connection.close()
        return url

    def _verify_boundary(self, system):
        from oh_access.native_boundary import verify_firewall, verify_native_listeners
        self._run(system, "sudo", "-v")
        code, snapshot = system.capture(("sudo", "-n", "nft", "--json", "list", "table", "inet", "thermite_native"), diagnostics=True)
        if code:
            raise AccessError("cannot verify the native listener network filter")
        verify_firewall(snapshot, self.context.host)
        code, snapshot = system.capture(("ss", "-H", "-ltn"))
        if code:
            raise AccessError("cannot inspect native listener isolation")
        verify_native_listeners(snapshot, http_port=self.context.host["http_port"],
                                https_port=self.context.host["https_port"], filtered=True)

    def _upstream(self, system, journal, receipt):
        current = read_private(self.context.area / self.context.token_file, 16384)
        if hashlib.sha256(current).hexdigest() != journal["runtime_credential_sha256"]:
            raise AccessError("runtime credential changed during gateway deployment; rerun to stage its current value")
        expectations = self.directory / "upstream-check.json"
        json_private(expectations, {"runtime_url": self.context.url,
                                    "runtime_credential_sha256": journal["runtime_credential_sha256"]})
        self._helper(system, "upstream", receipt["config"], expectations)

    def _profile(self, journal, metadata, receipt, url):
        json_private(self.context.var / "gateway/current.json",
                     {"version": 1, "url": url, "ca_file": str(self.root / "tls-cert.pem"),
                      "release": receipt["destination"], "state": metadata["state"],
                      "versions": self.versions, "journal": str(self.journal_path)})

    def perform(self, system):
        journal = None
        try:
            private_directory(self.directory)
            staging._trusted_ancestors(self.directory, "Gateway plan")
            atomic_private(self.helper, Path(staging.__file__).with_name("deployment.py").read_bytes())
            journal = self._load()
            if journal is None:
                old = installed_release(system)
                journal = {"version": 1, "versions": self.versions, "previous_release": str(old) if old else None}
                self._save(journal, phase="planned")
            self._credentials(system, journal)
            metadata = self._metadata(system, journal)
            self._boundary(system, journal, metadata)
            self._verify_boundary(system)
            receipt = self._stage(system, journal, metadata)
            public_cert = self.root / "tls-cert.pem"
            self._run(system, "sudo", "install", "-o", "root", "-g", "root", "-m", "0644", metadata["tls_cert"], public_cert)
            if journal.get("phase") == "complete":
                try:
                    self._upstream(system, journal, receipt)
                    self._live(system, receipt)
                    url = self._https(metadata)
                    self._profile(journal, metadata, receipt, url)
                    return 0
                except _UnitAuthorityError:
                    raise
                except (OSError, ValueError, http.client.HTTPException):
                    # Repair a stopped/unhealthy completed deployment. Its old
                    # rollback copy stays intact; this repair takes a new one.
                    self._save(journal, phase="repairing", backup_suffix="-" + secrets.token_hex(6))
            for command in receipt["ownership_commands"]:
                self._run(system, *command)
            self._run(system, *receipt["verification_command"])
            self._upstream(system, journal, receipt)
            self._stop(system)
            backup_path = self.root / "backups" / (self.identifier + journal.get("backup_suffix", "") + ".tar.gz")
            self._helper(system, "backup", metadata["state"], backup_path)
            self._save(journal, backup=str(backup_path), phase="backed-up")
            # Never initialize Django before the complete stopped-state backup.
            command = receipt["verification_command"]
            migrate = [*command[:-3], "migrate", "--config", receipt["config"]]
            self._run(system, *migrate)
            self._save(journal, phase="migrated")
            for command in receipt["unit_install_commands"]:
                self._run(system, *command)
            self._run(system, "sudo", "systemctl", "daemon-reload")
            self._run(system, "sudo", "systemctl", "enable", "--now", *receipt["units"])
            self._save(journal, phase="activated")
            last = None
            for attempt in range(30):
                try:
                    self._live(system, receipt)
                    url = self._https(metadata)
                    break
                except _UnitAuthorityError:
                    raise
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    last = exc
                    if attempt == 29:
                        raise AccessError("gateway did not verify after activation: " + str(last)) from None
                    system.sleep(1)
            self._verify_boundary(system)
            self._upstream(system, journal, receipt)
            self._save(journal, phase="complete", url=url)
            self._profile(journal, metadata, receipt, url)
            system.say("   Gateway verified: " + url)
            system.say("   TLS trust certificate: " + str(public_cert))
            system.say("   Complete identity backup: " + str(backup_path))
            return 0
        except (OSError, ValueError, http.client.HTTPException) as exc:
            system.say("   " + str(exc))
            system.say("   Gateway deployment is incomplete; rerun the same install or upgrade command to resume.")
            if journal and journal.get("previous_release"):
                system.say("   Previous gateway release retained: " + journal["previous_release"])
            if journal and journal.get("backup"):
                system.say("   Complete identity backup retained: " + journal["backup"])
            return 1
