"""Source alias and bounded Git transaction for the unified application update."""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys


SOURCE = Path(__file__).resolve().parents[1]
SITE_FILES = ("thermite-site.json", ".gitignore", "bin/thermite", "Makefile")


def run(*argv: str | Path, capture: bool = False) -> str:
    result = subprocess.run([str(arg) for arg in argv], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return result.stdout if capture else ""


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
            # init can stop after replacing its descriptor (for example while
            # removing a legacy wrapper). Keep that bounded partial output in
            # the same rollback transaction as a successful preparation.
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
            run("git", "-C", root, "commit", "--only", "-m", "Upgrade Thermite application", "--", *paths)
        clean(root)
    except BaseException:
        # A completed commit is a recovery point, never history to undo.
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


def upgrade(site: Path, **kwargs) -> int:
    """Compatibility entry point; there is only one acquisition workflow."""
    from oh_application_update import upgrade as apply
    return apply(site, **kwargs)


def main(argv: list[str] | None = None) -> int:
    return subprocess.call([sys.executable, str(SOURCE / "bin/thermite"), "upgrade",
                            *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
