"""The install transaction's own writes below the installation root, as the
owner with the thermite group.

    python lib/oh_install/writes.py --config SITE deploy COMMIT install|upgrade|rollback
    python lib/oh_install/writes.py gateway STATE EVENT
    python lib/oh_install/writes.py rolled-back STATE EVENT

The runtime state is the service account's, and the owner writes its shared
part (conf, the helpers' code, the deploy's records and history) through
the thermite group. The install that grants the group runs in a process
that does not have it yet, so the transaction runs each of these writes as
a command of this file: directly, or through sg or sudo -u when its own
process lacks the group (oh_install.steps.operator_command).

This is not a verb. Nothing here asks a question or runs sudo: the
transaction's Deploy step checks the pairing-log protection before `deploy`
and hands what it wrote to the service account after it, both as root
commands of the approved run; the transaction's record and the deploy
history say what happened.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if __name__ == "__main__":
    # Run by path. As bin/thermite does: no bytecode read or written beside
    # the release, lib/ in place of this directory (whose secrets.py would
    # stand in for the standard library's), and the site selected before
    # any module computes its paths.
    import os
    _here = Path(__file__).resolve().parent
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(_here / (".uncached-" + os.urandom(16).hex()))
    sys.path[:] = [str(_here.parent), *(entry for entry in sys.path if Path(entry).resolve() != _here)]
    import oh_paths
    try:
        _ARGUMENTS = oh_paths.select_args(sys.argv[1:])
    except oh_paths.SiteError as exc:
        sys.stderr.write(f"thermite: {exc}\n")
        raise SystemExit(2)

import oh_deploy as deploy  # noqa: E402
from oh_cli import AREA  # noqa: E402

#: The transaction names the commit whole, as it resolved it.
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
HOW = ("install", "upgrade", "rollback")


def print_changes(label: str, changes: deploy.Changes) -> None:
    print(f"{label:<8}{changes.summary()}")
    for kind in ("added", "changed", "removed"):
        for path in getattr(changes, kind):
            print(f"          {kind:<8}{path}")


def deploy_commit(commit: str, how: str) -> int:
    """Install `commit`'s conf/, helper code and metrics/ into the state its
    own host facts name, and record it (oh_deploy.deploy). What it prints
    shows in a verbose run, or when the step fails."""
    import oh_paths
    oh_paths.require_compatible()
    commit, host, _ = deploy.selection(AREA, commit)
    layout = deploy.state(host)
    result = deploy.deploy(AREA, layout, AREA / "var", commit, how=how, host=host)
    print(f"deployed {result.commit[:12]} {result.subject}")
    print_changes("conf", result.conf)
    print_changes("helper", result.atv_app)
    if result.metrics_kept:
        print(f"metrics kept as deployed: {result.commit[:12]} predates the installed metrics, so "
              f"{layout.metrics} and {layout.exporter_app} keep what the last deploy installed")
    else:
        print_changes("metrics", result.metrics)
        print_changes("exporter", result.exporter_app)
        for line in result.reloads:
            print(f"metrics {line}")
    return 0


def record(state: str, event: str, write, what: str) -> int:
    """Append one event line to the deploy history of the runtime state at
    `state`: the gateway release a transaction activated
    (oh_install.steps.gateway_history), or a transaction that rolled back
    (oh_install.changeover.rolled_back)."""
    layout = deploy.State(Path(state))
    try:
        value = json.loads(event)
        if not isinstance(value, dict):
            raise ValueError("not a JSON object")
        write(layout, value)
    except (ValueError, OSError, deploy.DeployError) as exc:
        print(f"thermite: could not record the {what} in {layout.history}: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "deploy" and COMMIT.fullmatch(argv[1]) and argv[2] in HOW:
            return deploy_commit(argv[1], argv[2])
        if len(argv) == 3 and argv[0] == "gateway":
            return record(argv[1], argv[2], deploy.record_gateway, "gateway release")
        if len(argv) == 3 and argv[0] == "rolled-back":
            return record(argv[1], argv[2], deploy.record_rollback, "rollback")
    except (deploy.DeployError, ValueError, OSError) as exc:
        print(f"thermite: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    print("thermite: the install transaction runs this file; see its first lines", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(_ARGUMENTS))
