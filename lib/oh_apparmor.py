"""Finite root installation of node_exporter's service-scoped confinement.

The installer presents this standalone helper before each administrative action.
Verification can reload only the named policy using the selected system parser.
It never changes global container configuration or starts/stops services.
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

PROFILE = "thermite-node-exporter"
CONFIG = Path("/etc/thermite/node-exporter-containers.conf")
POLICY = Path("/etc/apparmor.d/thermite-node-exporter")
LICENSE = Path("/usr/share/doc/thermite/licenses/node-exporter-apparmor.LICENSE")
MODULE_ENABLED = Path("/sys/module/apparmor/parameters/enabled")
PROFILES = Path("/sys/kernel/security/apparmor/profiles")
SOURCE = Path(__file__).with_name("thermite-node-exporter.apparmor")
MAXIMUM = 65536
PARSERS = ("/usr/sbin/apparmor_parser", "/usr/bin/apparmor_parser", "/sbin/apparmor_parser")


def enabled(system):
    value = system.read_bytes(MODULE_ENABLED)
    if value is None:
        if system.stat(MODULE_ENABLED) is not None:
            raise ValueError("cannot read the kernel AppArmor status")
        return False
    value = value.strip()
    if value not in (b"Y", b"N"):
        raise ValueError("unrecognized kernel AppArmor status")
    return value == b"Y"


def expected(active):
    if type(active) is not bool:
        raise ValueError("an explicit kernel AppArmor state is required")
    config = b"# Thermite node_exporter only; merged after the host configuration.\n[containers]\n"
    if not active:
        return {CONFIG: config}
    return {CONFIG: config + b'apparmor_profile="thermite-node-exporter"\n',
            POLICY: SOURCE.read_bytes(), LICENSE: Path(str(SOURCE) + ".LICENSE").read_bytes()}


def _parent(path, stack, uid, gid, *, create=True):
    descriptor = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, descriptor)
    for part in path.parts[1:]:
        created = False
        try:
            child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(part, 0o755, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
            child = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        stack.callback(os.close, child)
        if created:
            if os.fstat(child).st_uid != uid:
                raise ValueError("new confinement directory changed custody")
            writable = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=child)
            try:
                os.fchown(writable, uid, gid)
                os.fchmod(writable, 0o755)
            finally:
                os.close(writable)
        descriptor = child
    info = os.fstat(descriptor)
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("confinement parent must be administrator-owned and non-writable")
    descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
    stack.callback(os.close, descriptor)
    return descriptor


def _write(path, content, *, uid=0, gid=0):
    """Replace one regular administrative file through an anchored parent.

    The CLI supplies only expected()'s finite absolute paths and root identity.
    Tests exercise the same inode checks with their own unprivileged identity.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or not path.name or len(content) > MAXIMUM:
        raise ValueError("invalid confinement artifact")
    with ExitStack() as stack:
        parent = _parent(path.parent, stack, uid, gid)
        previous = None
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            pass
        else:
            stack.callback(os.close, descriptor)
            previous = os.fstat(descriptor)
            if (not stat.S_ISREG(previous.st_mode) or previous.st_uid != uid or previous.st_nlink != 1
                    or stat.S_IMODE(previous.st_mode) & 0o022):
                raise ValueError("confinement artifact must be private to its administrator and regular")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                same = stream.read(MAXIMUM + 1) == content
            if same:
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, 0o644)
                return False
        temporary = "." + path.name + "." + os.urandom(12).hex()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchown(stream.fileno(), uid, gid)
                os.fchmod(stream.fileno(), 0o644)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if previous is None:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                if not os.path.samestat(previous, os.stat(path.name, dir_fd=parent, follow_symlinks=False)):
                    raise ValueError("confinement artifact changed during installation")
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.fsync(parent)
        return True


def _verify(parser, *, uid=0, gid=0):
    """Verify enforcement as root; securityfs rejects ordinary owner reads.

    Policy reload and the durable recreation marker occur only when necessary.
    Private identity parameters let filesystem tests stay unprivileged.
    """
    if parser not in PARSERS:
        raise ValueError("choose the distribution's system AppArmor parser")
    with ExitStack() as stack:
        descriptors, parents = {}, {}
        for path, content in expected(True).items():
            parent = _parent(path.parent, stack, uid, gid, create=False)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != (uid, gid)
                    or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1):
                raise ValueError("unsafe confinement artifact during enforcement verification")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                if stream.read(MAXIMUM + 1) != content:
                    raise ValueError("confinement artifact differs from the reviewed application")
            descriptors[path], parents[path] = descriptor, parent
        def enforced():
            return f"{PROFILE} (enforce)" in PROFILES.read_text().splitlines()
        reload_ = (not enforced()
                   or os.fstat(descriptors[POLICY]).st_mtime_ns > os.fstat(descriptors[CONFIG]).st_mtime_ns)
        if reload_:
            argv = [parser, "-r", str(POLICY)]
            print(json.dumps({"load_policy": argv}), flush=True)
            subprocess.run(argv, check=True, timeout=60)
            if not enforced():
                raise ValueError("node exporter policy was not loaded in enforce mode")
            if not os.path.samestat(os.fstat(descriptors[CONFIG]),
                                    os.stat(CONFIG.name, dir_fd=parents[CONFIG], follow_symlinks=False)):
                raise ValueError("confinement configuration changed during verification")
            os.utime(descriptors[CONFIG], None)
            os.fsync(descriptors[CONFIG])
        return {"verified": True, "profile": PROFILE, "enforced": True, "restart_marked": reload_}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    verify = len(argv) == 2 and argv[0] == "--verify" and argv[1] in PARSERS
    if (argv and not verify) or os.geteuid() != 0:
        print("node confinement: run the exact installer-approved administrative command", file=sys.stderr)
        return 2
    try:
        try:
            value = MODULE_ENABLED.read_bytes().strip()
        except FileNotFoundError:
            value = b"N"
        if value not in (b"Y", b"N"):
            raise ValueError("invalid kernel AppArmor status")
        if verify:
            if value != b"Y":
                raise ValueError("verification requires an enabled AppArmor kernel")
            print(json.dumps(_verify(argv[1])))
            return 0
        artifacts = expected(value == b"Y")
        changed = [str(path) for path, content in artifacts.items() if _write(path, content)]
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("node confinement: operation refused; check administrative file custody and kernel status", file=sys.stderr)
        return 2
    print(json.dumps({"installed": True, "apparmor": value == b"Y", "changed": changed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
