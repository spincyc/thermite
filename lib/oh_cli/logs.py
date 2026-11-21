"""Every Thermite service's log, merged by time.

    thermite logs [COMPONENT ...] [--since WHEN] [--until WHEN] [--lines N] [-f] [--json]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import sys
from pathlib import Path

import oh_cli
import oh_logs
from oh_cli import AREA

#: oh_host.DEFAULT_ROOT's runtime/ (oh_deploy.Installation), where no
#: instance/host.yaml says otherwise.
DEFAULT_STATE = Path("/srv/thermite/runtime")

DESCRIPTION = """\
Print, read-only, the logs of this host's Thermite services as one list,
oldest first and merged by time: the runtime's openhab.log (with its
rotations, compressed or not) below the runtime state's logs/, and every
other service's journal (journalctl -u UNIT). The runtime also copies
openhab.log to the journal; while the file can be read those copies are
left out, and the unit's other journal lines (starts, stops, crash
output) kept.

Name components to read only those (default: all but events):
  runtime           openHAB: openhab.log, and its unit's journal
  events            openHAB's events.log: every Item's changes (only when named)
  gateway, worker, callbacks
                    the HTTPS gateway, its room policy worker, its callback relay
  atv, music        the Apple TV helper, Music Assistant
  prometheus, grafana, blackbox, node-exporter, metrics-exporter
                    the monitoring services
  boundary, share-gate
                    the native listener boundary, the network share gates

Without --since, --until or --lines it prints the last 200 records. WHEN
is now, today, yesterday; 90s, 30m, 12h, 7d or 2w ago; a date
(2026-11-21) or a date and time (2026-11-21 14:00, 2026-11-21T14:00:00),
with an offset (+01:00) or Z, else in this host's local time. --lines N
keeps the last N of the records in range. -f prints the last 10 (or
--lines) and then each new record until Ctrl-C.

Each line is the time (ISO 8601, with this host's offset, or Z with
--utc), the component, the level and the logger; a stack trace's lines
follow indented. --json prints one JSON object per line instead
(component, level, logger, message, source, time). Secrets (passwords,
tokens, keys, cookies, Authorization headers, a URL's password) print as
***. Addresses, host names and device names stay, because they are what
a log is read for: review the output before sharing it.

Reading openhab.log takes membership of the thermite group, and other
services' journals that of systemd-journal (or wheel or adm, where the
distribution grants it), or sudo. A source it cannot read is named on
stderr, and the rest still print. With --sandbox it reads the sandbox's
openhab.log alone: the sandbox has no other service.
"""

EXAMPLES = (
    ("thermite logs", "the last 200 records of every service"),
    ("thermite logs --since 1h", "everything of the last hour"),
    ('thermite logs --since "2026-11-21 14:00" --until "2026-11-21 15:00"', "one hour, in local time"),
    ("thermite logs runtime gateway -f", "follow the runtime and the gateway"),
    ("thermite logs --level warn --since today", "today's warnings and errors"),
    ("thermite logs --grep avpro --since 2h", "the records that mention avpro, in any case"),
    ("thermite logs --json --since 24h > logs.jsonl", "a day of records as JSON Lines, to share"),
    ("thermite logs --sandbox -f", "follow the sandbox's openhab.log"),
    ("thermite logs --url ssh://thermite --since 1h", "the last hour, read on the server named thermite"),
)

NOTES = """\
exit status:
  0  done: it read every source (none in range is a note on stderr)
  1  done, and a source could not be read: openhab.log without the
     thermite group, the journal without systemd-journal; the rest printed
  2  a wrong switch, an unknown component or time, or no source at all
     could be read
