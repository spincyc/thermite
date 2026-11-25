"""bin/thermite, the area's one program.

`thermite VERB [ACTION] [SWITCHES]` runs main(argv) of oh_cli/<verb>.py,
with _ for each - in VERB. A verb module has a docstring and
main(argv: list[str] | None) -> int, the exit status, and builds its
parser with command() (and action() for each action), so every verb's
help has the same shape: what it does, its switches, any notes, then
examples. A switch that recurs across verbs comes from an add_* helper
below, so it reads and means the same in each.

Every verb also takes the global switches (-q, -v, --no-color), after the
verb or before it: command() adds them to each parser, main() moves them
from before the verb to after it, and parsing them sets `switches`.

This package and oh_cli/apple_tv.py use only the standard library: the
Apple TV helper's container runs them (oh_deploy.ATV_APP), and
`thermite --help` works before PyYAML is installed. Nor do they need Git:
that container has none.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import difflib
import importlib
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import IO, NamedTuple, Sequence

from oh_paths import PROGRAM as PROGRAM_ROOT, SITE as AREA
from oh_release import VERSION
from oh_ports import HTTP_PORT, SANDBOX_URL
BIN = AREA / "bin"
PROGRAM = "thermite"
#: thermite's own version; the commit (`thermite --version`) tells builds apart.
HOUSE = AREA / "instance/devices.yaml"

#: (group, heading) in the order `thermite --help` lists them.
GROUPS = (
    ("house", "the house file, and what is generated from it"),
    ("test", "test on a test runtime, never production"),
    ("production", "production runtime on this host"),
    ("devices", "the house's devices"),
    ("use", "front ends, themes and docs"),
    ("program", "this program"),
)

#: Verb.reach: what --url may name for it. LOCAL verbs work on this
#: computer's files or change this host, and take no --url. SSH verbs read or
#: act on a running installation: the dispatcher runs them on the server
#: --url ssh://... names (over_ssh()). GATEWAY verbs talk to an HTTPS
#: gateway, which --url https://... names.
LOCAL, SSH, GATEWAY = "local", "ssh", "gateway"
#: The environment variable that names a server when a verb is given no
#: target switch: as --url, for each verb whose reach its kind suits.
URL_VARIABLE = "THERMITE_URL"


class Verb(NamedTuple):
    group: str
    summary: str
    #: Whether it runs on the selected private site, so the dispatcher first
    #: checks that site suits this application (oh_paths.require_compatible).
    site: bool = True
    reach: str = LOCAL


#: verb: Verb. The summaries are what `thermite --help` lists.
VERBS = {
    "init": Verb("house", "create a private site, or pin an installed application upgrade", site=False),
    "check": Verb("house", "validate, build and lint the selected configuration; write nothing"),
    "generate": Verb("house", "write conf/ and metrics/ from the house file, and deploy/'s systemd units"),
    "test": Verb("test", "boot conf/ on a test runtime; test routing and the controls end to end"),
    "sandbox": Verb("test", f"run a thermite sandbox from a copy of conf/ on {SANDBOX_URL.split('//')[1]}"),
    "install": Verb("production", "install thermite on this host, with one approval for the whole run", site=False),
    "upgrade": Verb("production", "update the application, native services and HTTPS gateway together", site=False),
    "uninstall": Verb("production", "stop thermite here and remove its units; --purge deletes its state"),
    "rollback": Verb("production", "install the commit deployed before the current one, or another"),
    "status": Verb("production", "what is deployed and running here, against this checkout", reach=SSH),
    "history": Verb("production", "this host's deploys, numbered, the latest last", reach=SSH),
    "logs": Verb("production", "every Thermite service's log, merged by time; --since and --until pick a range",
                 site=False, reach=SSH),
    "bugreport": Verb("production", "collect versions, status, history, logs and system facts into one archive",
                      site=False, reach=SSH),
    "dhcp-reservations": Verb("devices", "list the UniFi DHCP reservations the house file needs"),
    "matrix-settings": Verb("devices", "show matrix settings that differ from the house file; --apply sets them"),
    "control4": Verb("devices", "export the Control4 project read-only and compare it"),
    "discover": Verb("devices", "scan the LAN read-only and compare it with the house file"),
    "pair": Verb("devices", "pair a TV with runtime, or check the Sony TVs' pairing", reach=SSH),
    "device": Verb("devices", "everything about one device: what it is, what watches it, what holds now", reach=SSH),
    "reboot": Verb("devices", "restart a device now as the nightly reboots do, or list how each went", reach=SSH),
    "cameras": Verb("devices", "read camera status, or configure native Protect bridges privately", reach=SSH),
    "apple-tv": Verb("devices", "run the Apple TV helper runtime calls"),
    "ir-profile": Verb("devices", "validate verified IR keys or print an iTach packet without transmitting"),
    "remote": Verb("use", "the house remote in a terminal", reach=GATEWAY),
    "tui": Verb("use", "the house remote in a terminal on any computer, from its HTTPS gateway", site=False,
                reach=GATEWAY),
    "handheld": Verb("use", "handheld remotes: the bridge to the house, enrolling and forgetting them", site=False,
                     reach=GATEWAY),
    # Its server subcommands check the site themselves (oh_cli.access).
    "access": Verb("use", "site login, local identity bootstrap, HTTPS service and room policies", site=False,
                   reach=GATEWAY),
    "theme": Verb("use", "the flaming bowl mark and color themes: show, check, export", site=False),
    "docs": Verb("use", "build the docs site, or export it to the thermite repository", site=False),
    "version": Verb("program", "thermite's version, and runtime's: pinned, and running here", site=False,
                    reach=SSH),
    "package": Verb("program", "build a privacy-checked portable application wheel", site=False),
    "iso": Verb("program", "build a bootable installer for a dedicated Thermite machine", site=False),
    "completion": Verb("program", "print a completion script for bash, zsh or fish", site=False),
}


class Retired(NamedTuple):
    #: The command line that replaces it, as its warning names it.
    instead: str
    site: bool = True
    reach: str = LOCAL


#: verb: Retired, for the verbs another one absorbed. Each still runs as it
#: did, for one release, after a warning on stderr; `thermite --help`, the
#: completion scripts and the docs list only VERBS. A rollback and a revert
#: run the reinstated version's own `health --production --installation`
#: (oh_install.changeover.health), which older versions know by that name
#: alone: health stays until no supported version predates status --runtime.
RETIRED = {
    "validate": Retired("thermite check house"),
    "lint": Retired("thermite check lint"),
    "health": Retired("thermite status --runtime", reach=SSH),
    "revert": Retired("thermite rollback --restore-state"),
}


#: Switches that mean the same in every verb that has them (the add_*
#: helpers), as `thermite --help` explains them.
STANDARD_SWITCHES = (
    ("--config DIR", "select private configuration (also THERMITE_SITE)"),
    ("--json", "print the report as JSON"),
    ("-n, --dry-run", "report what it would do; change nothing"),
    ("--check", "change nothing; exit 1 if what it writes is stale"),
    ("--apply", "send changes to live devices (without it: only show them)"),
    ("--timeout SECONDS", "how long to wait"),
    ("--house FILE", "the house file (default: instance/devices.yaml)"),
    ("--production | --sandbox | --url URL", "this host, the sandbox, or a server: ssh://HOST runs it there, "
     "https://HOST is its gateway"),
    ("--token-file FILE", "a file holding the runtime's API token"),
    ("--state-dir DIR", "the runtime's state (default: runtime/ in instance/host.yaml's root)"),
    ("--yes", "approve it up front, unattended: ask nothing"),
    ("--theme NAME|FILE", "the colors: a built-in theme's name or a theme file"),
    ("--dark | --light", "which of the theme's two modes"),
)

#: (option strings, dest, help) of the switches every verb takes, before
#: it or after it.
GLOBAL_SWITCHES = (
    (("-q", "--quiet"), "quiet", "print only errors"),
    (("-v", "--verbose"), "verbose", "also print, on stderr, the commands and HTTP requests it runs"),
    (("--no-color",), "no_color", "no color in what it prints (as NO_COLOR=1 does)"),
)
_GLOBAL_DESTS = frozenset(dest for _, dest, _ in GLOBAL_SWITCHES)
_GLOBAL_OPTIONS = frozenset(option for options, _, _ in GLOBAL_SWITCHES for option in options)

EXIT_STATUS = (
    ("0", "done, and nothing is wrong"),
    ("1", "done, and it found a problem: a check failed, a file is stale"),
    ("2", "it could not run: a wrong switch, a broken house file, nothing answering"),
)

OVERVIEW_EXAMPLES = (
    ("thermite generate", "after any change to the house file or lib/"),
    ("thermite test boot", "before a deploy: does conf/ load cleanly?"),
    ("thermite install --dry-run", "what an install would change here"),
    ("thermite upgrade -n", "what moving to the pinned runtime would run here"),
    ("thermite remote --mock", "the remote in a terminal, sending nothing"),
    ("thermite tui", "the remote from a laptop: the gateway on the server named thermite"),
    ("thermite theme show", "the flaming bowl, in this terminal's colors"),
    ("thermite help sandbox", "one verb's actions, switches and examples"),
    ("thermite completion bash", "tab completion for bash (thermite help completion)"),
)

Examples = Sequence[tuple[str, str]]


def module_name(verb: str) -> str:
    """oh_cli.dhcp_reservations for dhcp-reservations."""
    if verb not in VERBS and verb not in RETIRED:
        raise ValueError(f"{verb} is not a {PROGRAM} verb")
    return f"{__name__}.{verb.replace('-', '_')}"


class Help(argparse.RawDescriptionHelpFormatter):
    """The same layout for every verb, whatever the terminal's width, so
    the help reads the same in a terminal and on the docs site."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=32, width=88)


