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
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from oh_paths import PROGRAM as PROGRAM_ROOT, SITE as AREA
from oh_release import VERSION
BIN = AREA / "bin"
PROGRAM = "thermite"
#: thermite's own version; the commit (`thermite --version`) tells builds apart.
SANDBOX_URL = "http://127.0.0.1:18080"
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

#: verb: (group, summary). The summaries are what `thermite --help` lists.
VERBS = {
    "init": ("house", "create a private site, or pin an installed application upgrade"),
    "generate": ("house", "write conf/ from the house file, and deploy/'s systemd units"),
    "validate": ("house", "check the house file, and that conf/ agrees with it"),
    "lint": ("house", "refuse credentials in Git, and docs naming a missing make target"),
    "test": ("test", "boot conf/ on a test runtime; test routing and the controls end to end"),
    "sandbox": ("test", "run a thermite sandbox from a copy of conf/ on 127.0.0.1:18080"),
    "install": ("production", "install thermite on this host, asking before each sudo command"),
    "upgrade": ("production", "upgrade thermite and the Apple TV helper here to the pinned versions"),
    "uninstall": ("production", "stop thermite here and remove its units; --purge deletes its state"),
    "deploy": ("production", "install a commit's conf/ and Apple TV helper here"),
    "rollback": ("production", "deploy the commit deployed before the current one, or another"),
    "status": ("production", "what is deployed and running here, against this checkout"),
    "history": ("production", "this host's deploys, numbered, the latest last"),
    "health": ("production", "read-only: every Thing ONLINE in the runtime"),
    "discover": ("devices", "scan the LAN read-only and compare it with the house file"),
    "pair": ("devices", "pair a TV with runtime, or check the Sony TVs' pairing"),
    "cameras": ("devices", "read camera status, or configure native Protect bridges privately"),
    "apple-tv": ("devices", "run the Apple TV helper runtime calls"),
    "ir-profile": ("devices", "validate verified IR keys or print an iTach packet without transmitting"),
    "remote": ("use", "the house remote in a terminal"),
    "access": ("use", "site login, local identity bootstrap, HTTPS service and room policies"),
    "theme": ("use", "the flaming bowl mark and color themes: show, check, export"),
    "docs": ("use", "build the docs site, or export it to the thermite repository"),
    "version": ("program", "thermite's version, and runtime's: pinned, and running here"),
    "package": ("program", "build a privacy-checked portable application wheel"),
    "completion": ("program", "print a completion script for bash, zsh or fish"),
}

#: Verbs for one house's own tools and migration (its old controller, its
#: network, its matrices' settings); the public docs leave them out.
HOUSE_ONLY = frozenset({"dhcp-reservations", "matrix-settings", "control4"})

#: Old names that still run a verb, listed nowhere. The installed helper
#: unit from before apple-tv runs `bin/thermite atv serve`; this goes once
#: the owner installs the regenerated deploy/openhab-atv.container.
ALIASES = {"atv": "apple-tv"}

#: A quadlet installed before bin/thermite runs the Apple TV helper as
#: /app/bin/openhab-atv; deploys keep a copy of bin/thermite there while it
#: does (oh_deploy.LEGACY_ATV_PROGRAM), and this maps that name to its verb.
LEGACY = {"openhab-atv": "apple-tv"}

#: Verbs that are gone, and the command that does their job now. They only
#: name it: running one runs nothing.
RENAMED = {
    "render": "generate",
    "deploy-render": "generate",
    "inventory-check": "validate",
    "secret-scan": "lint",
    "doc-targets": "lint",
    "boot-check": "test boot",
    "routing-check": "test routing",
    "av-check": "test controls",
    "smoke": "health",
    "reservations": "dhcp-reservations",
    "sony-psk": "pair",
    "control4-export": "control4",
    "brand": "theme",
    "site": "docs",
}
#: The same for actions that are gone: (verb, action): the command now.
RENAMED_ACTIONS = {
    ("deploy", "apply"): "deploy",
    ("deploy", "status"): "status",
    ("sandbox", "sync"): "sandbox reload",
    ("sandbox", "pair"): "pair --sandbox",
    ("control4", "summarize"): "control4 summary",
}

