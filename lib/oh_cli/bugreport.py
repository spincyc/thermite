"""Collect what a bug report needs into one archive.

    thermite bugreport [--out FILE | --out -] [--since WHEN] [--no-redact] [--json]
"""

from __future__ import annotations

import datetime
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Sequence

import oh_cli
from oh_paths import PROGRAM as PROGRAM_ROOT

SINCE = "24h"
#: Seconds each report may take; the logs of a busy day take the longest.
TIMEOUT = 300

DESCRIPTION = f"""\
Collect, read-only, what a bug report about this host's Thermite needs into
one compressed archive: thermite's version and the runtime's (thermite
version), what is deployed and running (thermite status, with the
runtime's checks), every deploy (thermite history), the services' logs and
the runtime's events.log since --since (default: the last {SINCE}; thermite
logs), and the system's facts: the kernel, the distribution, uptime, free
memory and disk, and Thermite's systemd units. Each part is the output of
that command, run as you; manifest.json names each command, its exit
status and what it said on stderr. A command that fails leaves its part
empty and the rest is still collected.

Secrets (passwords, tokens, keys, cookies, Authorization headers, a URL's
password) are written as ***; --no-redact keeps them. Addresses, host
names and device names stay: review the archive before sharing it.

It writes thermite-bugreport-HOST-TIME.tar.gz in the current directory, or
--out FILE; --out - writes the archive to standard output. With --url
ssh://HOST it collects on that server and writes the archive here.
"""

EXAMPLES = (
    ("thermite bugreport", "write thermite-bugreport-HOST-TIME.tar.gz here"),
    ("thermite bugreport --since 3d", "with the logs of the last three days"),
    ("thermite bugreport --url ssh://thermite", "collect on the server named thermite; write the archive here"),
    ("thermite bugreport --out - | ssh laptop 'cat > report.tar.gz'", "the archive on standard output"),
)

NOTES = """\
exit status:
  0  the archive is written (manifest.json names any part whose command failed)
  2  a wrong switch, or the archive could not be written
"""


def _commands(since: str, redact: bool) -> tuple[tuple[str, list[str]], ...]:
    """(file in the archive, thermite's switches that print it)."""
    secrets = [] if redact else ["--no-redact"]
    return (
        ("version.json", ["version", "--json"]),
        ("status.json", ["status", "--json"]),
        ("history.json", ["history", "--json", "--details", "--last", "0"]),
        ("logs.txt", ["logs", "--since", since, "--utc", *secrets]),
        ("events.txt", ["logs", "events", "--since", since, "--utc", *secrets]),
    )


#: The system's facts: (heading, argv), read-only, without root.
FACTS = (
    ("kernel", ["uname", "-a"]),
    ("uptime", ["uptime"]),
    ("memory", ["free", "-m"]),
    ("disk", ["df", "-h", "/", "/srv", "/var"]),
    ("units", ["systemctl", "list-units", "thermite*", "--all", "--no-pager", "--plain"]),
    ("timers", ["systemctl", "list-timers", "thermite*", "--all", "--no-pager"]),
    ("failed", ["systemctl", "--failed", "--no-pager", "--plain"]),
)
OS_RELEASE = Path("/etc/os-release")


def run(argv: Sequence[str], timeout: float = TIMEOUT) -> tuple[int | None, str, str, float]:
    """(exit status, or None when it could not run or took too long; stdout;
    stderr; seconds)."""
    started = time.monotonic()
    oh_cli._trace.debug("run %s", shlex.join(argv))
    # Each part is this host's: THERMITE_URL would send thermite's commands to a server.
    environment = {name: value for name, value in os.environ.items() if name != oh_cli.URL_VARIABLE}
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, errors="replace", timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL, env=environment)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return None, out, f"stopped after {timeout:g} seconds", time.monotonic() - started
    except OSError as exc:
        return None, "", str(exc), time.monotonic() - started
    return done.returncode, done.stdout, done.stderr, time.monotonic() - started


