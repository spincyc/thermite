"""Read every Thermite service's log, merge the records by time, and filter
and redact them (thermite logs).

The runtime (openHAB) writes openhab.log and events.log, with their
rotations, under the runtime state's logs/ (oh_logging, oh_service.LOGS),
and copies openhab.log's records to the journal as well. Every other
service logs to the journal alone: the containers through podman's
journald driver, the gateway everything on stderr. Standard library only:
oh_cli.logs imports this at its module level.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import BinaryIO
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import oh_secrets
import oh_trace
import oh_units

#: The journal reader. A name, found on PATH when it runs, so that a fake
#: put first on PATH (or this, patched) stands in for it.
JOURNALCTL = "journalctl"
#: The host's zone for times the files carry without one; None is the
#: system's own (and its daylight saving rules). Tests set a fixed one.
ZONE: tzinfo | None = None


def now() -> datetime:
    """The time now, with the local offset; tests replace it."""
    return datetime.now().astimezone()


#: The boundary unit has no oh_units name: oh_deploy writes it inline.
BOUNDARY = "thermite-native-boundary.service"
#: component: the systemd unit whose journal it is (None: a file only).
COMPONENTS: dict[str, str | None] = {
    "runtime": oh_units.RUNTIME,
    "events": None,
    "gateway": oh_units.ACCESS,
    "worker": oh_units.ACCESS_WORKER,
    "callbacks": oh_units.ACCESS_CALLBACKS,
    "atv": oh_units.ATV,
    "music": oh_units.MUSIC,
    "prometheus": oh_units.PROMETHEUS,
    "grafana": oh_units.GRAFANA,
    "blackbox": oh_units.BLACKBOX,
    "node-exporter": oh_units.NODE_EXPORTER,
    "metrics-exporter": oh_units.EXPORTER,
    "boundary": BOUNDARY,
    "share-gate": oh_units.SHARE_GATE.replace("@.", "@*."),
}
#: events.log is every Item's state changes: read only when named.
DEFAULT_COMPONENTS = tuple(name for name in COMPONENTS if name != "events")
_SHARE_GATE_PREFIX = oh_units.SHARE_GATE.split("@", 1)[0] + "@"
_UNIT_COMPONENT = {unit: name for name, unit in COMPONENTS.items() if unit and "*" not in unit}
_CONTAINER_COMPONENT = {container: _UNIT_COMPONENT[unit]
                        for unit, container in {**oh_units.CONTAINERS, **oh_units.ACCESS_CONTAINERS}.items()
                        if unit in _UNIT_COMPONENT}
#: The files, below the logs directory, and the component each belongs to.
FILES = {"runtime": "openhab.log", "events": "events.log"}
JOURNAL = "journal"

#: Most severe first; --level LEVEL keeps it and those before it.
LEVELS = ("error", "warn", "info", "debug", "trace")
_LEVEL_WORDS = {
    "trace": "trace", "debug": "debug", "dbg": "debug", "info": "info", "information": "info", "notice": "info",
    "warn": "warn", "warning": "warn", "error": "error", "err": "error", "eror": "error", "crit": "error",
    "critical": "error", "fatal": "error", "severe": "error", "panic": "error", "alert": "error", "emerg": "error",
}
#: journald's PRIORITY (syslog's severities) as a level.
_PRIORITY = {"0": "error", "1": "error", "2": "error", "3": "error", "4": "warn", "5": "info", "6": "info",
             "7": "debug"}

#: An openhab.log or events.log record's first line (as oh_sandbox.LOG_LINE).
LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d{3})\s+\[\s*(?P<level>[A-Z]+)\s*\]\s+"
    r"\[(?P<logger>[^\]]*)\]\s+-\s?(?P<msg>.*)$")
#: The same record in the journal: oh_logging.JOURNAL_PATTERN, with no time.
JOURNAL_LINE = re.compile(r"^\[(?P<level>[A-Z]+)\s*\]\s+\[(?P<logger>[^\]]*)\]\s+-\s?(?P<msg>.*)$", re.DOTALL)
#: Python's logging, as the gateway and its workers write it: `INFO name: message`.
PYTHON_LINE = re.compile(
    r"^(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)[ :](?P<logger>[A-Za-z_][\w.-]*):\s?(?P<msg>.*)$",
    re.DOTALL)
#: Python's logging with a time and a thread, as Music Assistant writes it.
STAMPED_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T][\d:.,]+ (?P<level>[A-Z]+) \([^)]*\) \[(?P<logger>[^\]]+)\] (?P<msg>.*)$", re.DOTALL)
_LOGFMT_LEVEL = re.compile(r"(?:^|\s)(?:level|lvl)=\"?(?P<level>[A-Za-z]+)")
_LOGFMT_LOGGER = re.compile(r"(?:^|\s)logger=\"?(?P<logger>[^\s\"]+)")
_LEVEL_WORD = re.compile(
    r"(?:^|[\s\[(|])(?P<level>TRACE|DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|ERR|CRITICAL|CRIT|FATAL|SEVERE|PANIC)"
    r"(?=[\s\]):|]|$)")
#: A line that goes on the record before it: a stack trace's.
_CONTINUATION = re.compile(r"^(?:\s|Caused by: |\.\.\. \d+ more)")
#: How close a journal line must follow a record to be part of it.
_CONTINUATION_GAP = timedelta(seconds=2)
#: A line of a private key block: base64, an encrypted key's header, or its END.
_KEY_LINE = re.compile(r"[A-Za-z0-9+/=]+|(?:Proc-Type|DEK-Info): [^\n]*|-----END [A-Z ]*PRIVATE KEY-----")

WHEN_FORMS = ("now, today, yesterday; 90s, 30m, 12h, 7d or 2w ago; a date (2026-11-21) or a date and time "
              "(2026-11-21 14:00, 2026-11-21T14:00:00), with an offset (+01:00) or Z, else in local time")
_RELATIVE = re.compile(r"^(\d+)\s*(s|m|h|d|w)$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?)?\s*(?:Z|[+-]\d{2}:?\d{2})?$",
                  re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

#: journalctl's hints, on stderr, when it shows only the user's own journal.
_JOURNAL_DENIED = ("insufficient permissions", "not seeing messages from other users", "No journal files were opened")
JOURNAL_ACCESS = ("the journal shows only this user's own messages: add the user to the systemd-journal group "
                  "(sudo usermod -aG systemd-journal $USER, then log in again) or run thermite logs with sudo")
GROUP_ACCESS = "permission denied; join the thermite group (sudo usermod -aG thermite $USER, then log in again)"


def local(naive: datetime) -> datetime:
    """A time the host's clock shows, with its offset."""
    return naive.replace(tzinfo=ZONE) if ZONE is not None else naive.astimezone()


