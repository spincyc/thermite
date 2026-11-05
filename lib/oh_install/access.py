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
from oh_access import AccessError, guest, staging
from oh_access.files import atomic_private, json_private, private_directory, read_private, service_identity
from oh_install import manifest
from oh_install.model import Check, Command, Step, Task


class _UnitAuthorityError(AccessError):
    """An unknown unit override must not be overwritten as a routine repair."""


#: Apple platforms reject TLS server certificates valid for longer than this,
#: or without serverAuth usage, even when their issuer is trusted.
LEAF_DAYS = 825
AUTHORITY_DAYS = 3650
#: Reissue the served certificate once less than this validity remains.
RENEW_SECONDS = 90 * 86400
_CERTIFICATE = re.compile(rb"-----BEGIN CERTIFICATE-----\r?\n.+?\r?\n-----END CERTIFICATE-----", re.S)
_HOST = re.compile(r"[A-Za-z0-9.:-]+")


def certificates(data: bytes) -> list[bytes]:
    """The PEM certificates of a chain, normalized for comparison."""
    return [block.replace(b"\r\n", b"\n") + b"\n" for block in _CERTIFICATE.findall(data)]


def subject_names(hosts) -> set[str]:
    """Certificate names as one set: addresses in canonical form, DNS names
    without case."""
    names = set()
    for host in hosts:
        try:
            names.add("IP:" + str(ipaddress.ip_address(host)))
        except ValueError:
            names.add("DNS:" + host.lower())
    return names


def served_names(output: str) -> set[str]:
    """The names `openssl x509 -noout -ext subjectAltName` lists."""
    values = [value.strip() for line in output.splitlines() for value in line.split(",")]
    return subject_names(value.split(":", 1)[1] for value in values if value.startswith(("DNS:", "IP Address:")))


def openssl_config(hosts) -> str:
    """Extension profiles: a signing-only private authority and its server leaf."""
    names = []
    for host in hosts:
        if not _HOST.fullmatch(host):
            raise AccessError("gateway TLS requires literal hostname/address inputs")
        try:
            ipaddress.ip_address(host)
            names.append("IP:" + host)
        except ValueError:
            names.append("DNS:" + host)
    server = ("[server]\nbasicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature\n"
              "extendedKeyUsage = serverAuth\nsubjectAltName = " + ",".join(names) + "\n"
              "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n") if names else ""
    return ("[req]\ndistinguished_name = subject\nprompt = no\n[subject]\nCN = Thermite\n"
            "[authority]\nbasicConstraints = critical, CA:TRUE, pathlen:0\n"
            "keyUsage = critical, keyCertSign, cRLSign\nsubjectKeyIdentifier = hash\n" + server)


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


#: The thermite-access.service directives that identify its release.
RELEASE_DIRECTIVES = ("User", "Group", "WorkingDirectory")


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
            # Only the identifying directives must be single; list directives
            # such as ReadWritePaths and Environment repeat in staged units.
            if key not in RELEASE_DIRECTIVES:
                continue
            if key in values:
                raise AccessError("installed gateway has ambiguous service directives")
            values[key] = value
    root = Path(values.get("WorkingDirectory", "").replace("%%", "%"))
    if (values.get("User") != "thermite" or values.get("Group") != "thermite"
            or not root.is_absolute() or ".." in root.parts or root == Path("/")):
        raise AccessError("installed gateway does not identify its persistent release safely")
    return root


