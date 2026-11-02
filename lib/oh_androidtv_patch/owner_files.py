"""Approved-data staging and compare-before-write transactions for binding maintenance.

No service, network, native API, cache or credential operations occur here.
The maintenance driver must prove the service/container is stopped before
calling a mutating transaction. Package manifests are approved by an external
SHA supplied separately from the package itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import stat

from . import ARTIFACT, MAVEN_PATH, REPOSITORY, PatchError, maven_config_text, override_text, pins

CORE = {
    "artifact": Path("userdata/thermite/maven") / MAVEN_PATH,
    "overrides": Path("userdata/etc/overrides.properties"),
    "maven": Path("userdata/etc/org.ops4j.pax.url.mvn.cfg"),
}
GOOGLE = {"google_yaml": Path("conf/yaml/googletv.yaml"),
          "google_js": Path("conf/automation/js/androidtv_googletv.js")}
MAX_BYTES = 32 * 1024 * 1024


class OwnerFileError(PatchError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parent(path: Path, create=False) -> int:
    """Walk with directory FDs: never follow any parent symlink."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise OwnerFileError("absolute_path_required")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=current)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def snapshot(path: Path) -> tuple[dict, bytes | None]:
    try:
        parent = _parent(path)
    except FileNotFoundError:
        return {"exists": False}, None
    try:
        try:
            # A FIFO must not block before fstat can reject it as non-regular.
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return {"exists": False}, None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
                raise OwnerFileError("regular_single_link_bounded_file_required")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(MAX_BYTES + 1)
            after = os.fstat(fd)
            if (len(data) != before.st_size or (before.st_ino, before.st_dev, before.st_mtime_ns, before.st_size)
                    != (after.st_ino, after.st_dev, after.st_mtime_ns, after.st_size)):
                raise OwnerFileError("file_changed_during_snapshot")
            return {"exists": True, "sha256": digest(data), "mode": stat.S_IMODE(before.st_mode),
                    "uid": before.st_uid, "gid": before.st_gid}, data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def same(current: dict, expected: dict) -> bool:
    return current == expected