def in_zone(moment: datetime, utc: bool = False) -> datetime:
    if utc:
        return moment.astimezone(timezone.utc)
    return moment.astimezone(ZONE) if ZONE is not None else moment.astimezone()


def parse_when(text: str, at: datetime | None = None) -> datetime:
    """--since and --until: a moment, with its offset; ValueError names WHEN_FORMS."""
    word = text.strip().lower()
    at = at or now()
    today = local(datetime.combine(in_zone(at).date(), datetime.min.time()))
    if word == "now":
        return at
    if word == "today":
        return today
    if word == "yesterday":
        return local(datetime.combine(in_zone(at).date() - timedelta(days=1), datetime.min.time()))
    relative = _RELATIVE.match(word)
    if relative:
        return at - timedelta(**{_UNITS[relative[2]]: int(relative[1])})
    if _ISO.match(text.strip()):
        try:
            moment = datetime.fromisoformat(text.strip().replace(",", ".").replace(" Z", "Z"))
        except ValueError:
            moment = None
        if moment is not None:
            return moment if moment.tzinfo else local(moment)
    raise ValueError(f"{text!r} is not a time: give {WHEN_FORMS}")


def stamp(moment: datetime, utc: bool = False) -> str:
    """ISO 8601 to the millisecond, with the local offset (or Z)."""
    text = in_zone(moment, utc).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z") if utc else text