class Gateway(Step):
    """Stage, migrate and activate a new release of the HTTPS gateway.

    Every run stages a release beside the installed ones, named by its
    install transaction. Its inputs live at fixed paths: the identity state
    in access/state, and the served TLS leaf and key and the verified
    callback map in access/inputs; the private authority stays in the
    site's var/gateway/authority. The transaction sets all of them aside
    before anything changes and puts them back if it rolls back, so the
    gateway keeps no journal of its own to resume from and takes no backup.
    What it cannot carry over refuses the run while it prepares (prove).

    ``boundary`` is an optional installer hook called after the inputs are
    read. It receives (system, metadata, existing_callback_path) and returns
    a private callback configuration path or None. Existing callbacks cannot
    be discarded.

    ``record`` is an optional installer hook called once per completed
    activation with {commit, application, release, previous_release}; it
    returns whether the runtime deploy history (thermite history) took it.
    """

    title = "HTTPS gateway and identity state"
    always = True
    stop_on_failure = True

    def __init__(self, context, *, boundary=None, callbacks=None, options=None, record=None, transaction=None):
        self.context = context
        self.boundary = boundary
        self.callbacks = callbacks
        self.options = options or {}
        self.record = record
        import oh_release
        application = context.application or {"version": oh_release.VERSION, "source_commit": context.head}
        self.versions = {"site": context.head, "application": application}
        # A dry run's work names no transaction; it never performs.
        self.id = transaction or manifest.transaction_id()
        self.root = Path(self.options.get("root", context.installation.access))
        self.inputs = self.root / "inputs"
        self.release = self.root / "releases" / self.id
        self.directory = context.var / "transaction" / self.id / "gateway"
        self.journal_path = self.directory / "deployment.json"
        self.helper = self.directory / "deployment.py"
        #: Whether this run found callbacks whose relay waits for the next
        #: transaction: one installs only the units it declared before it
        #: began, so a newly found map waits in access/inputs.
        self.deferred = False
        #: The installed release's adoption export (_exported), once made.
        self.adopted = None

    def check(self, system):
        try:
            old = installed_release(system)
            detail = "upgrade the gateway and preserve its identity, TLS and callbacks" if old else "install the persistent HTTPS gateway and identity state"
            return Check(False, detail)
        except (OSError, ValueError, AccessError) as exc:
            return Check(False, "", str(exc))

    def actions(self, system):
        return [Task("stage the gateway, stop writers, migrate identity state, activate and verify HTTPS", self.perform)]

    def relays(self, system):
        """Whether the callback relay runs: once its map exists, in the fixed
        inputs or behind an installed unit, it is never dropped."""
        return (system.lexists(self.root / "inputs" / "callbacks.json")
                or system.lexists(manifest.UNITS / staging.ACCESS_UNITS[2]))

    def desired(self, system):
        """The units and their links, the side-by-side releases, the identity
        state and the fixed inputs a release migrates or reissues, and the
        public trust certificate. Old identity tars stay where they are."""
        units = [*staging.ACCESS_UNITS[:2], *(staging.ACCESS_UNITS[2:] if self.relays(system) else ())]
        return [*(manifest.unit(manifest.UNITS / name) for name in units), *(manifest.link(name) for name in units),
                manifest.Item(self.root / "releases", "versioned"), manifest.Item(self.root / "state", "tree"),
                manifest.Item(self.root / "inputs", "tree"), manifest.Item(self.root / "tls-cert.pem", "file"),
                manifest.Item(self.root / "backups", "shared")]

    def _run(self, system, *argv):
        from oh_install.runner import perform
        argv = tuple(str(value) for value in argv)
        root = argv[0] == "sudo"
        code = perform(Command(argv[1:] if root else argv, root=root, terminal=root), system, self.context.area)
        if code is None or code:
            raise AccessError("gateway deployment command failed")

    def _workspace(self):
        """This run's private directory, with the root helper in it."""
        private_directory(self.directory)
        staging._trusted_ancestors(self.directory, "Gateway plan")
        atomic_private(self.helper, Path(staging.__file__).with_name("deployment.py").read_bytes())

    def _helper(self, system, *argv):
        staging._trusted_ancestors(self.directory, "Gateway plan")
        self._run(system, "sudo", "/usr/bin/python3", "-I", "-S", self.helper, *argv)

    def _save(self, record, **changes):
        """This attempt's record, for the owner to read; nothing resumes from it."""
        record.update(changes)
        json_private(self.journal_path, record)

    def _credentials(self, record):
        """Pin staging to the runtime token as it is now."""
        content = read_private(self.context.area / self.context.token_file, 16384)
        snapshot = self.directory / "runtime-input" / "bearer"
        atomic_private(snapshot, content)
        self._save(record, runtime_credential_sha256=hashlib.sha256(content).hexdigest(),
                   runtime_token_file=str(snapshot))

    def _hosts(self):
        """The names clients reach the gateway by: this host's name and LAN
        address as found this run, then loopback."""
        hosts = list(dict.fromkeys([self.context.hostname, *([self.context.address] if self.context.address else []),
                                    "localhost", "127.0.0.1"]))
        if any(not _HOST.fullmatch(host) for host in hosts):
            raise AccessError("gateway TLS requires literal hostname/address inputs")
        return hosts

    def _derived(self):
        """Settings derived from this host each run, as the ports are. An
        upgrade never adopts them from the release it replaces."""
        return {"allowed_hosts": self._hosts(), "bind_host": "0.0.0.0",
                "port": oh_ports.gateway_port(self.context.host), "timezone": self.context.host["timezone"]}

    def prove(self, system):
        """Refuse, before the transaction begins, what this run could not
        carry over from the installed gateway: identity state anywhere but
        access/state, or a callback relay without its exact map. Root reads
        the release a gateway installed before its inputs had fixed paths
        runs (_exported); nothing of Thermite's is written."""
        old = installed_release(system)
        if old is not None and not (self.inputs / "tls-key.pem").exists():
            mapped = self._exported(system, old)[1]["callbacks"]
        else:
            mapped = (self.inputs / "callbacks.json").exists()
        if not mapped:
            origin = system.unit_origin(staging.ACCESS_UNITS[2])
            if not origin.known or origin.load_state != "not-found":
                raise AccessError("installed callback service has no recoverable exact callback map; refusing to retire it")

    def _metadata(self, system, old):
        """The identity state and served TLS material at their fixed paths,
        and the hosts, bind address, port and timezone derived from this
        host. The first run on a gateway installed before the inputs had
        fixed paths takes them from the release it runs (_adopt)."""
        inputs = private_directory(self.inputs)
        derived = self._derived()
        certificate, key, callbacks = inputs / "tls-cert.pem", inputs / "tls-key.pem", inputs / "callbacks.json"
        authority = self._authority(system, derived["allowed_hosts"][0])
        if not key.exists() and old is not None:
            self._adopt(system, old, derived)
        elif not key.exists() or not certificate.exists():
            self._replace_leaf(system, derived["allowed_hosts"], authority)
        if callbacks.exists():
            self._relocate_callbacks(system, callbacks)
        return {"state": str(self.root / "state"), **derived, "tls_cert": str(certificate), "tls_key": str(key),
                "tls_ca": str(authority[0]), "callbacks": callbacks.exists(),
                "callback_file": str(callbacks) if callbacks.exists() else None}

    def _exported(self, system, old):
        """The release `old`'s adoption export (deployment.py adopt, as
        root, into this run's own directory) and what it says, made once:
        its identity state must already be access/state."""
        if self.adopted is None:
            self._workspace()
            destination = self.directory / ("adopted-" + secrets.token_hex(6))
            uid, _ = service_identity(required=True)
            self._helper(system, "adopt", Path(old) / "config.json", destination,
                         "--uid", os.geteuid(), "--gid", os.getegid(), "--service-uid", uid)
            adopted = json.loads(read_private(destination / "adopted.json"))
            if Path(adopted["state"]) != self.root / "state":
                raise AccessError(f"the installed gateway keeps its identity state in {adopted['state']}, not "
                                  f"{self.root / 'state'}; move it there before upgrading")
            self.adopted = destination, adopted
        return self.adopted

    def _adopt(self, system, old, derived):
        """Once, for a gateway installed before its inputs had fixed paths:
        copy its TLS leaf and key and its callback map out of the release it
        runs (_exported) into access/inputs. Only those are adopted; the
        port, hosts and timezone are this host's. Deleted with
        oh_install.bridge once every installation has run a release with
        fixed inputs."""
        destination, adopted = self._exported(system, old)
        if adopted["port"] != derived["port"]:
            system.say(f"   Gateway HTTPS moves from port {adopted['port']} to {derived['port']}: "
                       "update bookmarks, home-screen apps and terminal --url")
        if adopted["callbacks"]:
            atomic_private(self.inputs / "callbacks.json", read_private(destination / "callbacks.json", 256 * 1024))
        atomic_private(self.inputs / "tls-key.pem", read_private(adopted["tls_key"], 65536))
        atomic_private(self.inputs / "tls-cert.pem", read_private(adopted["tls_cert"], 65536))

    def _relocate_callbacks(self, system, path):
        """Keep the adopted map's verified routes; take its listener port and
        native upstream from this allocation, never the replaced release."""
        from oh_access.callback_service import load_config
        from oh_access.native_boundary import relocated_callbacks
        adopted = load_config(path)
        port = oh_ports.effective(self.context.host)["callbacks"]
        if adopted["port"] != port:
            system.say(f"   Sonos event callbacks move from port {adopted['port']} to {port}")
        json_private(path, relocated_callbacks(adopted, self.context.url, port))

    def _authority(self, system, name):
        """The site's private signing authority, created once in owner custody.

        Its key signs only this gateway's server certificates and never leaves
        the private site's var/. Clients trust its certificate, not a leaf.
        """
        directory = self.context.var / "gateway" / "authority"
        certificate, key = directory / "ca-cert.pem", directory / "ca-key.pem"
        if directory.exists() or directory.is_symlink():
            private_directory(directory)
            read_private(key, 65536)
            if not certificates(read_private(certificate, 65536)):
                raise AccessError("gateway certificate authority is incomplete; move " + str(directory)
                                  + " aside to create a new one, then trust that on every client")
            return certificate, key
        if not _HOST.fullmatch(name):
            raise AccessError("gateway TLS requires literal hostname/address inputs")
        work = private_directory(self.context.var / "gateway" / ("authority-" + secrets.token_hex(6)))
        config = work / "openssl.cnf"
        atomic_private(config, openssl_config(()).encode())
        self._run(system, "openssl", "req", "-x509", "-config", config, "-extensions", "authority",
                  "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-sha256", "-nodes",
                  "-days", AUTHORITY_DAYS, "-subj", "/CN=Thermite gateway authority " + name,
                  "-keyout", work / key.name, "-out", work / certificate.name)
        config.unlink()
        for path in (work / key.name, work / certificate.name):
            path.chmod(0o600)
        # Publish complete: a partial authority is never adopted.
        os.rename(work, directory)
        return certificate, key

    def _issue(self, system, directory, hosts, authority, days=LEAF_DAYS):
        """Write directory/tls-cert.pem (leaf, then authority) and tls-key.pem."""
        directory = private_directory(directory)
        config, request, leaf = directory / "openssl.cnf", directory / "tls-request.pem", directory / "tls-leaf.pem"
        certificate, key = directory / "tls-cert.pem", directory / "tls-key.pem"
        atomic_private(config, openssl_config(hosts).encode())
        self._run(system, "openssl", "req", "-new", "-config", config, "-newkey", "ec",
                  "-pkeyopt", "ec_paramgen_curve:prime256v1", "-sha256", "-nodes", "-subj", "/CN=" + hosts[0],
                  "-keyout", key, "-out", request)
        key.chmod(0o600)
        self._run(system, "openssl", "x509", "-req", "-in", request, "-CA", authority[0], "-CAkey", authority[1],
                  "-set_serial", "0x" + secrets.token_hex(16), "-days", days, "-sha256",
                  "-extfile", config, "-extensions", "server", "-out", leaf)
        leaf.chmod(0o600)
        chain = certificates(read_private(leaf, 65536)) + certificates(read_private(authority[0], 65536))
        if len(chain) != 2:
            raise AccessError("openssl did not produce the gateway certificate chain")
        atomic_private(certificate, b"".join(chain))
        for path in (config, request, leaf):
            path.unlink()
        return certificate, key

    def _served(self, system, metadata, authority, hosts):
        """None if the served certificate is current; else 'renew', 'names'
        or 'replace'.

        Renewal, and a leaf for this host's changed names, keep the authority
        clients already trust; replacement (the former self-signed profile,
        or a lost authority) needs new client trust.
        """
        try:
            served = certificates(Path(metadata["tls_cert"]).read_bytes())
        except FileNotFoundError:
            return "replace"
        if len(served) != 2 or served[1] != certificates(read_private(authority[0], 65536))[0]:
            return "replace"
        verified, _ = system.capture(("openssl", "verify", "-CAfile", str(authority[0]), "-purpose", "sslserver",
                                      metadata["tls_cert"]))
        remaining, _ = system.capture(("openssl", "x509", "-noout", "-checkend", str(RENEW_SECONDS),
                                       "-in", metadata["tls_cert"]))
        if verified or remaining:
            return "renew"
        code, names = system.capture(("openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", metadata["tls_cert"]))
        if code or served_names(names) != subject_names(hosts) or metadata["allowed_hosts"] != hosts:
            return "names"
        return None

    def _replace_leaf(self, system, hosts, authority):
        """Issue a leaf for `hosts` and put it in access/inputs, key first."""
        fresh = self._issue(system, self.directory / ("tls-" + secrets.token_hex(6)), hosts, authority)
        for source, name in zip(reversed(fresh), ("tls-key.pem", "tls-cert.pem")):
            os.replace(source, self.inputs / name)

    def _certificate(self, system, metadata):
        """Serve only a current leaf of the private authority for this host's
        names, reissuing when due or when the names changed."""
        hosts = self._hosts()
        authority = self._authority(system, hosts[0])
        reason = self._served(system, metadata, authority, hosts)
        if reason is None:
            return {**metadata, "tls_ca": str(authority[0])}
        self._replace_leaf(system, hosts, authority)
        trust = self.root / "tls-cert.pem"
        if reason == "renew":
            system.say(f"   Gateway TLS certificate renewed by its private authority; clients trusting {trust} need no change")
        elif reason == "names":
            system.say(f"   Gateway TLS certificate reissued by its private authority for {', '.join(hosts)}; "
                       f"clients trusting {trust} need no change")
        else:
            system.say(f"   Gateway TLS now uses a new private certificate authority: trust {trust} once on every "
                       "browser and terminal client")
        return {**metadata, "allowed_hosts": hosts, "tls_ca": str(authority[0])}

    def _stage(self, system, record, metadata):
        plan = self.directory / "plan"
        self._save(record, plan=str(plan), phase="preparing")
        callback = metadata["callback_file"] or self.callbacks
        house = Path(self.options.get("house", self.context.configuration / "instance/devices.yaml"))
        assets = Path(self.options.get("assets", self.context.configuration / "conf/html/remote"))
        assets = self._assets(assets)
        with site_inputs(self.context.configuration):
            receipt = staging.stage(self.release, state=metadata["state"], house=house, assets=assets,
                                    runtime_url=self.context.url, runtime_token_file=record["runtime_token_file"],
                                    tls_cert=metadata["tls_cert"], tls_key=metadata["tls_key"],
                                    allowed_hosts=metadata["allowed_hosts"], bind_host=metadata["bind_host"],
                                    port=metadata["port"], timezone=metadata["timezone"],
                                    area=self.context.area, plan_directory=plan, callbacks=callback,
                                    wheelhouse=self.options.get("wheelhouse"), host_ports=oh_ports.effective(self.context.host),
                                    guest_http=self.context.host.get("guest_http"),
                                    textfile=self.context.installation.state.textfile)
        self._save(record, stage=receipt, phase="staged")
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

    def _boundary(self, system, record, metadata):
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
                selected = self.inputs / "callbacks.json"
                json_private(selected, result["callbacks"])
        if callback and selected is None:
            raise AccessError("existing callback routes must be preserved during gateway upgrade")
        if selected and not callback:
            # Kept in access/inputs: one more install transaction, right
            # after this one commits, declares the relay's unit and
            # activates it (changeover.relay).
            system.say("   Sonos event callbacks were found; one more install transaction activates their relay")
            self.deferred = True
            selected = None
        metadata["callback_file"] = str(selected) if selected else None
        metadata["callbacks"] = bool(selected)
        self._save(record, metadata=metadata)

    def _stop(self, system):
        """The gateway's writers stopped before its identity state migrates:
        the transaction stopped all of Thermite, and a writer found running
        again is stopped here."""
        origins = system.unit_origins(staging.ACCESS_UNITS)
        for name, origin in origins.items():
            if not origin.known:
                raise AccessError("cannot determine whether every gateway writer is stopped")
            if origin.load_state != "not-found" and origin.active not in {"inactive", "failed"}:
                self._run(system, "sudo", "systemctl", "stop", name)
        for name, origin in system.unit_origins(staging.ACCESS_UNITS).items():
            if not origin.known or origin.load_state != "not-found" and origin.active not in {"inactive", "failed"}:
                raise AccessError("gateway and worker must both be stopped before migrating identity state")

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
        context = ssl.create_default_context(cafile=metadata["tls_ca"])
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

    def _guest_http(self):
        """Check the opt-in Guest listener answers a Guest on each address its
        client networks admit from this host, where that address is the peer.
        Returns the report lines."""
        settings = guest.settings(self.context.host.get("guest_http"))
        if settings is None:
            return []
        networks = [ipaddress.ip_network(value) for value in settings["clients"]]
        report = []
        for address, url in zip(settings["listen"], guest.origins(settings)):
            if not guest.admitted((address, 0), networks):
                report.append(f"   Guest HTTP {url}/ not checked: this host's address is outside guest_http.clients")
                continue
            connection = http.client.HTTPConnection(address, settings["port"], timeout=5)
            try:
                connection.sock = socket.create_connection((address, settings["port"]), timeout=5, source_address=(address, 0))
                connection.request("GET", "/api/v1/me", headers={"Accept": "application/json"})
                response = connection.getresponse()
                body = response.read(65537)
                value = json.loads(body) if len(body) <= 65536 else None
                if response.status != 200 or not isinstance(value, dict) or value.get("is_guest") is not True:
                    raise AccessError("gateway Guest HTTP listener returned an unexpected response")
            finally:
                connection.close()
            report.append(f"   Guest HTTP verified: {url}/")
        return report

    def _verify_boundary(self, system):
        from oh_access.native_boundary import firewall_ports, verify_firewall, verify_native_listeners
        self._run(system, "sudo", "-v")
        code, snapshot = system.capture(("sudo", "-n", "nft", "--json", "list", "table", "inet", "thermite_native"), diagnostics=True)
        if code:
            raise AccessError("cannot verify the native listener network filter")
        verify_firewall(snapshot, firewall_ports(self.context.host))
        code, snapshot = system.capture(("ss", "-H", "-ltn"))
        if code:
            raise AccessError("cannot inspect native listener isolation")
        verify_native_listeners(snapshot, http_port=self.context.host["http_port"],
                                https_port=self.context.host["https_port"], filtered=True)

    def _upstream(self, system, record, receipt):
        current = read_private(self.context.area / self.context.token_file, 16384)
        if hashlib.sha256(current).hexdigest() != record["runtime_credential_sha256"]:
            raise AccessError("runtime credential changed during gateway deployment; rerun to stage its current value")
        expectations = self.directory / "upstream-check.json"
        json_private(expectations, {"runtime_url": self.context.url,
                                    "runtime_credential_sha256": record["runtime_credential_sha256"]})
        self._helper(system, "upstream", receipt["config"], expectations)

    def _profile(self, metadata, receipt, url):
        json_private(self.context.var / "gateway/current.json",
                     {"version": 1, "url": url, "ca_file": str(self.root / "tls-cert.pem"),
                      "release": receipt["destination"], "state": metadata["state"],
                      "versions": self.versions, "journal": str(self.journal_path)})

    def _history(self, system, record, receipt):
        """Add the activation to the deploy history; the gateway never fails over it."""
        if self.record is None:
            return
        event = {"commit": self.context.head, "application": self.versions["application"],
                 "release": receipt["destination"], "previous_release": record.get("previous_release")}
        try:
            recorded = self.record(system, event)
        except (OSError, ValueError):
            recorded = False
        if not recorded:
            system.say("   The gateway release was not added to thermite history; "
                       "its record stays in " + str(self.journal_path))

    def perform(self, system):
        try:
            self._workspace()
            old = installed_release(system)
            record = {"version": 1, "versions": self.versions, "release": str(self.release),
                      "previous_release": str(old) if old else None}
            self._save(record, phase="planned")
            # Parent authority remains with the operator. Only the finite staged
            # release/state inventory passes to the service account.
            self._run(system, "sudo", "install", "-d", "-o", os.geteuid(), "-g", os.getegid(), "-m", "0755",
                      self.root, self.root / "releases")
            self._credentials(record)
            metadata = self._certificate(system, self._metadata(system, old))
            self._save(record, metadata=metadata, phase="inputs")
            self._boundary(system, record, metadata)
            self._verify_boundary(system)
            receipt = self._stage(system, record, metadata)
            public_cert = self.root / "tls-cert.pem"
            # Clients trust the private authority, never an individual leaf.
            self._run(system, "sudo", "install", "-o", "root", "-g", "root", "-m", "0644", metadata["tls_ca"], public_cert)
            for command in receipt["ownership_commands"]:
                self._run(system, *command)
            self._run(system, *receipt["verification_command"])
            self._upstream(system, record, receipt)
            self._stop(system)
            command = receipt["verification_command"]
            migrate = [*command[:-3], "migrate", "--config", receipt["config"]]
            self._run(system, *migrate)
            self._save(record, phase="migrated")
            for command in receipt["unit_install_commands"]:
                self._run(system, *command)
            self._run(system, "sudo", "systemctl", "daemon-reload")
            self._run(system, "sudo", "systemctl", "enable", "--now", *receipt["units"])
            self._save(record, phase="activated")
            last = None
            for attempt in range(30):
                try:
                    self._live(system, receipt)
                    url = self._https(metadata)
                    guest_report = self._guest_http()
                    break
                except _UnitAuthorityError:
                    raise
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    last = exc
                    if attempt == 29:
                        raise AccessError("gateway did not verify after activation: " + str(last)) from None
                    system.sleep(1)
            self._verify_boundary(system)
            self._upstream(system, record, receipt)
            self._save(record, phase="complete", url=url)
            self._profile(metadata, receipt, url)
            self._history(system, record, receipt)
            system.say("   Gateway verified: " + url)
            for line in guest_report:
                system.say(line)
            system.say("   TLS trust certificate: " + str(public_cert))
            return 0
        except (OSError, ValueError, http.client.HTTPException) as exc:
            system.say("   " + str(exc))
            system.say("   Gateway deployment is incomplete; the install transaction puts the previous gateway back.")
            return 1


class GatewayInputs(Step):
    """Before the transaction begins, what the gateway carries over from the
    installed one (Gateway.prove): a gateway it cannot carry over refuses
    the run before anything stops."""

    title = "Gateway identity state and callbacks"
    always = True
    stop_on_failure = True

    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    def check(self, system):
        return Check(False, f"the installed gateway's identity state in {self.gateway.root / 'state'}, and its "
                            "callback map")

    def actions(self, system):
        return [Task("check what the installed gateway keeps", self.perform)]

    def perform(self, system):
        try:
            self.gateway.prove(system)
        except (OSError, ValueError) as exc:
            system.say("   " + str(exc))
            return 1
        return 0
