"""Acquire one reviewed application and apply its complete host workflow.

Approval belongs to this invocation. The candidate gets it only on its host
phase command line; no environment variable or saved approval survives a retry.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

import oh_release
import oh_source_upgrade as transaction

OFFICIAL_SOURCE = "https://github.com/spincyc/thermite.git"
DEFAULT_REVISION = "main"
HOST_PHASE_PROTOCOL = 1

# Exact pip/distlib console entry points, following an absolute Python shebang.
# An existing custom owner command must never be silently replaced.
CONSOLE_SCRIPTS = (
    b"""# -*- coding: utf-8 -*-
import re
import sys
from thermite_app import main
if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])
    sys.exit(main())
""",
    b"""import sys
from thermite_app import main
if __name__ == '__main__':
    if sys.argv[0].endswith('.exe'):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
""",
)

# Ask the candidate's interpreter to verify its installed release. Importing
# the caller's oh_release would couple validation to the previous application.
VERIFY = """import json, os, pathlib, sys
import thermite_app
root = pathlib.Path(thermite_app.__file__).resolve().parent / 'runtime'
sys.pycache_prefix = str(root / ('.uncached-' + os.urandom(16).hex()))
sys.path.insert(0, str(root / 'lib'))
import oh_release
release = oh_release.manifest(root)
import oh_application_update
if getattr(oh_application_update, 'HOST_PHASE_PROTOCOL', None) != 1:
    raise SystemExit('selected application does not support the unified host workflow')