def level_of(word: str | None) -> str | None:
    return _LEVEL_WORDS.get(word.lower()) if word else None


@dataclasses.dataclass
class Record:
    time: datetime
    component: str
    level: str
    logger: str | None
    message: str
    source: str

    def report(self, utc: bool = False) -> dict:
        """--json's object."""
        return {"component": self.component, "level": self.level, "logger": self.logger,
                "message": self.message, "source": self.source, "time": stamp(self.time, utc)}

    def lines(self, width: int, utc: bool = False, paint: Callable[[str, str], str] | None = None) -> list[str]:
        """The text output: one line, then its continuations indented."""
        first, *rest = self.message.split("\n")
        level = f"{self.level.upper():<5}"
        if paint:
            level = paint(self.level, level)
        logger = f"[{self.logger}] " if self.logger else ""
        return [f"{stamp(self.time, utc)} {self.component:<{width}} {level} {logger}{first}".rstrip(),
                *("    " + line.rstrip() for line in rest)]


def smart_pattern(pattern: str) -> re.Pattern:
    """--grep: case-insensitive unless the pattern has an upper-case letter
    of its own (an escape such as \\S or \\W is not one); re.error if not a regex."""
    upper = re.search(r"(?<!\\)[A-Z]", re.sub(r"\\.", "", pattern)) is not None
    return re.compile(pattern, 0 if upper else re.IGNORECASE)


@dataclasses.dataclass
class Query:
    components: tuple[str, ...] = DEFAULT_COMPONENTS
    since: datetime | None = None
    until: datetime | None = None
    #: The last this many matching records; None: every one in range.
    lines: int | None = 200
    level: str | None = None
    grep: re.Pattern | None = None
    redact: bool = True

    def within(self, moment: datetime) -> bool:
        return (self.since is None or moment >= self.since) and (self.until is None or moment <= self.until)

    def prepare(self, record: Record) -> Record | None:
        """The record as printed (redacted), or None when a filter drops it."""
        if self.redact:
            record.message = oh_secrets.redact(record.message)
        if self.level and LEVELS.index(record.level) > LEVELS.index(self.level):
            return None
        if self.grep and not self.grep.search(f"{record.logger or ''} {record.message}"):
            return None
        return record


@dataclasses.dataclass
class Result:
    records: list[Record] = dataclasses.field(default_factory=list)
    #: What to say on stderr, each once.
    notes: list[str] = dataclasses.field(default_factory=list)
    #: A source that should be readable was not (exit 1).
    problem: bool = False
    #: Sources read at all; none is exit 2.
    read: int = 0
    tried: int = 0
    #: The journal's last cursor, where following it begins.
    cursor: str | None = None
    #: path: (inode, size) of each current file when it was read.
    positions: dict[Path, tuple[int, int]] = dataclasses.field(default_factory=dict)
    #: Whether openhab.log could be read, so its journal copies are dropped.
    runtime_file: bool = False


# --------------------------------------------------------------------------
# the files


def _family(directory: Path, name: str) -> list[Path]:
    """name and its rotations below directory, newest first: name, name.1
    (or name.1.gz), name.2... An uncompressed rotation is preferred to its
    .gz while log4j is still writing that."""
    found: dict[int, Path] = {}
    for entry in os.listdir(directory):
        match = re.fullmatch(re.escape(name) + r"\.(\d+)(\.gz)?", entry)
        if match and (int(match[1]) not in found or not match[2]):
            found[int(match[1])] = directory / entry
    current = [directory / name] if (directory / name).exists() else []
    return current + [found[number] for number in sorted(found)]


def _open(path: Path):
    return gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb")


