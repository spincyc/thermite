"""Source alias and bounded Git transaction for the unified application update."""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[1]
SITE_FILES = ("thermite-site.json", ".gitignore")


#: How long a child Ctrl-C reached too may clean up (git removes its lock
#: files) before it is killed.
INTERRUPT_GRACE = 10.0


def run(*argv: str | Path, capture: bool = False) -> str:
    """Run a child that must succeed. Captured, its standard output is the
    result; otherwise its output shows only if it fails, or with --verbose.
    A held child reads no terminal: git fails rather than ask unseen."""
    import oh_cli
    held = not capture and not oh_cli.switches.verbose
    process = subprocess.Popen([str(arg) for arg in argv], text=capture,
                               stdin=subprocess.DEVNULL if held else None,
                               stdout=subprocess.PIPE if capture or held else None,
                               stderr=subprocess.STDOUT if held else None,
                               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0",
                                    **({"GIT_TERMINAL_PROMPT": "0"} if held else {})})
    try:
        output = process.communicate()[0]
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=INTERRUPT_GRACE)
        process.kill()
        process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    if process.returncode:
        if held:
            from oh_install import output as child, progress
            if text := child.tail(output):
                with progress.paused():
                    print(text, file=sys.stderr, flush=True)
        raise subprocess.CalledProcessError(process.returncode, process.args, output if capture else None)
    return output if capture else ""


def foreground(*argv: str | Path) -> int:
    """Run a child that owns Ctrl-C, and return its exit status.

    A terminal's SIGINT reaches the whole foreground process group. The child
    stops its own work, sudo commands included, and says how to resume; this
    process only notes the signal and keeps waiting, so its locks outlive the
    child. subprocess.run would kill the child 0.25 s into that cleanup. A
    Python handler, unlike SIG_IGN, resets to the default in the exec'd child.
    """
    interrupted = []
    previous = signal.signal(signal.SIGINT, lambda number, frame: interrupted.append(number))
    try:
        code = subprocess.Popen([str(arg) for arg in argv], env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).wait()
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler if previous is None else previous)
    if interrupted:
        raise KeyboardInterrupt
    return code


def git(directory: Path, *args: str) -> str:
    return run("git", "-C", directory, *args, capture=True).strip()


