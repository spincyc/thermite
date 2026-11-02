"""One read-only program tree and one explicit mutable private site.

Full source checkouts retain their original defaults. Installed releases never
treat site-packages as a writable site, even though a fictional example is
included there for documentation and init.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROGRAM = Path(__file__).resolve().parents[1]
SITE_FILE = "thermite-site.json"
RELEASE_FILE = "thermite-release.json"


class SiteError(ValueError):
    pass


def select_args(argv: list[str]) -> list[str]:
    """Select the site before importing any verb or its module-level paths."""
    global SITE
    out, chosen, index = [], None, 0
    while index < len(argv):
        word = argv[index]
        if word == "--site":
            index += 1
            if index == len(argv): raise SiteError("--site needs a directory")
            value = argv[index]
        elif word.startswith("--site="):
            value = word.split("=", 1)[1]
        else:
            out.append(word); index += 1; continue
        if chosen is not None or not value: raise SiteError("pass --site exactly once")
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
    if not isinstance(data, dict) or data.get("version") != 1 or set(data) != {"version", "application", "release"}:
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
            raise SiteError("select a private site with --site DIR, or run thermite init DIR first")
        return None  # Original complete-checkout behavior.
    if data["application"] != VERSION:
        raise SiteError(f"site requires thermite {data['application']}; installed application is {VERSION}")
    if not installed:
        raise SiteError("this standalone site requires its installed application release; use the matching wheel")
    release = manifest(PROGRAM)
    if release["digest"] != data["release"]:
        raise SiteError("site requires a different application release; install its exact reviewed artifact")
    return data


def command(*args: str) -> tuple[str, ...]:
    """A subprocess uses the same program and site, including inside sudo plans."""
    import sys
    return (sys.executable, str(PROGRAM / "bin/thermite"), "--site", str(SITE), *args)