"""

LEVEL_COLORS = {"error": "\033[31m", "warn": "\033[33m"}


def state_dir() -> Path:
    """The runtime state: instance/host.yaml's root's runtime/, else DEFAULT_STATE."""
    try:
        import oh_deploy as deploy
        return deploy.state(deploy.load(AREA)[1]).root
    except Exception:  # no site, no host file, no PyYAML: an installed host's default
        return DEFAULT_STATE


def logs_of(state: Path) -> Path:
    return state / "logs"


def sandbox_logs() -> Path:
    try:
        import oh_sandbox
        return oh_sandbox.sandbox_instance().openhab_log.parent
    except ImportError:
        return AREA / "var/sandbox/userdata/logs"


def _paint(level: str, text: str) -> str:
    color = LEVEL_COLORS.get(level)
    return f"{color}{text}\033[0m" if color else text


def _moment(parser: argparse.ArgumentParser, switch: str, value: str | None):
    if value is None:
        return None
    try:
        return oh_logs.parse_when(value)
    except ValueError as exc:
        parser.error(f"{switch}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("logs", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("components", nargs="*", metavar="COMPONENT",
                        help="read only these: " + ", ".join(oh_logs.COMPONENTS) + " (default: all but events)")
    oh_cli.add_target(parser, "production", "sandbox", "url", default="production",
                      meaning={"production": "this host's services", "sandbox": "the sandbox's openhab.log",
                               "url": "run it on the server ssh://[USER@]HOST[:PORT][/SITE] names"})
    parser.add_argument("--since", metavar="WHEN", help="records from WHEN on: 1h, today, 2026-11-21 14:00 ...")
    parser.add_argument("--until", metavar="WHEN", help="records up to WHEN")
    parser.add_argument("--lines", type=int, metavar="N",
                        help="the last N matching records (default: 200, or every one in range; 10 with -f)")
    parser.add_argument("-f", "--follow", action="store_true", help="then print each new record until Ctrl-C")
    parser.add_argument("--grep", metavar="PATTERN",
                        help="records whose logger or message match this regular expression; case-insensitive "
                             "unless it has an upper-case letter")
    parser.add_argument("--level", choices=oh_logs.LEVELS, metavar="LEVEL",
                        help="records of LEVEL or worse: " + ", ".join(oh_logs.LEVELS) + " (default: all)")
    parser.add_argument("--utc", action="store_true", help="print times in UTC (Z) instead of local time")
    oh_cli.add_json(parser)
    parser.add_argument("--no-redact", dest="redact", action="store_false",
                        help="print secrets as the log has them (default: as ***)")
    parser.add_argument("--state-dir", type=Path, metavar="DIR",
                        help="the runtime state, whose logs/ holds openhab.log (default: runtime/ in "
                             "instance/host.yaml's root, /srv/thermite/runtime)")
    arguments = parser.parse_args(argv)
    if arguments.url:
        return oh_cli.refuse_url("logs", arguments.url)

    unknown = [name for name in arguments.components if name not in oh_logs.COMPONENTS]
    if unknown:
        parser.error(f"no component {', '.join(map(repr, unknown))}; the components: "
                     + ", ".join(oh_logs.COMPONENTS))
    since = _moment(parser, "--since", arguments.since)
    until = _moment(parser, "--until", arguments.until)
    if since is not None and until is not None and since > until:
        parser.error("--since is after --until")
    if arguments.follow and until is not None:
        parser.error("-f follows new records: it takes no --until")
    if arguments.lines is not None and arguments.lines < (0 if arguments.follow else 1):
        parser.error("--lines takes 1 or more (0 only with -f)")
    try:
        grep = oh_logs.smart_pattern(arguments.grep) if arguments.grep is not None else None
    except re.error as exc:
        parser.error(f"--grep: {exc}")
    lines = arguments.lines
    if lines is None and (arguments.follow or (since is None and until is None)):
        lines = 10 if arguments.follow and since is None else None if arguments.follow else 200
    components = tuple(dict.fromkeys(arguments.components)) or oh_logs.DEFAULT_COMPONENTS

    say = _say
    if arguments.sandbox:
        others = [name for name in arguments.components if name not in oh_logs.FILES]
        if others:
            say(f"the sandbox has only the runtime's log: no {', '.join(others)}")
        components = tuple(name for name in components if name in oh_logs.FILES)
        if not components:
            return 2
        logs, journal = sandbox_logs(), False
    else:
        logs, journal = logs_of(arguments.state_dir or state_dir()), True

    query = oh_logs.Query(components, since, until, lines, arguments.level, grep, arguments.redact)
    result = oh_logs.read(query, logs, journal)
    for note in result.notes:
        say(note)
    if result.read == 0:
        say("no log could be read")
        return 2
    width = max((len(name) for name in components), default=0)
    if not arguments.follow and result.records:
        width = max(len(record.component) for record in result.records)
    paint = _paint if not arguments.json and oh_cli.color() and sys.stdout.isatty() else None

    def show(record: oh_logs.Record) -> None:
        if arguments.json:
            oh_cli.emit(record.report(arguments.utc))
        else:
            print("\n".join(record.lines(width, arguments.utc, paint)))

    try:
        for record in result.records:
            show(record)
        sys.stdout.flush()
        if not result.records and not arguments.follow and not oh_cli.switches.quiet:
            say("no log records in that range" if since or until else "no log records")
        if arguments.follow:
            def shown(record: oh_logs.Record) -> None:
                show(record)
                sys.stdout.flush()
            try:
                oh_logs.follow(query, logs, journal, result, shown)
            except KeyboardInterrupt:
                return 1 if result.problem else 0
    except BrokenPipeError:
        # The reader left (| head): no error at exit for the unflushed rest.
        with contextlib.suppress(OSError, ValueError, io.UnsupportedOperation):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1 if result.problem else 0
    return 1 if result.problem else 0


def _say(message: str) -> None:
    print(f"{oh_cli.PROGRAM} logs: {message}", file=sys.stderr)
