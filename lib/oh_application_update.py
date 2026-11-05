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
import shutil
import stat
import subprocess
import sys
import tempfile

import oh_release
import oh_source_upgrade as transaction

OFFICIAL_SOURCE = "https://github.com/spincyc/thermite.git"
DEFAULT_REVISION = "main"
HOST_PHASE_PROTOCOL = 1
SITE_FILE = "thermite-site.json"
#: The hashed dependency lock installed before the application itself.
REQUIREMENTS = "lib/requirements-app.txt"
#: Application environments link to this interpreter; another minor version
#: cannot import what was installed for it.
PYTHON = "%d.%d" % sys.version_info[:2]
PROBE = "import sys, thermite_app; print('%d.%d' % sys.version_info[:2])"

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


class Transacted(Exception):
    """The upgrade's host phase ran its install transaction to an outcome
    other than a commit: it said what happened; nothing is resumed."""

    MESSAGES = {
        1: "The upgrade did not begin, and the previous owner command stays selected.",
        2: "Nothing of Thermite was changed; the previous owner command stays selected.",
        3: "The upgrade rolled back: the previous version runs again, and the previous owner command stays "
           "selected.",
        4: "The upgrade rolled back: the previous version runs again, but its health does not pass (above). "
           "The previous owner command stays selected.",
        5: "The rollback did not finish: Thermite stays stopped. Run {command} again to finish it.",
        130: "Interrupted: run {command} again; it rolls back the unfinished transaction first.",
    }

    def __init__(self, code: int, site: Path):
        super().__init__(code)
        self.code = code
        command = shlex.join(["thermite", "upgrade", "--config", str(site)])
        self.message = self.MESSAGES.get(code, self.MESSAGES[1]).format(command=command)


class Downgrade(ValueError):
    """The selected release is older than the one the site already pins."""


def run(*argv: str | Path, capture: bool = False) -> str:
    return transaction.run(*argv, capture=capture)


def host(*argv: str | Path) -> None:
    """Run a host phase in the foreground; it owns Ctrl-C and its cleanup."""
    code = transaction.foreground(*argv)
    if code:
        raise subprocess.CalledProcessError(code, [str(arg) for arg in argv])


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


def requirements(package: Path) -> Path:
    """The selected application's hashed dependency lock, else this program's."""
    for path in (Path(package) / REQUIREMENTS, Path(__file__).resolve().parent / Path(REQUIREMENTS).name):
        if path.is_file() and not path.is_symlink():
            return path
    raise ValueError(f"the application lacks its hashed dependency lock {REQUIREMENTS}")


def pyvenv(environment: Path) -> str | None:
    """The interpreter version an environment was created with."""
    try:
        lines = (environment / "pyvenv.cfg").read_text().splitlines()
    except OSError:
        return None
    return next((value.strip() for key, _, value in (line.partition("=") for line in lines)
                 if key.strip() == "version"), None)


def stale(environment: Path, saved: dict) -> str | None:
    """Why a retained environment cannot run here any more, or None.

    Environments link to the system interpreter; after a minor upgrade the
    link runs a Python that cannot import what was installed for the old one.
    """
    if saved.get("python") not in (None, PYTHON):
        return f"it was built for Python {saved['python']} and this is Python {PYTHON}"
    try:
        running = run(environment / "bin/python", "-I", "-B", "-c", PROBE, capture=True).strip()
    except (subprocess.CalledProcessError, OSError):
        return "its Python can no longer start the application"
    built = ".".join((pyvenv(environment) or running).split(".")[:2])
    if running != built:
        return f"it was built for Python {built} and its interpreter is now Python {running}"
    return None


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
        reason = stale(environment, saved)
        if reason is None:
            release = verify(environment)
            if saved.get("release") != release:
                raise ValueError("saved application release changed; preserve it for review")
            say(f"Using verified Thermite {release['digest'][:12]} from {environment}")
            return environment, release
        say(f"Rebuilding the application environment: {reason}. {environment} is retained unchanged.")
    environment = Path(tempfile.mkdtemp(prefix="environment-", dir=store))
    say(f"Preparing the selected Thermite application in {environment}")
    run(sys.executable, "-m", "venv", environment)
    pip = (environment / "bin/python", "-I", "-m", "pip", "--isolated", "install", "--disable-pip-version-check")
    run(*pip, "--require-hashes", "-r", requirements(package))
    run(*pip, "--no-deps", "--no-build-isolation", package)
    release = verify(environment)
    if key.startswith("git-") and transaction.clean(package) != key.removeprefix("git-"):
        raise ValueError("the selected application changed during packaging")
    if key.startswith("release-") and release["digest"] != key.removeprefix("release-"):
        raise ValueError("packaged application differs from the selected installed release")
    descriptor, temporary = tempfile.mkstemp(prefix=".candidate-", dir=store)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({"environment": environment.name, "release": release, "python": PYTHON,
                       "pyvenv": pyvenv(environment)}, output)
            output.write("\n")
        os.replace(temporary, record)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return environment, release