#: Switches that mean the same in every verb that has them (the add_*
#: helpers), as `thermite --help` explains them.
STANDARD_SWITCHES = (
    ("--site DIR", "select a private site (also THERMITE_SITE; before or after the verb)"),
    ("--json", "print the report as JSON"),
    ("-n, --dry-run", "report what it would do; change nothing"),
    ("--check", "change nothing; exit 1 if what it writes is stale"),
    ("--apply", "send changes to live devices (without it: only show them)"),
    ("--timeout SECONDS", "how long to wait"),
    ("--house FILE", "the house file (default: instance/devices.yaml)"),
    ("--production | --sandbox | --url URL", "which runtime: this host's, the sandbox's, or one at URL"),
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
    ("thermite deploy --dry-run", "what a deploy would change here"),
    ("thermite upgrade -n", "what moving to the pinned runtime would run here"),
    ("thermite remote --mock", "the remote in a terminal, sending nothing"),
    ("thermite theme show", "the flaming bowl, in this terminal's colors"),
    ("thermite help sandbox", "one verb's actions, switches and examples"),
    ("thermite completion bash", "tab completion for bash (thermite help completion)"),
)

Examples = Sequence[tuple[str, str]]


def module_name(verb: str) -> str:
    """oh_cli.dhcp_reservations for dhcp-reservations."""
    if verb not in VERBS:
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


def trace(what: str) -> None:
    """With -v: something thermite does outside itself, on stderr."""
    if switches.verbose:
        print(f"{PROGRAM}: {what}", file=sys.stderr, flush=True)


def trace_run(argv: Sequence[object], cwd: Path | str | None = None) -> None:
    trace("run " + shlex.join(str(part) for part in argv) + (f"   (in {cwd})" if cwd else ""))


def trace_http(method: str, url: str) -> None:
    trace(f"{method} {url}")


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

    def parse_args(self, args=None, namespace=None):
        if Parser.capturing:
            raise Captured(self)
        namespace = super().parse_args(args, namespace)
        _apply(namespace)
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
    if verb not in VERBS:
        raise ValueError(f"{verb} is not a {PROGRAM} verb")
    parser = Parser(prog=f"{PROGRAM} {verb}", description=description.rstrip("\n"),
                    epilog=epilog(examples, notes), formatter_class=Help)
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


TARGETS = {
    "production": "production runtime on this host",
    "sandbox": f"the sandbox, {SANDBOX_URL} (thermite sandbox)",
    "url": "runtime at URL, e.g. http://thermite.example:8080",
    "mock": "the remote page's mock backend, which sends nothing",
}


def add_target(parser: argparse.ArgumentParser, *where: str, default: str | None = None,
               meaning: dict[str, str] | None = None) -> None:
    """Mutually exclusive --production, --sandbox, --url URL and --mock (the
    ones in `where`); without a default one of them is required. `meaning`
    replaces a switch's help where it picks something other than a runtime."""
    group = parser.add_mutually_exclusive_group(required=default is None)
    for name in where:
        text = (meaning or {}).get(name, TARGETS[name]) + (" (default)" if name == default else "")
        if name == "url":
            group.add_argument("--url", metavar="URL", help=text)
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
        trace_run(argv)
        head = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        if head.returncode or not head.stdout.strip():
            return None
        argv = ["git", "-C", str(area), "status", "--porcelain", "--untracked-files=no", "--", "."]
        trace_run(argv)
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
    width = max(len(verb) for verb in VERBS) + 4
    lines = [
        f"usage: {PROGRAM} VERB [ACTION] [SWITCHES]",
        "",
        "thermite controls a house from one house file: it generates the runtime's",
        "configuration, tests it on a test runtime, installs it, and drives the",
        "remotes. One program, a verb for each job; `thermite help VERB` explains one.",
    ]
    for group, heading in GROUPS:
        lines += ["", f"{heading}:"]
        lines += [f"  {verb:<{width}}{summary}" for verb, (g, summary) in VERBS.items() if g == group and verb in verbs]
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
    first = ALIASES.get(first, first)
    if first in RENAMED:
        return usage_error(f"no verb {first!r}; it is now `{PROGRAM} {RENAMED[first]}`")
    if len(argv) > 1 and (first, argv[1]) in RENAMED_ACTIONS:
        return usage_error(f"no `{PROGRAM} {first} {argv[1]}`; it is now "
                           f"`{PROGRAM} {RENAMED_ACTIONS[first, argv[1]]}`")
    if first not in VERBS:
        close = difflib.get_close_matches(first, list(VERBS), n=1)
        return usage_error(f"no verb {first!r}" + (f"; did you mean {close[0]}?" if close else ""))
    if first not in {"init", "package", "theme", "docs", "completion", "version"} and not any(arg in {"-h", "--help"} for arg in argv):
        import oh_paths
        try:
            oh_paths.require_compatible()
        except ValueError as exc:
            return usage_error(str(exc))
    return importlib.import_module(module_name(first)).main([*pulled, *argv[1:]])


def run(program: str, argv: list[str]) -> int:
    """bin/thermite's entry point, as `program` (its file name) with `argv`."""
    global _whole_process
    _whole_process = True
    return main([LEGACY[program], *argv] if program in LEGACY else argv)