def _rows(rows: Sequence[tuple[str, str]], width: int = 34) -> list[str]:
    out = []
    for left, right in rows:
        if len(left) + 2 > width:
            out += [f"  {left}", f"  {'':<{width}}{right}"]
        else:
            out.append(f"  {left:<{width}}{right}")
    return out


def epilog(examples: Examples = (), notes: str = "") -> str | None:
    """A verb's or an action's text after its switches: notes, then examples."""
    parts = [notes.rstrip("\n")] if notes.strip() else []
    if examples:
        parts.append("\n".join(["examples:", *_rows(examples)]))
    return "\n\n".join(parts) + "\n" if parts else None


# --------------------------------------------------------------------------
# the global switches, and what they set


@dataclasses.dataclass
class Switches:
    quiet: bool = False
    verbose: bool = False
    no_color: bool = False


#: What the global switches of the verb running now set.
switches = Switches()
#: Set by run(): this process is bin/thermite itself, so -q may silence the
#: programs it starts too, not only this Python's sys.stdout.
_whole_process = False
#: Set by main() for a verb that runs on the selected site (Verb.site): its
#: parser's parse_args checks the site once the switches parse.
_site_check = False


def is_global(word: str) -> bool:
    """Whether word is global switches (-q, -v, --no-color, or -qv and the like)."""
    return word in _GLOBAL_OPTIONS or bool(re.fullmatch(r"-[qv]+", word))