def commits(store: Path) -> dict[str, set[str]]:
    """Release digest -> the source commits this owner prepared it from."""
    found: dict[str, set[str]] = {}
    for record in store.glob("git-*.json"):
        try:
            digest = json.loads(record.read_text())["release"]["digest"]
        except (OSError, ValueError, TypeError, KeyError):
            continue
        commit = record.name.removeprefix("git-").removesuffix(".json")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{40,64}", commit):
            found.setdefault(digest, set()).add(commit)
    return found


def status(repository: Path, *args: str) -> int:
    """A read-only Git query's exit status, without its output."""
    return subprocess.run(["git", "-C", str(repository), *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).returncode


def ancestor(repositories: Sequence[Path], first: str, later: str) -> bool | None:
    """Whether commit `first` precedes `later` in a repository holding both, if any does."""
    for repository in repositories:
        if repository.is_dir() and not any(status(repository, "cat-file", "-e", commit + "^{commit}")
                                           for commit in (first, later)):
            code = status(repository, "merge-base", "--is-ancestor", first, later)
            if code in (0, 1):
                return code == 0
    return None


def pin_order(site_root: Path, site: Path) -> list[str]:
    """Release digests in the order this site's Git history first pinned them."""
    path = (site / SITE_FILE).relative_to(site_root).as_posix()
    order: list[str] = []
    for commit in transaction.git(site_root, "log", "--reverse", "--diff-filter=AM", "--format=%H",
                                  "--", f":(literal){path}").split():
        try:
            digest = json.loads(transaction.git(site_root, "show", f"{commit}:{path}"))["release"]
        except (subprocess.CalledProcessError, ValueError, TypeError, KeyError):
            continue
        if isinstance(digest, str) and digest not in order:
            order.append(digest)
    return order


def older(store: Path, site_root: Path, site: Path, digest: str, commit: str | None,
          repositories: Sequence[Path] = ()) -> str | None:
    """Why release `digest` is older than the site's committed pin, or None.

    Every development build shares one version, so order comes from Git: the
    source commits this owner prepared both releases from, else the order in
    which the site first pinned them. Unknown order is not a downgrade.
    """
    try:
        pinned = json.loads((site / SITE_FILE).read_text())["release"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(pinned, str) or pinned == digest:
        return None
    if commit is not None:
        repositories = [*repositories, store / "source.git", store / "sources" / commit]
        for newer in sorted(commits(store).get(pinned, ())):
            known = ancestor(repositories, commit, newer)
            if known is not None:
                return f"its source {commit[:12]} precedes the pinned release's {newer[:12]}" if known else None
    order = pin_order(site_root, site)
    if digest in order and pinned in order and order.index(digest) < order.index(pinned):
        return "the site pinned it before the current release"
    return None


def refuse_older(reason: str | None, site: Path, digest: str) -> None:
    if reason is not None:
        try:
            pinned = json.loads((site / SITE_FILE).read_text())["release"]
        except (OSError, ValueError, TypeError, KeyError):
            pinned = "?"
        raise Downgrade(f"refusing to pin release {digest[:12]} over the site's newer release {str(pinned)[:12]}: "
                        f"{reason}. The newer release may already have migrated state, such as the identity schema, "
                        "that an older one cannot read. Restore matching backups deliberately "
                        "(docs/installation.md#update-or-remove), then rerun with --allow-downgrade.")


def preview(operation: str, site: Path, program: Path, exact: bool, host_args: Sequence[str],
            say: Callable[[str], None]) -> int:
    """Describe this host now with the running release, when the site pins it.

    The selected application plans its own steps only after approval; this
    read-only child is the running release's own host dry run.
    """
    try:
        current = oh_release.manifest(program)["digest"]
        pinned = json.loads((site / SITE_FILE).read_text())["release"]
    except (oh_release.ReleaseError, OSError, ValueError, TypeError, KeyError):
        current = pinned = None
    if current is None or current != pinned:
        say("No host plan is shown: the site does not pin the release running this command. An approved run "
            "plans the host with the selected application after committing its requirement.")
        return 0
    say(f"Provisional host plan from the current release {current[:12]}: it describes this host now"
        + ("; the approved run checks every step again." if exact else
           "; the selected application plans its own steps after approval, and they may differ."))
    return transaction.foreground(sys.executable, program / "bin/thermite", operation, "--config", site,
                                  "--_host-phase", "--dry-run", *host_args)


def apply(operation: str, site: Path, *, source: Path | None = None, revision: str | None = None,
          program: Path | None = None, dry_run: bool = False, approve: Callable[[], bool],
          home: Path | None = None, runtime_token_file: Path | None = None,
          host_args: Sequence[str | Path] = (), allow_downgrade: bool = False,
          say: Callable[[str], None] = print) -> int:
    if operation not in {"install", "upgrade"}:
        raise ValueError("application workflow must be install or upgrade")
    if revision is not None and (not revision or revision.startswith("-") or "\0" in revision):
        raise ValueError("select a Git branch, tag or commit as --revision")
    site = Path(site).expanduser().absolute()
    if not (site / SITE_FILE).is_file():
        raise ValueError("--config must name an existing private Thermite site; create a separate site "
                         "with thermite init DIR, commit its configuration, then pass --config DIR")
    site_root = Path(transaction.git(site, "rev-parse", "--show-toplevel"))
    site_head = transaction.clean(site_root)
    if program is None:
        from oh_paths import PROGRAM
        program = PROGRAM
    program = Path(program).expanduser().absolute()
    # An installed application's first deployment uses its exact release. A
    # source launch uses its clean current checkout. Only upgrade fetches latest.
    if source is None and operation == "install":
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
    if release_digest is not None and not allow_downgrade:
        refuse_older(older(store, site_root, site, release_digest, None), site, release_digest)
    forwarded = [*map(str, host_args)]
    if runtime_token_file is not None:
        forwarded.extend(("--runtime-token-file", str(runtime_token_file)))
    if dry_run:
        say("Would resolve the selected application, prepare or reuse a private Python environment, "
            "validate and commit its site release requirement, apply the complete host and access "
            "workflow, and select the owner command only after all readiness checks pass.")
        if source is None:
            say("The remote revision and its host changes are unknown until an approved run fetches it; "
                "this dry run does not fetch or change the site, application, or host.")
        return preview(operation, site, program, release_digest is not None and source == program, forwarded, say)
    # Fail a missing commit identity before asking or preparing anything.
    transaction.git(site_root, "var", "GIT_AUTHOR_IDENT")
    transaction.git(site_root, "var", "GIT_COMMITTER_IDENT")
    say("This run includes application acquisition, the site release commit, native services, "
        "the access gateway, sudo commands and readiness checks"
        + (", as one install transaction that commits or rolls back." if operation == "upgrade" else "."))
    if not approve():
        return 1
    key = None
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
            if not allow_downgrade:
                commit = key.removeprefix("git-") if key.startswith("git-") else None
                refuse_older(older(store, site_root, site, release["digest"], commit,
                                   [source] if source_head is not None else []), site, release["digest"])
            command = environment / "bin/thermite"
            transaction.site_update(command, site, expected_head=site_head)
            committed = transaction.clean(site_root)
            if operation == "upgrade":
                # The host phase undoes the pin commit itself if its install
                # transaction rolls back or never begins.
                try:
                    host(command, operation, "--config", site, "--_host-phase", "--_run-approved",
                         "--_site-before", site_head, *forwarded)
                except subprocess.CalledProcessError as exc:
                    raise Transacted(exc.returncode, site) from None
            else:
                host(command, operation, "--config", site, "--_host-phase", "--_run-approved", *forwarded)
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
        except Downgrade:
            raise
        except Transacted as outcome:
            say(outcome.message)
            return outcome.code
        except BaseException:
            from oh_install import changeover
            if operation == "upgrade" and changeover.open_transaction(site / "var"):
                say("The run stopped with an install transaction open. Run "
                    + shlex.join(["thermite", "upgrade", "--config", str(site)])
                    + " again: it rolls the transaction back first, then stops.")
                raise
            say("The run stopped; the previous owner command and application environments are retained. "
                "A completed site release commit is retained so the same command can resume.")
            resume = ["thermite", operation, "--config", str(site)]
            if source is not None and (operation == "upgrade" or program != source):
                resume.extend(("--source", str(source)))
            if revision is not None:
                resume.extend(("--revision", revision))
            elif source is None and key is not None and key.startswith("git-"):
                # Resume the fetched commit, not whatever the branch names later.
                resume.extend(("--revision", key.removeprefix("git-")))
            if allow_downgrade:
                resume.append("--allow-downgrade")
            resume.extend(forwarded)
            say("Resume: " + shlex.join(resume))
            raise
    say(f"{operation.capitalize()} complete. {launcher} selects the verified application; "
        "previous environments are retained.")
    found = shutil.which("thermite")
    if found is None or Path(found).resolve() != launcher.resolve():
        say(f"Warning: `thermite` on PATH is {found or 'missing'}, not {launcher}. Put {launcher.parent} "
            "first on PATH so later commands run the verified application.")
    return 0


def upgrade(site: Path, **kwargs) -> int:
    return apply("upgrade", site, **kwargs)


def install(site: Path, **kwargs) -> int:
    return apply("install", site, **kwargs)