def thermite(*words: str) -> list[str]:
    """This thermite's command line for words, on the site selected now
    (oh_paths.select_args left it in THERMITE_SITE, which it inherits)."""
    return [sys.executable, str(PROGRAM_ROOT / "bin/thermite"), "--no-color", *words]


def collect(since: str, redact: bool, runner=None) -> tuple[dict[str, str], dict]:
    """({file: text}, the manifest) of a report; runner (default: run)
    runs each command."""
    runner = runner or run
    clean = _redactor(redact)
    files: dict[str, str] = {}
    commands = []
    for name, words in _commands(since, redact):
        argv = thermite(*words)
        code, out, err, seconds = runner(argv)
        files[name] = clean(out)
        commands.append({"file": name, "command": shlex.join([oh_cli.PROGRAM, *words]), "exit": code,
                         "seconds": round(seconds, 1), "stderr": clean(err).strip()})
    facts = []
    for heading, argv in FACTS:
        code, out, err, _ = runner(argv, 30)
        facts.append(f"## {heading}: {shlex.join(argv)}" + ("" if code == 0 else f" (exit {code})") + "\n"
                     + (out or err).rstrip("\n") + "\n")
    try:
        facts.append(f"## distribution: {OS_RELEASE}\n" + OS_RELEASE.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        facts.append(f"## distribution: {OS_RELEASE}: {exc}\n")
    facts.append(f"## python\n{sys.version}\n")
    files["system.txt"] = clean("\n".join(facts))
    manifest = {"thermite": oh_cli.VERSION, "host": socket.gethostname(),
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "since": since, "redacted": redact, "commands": commands}
    return files, manifest


def _redactor(redact: bool):
    if not redact:
        return lambda text: text
    import oh_secrets
    return oh_secrets.redact


def stamp(now: datetime.datetime | None = None) -> str:
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def default_name(host: str, now: datetime.datetime | None = None) -> str:
    return f"thermite-bugreport-{host.split('.')[0]}-{stamp(now)}.tar.gz"


def archive(files: dict[str, str], manifest: dict, folder: str) -> bytes:
    """The .tar.gz of files and manifest.json, below folder/."""
    buffer = io.BytesIO()
    created = time.time()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in {**files, "manifest.json": json.dumps(manifest, indent=2, sort_keys=True) + "\n"}.items():
            data = text.encode("utf-8")
            member = tarfile.TarInfo(f"{folder}/{name}")
            member.size, member.mode, member.mtime = len(data), 0o600, int(created)
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def write(path: Path, data: bytes) -> None:
    """data in path, readable only by its owner, written whole or not at all."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _size(count: int) -> str:
    return f"{count / 1e6:.1f} MB" if count >= 1e5 else f"{count / 1e3:.0f} kB"


def summary(path: str, data: bytes, manifest: dict, as_json: bool) -> None:
    failed = [entry for entry in manifest["commands"] if entry["exit"] not in (0, 1)]
    if as_json:
        oh_cli.emit({"archive": path, "bytes": len(data), "commands": manifest["commands"]})
        return
    for entry in failed:
        print(f"{oh_cli.PROGRAM} bugreport: {entry['command']} " +
              ("did not run" if entry["exit"] is None else f"exited {entry['exit']}")
              + (f": {entry['stderr'].splitlines()[-1]}" if entry["stderr"] else ""), file=sys.stderr)
    print(f"wrote {path} ({_size(len(data))}); review it before sharing: addresses, host names and device names "
          "stay in it" + ("" if manifest["redacted"] else ", and secrets too (--no-redact)"))


def _taken(words: Sequence[str], switch: str) -> tuple[str | None, list[str]]:
    """(the value of the last `switch` VALUE or switch=VALUE in words, or
    None; words without them)."""
    found, out, index = None, [], 0
    words = list(words)
    end = len(oh_cli._switches(words))
    while index < len(words):
        word = words[index]
        if index < end and word == switch and index + 1 < end:
            found, index = words[index + 1], index + 2
            continue
        if index < end and word.startswith(switch + "="):
            found, index = word.split("=", 1)[1], index + 1
            continue
        out.append(word)
        index += 1
    return found, out


def fetch(words: Sequence[str], url: str) -> int:
    """`thermite bugreport --url ssh://...`: collect on that server, with
    the archive on its standard output, and write it here."""
    out, rest = _taken(words, "--out")
    switches = oh_cli._switches(rest)
    as_json = "--json" in switches
    quiet = any(word == "--quiet" or (oh_cli.is_global(word) and "q" in word) for word in switches)

    def unquiet(word: str) -> str:
        # -qv: the server still gets -v.
        return "-" + word[1:].replace("q", "") if oh_cli.is_global(word) and not word.startswith("--") else word
    # The archive comes back on standard output: the server must print it,
    # and nothing else there.
    rest = [unquiet(word) for word in rest if word not in ("--json", "--quiet") and unquiet(word) != "-"]
    if out == "-" and as_json:
        return oh_cli.usage_error("--json prints a summary on standard output, where --out - writes the archive")
    if out == "-" and quiet:
        return oh_cli.usage_error("-q silences standard output, where --out - writes the archive")
    if out == "-" and sys.stdout.isatty():
        return oh_cli.usage_error("--out - writes a compressed archive: send standard output to a file or a pipe")
    try:
        target, _ = oh_cli.ssh_target(url)
    except ValueError as exc:
        return oh_cli.usage_error(str(exc))
    if out == "-":
        sys.stdout.flush()
        return oh_cli.over_ssh("bugreport", [*rest, "--out", "-"], url, stdout=sys.stdout.buffer)
    path = Path(out) if out else Path(default_name(target[-1].rpartition("@")[2]))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        print(f"{oh_cli.PROGRAM} bugreport: cannot write {path}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    try:
        with os.fdopen(descriptor, "wb") as stream:
            status = oh_cli.over_ssh("bugreport", [*rest, "--out", "-"], url, stdout=stream)
        if status == 0:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if status != 0:
        return status
    size = path.stat().st_size
    if as_json:
        oh_cli.emit({"archive": str(path), "bytes": size, "url": url})
    elif not quiet:
        print(f"wrote {path} ({_size(size)}) from {target[-1]}; review it before sharing: addresses, host "
              "names and device names stay in it")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("bugreport", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("--out", metavar="FILE",
                        help="write the archive here (default: thermite-bugreport-HOST-TIME.tar.gz in the current "
                             "directory); - writes it to standard output")
    parser.add_argument("--since", default=SINCE, metavar="WHEN",
                        help=f"the logs from WHEN on, as thermite logs --since takes it (default: {SINCE})")
    parser.add_argument("--no-redact", dest="redact", action="store_false",
                        help="keep secrets as the logs and reports have them (default: as ***)")
    oh_cli.add_json(parser)
    args = parser.parse_args(argv)
    if args.out == "-" and args.json:
        parser.error("--json prints a summary on standard output, where --out - writes the archive")
    if args.out == "-" and oh_cli.switches.quiet:
        parser.error("-q silences standard output, where --out - writes the archive")
    if args.out == "-" and sys.stdout.isatty():
        parser.error("--out - writes a compressed archive: send standard output to a file or a pipe")
    import oh_logs
    try:
        oh_logs.parse_when(args.since)
    except ValueError as exc:
        parser.error(f"--since: {exc}")
    files, manifest = collect(args.since, args.redact)
    name = default_name(socket.gethostname())
    data = archive(files, manifest, name.removesuffix(".tar.gz"))
    if args.out == "-":
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0
    path = Path(args.out) if args.out else Path(name)
    try:
        write(path, data)
    except OSError as exc:
        print(f"{oh_cli.PROGRAM} bugreport: cannot write {path}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    summary(str(path), data, manifest, args.json)
    return 0