def _apply(namespace: argparse.Namespace) -> None:
    for dest in _GLOBAL_DESTS:
        if getattr(namespace, dest, False):
            setattr(switches, dest, True)
        if hasattr(namespace, dest):
            delattr(namespace, dest)
    if switches.no_color:
        # The environment, so that programs thermite starts (the terminal
        # remote) see it too.
        os.environ["NO_COLOR"] = "1"
    if switches.quiet and not isinstance(sys.stdout, _Discard):
        sys.stdout.flush()
        if _whole_process:
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.close(null)
        sys.stdout = _Discard()


class _Discard(io.TextIOBase):
    """sys.stdout under -q."""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return len(text)


def color() -> bool:
    """Whether thermite may print ANSI color: neither --no-color nor NO_COLOR (https://no-color.org)."""
    return not switches.no_color and not os.environ.get("NO_COLOR")


#: The logger libraries say what thermite does outside itself to
#: (oh_trace.NAME). Named here too: this package runs where oh_trace is
#: not installed (the Apple TV helper's container).
TRACE = "thermite.trace"


class _Trace(logging.Handler):
    """With -v: each of those lines, on stderr."""

    def emit(self, record: logging.LogRecord) -> None:
        if switches.verbose:
            print(f"{PROGRAM}: {record.getMessage()}", file=sys.stderr, flush=True)


_trace = logging.getLogger(TRACE)
_trace.setLevel(logging.DEBUG)
_trace.propagate = False
_trace.handlers[:] = [_Trace()]


def add_global(parser: argparse.ArgumentParser) -> None:
    """The global switches, in a group of their own. Their default is
    SUPPRESS so that an action's parser leaves the verb's alone."""
    group = parser.add_argument_group("switches every verb takes")
    parser._thermite_global = group
    for options, dest, text in GLOBAL_SWITCHES:
        group.add_argument(*options, dest=dest, action="store_true", default=argparse.SUPPRESS, help=text)


# --------------------------------------------------------------------------
# the parsers


class Captured(Exception):
    """Carries a verb's parser out of its main(), before it parses anything."""

    def __init__(self, parser: argparse.ArgumentParser):
        super().__init__(parser.prog)
        self.parser = parser