print(json.dumps({'root': str(root), 'digest': release['digest'], 'application': release['application']}))
"""


def run(*argv: str | Path, capture: bool = False) -> str:
    return transaction.run(*argv, capture=capture)


def safe_directory(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"application paths must not contain symlinks: {parent}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"application path must be a directory: {parent}")
    if path.exists() and path.stat().st_uid != os.getuid():
        raise ValueError(f"application directory must belong to this user: {path}")


def launcher_state(path: Path) -> str | tuple[bytes, int] | None:
    safe_directory(path.parent)
    if path.is_symlink():
        return os.readlink(path)
    if path.exists():
        info = path.stat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022:
            data = path.read_bytes()
            shebang, separator, body = data.partition(b"\n")
            interpreter = os.fsdecode(shebang[2:])
            if (separator and shebang.startswith(b"#!/") and body in CONSOLE_SCRIPTS
                    and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(interpreter).name)
                    and not any(char.isspace() for char in interpreter)):
                return data, stat.S_IMODE(info.st_mode)
        raise ValueError(f"refusing to replace a non-symlink launcher that is not a standard Thermite "
                         f"entry point: {path}; preserve the custom command elsewhere before retrying")
    return None


def retain_launcher(store: Path, previous: tuple[bytes, int]) -> Path:
    data, mode = previous
    backup = store / ("previous-launcher-" + hashlib.sha256(data).hexdigest())
    if backup.exists() or backup.is_symlink():
        if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != data:
            raise ValueError("saved previous owner command differs; preserving it for review")
        return backup
    with backup.open("xb") as output:
        output.write(data)
    backup.chmod(mode)
    return backup


def verify(environment: Path) -> dict:
    safe_directory(environment / "bin")
    command = environment / "bin/thermite"
    if command.is_symlink() or not command.is_file():
        raise ValueError("candidate application launcher is missing or was replaced")
    result = json.loads(run(environment / "bin/python", "-I", "-B", "-c", VERIFY, capture=True))
    if (not isinstance(result, dict) or set(result) != {"root", "digest", "application"}
            or not oh_release.HEX.fullmatch(str(result["digest"]))
            or not isinstance(result["application"], str)
            or environment.resolve() not in Path(result["root"]).resolve().parents):
        raise ValueError("candidate application reported an invalid release")
    return result


def unchanged_source(source: Path | None, head: str | None, release: str | None) -> None:
    if source is None:
        return
    if release is not None:
        oh_release.manifest(source, release)
    elif transaction.clean(source) != head:
        raise ValueError("the application revision changed during preparation; retry from a clean checkout")


def acquire(store: Path, source: Path | None, revision: str | None, release: str | None) -> tuple[Path, str]:
    """Return immutable packaging input and its cache key, after approval."""
    if release is not None:
        return source, "release-" + release
    if source is None:
        mirror = store / "source.git"
        safe_directory(mirror)
        if not mirror.exists():
            run("git", "init", "--bare", mirror)
            transaction.git(mirror, "remote", "add", "origin", OFFICIAL_SOURCE)
        if transaction.git(mirror, "remote", "get-url", "origin") != OFFICIAL_SOURCE:
            raise ValueError("the saved application origin differs from the official source")
        run("git", "-C", mirror, "fetch", "--no-tags", "origin", revision or DEFAULT_REVISION)
        commit = transaction.git(mirror, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        origin = mirror
    else:
        commit = transaction.git(source, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}")
        origin = source
    key = "git-" + commit
    checkout = store / "sources" / commit
    safe_directory(checkout)
    if not checkout.exists():
        checkout.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".source-", dir=store)) / "checkout"
        run("git", "clone", "--no-hardlinks", "--no-checkout", "--", origin, staging)
        transaction.git(staging, "checkout", "--detach", commit)
        staging.rename(checkout)
        staging.parent.rmdir()
    if transaction.clean(checkout) != commit:
        raise ValueError("saved application checkout changed; preserve and review it before retrying")
    return checkout, key


def candidate(store: Path, package: Path, key: str, say: Callable[[str], None]) -> tuple[Path, dict]:
    record = store / (key + ".json")
    if record.is_symlink():
        raise ValueError("candidate record must not be a symlink")
    if record.exists():
        saved = json.loads(record.read_text())
        name = saved.get("environment") if isinstance(saved, dict) else None
        if not isinstance(name, str) or Path(name).name != name or not name.startswith("environment-"):
            raise ValueError("invalid saved application environment")
        environment = store / name
        release = verify(environment)
        if saved.get("release") != release:
            raise ValueError("saved application release changed; preserve it for review")
        say(f"Using verified Thermite {release['digest'][:12]} from {environment}")
        return environment, release
    environment = Path(tempfile.mkdtemp(prefix="environment-", dir=store))
    say(f"Preparing the selected Thermite application in {environment}")
    run(sys.executable, "-m", "venv", environment)
    run(environment / "bin/python", "-I", "-m", "pip", "--isolated", "install",
        "--disable-pip-version-check", package)
    release = verify(environment)
    if key.startswith("git-") and transaction.clean(package) != key.removeprefix("git-"):
        raise ValueError("the selected application changed during packaging")
    if key.startswith("release-") and release["digest"] != key.removeprefix("release-"):
        raise ValueError("packaged application differs from the selected installed release")
    descriptor, temporary = tempfile.mkstemp(prefix=".candidate-", dir=store)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({"environment": environment.name, "release": release}, output)
            output.write("\n")
        os.replace(temporary, record)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return environment, release


def apply(operation: str, site: Path, *, source: Path | None = None, revision: str | None = None,
          program: Path | None = None, dry_run: bool = False, approve: Callable[[], bool],
          home: Path | None = None, runtime_token_file: Path | None = None,
          host_args: Sequence[str | Path] = (), say: Callable[[str], None] = print) -> int:
    if operation not in {"install", "upgrade"}:
        raise ValueError("application workflow must be install or upgrade")
    if revision is not None and (not revision or revision.startswith("-") or "\0" in revision):
        raise ValueError("select a Git branch, tag or commit as --revision")
    site = Path(site).expanduser().absolute()
    if not (site / "thermite-site.json").is_file():
        raise ValueError("--config must name an existing private Thermite site; create a separate site "
                         "with thermite init DIR, commit its configuration, then pass --config DIR")
    site_root = Path(transaction.git(site, "rev-parse", "--show-toplevel"))
    site_head = transaction.clean(site_root)
    # An installed application's first deployment uses its exact release. A
    # source launch uses its clean current checkout. Only upgrade fetches latest.
    if source is None and operation == "install":
        if program is None:
            from oh_paths import PROGRAM
            program = PROGRAM
        source = program
    source = Path(source).expanduser().absolute() if source is not None else None
    source_head = release_digest = None
    if source is not None:
        if (source / oh_release.FILE).is_file():
            if revision is not None:
                raise ValueError("--revision requires a Git application source")
            release_digest = oh_release.manifest(source)["digest"]
        else:
            source_head = transaction.clean(source)
            transaction.git(source, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}")
    home = Path.home() if home is None else Path(home)
    store = home / ".local/share/thermite/application"
    launcher = home / ".local/bin/thermite"
    safe_directory(store)
    previous_launcher = launcher_state(launcher)
    selected = str(source) if source is not None else OFFICIAL_SOURCE
    selected_revision = revision or ("current release" if release_digest else "HEAD" if source else DEFAULT_REVISION)
    say(f"Application: {selected} ({selected_revision}).")
    if dry_run:
        say("Would resolve the selected application, prepare or reuse a private Python environment, "
            "validate and commit its site release requirement, apply the complete host and access "
            "workflow, and select the owner command only after all readiness checks pass.")
        if source is None:
            say("The remote revision and its host changes are unknown until an approved run fetches it; "
                "this dry run does not fetch or change the site, application, or host.")
        return 0
    # Fail a missing commit identity before asking or preparing anything.
    transaction.git(site_root, "var", "GIT_AUTHOR_IDENT")
    transaction.git(site_root, "var", "GIT_COMMITTER_IDENT")
    say("This run includes application acquisition, the site release commit, native services, "
        "the access gateway, backups when needed, sudo commands and readiness checks.")
    if not approve():
        return 1
    with transaction.locked(store):
        if transaction.clean(site_root) != site_head:
            raise ValueError("the site revision changed while waiting for approval")
        unchanged_source(source, source_head, release_digest)
        if launcher_state(launcher) != previous_launcher:
            raise ValueError("the owner command changed while waiting for approval")
        try:
            package, key = acquire(store, source, revision, release_digest)
            environment, release = candidate(store, package, key, say)
            unchanged_source(source, source_head, release_digest)
            if release_digest is None and transaction.clean(package) != key.removeprefix("git-"):
                raise ValueError("the application revision changed during packaging")
            if transaction.clean(site_root) != site_head:
                raise ValueError("the site revision changed during application preparation")
            command = environment / "bin/thermite"
            transaction.site_update(command, site, expected_head=site_head)
            committed = transaction.clean(site_root)
            args = [str(command), operation, "--config", str(site), "--_host-phase", "--_run-approved",
                    *map(str, host_args)]
            if runtime_token_file is not None:
                args.extend(("--runtime-token-file", str(runtime_token_file)))
            run(*args)
            if transaction.clean(site_root) != committed:
                raise ValueError("the site revision changed during host verification")
            unchanged_source(source, source_head, release_digest)
            if verify(environment) != release:
                raise ValueError("the candidate application changed during host verification")
            if launcher_state(launcher) != previous_launcher:
                raise ValueError("the owner command changed during host verification; preserving it")
            if previous_launcher != str(command):
                if isinstance(previous_launcher, tuple):
                    backup = retain_launcher(store, previous_launcher)
                    say(f"Retained the previous installed owner command at {backup}")
                launcher.parent.mkdir(parents=True, exist_ok=True)
                temporary = launcher.with_name(".thermite-" + environment.name)
                try:
                    temporary.symlink_to(command)
                    os.replace(temporary, launcher)
                finally:
                    temporary.unlink(missing_ok=True)
        except BaseException:
            say("The run stopped; the previous owner command and application environments are retained. "
                "A completed site release commit is retained so the same command can resume.")
            resume = ["thermite", operation, "--config", str(site)]
            if source is not None and (operation == "upgrade" or program != source):
                resume.extend(("--source", str(source)))
            if revision is not None:
                resume.extend(("--revision", revision))
            resume.extend(map(str, host_args))
            if runtime_token_file is not None:
                resume.extend(("--runtime-token-file", str(runtime_token_file)))
            say("Resume: " + shlex.join(resume))
            raise
    say(f"{operation.capitalize()} complete. {launcher} selects the verified application; "
        "previous environments are retained.")
    return 0


def upgrade(site: Path, **kwargs) -> int:
    return apply("upgrade", site, **kwargs)


def install(site: Path, **kwargs) -> int:
    return apply("install", site, **kwargs)