def _reverse_lines(path: Path, end: int | None = None, block: int = 1 << 16) -> Iterator[str]:
    """path's lines, last first; a plain file is read from its end (or the
    byte offset end) back, a block at a time, so the last records cost
    little however long it is."""
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as fh:
            lines = fh.read().split(b"\n")
        if lines and not lines[-1]:
            lines.pop()
        for raw in reversed(lines):
            yield raw.decode("utf-8", errors="replace").rstrip("\r")
        return
    with open(path, "rb") as fh:
        position = fh.seek(0, os.SEEK_END) if end is None else end
        rest = b""
        first = True
        while position > 0:
            step = min(block, position)
            position -= step
            fh.seek(position)
            pieces = (fh.read(step) + rest).split(b"\n")
            rest = pieces.pop(0)
            for raw in reversed(pieces):
                if first and not raw:
                    first = False
                    continue
                first = False
                yield raw.decode("utf-8", errors="replace").rstrip("\r")
        if rest or not first:
            yield rest.decode("utf-8", errors="replace").rstrip("\r")


def _file_record(match: re.Match, component: str, source: str, continuation: Iterable[str] = ()) -> Record | None:
    try:
        moment = local(datetime.fromisoformat(match["ts"].replace(",", ".")))
    except ValueError:
        return None
    message = "\n".join([match["msg"], *continuation])
    return Record(moment, component, level_of(match["level"]) or "info", match["logger"].strip() or None,
                  message, source)


def records_backwards(path: Path, component: str, source: str, end: int | None = None) -> Iterator[Record]:
    """path's records, newest first, each with its continuation lines.
    Lines before the file's first record (the end of one in an older
    rotation) are left out."""
    continuation: list[str] = []
    for line in _reverse_lines(path, end):
        match = LOG_LINE.match(line)
        record = _file_record(match, component, source, reversed(continuation)) if match else None
        if record is None:
            continuation.append(line)
            continue
        continuation = []
        yield record