def atomic_change(path: Path, expected: dict, data: bytes | None, metadata: dict | None = None) -> None:
    current, _ = snapshot(path)
    if not same(current, expected):
        raise OwnerFileError("preimage_mismatch")
    if data is None and not current["exists"]:
        return
    parent = _parent(path, create=data is not None)
    temporary = ".thermite-androidtv-" + secrets.token_hex(12)
    try:
        # Recheck immediately before replacing/removing the leaf.
        now, _ = snapshot(path)
        if not same(now, expected):
            raise OwnerFileError("preimage_mismatch")
        if data is None:
            os.unlink(path.name, dir_fd=parent)
            os.fsync(parent)
            return
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                out.write(data)
                out.flush()
                os.fsync(fd)
            mode = (metadata or {}).get("mode", 0o644)
            if mode & 0o7000:
                raise OwnerFileError("special_file_mode_rejected")
            if metadata:
                info = os.fstat(fd)
                if (info.st_uid, info.st_gid) != (metadata["uid"], metadata["gid"]):
                    os.fchown(fd, metadata["uid"], metadata["gid"])
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.rename(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def stage(state: Path, artifact: Path, receipt: Path, expected_receipt_sha256: str,
          package: Path, *, runtime_repository: Path = REPOSITORY, original_artifact: Path | None = None) -> dict:
    """Read exact owned inputs; create a fresh private DATA package, never change the state."""
    state, package = Path(state).absolute(), Path(package).absolute()
    if package.exists() or package.is_symlink():
        raise OwnerFileError("fresh_package_required")
    receipt_meta, receipt_bytes = snapshot(receipt)
    if receipt_bytes is None or receipt_meta["sha256"] != expected_receipt_sha256:
        raise OwnerFileError("build_receipt_sha_mismatch")
    build = json.loads(receipt_bytes)
    artifact_meta, artifact_bytes = snapshot(artifact)
    if (artifact_bytes is None or build.get("upstream") != "5.2.3" or build.get("patched") != "5.2.3.thermite1"
            or build.get("artifact", {}).get("sha256") != artifact_meta["sha256"]
            or build.get("inputs", {}).get("bundle") != pins()["assets"]["bundle"]["sha256"]):
        raise OwnerFileError("approved_build_artifact_required")
    observed = {}
    for role, relative in {**CORE, **GOOGLE}.items():
        observed[role] = snapshot(state / relative)
    if any(observed[role][1] is None for role in ("google_yaml", "google_js", "maven")):
        raise OwnerFileError("managed_google_configuration_required")
    import yaml
    try:
        document = yaml.safe_load(observed["google_yaml"][1])
    except yaml.YAMLError as exc:
        raise OwnerFileError("managed_yaml_invalid") from exc
    if not isinstance(document, dict) or not isinstance(document.get("things"), dict):
        raise OwnerFileError("managed_yaml_invalid")
    things = sorted(document["things"])
    import re
    if not things or len(things) > 64 or any(not re.fullmatch(r"androidtv:googletv:[A-Za-z0-9_-]+", uid) for uid in things):
        raise OwnerFileError("only_managed_google_things_allowed")
    try:
        wanted = {
            "artifact": artifact_bytes,
            "overrides": override_text((observed["overrides"][1] or b"").decode()).encode(),
            "maven": maven_config_text(observed["maven"][1].decode(), repository=runtime_repository).encode(),
        }
    except (PatchError, UnicodeError) as exc:
        raise OwnerFileError("configuration_merge_refused") from exc
    original = None
    if original_artifact is not None:
        meta, original = snapshot(original_artifact)
        if original is None or meta["sha256"] != pins()["assets"]["bundle"]["sha256"]:
            raise OwnerFileError("official_original_artifact_required")
    parent = _parent(package, create=False)
    try:
        os.mkdir(package.name, 0o700, dir_fd=parent)
    finally:
        os.close(parent)
    manifest = {"version": 1, "state": str(state), "artifact_sha256": artifact_meta["sha256"],
                "build_receipt_sha256": expected_receipt_sha256, "google_things": things,
                "runtime_repository": str(runtime_repository), "files": {}}
    if original is not None:
        atomic_change(package / "original.jar", {"exists": False}, original,
                      {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})
        manifest["original_sha256"] = digest(original)
    for role, relative in {**CORE, **GOOGLE}.items():
        before, content = observed[role]
        row = {"path": str(relative), "before": before}
        if content is not None:
            name = role + ".before"
            atomic_change(package / name, {"exists": False}, content, {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})
            row["backup"] = name
        if role in wanted:
            name = role + ".desired"
            atomic_change(package / name, {"exists": False}, wanted[role], {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})
            row.update(payload=name, desired_sha256=digest(wanted[role]))
            native = state.joinpath("userdata").stat()
            row["after"] = {"exists": True, "sha256": digest(wanted[role]),
                            "mode": before.get("mode", 0o644), "uid": before.get("uid", native.st_uid),
                            "gid": before.get("gid", native.st_gid)}
        manifest["files"][role] = row
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    atomic_change(package / "manifest.json", {"exists": False}, encoded,
                  {"mode": 0o600, "uid": os.getuid(), "gid": os.getgid()})
    return {"manifest_sha256": digest(encoded), "package": str(package), "files": len(manifest["files"]),
            "google_things": len(things), "activation": "not_started"}


def load(package: Path, expected_manifest_sha256: str) -> dict:
    meta, encoded = snapshot(package / "manifest.json")
    if encoded is None or meta["sha256"] != expected_manifest_sha256:
        raise OwnerFileError("manifest_external_sha_mismatch")
    manifest = json.loads(encoded)
    expected_paths = {role: str(path) for role, path in {**CORE, **GOOGLE}.items()}
    if (manifest.get("version") != 1 or set(manifest.get("files", {})) != set(expected_paths)
            or any(manifest["files"][role].get("path") != path for role, path in expected_paths.items())):
        raise OwnerFileError("manifest_scope_mismatch")
    for role, row in manifest["files"].items():
        for field, suffix, expected in (("backup", ".before", row["before"].get("sha256")),
                                        ("payload", ".desired", row.get("desired_sha256"))):
            if field not in row:
                continue
            if row[field] != role + suffix:
                raise OwnerFileError("manifest_blob_path_mismatch")
            value, _ = snapshot(package / row[field])
            if not value["exists"] or value["sha256"] != expected:
                raise OwnerFileError("package_blob_changed")
    if "original_sha256" in manifest:
        original, _ = snapshot(package / "original.jar")
        if (original.get("sha256") != manifest["original_sha256"]
                or manifest["original_sha256"] != pins()["assets"]["bundle"]["sha256"]):
            raise OwnerFileError("official_original_artifact_required")
    return manifest


def install_core(package: Path, manifest: dict, *, assert_stopped) -> None:
    assert_stopped()
    state = Path(manifest["state"])
    for role in CORE:
        current, _ = snapshot(state / manifest["files"][role]["path"])
        if current != manifest["files"][role]["before"]:
            raise OwnerFileError("preimage_mismatch")
    for role in CORE:
        row = manifest["files"][role]
        _, content = snapshot(package / row["payload"])
        before = row["before"]
        atomic_change(state / row["path"], before, content, row["after"])


def quarantine_google(package: Path, manifest: dict, *, assert_stopped) -> None:
    assert_stopped()
    for role in GOOGLE:
        row = manifest["files"][role]
        current, _ = snapshot(Path(manifest["state"]) / row["path"])
        if current != row["before"]:
            raise OwnerFileError("preimage_mismatch")
    for role in GOOGLE:
        row = manifest["files"][role]
        atomic_change(Path(manifest["state"]) / row["path"], row["before"], None)


def restore_google(package: Path, manifest: dict, *, assert_safe) -> None:
    """Caller proves stopped OR the intended binding is verified before restoring sources."""
    assert_safe()
    for role in GOOGLE:
        row = manifest["files"][role]
        _, content = snapshot(package / row["backup"])
        atomic_change(Path(manifest["state"]) / row["path"], {"exists": False}, content, row["before"])


def rollback_core(package: Path, manifest: dict, *, assert_stopped) -> None:
    """Keep Google sources quarantined until the original binding is verified."""
    assert_stopped()
    state = Path(manifest["state"])
    current = {}
    for role in CORE:
        row = manifest["files"][role]
        current[role], _ = snapshot(state / row["path"])
        if current[role] not in (row["before"], row["after"]):
            raise OwnerFileError("rollback_concurrent_edit")
    for role in reversed(CORE):
        row = manifest["files"][role]
        if current[role] == row["before"]:
            continue
        content = snapshot(package / row["backup"])[1] if "backup" in row else None
        atomic_change(state / row["path"], current[role], content,
                      row["before"] if row["before"]["exists"] else None)


def rollback_files(package: Path, manifest: dict, *, assert_stopped) -> None:
    """Restore only exact approved preimages; refuse every unrelated concurrent edit."""
    assert_stopped()
    state = Path(manifest["state"])
    current = {}
    for role, row in manifest["files"].items():
        current[role], _ = snapshot(state / row["path"])
        before = row["before"]
        if current[role] == before:
            continue
        acceptable = (not current[role]["exists"] if role in GOOGLE else current[role] == row["after"])
        if not acceptable:
            raise OwnerFileError("rollback_concurrent_edit")
    for role, row in reversed(list(manifest["files"].items())):
        if current[role] == row["before"]:
            continue
        content = snapshot(package / row["backup"])[1] if "backup" in row else None
        atomic_change(state / row["path"], current[role], content, row["before"] if row["before"]["exists"] else None)
