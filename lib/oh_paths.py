"""One read-only program tree and one explicit mutable private site.

Full source checkouts retain their original defaults. Installed releases never
treat site-packages as a writable site, even though a fictional example is
included there for documentation and init.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

PROGRAM = Path(__file__).resolve().parents[1]
SITE_FILE = "thermite-site.json"
RELEASE_FILE = "thermite-release.json"
RELEASES = "var/application-releases"
#: Set to the release digest this process re-executed into; stops a loop.
REEXEC = "THERMITE_PINNED_RELEASE"
#: The launcher's own arguments, when it handed select_args all of sys.argv.
LAUNCHED: list[str] | None = None


class SiteError(ValueError):
    pass


def select_args(argv: list[str]) -> list[str]:
    """Select the site before importing any verb or its module-level paths."""
    global SITE, LAUNCHED
    if list(argv) == sys.argv[1:] and Path(sys.argv[0]).name == "thermite":
        LAUNCHED = list(argv)
    out, chosen, index = [], None, 0
    while index < len(argv):
        word = argv[index]
        if word == "--":
            out.extend(argv[index:])
            break
        # Access daemons already use --config FILE for their compiled service
        # descriptor. Preserve that option; --site still selects their site.
        leading = [value for value in out if value not in {"-q", "--quiet", "-v", "--verbose", "--no-color"}]
        service_config = (len(leading) >= 2 and leading[0] == "access"
                          and leading[1] in {"bootstrap", "setup", "migrate", "serve", "worker", "verify", "callbacks"})
        if service_config and (word == "--config" or word.startswith("--config=")):
            out.append(word)
            index += 1
            if word == "--config" and index < len(argv):
                out.append(argv[index])
                index += 1
            continue
        if word in {"--config", "--site"}:
            index += 1
            if index == len(argv) or argv[index].startswith("-"):
                raise SiteError(f"{word} needs a directory")
            value = argv[index]
        elif word.startswith(("--config=", "--site=")):
            value = word.split("=", 1)[1]
        else:
            out.append(word); index += 1; continue
        if chosen is not None or not value: raise SiteError("pass --config (or --site) exactly once")
        chosen = str(Path(value).expanduser().absolute())
        index += 1
    if chosen is not None: os.environ["THERMITE_SITE"] = chosen
    SITE = site_root()
    return out


def site_root() -> Path:
    explicit = os.environ.get("THERMITE_SITE")
    if explicit: return Path(explicit).expanduser().absolute()
    if not (PROGRAM / RELEASE_FILE).is_file(): return PROGRAM
    current = Path.cwd().absolute()
    for candidate in (current, *current.parents):
        if (candidate / SITE_FILE).is_file(): return candidate
    return PROGRAM


SITE = site_root()


def descriptor(root: Path | None = None) -> dict | None:
    root = SITE if root is None else Path(root)
    path = root / SITE_FILE
    if not path.exists(): return None
    if path.is_symlink() or not path.is_file(): raise SiteError("site manifest must be a regular file")
    try: data = json.loads(path.read_text())
    except (OSError, ValueError) as exc: raise SiteError("invalid site manifest") from exc
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] not in {1, 2} or set(data) != {"version", "application", "release"}):
        raise SiteError("unknown site manifest; expected version, application and release")
    if not isinstance(data["application"], str) or not isinstance(data["release"], str):
        raise SiteError("invalid application requirement")
    return data


def require_compatible() -> dict | None:
    from oh_release import VERSION, manifest
    data = descriptor()
    installed = (PROGRAM / RELEASE_FILE).is_file()
    if data is None:
        if installed:
            raise SiteError("select a private site with --config DIR, or run thermite init DIR first")
        return None  # Original complete-checkout behavior.
    current = manifest(PROGRAM)["digest"] if installed else None
    if data["application"] != VERSION or current != data["release"]:
        pinned(data, current)
    os.environ.pop(REEXEC, None)
    return data


def prepared(digest: str) -> Path | None:
    """The owner's prepared environment launcher for a release, if one is recorded."""
    try:
        store = Path.home() / ".local/share/thermite/application"
    except RuntimeError:
        return None
    for record in sorted(store.glob("*.json")) if store.is_dir() else ():
        try:
            saved = json.loads(record.read_text())
            name, release = saved["environment"], saved["release"]["digest"]
        except (OSError, ValueError, TypeError, KeyError):
            continue
        launcher = store / str(name) / "bin/thermite"
        if release == digest and Path(str(name)).name == name and launcher.is_file():
            return launcher
    return None


def pinned(data: dict, current: str | None) -> None:
    """Run the site's verified pinned release in place of this one, or say how to resume.

    A failed host phase leaves the site pinned to its candidate while the owner
    command still selects the previous application. Every site verb then runs
    the exact cached candidate rather than refusing.
    """
    import oh_release
    digest, store = data["release"], SITE / RELEASES
    try:
        root = oh_release.resolve(store, digest, data["application"], program=store / str(digest))
        problem = ""
    except (oh_release.ReleaseError, OSError) as exc:
        root, problem = None, f" Its verified copy {store / str(digest)} is unusable: {exc}."
    # Only the owner's own launcher re-executes, never root: the cache is the
    # owner's private, writable site data.
    if (root is not None and current is not None and LAUNCHED is not None and sys.executable
            and os.environ.get(REEXEC) != digest and os.geteuid() != 0 and root.stat().st_uid == os.getuid()):
        oh_cli = sys.modules.get("oh_cli")
        if not getattr(getattr(oh_cli, "switches", None), "quiet", False):
            print(f"thermite: {SITE} pins application release {digest[:12]}; running its verified copy {root}",
                  file=sys.stderr)
        sys.stdout.flush(); sys.stderr.flush()
        os.environ[REEXEC] = digest
        os.execv(sys.executable, [sys.executable, str(root / "bin/thermite"), *LAUNCHED])
    import shlex
    running = f"release {current[:12]}" if current else "a source checkout"
    launcher = prepared(str(digest))
    raise SiteError(f"site {SITE} requires thermite {data['application']} release {str(digest)[:12]}; this command "
                    f"runs {running}." + (f" Its verified copy is {root}." if root is not None else problem)
                    + (f" Its prepared launcher is {launcher}." if launcher else "")
                    + " To finish an interrupted application update, rerun its printed Resume command, usually "
                    f"`thermite upgrade --config {shlex.quote(str(SITE))}` (or the matching thermite install).")


def command(*args: str) -> tuple[str, ...]:
    """A subprocess uses the same program and site, including inside sudo plans."""
    import sys
    return (sys.executable, str(PROGRAM / "bin/thermite"), "--site", str(SITE), *args)