def _first_time(path: Path) -> datetime | None:
    """When path's first record was written."""
    with _open(path) as fh:
        for _ in range(200):
            raw = fh.readline()
            if not raw:
                return None
            match = LOG_LINE.match(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            record = _file_record(match, "", "") if match else None
            if record:
                return record.time
    return None


def read_file(query: Query, directory: Path, component: str, result: Result,
              end: int | None = None) -> list[Record]:
    """component's file and its rotations: the matching records, newest
    first. Rotations are read newest first, and reading stops at the first
    record before --since, or once --lines are found."""
    name = FILES[component]
    path = directory / name
    result.tried += 1
    try:
        files = _family(directory, name)
        if files and files[0] == path:
            with open(path, "rb"):
                pass
    except PermissionError:
        result.notes.append(f"{path}: {GROUP_ACCESS}")
        result.problem = True
        return []
    except FileNotFoundError:
        result.notes.append(f"{path}: not found (is the runtime installed here? --state-dir names another)")
        return []
    if not files:
        result.notes.append(f"{path}: not found (is the runtime installed here? --state-dir names another)")
        return []
    result.read += 1
    if component == "runtime":
        result.runtime_file = True
    found: list[Record] = []
    for index, each in enumerate(files):
        try:
            if index and query.since is not None and each.stat().st_mtime < query.since.timestamp():
                break
            if query.until is not None:
                first = _first_time(each)
                if first is not None and first > query.until:
                    continue
            upto = None
            if each == path:
                stat = each.stat()
                upto = stat.st_size if end is None else end
                result.positions[each] = (stat.st_ino, upto)
            for record in records_backwards(each, component, name, upto):
                if query.until is not None and record.time > query.until:
                    continue
                if query.since is not None and record.time < query.since:
                    return found
                kept = query.prepare(record)
                if kept:
                    found.append(kept)
                    if query.lines is not None and len(found) >= query.lines:
                        return found
        except PermissionError:
            result.notes.append(f"{each}: {GROUP_ACCESS}")
            result.problem = True
        except (OSError, EOFError) as exc:
            result.notes.append(f"{each}: {exc}")
            result.problem = True
    return found


# --------------------------------------------------------------------------
# the journal


def journal_units(components: Iterable[str]) -> list[str]:
    return [unit for name in components if (unit := COMPONENTS.get(name))]


def journal_argv(units: Iterable[str], since: datetime | None = None, until: datetime | None = None,
                 lines: int | None = None, follow: bool = False, cursor: str | None = None) -> list[str]:
    argv = [shutil.which(JOURNALCTL) or JOURNALCTL, "--no-pager", "-o", "json", "--utc"]
    for unit in units:
        argv += ["-u", unit]
    if cursor:
        argv += ["--after-cursor", cursor]
    elif since is not None:
        argv += ["--since", f"@{int(since.timestamp())}"]
    if until is not None:
        argv += ["--until", f"@{-int(-until.timestamp() // 1)}"]
    if lines is not None:
        argv += ["-n", str(lines)]
    if follow:
        argv.append("-f")
    return argv


def component_of(entry: dict) -> str | None:
    """The component a journal entry belongs to: its unit's, or its container's."""
    for key in ("_SYSTEMD_UNIT", "UNIT", "OBJECT_SYSTEMD_UNIT", "COREDUMP_UNIT"):
        unit = entry.get(key)
        if isinstance(unit, str):
            if unit in _UNIT_COMPONENT:
                return _UNIT_COMPONENT[unit]
            if unit.startswith(_SHARE_GATE_PREFIX):
                return "share-gate"
    container = entry.get("CONTAINER_NAME")
    return _CONTAINER_COMPONENT.get(container) if isinstance(container, str) else None


def _text(value: object) -> str:
    """MESSAGE: a string, or a list of byte values when it is not UTF-8."""
    if isinstance(value, list):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            return ""
    return value if isinstance(value, str) else "" if value is None else str(value)


def classify(message: str, priority: str | None) -> tuple[str, str | None, str]:
    """(level, logger, message) of a journal line: from the line when it
    names a level (log4j's or Python's prefix, logfmt's level=, a level
    word, a Python traceback), else from PRIORITY. A prefix that carried
    them is taken off the message."""
    for pattern in (JOURNAL_LINE, PYTHON_LINE, STAMPED_LINE):
        match = pattern.match(message)
        if match and level_of(match["level"]):
            return level_of(match["level"]) or "info", match["logger"].strip() or None, match["msg"]
    logger_match = _LOGFMT_LOGGER.search(message)
    logger = logger_match["logger"] if logger_match else None
    found = _LOGFMT_LEVEL.search(message)
    level = level_of(found["level"]) if found else None
    if level is None:
        found = _LEVEL_WORD.search(message[:80])
        level = level_of(found["level"]) if found else None
    if level is None and message.startswith("Traceback (most recent call last)"):
        level = "error"
    return level or _PRIORITY.get(str(priority), "info"), logger, message


class JournalParser:
    """Turns journal entries, oldest first, into records: a stack trace's
    lines join the record they follow, and while openhab.log can be read
    the runtime's copies of its records (and their lines) are dropped,
    keeping the unit's other lines (systemd, podman, crash output)."""

    def __init__(self, runtime_file: bool):
        self.runtime_file = runtime_file
        #: Each component's last record, which the lines after it may continue.
        self.pending: dict[str, Record] = {}
        #: The components whose last record was a dropped copy.
        self.dropped: set[str] = set()
        #: When the runtime last printed a line of one of openhab.log's records.
        self.copied: datetime | None = None
        self.cursor: str | None = None
        #: The components whose last record began a private key block that
        #: no line has ended yet.
        self.keys: set[str] = set()

    def feed(self, entry: dict) -> list[Record]:
        if isinstance(entry.get("__CURSOR"), str):
            self.cursor = entry["__CURSOR"]
        component = component_of(entry)
        if component is None:
            return []
        try:
            moment = datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc)
        except (KeyError, TypeError, ValueError):
            return []
        message = _text(entry.get("MESSAGE")).rstrip("\n")
        identifier = entry.get("SYSLOG_IDENTIFIER")
        container = entry.get("CONTAINER_NAME")
        own = bool(container) or identifier == (COMPONENTS[component] or "").removesuffix(".service")
        previous = self.pending.get(component)
        follows = previous is not None and moment - previous.time <= _CONTINUATION_GAP
        if component in self.keys:
            self.keys.discard(component)
            # A key printed a line per entry stays one record, for redact()
            # to hide whole.
            if follows and previous is not None and _KEY_LINE.fullmatch(message.strip()):
                previous.message += "\n" + message
                if oh_secrets.unclosed_key(previous.message):
                    self.keys.add(component)
                return []
        continued = own and _CONTINUATION.match(message) is not None
        if component == "runtime" and own:
            copy = JOURNAL_LINE.match(message) is not None
            # The lines of an exception (java.lang.X: ...) follow its record at once.
            continued = not copy and (continued or (self.copied is not None
                                                    and moment - self.copied <= _CONTINUATION_GAP))
            self.copied = moment if copy or continued else None
            if self.runtime_file and (copy or (continued and (component in self.dropped or previous is None))):
                self.dropped.add(component)
                return []
        if continued and follows and previous is not None:
            previous.message += "\n" + message
            if oh_secrets.unclosed_key(previous.message):
                self.keys.add(component)
            return []
        self.dropped.discard(component)
        level, logger, text = classify(message, entry.get("PRIORITY"))
        if logger is None and not own and isinstance(identifier, str):
            logger = identifier
        out = [previous] if previous is not None else []
        self.pending[component] = Record(moment, component, level, logger, text, JOURNAL)
        if oh_secrets.unclosed_key(text):
            self.keys.add(component)
        return out

    def flush(self) -> list[Record]:
        out = sorted(self.pending.values(), key=lambda record: record.time)
        self.pending = {}
        return out