def clean(directory: Path) -> str:
    try:
        head = git(directory, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        raise ValueError(f"git rev-parse HEAD failed: commit this repository before deployment: {directory}") from None
    if git(directory, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"commit or set aside local changes before upgrading: {directory}")
    return head


def changes(directory: Path) -> list[str]:
    tracked = run("git", "-C", directory, "diff", "--name-only", "--no-renames", "-z", "HEAD", capture=True)
    new = run("git", "-C", directory, "ls-files", "--others", "--exclude-standard", "-z", capture=True)
    return sorted(set(filter(None, (tracked + new).split("\0"))))


def snapshot(path: Path) -> tuple[bytes, int] | None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError(f"site upgrade paths must not contain symlinks: {path}")
    return (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None


def site_update(command: Path, site: Path, *, expected_head: str | None = None) -> None:
    """Validate before committing, restoring only our own edits on failure."""
    root = Path(git(site, "rev-parse", "--show-toplevel"))
    head = clean(root)
    if expected_head is not None and head != expected_head:
        raise ValueError("the site revision changed before release preparation")
    tracked = set(run("git", "-C", root, "ls-files", "-z", capture=True).split("\0"))
    saved = {(site / name).relative_to(root).as_posix(): snapshot(site / name) for name in SITE_FILES}
    produced = {}
    try:
        try:
            run(command, "init", "--upgrade", site)
        finally:
            # init can stop after replacing its descriptor. Keep that bounded
            # partial output in the same rollback transaction as a successful
            # preparation.
            produced = {name: snapshot(root / name) for name in saved}
        run(command, "check", "--config", site)
        changed = changes(root)
        if set(changed) - saved.keys():
            raise ValueError("other site files changed during preparation; review the checkout before retrying")
        if git(root, "rev-parse", "HEAD") != head:
            raise ValueError("the site revision changed during preparation; review the checkout before retrying")
        if any(snapshot(root / name) != candidate for name, candidate in produced.items()):
            raise ValueError("site upgrade files changed during validation; preserving those edits for review")
        if changed:
            paths = [f":(literal){name}" for name in changed]
            run("git", "-C", root, "add", "-A", "--", *paths)
            from oh_install import progress
            # Signing may ask for a passphrase at the terminal: no spinner
            # paints over it.
            with progress.paused():
                run("git", "-C", root, "commit", "--only", "-m", "Upgrade Thermite application", "--", *paths)
        clean(root)
    except BaseException:
        # Restore our own edits before a commit. A completed commit is
        # undone only by an install transaction that rolls back
        # (reset_pin), never here.
        if git(root, "rev-parse", "HEAD") == head:
            for name, previous in saved.items():
                path = root / name
                if name not in produced:
                    continue
                try:
                    if snapshot(path) != produced[name]:
                        print(f"Preserved a concurrent edit: {path}", file=sys.stderr)
                        continue
                except (OSError, ValueError):
                    continue
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    data, mode = previous
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    path.chmod(mode)
                if name in tracked:
                    run("git", "-C", root, "restore", "--source", head, "--staged", "--", f":(literal){name}")
                else:
                    run("git", "-C", root, "rm", "--cached", "--ignore-unmatch", "--", f":(literal){name}")
        raise


class PinMoved(ValueError):
    """The site is no longer at the pin commit a rollback would undo."""


class PinUnfinished(ValueError):
    """The site's branch moved back, but the files the pin commit changed
    could not follow; `command` puts them back, as rerunning reset_pin does."""

    def __init__(self, message: str, command: str):
        super().__init__(message)
        self.command = command


def _same(root: Path, revision: str, paths: list[str], *options: str) -> bool:
    """Whether the working tree (or, with --cached, the index) holds
    `paths` as `revision` has them."""
    code = subprocess.run(["git", "-C", str(root), "diff", "--quiet", *options, revision, "--", *paths],
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).returncode
    if code not in (0, 1):
        raise subprocess.CalledProcessError(code, ["git", "diff", "--quiet", *options, revision])
    return code == 0


def reset_pin(site: Path, *, expected: str, previous: str) -> bool:
    """Undo a pin commit: move HEAD from `expected` back to `previous`
    (compare and swap), then put the files that commit changed back as
    `previous` has them. A reset that stopped between the two, HEAD at one
    commit with exactly those files as the other has them, is finished.
    False when the site is already at `previous` with those files as it
    has them, whatever else is uncommitted beside them; PinMoved, changing
    nothing, when anyone else moved it or changed files since;
    PinUnfinished when HEAD moved but the files could not follow."""
    root = Path(git(site, "rev-parse", "--show-toplevel"))
    head = git(root, "rev-parse", "HEAD")
    if head not in (expected, previous):
        raise PinMoved(f"the site is at {head[:12]}, not the pin commit {expected[:12]}")
    names = [name for name in run("git", "-C", root, "diff", "--name-only", "--no-renames", "-z", previous,
                                  expected, capture=True).split("\0") if name]
    paths = [f":(literal){name}" for name in names]
    if head == previous and (not paths or _same(root, previous, paths) and _same(root, previous, paths, "--cached")):
        return False
    files = head
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        changed = {name for options in ((), ("--cached",)) for name in run(
            "git", "-C", root, "diff", "--name-only", "--no-renames", "-z", *options, "HEAD", capture=True).split("\0")
            if name}
        files = expected if head == previous else previous
        if not changed <= set(names) or not (_same(root, files, paths) and _same(root, files, paths, "--cached")):
            raise PinMoved("the site has uncommitted changes")
    if head == expected:
        run("git", "-C", root, "update-ref", "-m", "thermite: undo the application pin commit", "HEAD", previous,
            expected)
    if files == expected and paths:
        try:
            run("git", "-C", root, "restore", "--source", previous, "--staged", "--worktree", "--", *paths)
        except subprocess.CalledProcessError as exc:
            command = shlex.join(["git", "-C", str(root), "restore", "--source", previous, "--staged", "--worktree",
                                  "--", *names])
            raise PinUnfinished(f"its files could not be put back (git restore exited {exc.returncode})",
                                command) from None
    return True


@contextlib.contextmanager
def locked(directory: Path):
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(directory / ".upgrade.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another Thermite application operation is running") from None
        yield


def main(argv: list[str] | None = None) -> int:
    return subprocess.call([sys.executable, str(SOURCE / "bin/thermite"), "upgrade",
                            *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