class Parser(argparse.ArgumentParser):
    """Every verb's parser (command() and action()). Its help lists the
    global switches last and leaves them out of the usage line, which they
    would only lengthen. While `capturing` is set, parse_args raises
    Captured instead of parsing, so parser_of() reads a verb's parser
    without running the verb."""

    capturing = False

    def __init__(self, *args, **kwargs):
        # A switch is its whole spelling. bin/thermite takes --config out of
        # a command line by that rule before any parser runs, and each
        # verb's parser declares it only for its help: one that took
        # --confi for it would accept a site and select none.
        super().__init__(*args, **{**kwargs, "allow_abbrev": False})

    def parse_args(self, args=None, namespace=None):
        global _site_check
        if Parser.capturing:
            raise Captured(self)
        namespace = super().parse_args(args, namespace)
        _apply(namespace)
        if _site_check:
            _site_check = False
            import oh_paths
            try:
                oh_paths.require_compatible()
            except ValueError as exc:
                raise SystemExit(usage_error(str(exc))) from None
        return namespace

    def _usage_actions(self) -> list[argparse.Action]:
        return [a for a in self._actions if a.dest not in _GLOBAL_DESTS]

    def format_usage(self) -> str:
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._usage_actions(), self._mutually_exclusive_groups)
        return formatter.format_help()

    def format_help(self) -> str:
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._usage_actions(), self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        last = getattr(self, "_thermite_global", None)
        for group in [g for g in self._action_groups if g is not last] + ([last] if last else []):
            formatter.start_section(group.title)
            formatter.add_text(group.description)
            formatter.add_arguments(group._group_actions)
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def parser_of(verb: str) -> argparse.ArgumentParser:
    """`thermite VERB`'s parser, as its main() builds it, without running it."""
    module = importlib.import_module(module_name(verb))
    Parser.capturing = True
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            module.main([])
    except Captured as found:
        return found.parser
    finally:
        Parser.capturing = False
    raise RuntimeError(f"{PROGRAM} {verb} built no parser")


def command(verb: str, description: str, examples: Examples, notes: str = "") -> argparse.ArgumentParser:
    """The parser of `thermite VERB`."""
    if verb not in VERBS and verb not in RETIRED:
        raise ValueError(f"{verb} is not a {PROGRAM} verb")
    parser = Parser(prog=f"{PROGRAM} {verb}", description=description.rstrip("\n"),
                    epilog=epilog(examples, notes), formatter_class=Help)
    # The bootstrap consumes this before importing modules with site paths.
    # Keep it in each parser so help and shell completion expose the option.
    parser.add_argument("--config", metavar="DIR", help="use this private configuration directory")
    if verb in VERBS and VERBS[verb].reach == SSH:
        # reached() intercepts --url ssh://... before the verb runs.
        # Keep it in each SSH verb's parser so help and completion expose it.
        parser.add_argument("--url", metavar="URL", help=TARGETS[SSH])
    add_global(parser)
    return parser


def actions(parser: argparse.ArgumentParser, required: bool = True) -> argparse._SubParsersAction:
    return parser.add_subparsers(dest="command", required=required, metavar="ACTION", title="actions")


def action(actions_: argparse._SubParsersAction, name: str, summary: str, *, description: str = "",
           examples: Examples = (), notes: str = "", parents: Sequence[argparse.ArgumentParser] = ()
           ) -> argparse.ArgumentParser:
    """The parser of `thermite VERB ACTION`."""
    text = summary[:1].upper() + summary[1:] + "."
    parser = actions_.add_parser(name, help=summary, parents=list(parents),
                                 description=(text + "\n\n" + description.rstrip("\n")) if description else text,
                                 epilog=epilog(examples, notes), formatter_class=Help)
    add_global(parser)
    return parser


# --------------------------------------------------------------------------
# the standard switches


def add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def emit(report: object) -> None:
    """Print a --json report: one line of JSON, keys sorted, as every verb does."""
    print(json.dumps(report, sort_keys=True))


def add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--dry-run", action="store_true", help="report what it would do; change nothing")