def _journal_notes(stderr: str, status: int, result: Result) -> None:
    if any(hint in stderr for hint in _JOURNAL_DENIED):
        result.notes.append(JOURNAL_ACCESS)
        result.problem = True
    elif status:
        said = next((line.strip() for line in stderr.splitlines() if line.strip()), f"exit status {status}")
        result.notes.append(f"journalctl: {said}")
        result.problem = True


def _run_journal(argv: list[str], parser: JournalParser, query: Query, result: Result) -> tuple[list[Record], int]:
    """(matching records oldest first, entries journalctl printed)."""
    oh_trace.run(argv)
    found: list[Record] = []
    entries = 0
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL) as process:
            assert process.stdout is not None
            for raw in process.stdout:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                entries += 1
                for record in parser.feed(entry):
                    if query.within(record.time) and (kept := query.prepare(record)):
                        found.append(kept)
            for record in parser.flush():
                if query.within(record.time) and (kept := query.prepare(record)):
                    found.append(kept)
        errors.seek(0)
        _journal_notes(errors.read().decode("utf-8", errors="replace"), process.returncode, result)
    return found, entries


def read_journal(query: Query, result: Result) -> list[Record]:
    """The selected units' journal, in one journalctl run: matching records,
    oldest first. For the last N it asks for more entries than N (copies
    and filtered lines take some), and again for twice as many while too
    few match and the journal has more."""
    units = journal_units(query.components)
    if not units:
        return []
    result.tried += 1
    if shutil.which(JOURNALCTL) is None:
        result.notes.append(f"{JOURNALCTL} not found: showing the log files only")
        return []
    asked = None if query.lines is None else max(4 * query.lines, 200)
    while True:
        parser = JournalParser(result.runtime_file)
        attempt = Result()
        try:
            found, entries = _run_journal(journal_argv(units, query.since, query.until, asked), parser, query, attempt)
        except OSError as exc:
            result.notes.append(f"{JOURNALCTL}: {exc}")
            result.problem = True
            return []
        if asked is None or len(found) >= (query.lines or 0) or entries < asked or asked >= 1 << 20:
            break
        asked *= 2
    result.read += 1
    result.notes += attempt.notes
    result.problem = result.problem or attempt.problem
    result.cursor = parser.cursor
    return found if query.lines is None else found[-query.lines:]


# --------------------------------------------------------------------------
# reading them all


def read(query: Query, logs: Path | None, journal: bool = True) -> Result:
    """Every selected source's matching records, merged by time: the last
    query.lines of them, or every one in range."""
    result = Result()
    newest_first: list[Record] = []
    for component in ("runtime", "events"):
        if component in query.components and logs is not None:
            newest_first += read_file(query, logs, component, result)
    merged = sorted(reversed(newest_first), key=lambda record: record.time)
    if journal:
        merged = sorted([*merged, *read_journal(query, result)], key=lambda record: record.time)
    result.records = merged if query.lines is None else merged[-query.lines:] if query.lines else []
    return result


