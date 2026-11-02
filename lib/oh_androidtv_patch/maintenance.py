"""Owner-only, stop-first binding maintenance. Never activated by normal deploy.

The native adapter is injected into the transaction so its exact ordering can
also be exercised in the coordinator's isolated native fixture.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

from . import PACKAGE, cached_jar_report, pins
from . import owner_files as files
from .integrity import manifest_headers

SCRIPT = Path("conf/automation/js/thermite_androidtv_maintenance.js")
CLASSES = {"manager_sha256": PACKAGE + "protocol/googletv/GoogleTVConnectionManager.class",
           "handler_sha256": PACKAGE + "AndroidTVHandler.class",
           "helper_sha256": PACKAGE + "protocol/googletv/GoogleTVSocketLifecycle.class"}


def program_hashes() -> dict:
    return {path.name: files.digest(path.read_bytes()) for path in Path(__file__).parent.iterdir()
            if path.suffix in (".py", ".js")}


def proof(path: Path, expected: str, artifact_sha: str, *, maintenance=False) -> dict:
    metadata, data = files.snapshot(path)
    if data is None or metadata["sha256"] != expected:
        raise files.OwnerFileError("native_proof_external_sha_mismatch")
    value = json.loads(data)
    if (value.get("status") != "pass" or not value.get("checks") or not all(v is True for v in value["checks"].values())
            or len(value.get("cleanup", {})) < 10 or not all(v is True for v in value["cleanup"].values())):
        raise files.OwnerFileError("native_proof_not_complete")
    if maintenance:
        if (value.get("artifact_sha256") != artifact_sha or value.get("program") != program_hashes()
                or value.get("apply", {}).get("status") != "applied"
                or value.get("rollback", {}).get("status") != "rolled_back"):
            raise files.OwnerFileError("maintenance_proof_program_mismatch")
    else:
        caches = [row for boot in value.get("boots", []) for row in boot.get("verified_cache_artifact", [])]
        if not caches or any(row.get("staged_sha256") != artifact_sha for row in caches):
            raise files.OwnerFileError("native_proof_artifact_mismatch")
    return {"sha256": expected, "artifact_sha256": artifact_sha}


def class_hashes(artifact: Path) -> dict:
    import zipfile
    with zipfile.ZipFile(artifact) as archive:
        return {key: hashlib.sha256(archive.read(name)).hexdigest() for key, name in CLASSES.items()}


def original_cache_report(original: Path, cached: Path) -> dict:
    """Pax adds the EXACT original Maven location on rollback; every other byte stays pinned."""
    import zipfile
    source, _ = files.snapshot(original)
    result, _ = files.snapshot(cached)
    if source.get("sha256") != pins()["assets"]["bundle"]["sha256"]:
        raise files.OwnerFileError("official_original_artifact_required")
    with zipfile.ZipFile(original) as a, zipfile.ZipFile(cached) as b:
        names, other = a.namelist(), b.namelist()
        if len(names) != len(set(names)) or len(other) != len(set(other)) or set(names) != set(other):
            raise files.OwnerFileError("original_cache_entry_set_mismatch")
        for name in names:
            if name == "META-INF/MANIFEST.MF":
                continue
            if a.getinfo(name).file_size != b.getinfo(name).file_size or a.read(name) != b.read(name):
                raise files.OwnerFileError("original_cache_entry_bytes_mismatch")
        if b.getinfo("META-INF/MANIFEST.MF").file_size > a.getinfo("META-INF/MANIFEST.MF").file_size + 256:
            raise files.OwnerFileError("original_cache_manifest_oversized")
        before = manifest_headers(a.read("META-INF/MANIFEST.MF"))
        after = manifest_headers(b.read("META-INF/MANIFEST.MF"))
        if after != before:
            key = next((key for key in after if key.lower() == "bundle-updatelocation"), None)
            if (key != "Bundle-UpdateLocation" or key in before
                    or after.pop(key) != "mvn:org.openhab.addons.bundles/org.openhab.binding.androidtv/5.2.3" or after != before):
                raise files.OwnerFileError("original_cache_manifest_mismatch")
    return {"original_sha256": source["sha256"], "cached_sha256": result["sha256"], "entries": len(names),
            "all_nonmanifest_entry_bytes_equal": True, "manifest_policy": "unchanged_or_exact_original_Bundle-UpdateLocation"}


def addon_state(state: Path) -> dict:
    """Refuse an independent watched AndroidTV owner, rather than silently shadow it."""
    import zipfile
    path = state / "addons"
    parent = files._parent(path)
    os.close(parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISDIR(info.st_mode):
        raise files.OwnerFileError("watched_addons_directory_required")
    parent = files._parent(path / "placeholder") # Validate the addons directory too.
    os.close(parent)
    result = {}
    entries = list(path.iterdir())
    if len(entries) > 256:
        raise files.OwnerFileError("watched_addons_scope_too_large")
    for item in entries:
        info = item.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise files.OwnerFileError("watched_addon_not_regular")
        row = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode)}
        if item.suffix.lower() == ".kar":
            raise files.OwnerFileError("watched_feature_archive_requires_review")
        if item.suffix.lower() == ".jar":
            metadata, _ = files.snapshot(item)
            row["sha256"] = metadata["sha256"]
            try:
                with zipfile.ZipFile(item) as archive:
                    if len(archive.namelist()) != len(set(archive.namelist())):
                        raise files.OwnerFileError("watched_addon_duplicate_entries")
                    if archive.getinfo("META-INF/MANIFEST.MF").file_size > 128 * 1024:
                        raise files.OwnerFileError("watched_addon_manifest_oversized")
                    main = archive.read("META-INF/MANIFEST.MF").decode("utf-8").replace("\r\n", "\n").split("\n\n", 1)[0]
                    headers = manifest_headers((main + "\n\n").encode())
                    bsn = next((v for k, v in headers.items() if k.lower() == "bundle-symbolicname"), "")
                    if bsn.split(";", 1)[0].strip() == "org.openhab.binding.androidtv":
                        raise files.OwnerFileError("competing_watched_androidtv_bundle")
            except (KeyError, UnicodeError, zipfile.BadZipFile) as error:
                raise files.OwnerFileError("watched_addon_manifest_invalid") from error
        result[item.name] = row
    return result


def verified_report(report: dict, manifest: dict, *, patched: bool, require_absent=False) -> dict:
    if report.get("status") != "ok" or not isinstance(report.get("things"), list):
        raise files.OwnerFileError("native_report_unavailable")
    rows = report["things"]
    if any(set(row) != {"uid", "editable", "disabled"} or type(row["disabled"]) is not bool
           or row["editable"] is not False for row in rows):
        raise files.OwnerFileError("editable_or_invalid_google_thing")
    expected = [] if require_absent else manifest["google_things"]
    if sorted(row["uid"] for row in rows) != expected:
        raise files.OwnerFileError("google_thing_scope_mismatch")
    active = [row for row in report.get("bundles", []) if row.get("state") == 32]
    if len(active) != 1 or type(active[0].get("id")) is not int or not 0 < active[0]["id"] < 100000:
        raise files.OwnerFileError("sole_active_binding_required")
    row = active[0]
    version = "5.2.3.thermite1" if patched else "5.2.3"
    if row.get("version") != version:
        raise files.OwnerFileError("binding_version_mismatch")
    return row


def verify_loaded(package: Path, manifest: dict, report: dict, *, patched: bool, require_absent=False) -> dict:
    row = verified_report(report, manifest, patched=patched, require_absent=require_absent)
    cache = Path(manifest["state"]) / "userdata/cache/org.eclipse.osgi" / str(row["id"])
    matches = []
    if patched:
        wanted = class_hashes(package / manifest["files"]["artifact"]["payload"])
        if any(row.get(key) != value for key, value in wanted.items()):
            raise files.OwnerFileError("loaded_class_mismatch")
    for candidate in cache.glob("*/bundleFile"):
        if patched:
            current, _ = files.snapshot(candidate)
            # The retired original revision is expected, never executed/accepted as the derivative.
            if current.get("sha256") == pins()["assets"]["bundle"]["sha256"]:
                continue
            try:
                original_cache_report(package / "original.jar", candidate)
                continue
            except (OSError, ValueError):
                pass
            matches.append(cached_jar_report(package / manifest["files"]["artifact"]["payload"], candidate,
                                            manifest["artifact_sha256"]))
        else:
            try:
                report = original_cache_report(package / "original.jar", candidate)
                original = class_hashes_original(candidate)
                if any(row.get(key) != value for key, value in original.items()):
                    raise files.OwnerFileError("loaded_original_class_mismatch")
                matches.append(report)
            except (ValueError, OSError):
                # A retained retired derivative is accepted ONLY by its separate exact verifier.
                cached_jar_report(package / manifest["files"]["artifact"]["payload"], candidate,
                                  manifest["artifact_sha256"])
    if len(matches) != 1:
        raise files.OwnerFileError("active_cache_artifact_not_verified")
    return {"version": row["version"], "bundle_id": row["id"], "cache": matches[0]}


def class_hashes_original(artifact: Path) -> dict:
    import zipfile
    with zipfile.ZipFile(artifact) as archive:
        return {key: hashlib.sha256(archive.read(name)).hexdigest()
                for key, name in CLASSES.items() if key != "helper_sha256"}


def _journal(package: Path, value: dict) -> None:
    path = package / "transaction.json"
    old, _ = files.snapshot(path)
    files.atomic_change(path, old, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(),
                        {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})


def operate(package: Path, manifest_sha: str, adapter, *, rollback=False) -> dict:
    """No automatic undo on ambiguity: stop, keep sources quarantined, report failure."""
    package = Path(package)
    manifest = files.load(package, manifest_sha)
    lock = os.open(package / "operation.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior = package / "transaction.json"
        saved = json.loads(files.snapshot(prior)[1]) if prior.exists() else None
        if rollback and (not saved or saved.get("manifest_sha256") != manifest_sha):
            raise files.OwnerFileError("matching_apply_receipt_required")
        adapter.preflight(manifest, allow_stopped=rollback)
        if addon_state(Path(manifest["state"])) != manifest.get("addons", {}):
            raise files.OwnerFileError("watched_addons_changed")
        if rollback:
            if not saved or saved.get("manifest_sha256") != manifest_sha or not isinstance(saved.get("disabled"), dict):
                raise files.OwnerFileError("matching_apply_receipt_required")
            if saved.get("status") == "rolled_back":
                raise files.OwnerFileError("already_rolled_back")
            disabled = saved["disabled"]
            if adapter.is_running() and all(files.snapshot(Path(manifest["state"]) / row)[0]["exists"]
                                            for row in files.GOOGLE.values()):
                before = adapter.probe("inspect")
                verified_report(before, manifest, patched=True)
                verify_loaded(package, manifest, before, patched=True)
                disabled = {row["uid"]: row["disabled"] for row in before["things"]}
        else:
            if saved is not None:
                raise files.OwnerFileError("transaction_already_started_use_rollback")
            before = adapter.probe("inspect")
            verified_report(before, manifest, patched=False)
            verify_loaded(package, manifest, before, patched=False)
            disabled = {row["uid"]: row["disabled"] for row in before["things"]}
        result = {"version": 1, "manifest_sha256": manifest_sha, "operation": "rollback" if rollback else "apply",
                  "status": "started", "disabled": disabled, "events": []}
        def event(name):
            result["events"].append(name)
            _journal(package, result)
        event("preflight_complete")
        try:
            adapter.stop()
            adapter.assert_stopped()
            if addon_state(Path(manifest["state"])) != manifest.get("addons", {}):
                raise files.OwnerFileError("watched_addons_changed")
            event("old_process_retired")
            # Never unload old blocking handlers while the old process is alive.
            for role in files.GOOGLE:
                path = Path(manifest["state"]) / manifest["files"][role]["path"]
                current, _ = files.snapshot(path)
                if current == manifest["files"][role]["before"]:
                    files.atomic_change(path, current, None)
                elif current["exists"]:
                    raise files.OwnerFileError("quarantine_concurrent_edit")
            event("google_sources_quarantined")
            if rollback:
                files.rollback_core(package, manifest, assert_stopped=adapter.assert_stopped)
            else:
                files.install_core(package, manifest, assert_stopped=adapter.assert_stopped)
            event("core_files_restored" if rollback else "core_files_installed")
            adapter.start()
            # This one-shot calls the public FeaturesService API, never Karaf credentials.
            sample = adapter.probe("refresh")
            if sample.get("refreshed") is not True:
                raise files.OwnerFileError("native_refresh_not_acknowledged")
            result["binding"] = verify_loaded(package, manifest, sample, patched=not rollback, require_absent=True)
            event("sole_binding_verified_without_google_things")
            # Remove the privileged one-shot before restoring the ordinary source files.
            adapter.assert_no_probe()
            files.restore_google(package, manifest, assert_safe=lambda: None)
            event("exact_google_sources_restored")
            after = adapter.await_things(manifest["google_things"])
            verified_report(after, manifest, patched=not rollback)
            if {row["uid"]: row["disabled"] for row in after["things"]} != disabled:
                raise files.OwnerFileError("enabled_flags_changed")
            result["status"] = "rolled_back" if rollback else "applied"
            event("source_identity_and_enabled_flags_preserved")
        except BaseException:
            # A failed phase never restores sources into an unverified running binding.
            try:
                adapter.stop()
                adapter.assert_stopped()
                for role in files.GOOGLE:
                    path = Path(manifest["state"]) / manifest["files"][role]["path"]
                    current, _ = files.snapshot(path)
                    if current == manifest["files"][role]["before"]:
                        files.atomic_change(path, current, None)
                result["status"] = "failed_stopped"
            except BaseException:
                result["status"] = "failed_stop_unconfirmed"
            _journal(package, result)
            raise
        return result
    finally:
        os.close(lock)


class SystemAdapter:
    """Fixed rootful Quadlet service/container; no arbitrary service/URL/command options."""
    def __init__(self, state: Path, *, timeout=240):
        if not 30 <= timeout <= 300:
            raise files.OwnerFileError("bounded_timeout_required")
        self.state, self.timeout = Path(state), timeout
        self.old_pids = {}

    def preflight(self, manifest, *, allow_stopped=False):
        if self.state != Path(manifest["state"]):
            raise files.OwnerFileError("running_native_state_required")
        if not self.is_running():
            if allow_stopped:
                # Only reachable after matching an earlier guarded apply journal.
                self.assert_stopped()
                return
            raise files.OwnerFileError("running_native_state_required")
        mounts = json.loads(self.command(["/usr/bin/podman", "container", "inspect", "openhab", "--format", "{{json .Mounts}}"]),)
        if not isinstance(mounts, list):
            raise files.OwnerFileError("native_mounts_unavailable")
        for folder in ("conf", "userdata", "addons"):
            found = [row for row in mounts if row.get("Destination") == "/openhab/" + folder]
            if len(found) != 1 or found[0].get("Source") != str(self.state / folder):
                raise files.OwnerFileError("native_state_mount_mismatch")

    def command(self, args):
        value = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout, check=False,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        if value.returncode:
            raise files.OwnerFileError("owner_service_command_failed")
        return value.stdout

    def status(self):
        output = self.command(["/usr/bin/systemctl", "show", "openhab.service", "--no-pager",
                               "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID", "-p", "ControlGroup"])
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if values.get("LoadState") != "loaded" or not re.fullmatch(r"[0-9]+", values.get("MainPID", "")):
            raise files.OwnerFileError("known_native_service_required")
        return values

    def cgroup_pids(self, group):
        if not group:
            return set()
        if not group.startswith("/") or ".." in Path(group).parts or not group.endswith("/openhab.service"):
            raise files.OwnerFileError("unexpected_native_cgroup")
        base = Path("/sys/fs/cgroup") / group.lstrip("/")
        return {int(line) for path in base.rglob("cgroup.procs") for line in path.read_text().splitlines()}

    def stop(self):
        before = self.status()
        self.group = before.get("ControlGroup", "")
        for pid in self.cgroup_pids(self.group):
            try:
                self.old_pids[pid] = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except FileNotFoundError:
                pass
        self.command(["/usr/bin/systemctl", "stop", "openhab.service"])
        self.assert_stopped()

    def is_running(self):
        return self.status().get("ActiveState") in ("active", "activating", "reloading")

    def assert_stopped(self):
        current = self.status()
        if current.get("ActiveState") not in ("inactive", "failed") or current["MainPID"] != "0":
            raise files.OwnerFileError("native_service_not_stopped")
        if (self.cgroup_pids(getattr(self, "group", "/system.slice/openhab.service"))
                or self.cgroup_pids(current.get("ControlGroup", ""))):
            raise files.OwnerFileError("old_native_cgroup_not_retired")
        for pid, start in self.old_pids.items():
            try:
                if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19] == start:
                    raise files.OwnerFileError("old_native_process_not_retired")
            except FileNotFoundError:
                pass
        existence = subprocess.run(["/usr/bin/podman", "container", "exists", "openhab"], capture_output=True,
                                   timeout=10, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        if existence.returncode == 1:
            return # Quadlet normally removes its stopped container.
        if existence.returncode != 0:
            raise files.OwnerFileError("native_container_state_unavailable")
        status = self.command(["/usr/bin/podman", "container", "inspect", "openhab", "--format", "{{.State.Running}} {{.State.Pid}}"])
        if status.strip() != "false 0":
            raise files.OwnerFileError("native_container_not_stopped")

    def start(self):
        self.assert_stopped()
        self.command(["/usr/bin/systemctl", "start", "openhab.service"])

    def assert_no_probe(self):
        if files.snapshot(self.state / SCRIPT)[0]["exists"]:
            raise files.OwnerFileError("maintenance_script_still_installed")

    def probe(self, action):
        if action not in ("inspect", "refresh"):
            raise files.OwnerFileError("invalid_probe_action")
        self.assert_no_probe()
        identity = secrets.token_hex(16)
        folder = self.state / "userdata/thermite/maintenance" / identity
        parent = files._parent(folder, create=True)
        try:
            os.mkdir(folder.name, 0o700, dir_fd=parent)
        finally:
            os.close(parent)
        native = (self.state / "userdata").stat()
        os.chown(folder, native.st_uid, native.st_gid)
        source = Path(__file__).with_name("maintenance_probe.js").read_text()
        payload = source.replace("__THERMITE_CONFIG__", json.dumps({"id": identity, "action": action})).encode()
        metadata = {"mode": 0o644, "uid": native.st_uid, "gid": native.st_gid}
        script = self.state / SCRIPT
        files.atomic_change(script, {"exists": False}, payload, metadata)
        expected, _ = files.snapshot(script)
        try:
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                report, data = files.snapshot(folder / "report.json")
                if data is not None:
                    if len(data) > 128 * 1024:
                        raise files.OwnerFileError("native_report_oversized")
                    value = json.loads(data)
                    if value.get("id") != identity or value.get("action") != action:
                        raise files.OwnerFileError("native_report_identity_mismatch")
                    return value
                time.sleep(.2)
            raise files.OwnerFileError("native_probe_timeout")
        finally:
            files.atomic_change(script, expected, None)

    def await_things(self, uids):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            sample = self.probe("inspect")
            if sorted(row["uid"] for row in sample.get("things", [])) == uids:
                return sample
            time.sleep(.5)
        raise files.OwnerFileError("google_restore_timeout")


def trusted(path: Path, *, directory=False):
    """Root operations never execute or consume mutable user-owned package code/data."""
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise files.OwnerFileError("root_owned_nonwritable_path_required")
    if directory and not path.is_dir():
        raise files.OwnerFileError("directory_required")


def package_program(destination: Path) -> dict:
    """Stage reviewed source as data. Root must independently pin/verify it before execution."""
    if destination.exists() or not destination.is_absolute():
        raise files.OwnerFileError("fresh_absolute_program_path_required")
    parent = files._parent(destination)
    try:
        os.mkdir(destination.name, 0o700, dir_fd=parent)
    finally:
        os.close(parent)
    package = destination / "oh_androidtv_patch"
    package.mkdir(mode=0o700)
    sources = [p for p in Path(__file__).parent.iterdir() if p.is_file() and p.suffix in (".py", ".js", ".json", ".patch", ".java", ".md")]
    manifest = {"version": 1, "files": {}}
    for source in sources:
        metadata, data = files.snapshot(source)
        files.atomic_change(package / source.name, {"exists": False}, data,
                            {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})
        manifest["files"][source.name] = metadata["sha256"]
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    files.atomic_change(destination / "program.json", {"exists": False}, encoded,
                        {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})
    return {"program": str(destination), "program_sha256": files.digest(encoded), "files": len(sources), "activation": "not_started"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    program = sub.add_parser("package-program", help="copy reviewed code into a private owner-installable data bundle")
    program.add_argument("--output", type=Path, required=True)
    stage = sub.add_parser("stage", help="read approved state; create private data package only")
    for name in ("state", "artifact", "original-artifact", "build-receipt", "package"):
        stage.add_argument("--" + name, type=Path, required=True)
    stage.add_argument("--build-receipt-sha256", required=True)
    for name in ("native-proof", "maintenance-proof"):
        stage.add_argument("--" + name, type=Path, required=True)
        stage.add_argument("--" + name + "-sha256", required=True)
    for action in ("apply", "rollback"):
        command = sub.add_parser(action)
        command.add_argument("--package", type=Path, required=True)
        command.add_argument("--manifest-sha256", required=True)
        command.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    try:
        if args.action == "package-program":
            result = package_program(args.output)
        elif args.action == "stage":
            artifact_sha = files.snapshot(args.artifact)[0].get("sha256")
            proofs = {"lifecycle": proof(args.native_proof, args.native_proof_sha256, artifact_sha),
                      "maintenance": proof(args.maintenance_proof, args.maintenance_proof_sha256, artifact_sha, maintenance=True),
                      "program": program_hashes()}
            addons = addon_state(args.state)
            result = files.stage(args.state, args.artifact, args.build_receipt, args.build_receipt_sha256, args.package,
                                 original_artifact=args.original_artifact)
            path = args.package / "manifest.json"
            previous, data = files.snapshot(path)
            manifest = json.loads(data)
            manifest["proofs"] = proofs
            manifest["addons"] = addons
            encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            files.atomic_change(path, previous, encoded, previous)
            result["manifest_sha256"] = files.digest(encoded)
        else:
            if os.geteuid() != 0:
                raise files.OwnerFileError("owner_root_execution_required")
            for path in Path(__file__).parent.iterdir():
                if path.suffix in (".py", ".js", ".json"):
                    trusted(path)
            trusted(args.package, directory=True)
            for path in args.package.iterdir():
                trusted(path)
            manifest = files.load(args.package, args.manifest_sha256)
            approved = manifest.get("proofs", {})
            if (set(approved) != {"lifecycle", "maintenance", "program"} or approved["program"] != program_hashes()
                    or any(approved[key].get("artifact_sha256") != manifest["artifact_sha256"] for key in ("lifecycle", "maintenance"))):
                raise files.OwnerFileError("matching_native_and_maintenance_proofs_required")
            result = operate(args.package, args.manifest_sha256, SystemAdapter(Path(manifest["state"]), timeout=args.timeout),
                             rollback=args.action == "rollback")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (files.PatchError, OSError, ValueError, subprocess.SubprocessError) as error:
        # No paths, config snippets, native exceptions or subprocess output reach the terminal.
        code = str(error) if isinstance(error, files.PatchError) and re.fullmatch(r"[a-z0-9_]+", str(error)) else "maintenance_failed"
        print(json.dumps({"status": "refused", "reason": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