def add_check(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--check", action="store_true", help=f"change nothing; exit 1 if {what} is stale")


def add_apply(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument("--apply", action="store_true", help=f"{what} (without it: only show them)")


def add_timeout(parser: argparse.ArgumentParser, default: float, what: str, dest: str = "timeout") -> None:
    parser.add_argument("--timeout", type=float, default=default, metavar="SECONDS", dest=dest,
                        help=f"seconds to wait for {what} (default: {default:g})")


def add_house(parser: argparse.ArgumentParser, default: Path | None = HOUSE, dest: str = "house") -> None:
    parser.add_argument("--house", type=Path, default=default, metavar="FILE", dest=dest,
                        help="the house file (default: instance/devices.yaml)")


def add_conf(parser: argparse.ArgumentParser, default: Path | None = AREA / "conf") -> None:
    parser.add_argument("--conf", type=Path, default=default, metavar="DIR",
                        help="the runtime conf directory (default: conf/)")


def add_yes(parser: argparse.ArgumentParser, what: str) -> None:
    """--yes: the run's one question answered yes before it is asked."""
    parser.add_argument("--yes", action="store_true",
                        help=f"approve {what} up front, unattended: nothing reads standard input")


def add_token_file(parser: argparse.ArgumentParser, what: str = "file holding the runtime's API token") -> None:
    parser.add_argument("--token-file", type=Path, metavar="FILE", help=what)


#: What --state-dir names, where a verb gives no other help.
STATE_DIR = "the runtime state (default: runtime/ in instance/host.yaml's root, /srv/thermite)"


def add_state_dir(parser: argparse.ArgumentParser, what: str = STATE_DIR) -> None:
    """--state-dir DIR, as `state_dir`, and --state, its spelling before."""
    parser.add_argument("--state-dir", type=Path, metavar="DIR", help=what)
    add_renamed(parser, "--state", "--state-dir")


def add_state_file(parser: argparse.ArgumentParser, what: str, default: Path | None = None) -> None:
    """--state-file FILE, as `state_file`, and --state, its spelling before."""
    parser.add_argument("--state-file", type=Path, default=default, metavar="FILE", help=what)
    add_renamed(parser, "--state", "--state-file")


def renamed(prog: str, old: str, new: str) -> None:
    """Say, on stderr, that `old` is spelled `new` now."""
    print(f"{prog}: {old} is now {new}; {old} goes in the next release", file=sys.stderr)


class _Renamed(argparse.Action):
    """A switch's spelling before the one that replaced it (add_renamed)."""

    def __init__(self, option_strings, dest, target: argparse.Action, **kwargs):
        super().__init__(option_strings, target.dest, nargs=target.nargs, const=target.const,
                         type=target.type,  # type: ignore[arg-type]  # the target's, which argparse took
                         choices=target.choices, metavar=target.metavar, default=argparse.SUPPRESS,
                         help=argparse.SUPPRESS)
        self.target = target

    def __call__(self, parser, namespace, values, option_string=None):
        new = max(self.target.option_strings, key=len)
        renamed(parser.prog, option_string or self.option_strings[0], new)
        self.target(parser, namespace, values, new)


def add_renamed(parser: argparse.ArgumentParser, old: str, new: str) -> None:
    """`old`, the spelling switch `new` (added already) had before: taken
    for one release, with a warning, and left out of the help."""
    target = next(action for action in parser._actions if new in action.option_strings)
    parser.add_argument(old, action=_Renamed, target=target)


TARGETS = {
    "production": "production runtime on this host",
    "sandbox": f"the sandbox, {SANDBOX_URL} (thermite sandbox)",
    SSH: f"run it on a server instead, over ssh: ssh://[USER@]HOST[:PORT][/SITE] (default: {URL_VARIABLE} when it "
         "names one), SITE being the site's path there, below /~/ for one in its home",
    GATEWAY: f"a Thermite gateway: https://HOST[:PORT] (default: {URL_VARIABLE} when it names one)",
    "runtime-url": f"advanced: the runtime's REST API at URL, e.g. http://127.0.0.1:{HTTP_PORT}, with --token-file",
    "mock": "the remote page's mock backend, which sends nothing",
}


def add_target(parser: argparse.ArgumentParser, *where: str, default: str | None = None,
               meaning: dict[str, str] | None = None) -> None:
    """Mutually exclusive --production, --sandbox, --url URL and --mock (the
    ones in `where`); without a default one of them is required. `meaning`
    replaces a switch's help where it picks something other than a runtime."""
    group = parser.add_mutually_exclusive_group(required=default is None)
    verb = parser.prog.split()[1] if len(parser.prog.split()) > 1 else ""
    for name in where:
        known = TARGETS[VERBS[verb].reach if name == "url" and verb in VERBS else name]
        text = (meaning or {}).get(name, known) + (" (default)" if name == default else "")
        if name == "url":
            existing = parser._option_string_actions.pop("--url", None)
            if existing is not None:
                parser._actions.remove(existing)
                for action_group in parser._action_groups:
                    if existing in action_group._group_actions:
                        action_group._group_actions.remove(existing)
        if name in ("url", "runtime-url"):
            group.add_argument(f"--{name}", metavar="URL", help=text)
        else:
            group.add_argument(f"--{name}", action="store_true", help=text)


def add_mode(parser: argparse.ArgumentParser, default: str | None = "dark", note: str = "") -> None:
    """Mutually exclusive --dark and --light, as `mode` (None when neither is given)."""
    group = parser.add_mutually_exclusive_group()
    for mode in ("dark", "light"):
        group.add_argument(f"--{mode}", dest="mode", action="store_const", const=mode,
                           help=f"the theme's {mode} mode{note}" + (" (default)" if mode == default else ""))


def add_theme(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--theme", metavar="NAME|FILE",
                        help="the colors: a built-in theme's name or a theme file (default: thermite)")
    add_mode(parser)


# --------------------------------------------------------------------------
# the program


def commit(area: Path = AREA, timeout: float = 5) -> tuple[str, bool] | None:
    """(HEAD, whether the area's tracked files differ from it), or None
    without Git or outside a repository."""
    try:
        argv = ["git", "-C", str(area), "rev-parse", "--verify", "--quiet", "HEAD"]
        _trace.debug("run %s", shlex.join(argv))
        head = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        if head.returncode or not head.stdout.strip():
            return None
        argv = ["git", "-C", str(area), "status", "--porcelain", "--untracked-files=no", "--", "."]
        _trace.debug("run %s", shlex.join(argv))
        status = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return head.stdout.strip(), status.returncode == 0 and bool(status.stdout.strip())


def version_line(area: Path = AREA) -> str:
    """`thermite --version`: the version, and the commit it runs from when Git can tell."""
    from oh_paths import RELEASE_FILE
    if (PROGRAM_ROOT / RELEASE_FILE).is_file():
        from oh_release import manifest
        release = manifest(PROGRAM_ROOT)
        return f"{PROGRAM} {VERSION} (release {release['digest'][:12]})"
    found = commit(area)
    if not found:
        return f"{PROGRAM} {VERSION}"
    return f"{PROGRAM} {VERSION} ({found[0][:12]}{', modified' if found[1] else ''})"


def overview(verbs: Sequence[str] | None = None) -> str:
    """`thermite --help`, listing `verbs` (default: every one)."""
    verbs = list(VERBS) if verbs is None else verbs
    width = max((len(verb) for verb in verbs), default=0) + 4
    lines = [
        f"usage: {PROGRAM} VERB [ACTION] [SWITCHES]",
        "",
        "thermite controls a house from one house file: it generates the runtime's",
        "configuration, tests it on a test runtime, installs it, and drives the",
        "remotes. One program, a verb for each job; `thermite help VERB` explains one.",
    ]
    for group, heading in GROUPS:
        lines += ["", f"{heading}:"]
        lines += [f"  {verb:<{width}}{spec.summary}" for verb, spec in VERBS.items() if spec.group == group and verb in verbs]
    lines += ["", "switches that mean the same in every verb that has them:", *_rows(STANDARD_SWITCHES)]
    lines += ["", "switches every verb takes, before it or after it:",
              *_rows([(", ".join(options), text) for options, _, text in GLOBAL_SWITCHES]),
              *_rows([("-V, --version", f"print {PROGRAM}'s version (before any verb)")])]
    lines += ["", "exit status, in every verb (its help names any other):",
              *[f"  {code}  {what}" for code, what in EXIT_STATUS]]
    lines += ["", "examples:", *_rows(OVERVIEW_EXAMPLES)]
    return "\n".join(lines) + "\n"


def usage_error(message: str) -> int:
    print(f"{PROGRAM}: {message} (`{PROGRAM} --help` lists the verbs)", file=sys.stderr)
    return 2


def refuse_url(verb: str, url: str) -> int:
    """Exit status 2, and why `thermite VERB --url URL` cannot reach URL."""
    reach = VERBS[verb].reach
    if reach == LOCAL:
        why = (f"{verb} works on this computer and takes no --url; on a server, run "
               f"`ssh HOST {PROGRAM} {verb}` there")
    elif reach == SSH:
        why = (f"{verb} reads the server's own state, which no gateway serves: name the server as "
               f"--url ssh://[USER@]HOST[:PORT][/SITE], not {url}")
    else:
        why = f"{verb} talks to a Thermite gateway: name it as --url https://HOST[:PORT], not {url}"
    print(f"{PROGRAM} {verb}: {why}", file=sys.stderr)
    return 2


#: The switches that pick where a verb acts (add_target and the remotes').
TARGET_SWITCHES = ("--production", "--sandbox", "--url", "--runtime-url", "--mock", "--native")
#: Runs on the server as its operator's login shell would not: ssh runs a
#: command without the login profile, where ~/.local/bin (thermite install
#: puts the owner command there) is often not on PATH.
# Unset there: a THERMITE_URL in the server's own environment would send
# the command on again.
REMOTE_SHELL = f'unset {URL_VARIABLE}; PATH="$HOME/.local/bin:$PATH"; export PATH; exec thermite "$@"'


def _switches(words: Sequence[str]) -> list[str]:
    """The words before a lone --: the ones that may be switches."""
    words = list(words)
    return words[:words.index("--")] if "--" in words else words


def named_url(words: Sequence[str]) -> tuple[str | None, list[str]]:
    """(the URL --url names in words, or None; words without it)."""
    words, url, out, index = list(words), None, [], 0
    end = len(_switches(words))
    while index < len(words):
        word = words[index]
        if index < end and word == "--url" and index + 1 < end:
            url, index = words[index + 1], index + 2
            continue
        if index < end and word.startswith("--url="):
            url, index = word.split("=", 1)[1], index + 1
            continue
        out.append(word)
        index += 1
    return url, out


def ssh_target(url: str) -> tuple[list[str], str | None]:
    """(ssh's switches and destination, the site's path on the server or
    None) for ssh://[USER@]HOST[:PORT][/SITE]. A path below /~/ is the
    server's home directory's (where ssh starts); any other is absolute."""
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (parts.scheme != "ssh" or not parts.hostname or parts.password or parts.query or parts.fragment
            or port == -1 or parts.hostname.startswith("-") or (parts.username or "").startswith("-")):
        raise ValueError(f"--url {url} is not ssh://[USER@]HOST[:PORT][/SITE]")
    destination = f"{parts.username}@{parts.hostname}" if parts.username else parts.hostname
    path = urllib.parse.unquote(parts.path)
    site = None if path in ("", "/") else path[3:] if path.startswith("/~/") else path
    return [*(["-p", str(port)] if port else []), destination], site or None


def over_ssh(verb: str, words: Sequence[str], url: str, stdout: IO[bytes] | None = None) -> int:
    """Run `thermite VERB WORDS` on the server url names, over ssh, with
    its output (or with `stdout`, its standard output there) and exit
    status as this command's own."""
    try:
        target, site = ssh_target(url)
    except ValueError as exc:
        return usage_error(str(exc))
    remote = [verb, *(["--config", site] if site else []), *words]
    terminal = stdout is None and sys.stdin.isatty() and sys.stdout.isatty()
    argv = ["ssh", "-t" if terminal else "-T", *target[:-1], "--", target[-1],
            shlex.join(["sh", "-c", REMOTE_SHELL, PROGRAM, *remote])]
    _trace.debug("run %s", shlex.join(argv))
    sys.stdout.flush()
    try:
        status = subprocess.call(argv, stdout=stdout)
    except FileNotFoundError:
        print(f"{PROGRAM} {verb}: --url {url} needs ssh, and there is none on PATH", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    if status == 255:
        # ssh's own failure: the server was not reached, or refused the login.
        print(f"{PROGRAM} {verb}: ssh could not run it on {target[-1]} (exit status 255)", file=sys.stderr)
        return 2
    return status


#: Verbs whose --url named the runtime's REST API before --runtime-url did:
#: for one release, a --url that is not ssh:// is still taken as that.
RUNTIME_URL_BEFORE = frozenset({"cameras"})


def named_server() -> str | None:
    """THERMITE_URL as a gateway verb's default --url: an https://... one as
    it is, and the host an ssh://[USER@]HOST... one names, whose gateway is
    on its usual port; None without one."""
    value = os.environ.get(URL_VARIABLE, "").strip()
    if not value.startswith("ssh://"):
        return value or None
    try:
        target, _ = ssh_target(value)
    except ValueError:
        return None
    return target[-1].rpartition("@")[2]


def reached(verb: str, words: Sequence[str]) -> tuple[int | None, list[str]]:
    """(the exit status of `thermite VERB WORDS` where --url, or
    THERMITE_URL, sends it to another server, else None; the words to run
    it with here). A URL that its reach cannot use is refused."""
    import oh_paths
    words = list(words)
    switches = _switches(words)
    if "-h" in switches or "--help" in switches:
        return None, words
    retired = verb not in VERBS
    reach = (RETIRED[verb] if retired else VERBS[verb]).reach
    url, rest = named_url(words)
    explicit_config = (oh_paths.explicit_config()
                       or any(word in oh_paths.SELECTORS
                              or word.startswith(tuple(selector + "=" for selector in oh_paths.SELECTORS))
                              for word in switches))
    if explicit_config and reach == SSH and url is not None and url.startswith("ssh://"):
        print(f"{PROGRAM} {verb}: --config selects a local site and cannot be combined with --url {url}; "
              "name the remote site in the URL path instead (ssh://[USER@]HOST[:PORT]/SITE)", file=sys.stderr)
        return 2, words
    if retired:
        # Unchanged for its last release: only ssh:// is new to it.
        return (over_ssh(verb, rest, url) if url and url.startswith("ssh://") and reach == SSH else None), words
    if (url is None and reach == SSH and not explicit_config
            and not any(word.split("=", 1)[0] in TARGET_SWITCHES for word in switches)):
        variable = os.environ.get(URL_VARIABLE, "").strip()
        if variable.startswith("ssh://"):
            url = variable
    if url is None:
        return None, words
    if reach == SSH and verb in RUNTIME_URL_BEFORE and not url.startswith("ssh://"):
        print(f"{PROGRAM} {verb}: --url names a server now, and --runtime-url the runtime's REST API; --url {url} "
              "is taken as --runtime-url until the next release", file=sys.stderr)
        end = len(switches)
        return None, [("--runtime-url" if word == "--url" and index < end else
                       "--runtime-url=" + word.split("=", 1)[1] if word.startswith("--url=") and index < end else word)
                      for index, word in enumerate(words)]
    if reach == LOCAL or (reach == SSH) != url.startswith("ssh://"):
        return refuse_url(verb, url), words
    if verb == "bugreport":
        from oh_cli import bugreport
        return bugreport.fetch(rest, url), words
    return (over_ssh(verb, rest, url) if reach == SSH else None), words

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    saved = (sys.stdout, dataclasses.replace(switches), os.environ.get("NO_COLOR"))
    try:
        return _main(argv)
    finally:
        # Only for a caller in this process (the tests): bin/thermite exits.
        if sys.stdout is not saved[0]:
            sys.stdout = saved[0]
        for dest in _GLOBAL_DESTS:
            setattr(switches, dest, getattr(saved[1], dest))
        if saved[2] is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = saved[2]


def _main(argv: list[str]) -> int:
    pulled = []
    while argv and is_global(argv[0]):
        pulled.append(argv.pop(0))
    if argv[:1] in (["-V"], ["--version"]):
        print(version_line())
        return 0
    if not argv:
        sys.stderr.write(overview())
        return 2
    first = argv[0]
    if first in ("-h", "--help") or argv == ["help"]:
        sys.stdout.write(overview())
        return 0
    if first == "help":
        return _main([*argv[1:], *pulled, "--help"])
    if first.startswith("-"):
        return usage_error(f"{first} goes after a verb: {PROGRAM} VERB {first}")
    if first not in VERBS and first not in RETIRED:
        close = difflib.get_close_matches(first, list(VERBS), n=1)
        return usage_error(f"no verb {first!r}" + (f"; did you mean {close[0]}?" if close else ""))
    if first in RETIRED:
        print(f"{PROGRAM} {first}: {RETIRED[first].instead} replaces it; {PROGRAM} {first} goes in the next release",
              file=sys.stderr)
    elsewhere, words = reached(first, [*pulled, *argv[1:]])
    if elsewhere is not None:
        return elsewhere
    global _site_check
    # Checked once the verb's switches parse (Parser.parse_args), so that its
    # help, and a wrong switch, need no compatible site.
    _site_check = (VERBS.get(first) or RETIRED[first]).site
    try:
        return importlib.import_module(module_name(first)).main(words)
    except Exception as exc:
        inventory = sys.modules.get("oh_inventory")
        if inventory is None or not isinstance(exc, inventory.ConfigError):
            raise
        for line in exc.lines():
            print(f"{PROGRAM} {first}: {line}", file=sys.stderr)
        return 2
    finally:
        _site_check = False


def run(program: str, argv: list[str]) -> int:
    """bin/thermite's entry point, as `program` (its file name) with `argv`."""
    global _whole_process
    _whole_process = True
    try:
        return main(argv)
    except BrokenPipeError:
        # The reader stopped early (thermite logs | head), having read what
        # it wanted: no traceback, and none at exit for the unflushed rest.
        with contextlib.suppress(OSError, ValueError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