# --------------------------------------------------------------------------
# following them


class Tail:
    """New records of one growing file, through rotation: when the path
    names another file (or a shorter one), the rest of the old one is read,
    then the new one from its start."""

    def __init__(self, path: Path, component: str, position: tuple[int, int] | None = None):
        self.path = path
        self.component = component
        self.handle: BinaryIO | None = None
        self.inode: int | None = None
        self.buffer = b""
        self.pending: Record | None = None
        self.idle = 0
        if position is not None:
            self._open(position[0], position[1])
        else:
            try:
                stat = path.stat()
            except OSError:
                return
            self._open(stat.st_ino, stat.st_size)

    def _open(self, inode: int | None, offset: int) -> None:
        try:
            handle = open(self.path, "rb")
        except OSError:
            return
        if inode is not None and os.fstat(handle.fileno()).st_ino != inode:
            offset = 0
        handle.seek(offset)
        self.handle, self.inode = handle, os.fstat(handle.fileno()).st_ino

    def close(self) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None

    def _lines(self, data: bytes) -> list[Record]:
        out: list[Record] = []
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            match = LOG_LINE.match(line)
            record = _file_record(match, self.component, FILES[self.component]) if match else None
            if record is not None:
                if self.pending is not None:
                    out.append(self.pending)
                self.pending = record
            elif self.pending is not None:
                self.pending.message += "\n" + line
        return out

    def poll(self) -> list[Record]:
        out: list[Record] = []
        data = b""
        try:
            stat = self.path.stat()
        except OSError:
            stat = None
        if self.handle is not None:
            data = self.handle.read()
            if stat is None or stat.st_ino != self.inode or stat.st_size < self.handle.tell():
                out += self._lines(data + (b"\n" if self.buffer else b""))
                self.close()
                data = b""
        if self.handle is None and stat is not None:
            self._open(None, 0)
            data += self.handle.read() if self.handle else b""
        out += self._lines(data)
        self.idle = 0 if data else self.idle + 1
        # A record's continuation lines come with it, or within a poll or two.
        if self.idle >= 2 and self.pending is not None:
            out.append(self.pending)
            self.pending = None
        return out


def _pump(stream, into: queue.Queue) -> None:
    for raw in stream:
        into.put(raw)
    into.put(None)


def follow(query: Query, logs: Path | None, journal: bool, start: Result, emit: Callable[[Record], None],
           stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep,
           interval: float = 0.5) -> Result:
    """Print (emit) each new matching record, in time order a poll at a time,
    until stop() is true (or Ctrl-C, KeyboardInterrupt, outside)."""
    result = Result()
    tails = [Tail(logs / FILES[component], component, start.positions.get(logs / FILES[component]))
             for component in ("runtime", "events") if component in query.components and logs is not None]
    units = journal_units(query.components) if journal else []
    process = None
    lines: queue.Queue = queue.Queue()
    parser = JournalParser(start.runtime_file)
    if units and shutil.which(JOURNALCTL):
        argv = journal_argv(units, cursor=start.cursor, lines=None if start.cursor else 0, follow=True)
        oh_trace.run(argv)
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.DEVNULL)
        threading.Thread(target=_pump, args=(process.stdout, lines), daemon=True).start()
    try:
        idle = 0
        while True:
            batch: list[Record] = []
            for tail in tails:
                batch += tail.poll()
            fed = False
            while True:
                try:
                    raw = lines.get_nowait()
                except queue.Empty:
                    break
                if raw is None:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    fed = True
                    batch += parser.feed(entry)
            idle = 0 if fed else idle + 1
            if idle >= 2:
                batch += parser.flush()
            for record in sorted(batch, key=lambda record: record.time):
                if query.within(record.time) and (kept := query.prepare(record)):
                    emit(kept)
            if stop():
                return result
            sleep(interval)
    finally:
        for tail in tails:
            tail.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stdout:
                process.stdout.close()

