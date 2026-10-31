"""Print a shell completion script for thermite.

    thermite completion bash|zsh|fish
"""

from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

import oh_cli

SHELLS = ("bash", "zsh", "fish")

DESCRIPTION = """\
Print a script that teaches bash, zsh or fish to complete thermite's
command lines: every verb, each verb's actions, each verb's and action's
switches, the choices a switch or an argument takes, and file and directory
names where a switch takes a FILE or a DIR. It is built from the verbs' own
parsers, so it completes what this thermite has. Install it again after an
update that adds verbs or switches.
"""

NOTES = """\
installing it:
  bash  for this shell: source <(thermite completion bash)
        for every new one: thermite completion bash >
          ~/.local/share/bash-completion/completions/thermite
  zsh   save it as _thermite in a directory on $fpath, before compinit runs:
          thermite completion zsh > ~/.zfunc/_thermite
        with fpath+=(~/.zfunc) above compinit in ~/.zshrc
  fish  thermite completion fish > ~/.config/fish/completions/thermite.fish

  Each completes the command `thermite`; put bin/thermite on $PATH under that
  name (a symlink will do).
"""

EXAMPLES = (
    ("thermite completion bash", "the bash script; source <(thermite completion bash) uses it now"),
    ("thermite completion zsh > ~/.zfunc/_thermite", "zsh's, where compinit finds it"),
    ("thermite completion fish > ~/.config/fish/completions/thermite.fish", "fish's"),
)


@dataclasses.dataclass
class Option:
    names: tuple[str, ...]
    help: str
    valued: bool
    #: "files", "dirs", a tuple of choices, or None for free text
    values: object = None
    metavar: str = ""


@dataclasses.dataclass
class Positional:
    name: str
    values: object
    many: bool
    optional: bool


@dataclasses.dataclass
class Command:
    options: list[Option]
    positionals: list[Positional]
    #: name: (summary, its Command)
    actions: dict[str, tuple[str, "Command"]]


def _values(action: argparse.Action) -> object:
    if action.choices and not isinstance(action, argparse._SubParsersAction):
        return tuple(str(choice) for choice in action.choices)
    metavar = action.metavar if isinstance(action.metavar, str) else ""
    if "DIR" in metavar:
        return "dirs"
    if "FILE" in metavar or action.type is Path:
        return "files"
    return None


def describe(parser: argparse.ArgumentParser) -> Command:
    """What completes after a parser's command words."""
    formatter = parser._get_formatter()
    command = Command([], [], {})
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                command.actions[choice.dest] = (choice.help or "", describe(action.choices[choice.dest]))
            continue
        if action.help is argparse.SUPPRESS:
            continue
        text = " ".join(formatter._expand_help(action).split()) if action.help else ""
        metavar = action.metavar if isinstance(action.metavar, str) else action.dest.upper()
        if action.option_strings:
            command.options.append(Option(tuple(action.option_strings), text, action.nargs != 0,
                                          _values(action), metavar))
        else:
            command.positionals.append(Positional(metavar, _values(action), action.nargs in ("+", "*"),
                                                  action.nargs in ("?", "*")))
    return command


def verbs() -> dict[str, tuple[str, Command]]:
    """Every verb thermite runs under its own name: no alias and no removed name."""
    return {verb: (summary, describe(oh_cli.parser_of(verb))) for verb, (_, summary) in oh_cli.VERBS.items()}


TOP = Command([Option(("-h", "--help"), "show the verbs, and exit", False),
               Option(("-V", "--version"), "print thermite's version, and exit", False),
               *(Option(options, text, False) for options, _, text in oh_cli.GLOBAL_SWITCHES)], [], {})
HELP = "a verb's help: thermite help VERB [ACTION]"


def contexts(table: dict[str, tuple[str, Command]]) -> list[tuple[str, Command]]:
    """("verb" or "verb action", its Command) for every verb and action."""
    out = []
    for verb, (_, command) in table.items():
        out.append((verb, command))
        out += [(f"{verb} {name}", sub) for name, (_, sub) in command.actions.items()]
    return out


def _sq(text: str) -> str:
    """text in single quotes, for bash or zsh."""
    return "'" + text.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# bash


def _bash_offer(values: object) -> str:
    if values in ("files", "dirs"):
        return "@" + values
    return " ".join(values) if isinstance(values, tuple) else ""


def _case(pattern: str, words: str) -> str:
    return f"        {pattern}) printf '%s' {_sq(words)} ;;"


BASH_MAIN = r'''
_thermite_offer() {
    case $1 in
        '') ;;
        @files) compopt -o filenames 2>/dev/null; COMPREPLY=($(compgen -f -- "$cur")) ;;
        @dirs) compopt -o filenames 2>/dev/null; COMPREPLY=($(compgen -d -- "$cur")) ;;
        *) COMPREPLY=($(compgen -W "$1" -- "$cur")) ;;
    esac
}

_thermite() {
    local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]}
    local verb='' action='' context='' skip='' word i
    local -a args=()
    for ((i = 1; i < COMP_CWORD; i++)); do
        word=${COMP_WORDS[i]}
        if [[ -n $skip ]]; then skip=''; continue; fi
        if [[ $word == -* ]]; then
            [[ " $(_thermite_valued "$context") " == *" $word "* ]] && skip=1
            continue
        fi
        if [[ -z $verb ]]; then
            verb=$word context=$word
        elif [[ -z $action && ${#args[@]} -eq 0 && " $(_thermite_actions "$verb") " == *" $word "* ]]; then
            action=$word context="$verb $word"
        else
            args+=("$word")
        fi
    done
    COMPREPLY=()
    if [[ -n $skip ]]; then
        _thermite_offer "$(_thermite_value "$context|$prev")"
    elif [[ $cur == -* ]]; then
        COMPREPLY=($(compgen -W "$(_thermite_switches "$context")" -- "$cur"))
    elif [[ -z $verb ]]; then
        COMPREPLY=($(compgen -W "$_thermite_verbs help" -- "$cur"))
    elif [[ $verb == help ]]; then
        case ${#args[@]} in
            0) COMPREPLY=($(compgen -W "$_thermite_verbs" -- "$cur")) ;;
            1) COMPREPLY=($(compgen -W "$(_thermite_actions "${args[0]}")" -- "$cur")) ;;
        esac
    elif [[ -z $action && ${#args[@]} -eq 0 && -n $(_thermite_actions "$verb") ]]; then
        COMPREPLY=($(compgen -W "$(_thermite_actions "$verb")" -- "$cur"))
    else
        _thermite_offer "$(_thermite_positional "$context|${#args[@]}")"
    fi
}

complete -F _thermite thermite
'''


def bash(table: dict[str, tuple[str, Command]]) -> str:
    actions = [_case(_sq(verb), " ".join(command.actions)) for verb, (_, command) in table.items()
               if command.actions]
    switches = [_case("''", " ".join(name for option in TOP.options for name in option.names))]
    valued, values, positionals = [], [], []
    for context, command in contexts(table):
        switches.append(_case(_sq(context), " ".join(name for option in command.options for name in option.names)))
        names = [name for option in command.options if option.valued for name in option.names]
        if names:
            valued.append(_case(_sq(context), " ".join(names)))
        for option in command.options:
            offer = _bash_offer(option.values)
            if option.valued and offer:
                values += [_case(_sq(f"{context}|{name}"), offer) for name in option.names]
        for index, positional in enumerate(command.positionals):
            offer = _bash_offer(positional.values)
            if offer:
                pattern = _sq(f"{context}|") + "*" if positional.many else _sq(f"{context}|{index}")
                positionals.append(_case(pattern, offer))
            if positional.many:
                break
    lines = [
        f"# bash completion for thermite {oh_cli.VERSION}, from `thermite completion bash`.",
        "# For this shell: source <(thermite completion bash); for every new one, save it as",
        "# ~/.local/share/bash-completion/completions/thermite.",
        "",
        f"_thermite_verbs={_sq(' '.join(table))}",
    ]
    for name, what, cases in (
            ("actions", "The actions of verb $1.", actions),
            ("switches", "The switches of $1: '', a verb, or 'verb action'.", switches),
            ("valued", "The switches of $1 that take a value.", valued),
            ("value", "What the value of switch $2 of $1 is, asked as '$1|$2': words, @files or @dirs.", values),
            ("positional", "What argument $2 (from 0) after $1 is, asked as '$1|$2': words, @files or @dirs.",
             positionals)):
        lines += ["", f"# {what}", f"_thermite_{name}() {{", "    case $1 in", *cases, "    esac", "}"]
    return "\n".join(lines) + "\n" + BASH_MAIN


# --------------------------------------------------------------------------
# zsh


def _zesc(text: str) -> str:
    """Text inside an _arguments spec's [...], or a _describe entry's description."""
    return text.replace("\\", "\\\\").replace("[", "(").replace("]", ")").replace(":", "\\:")


def _zaction(values: object) -> str:
    if values == "files":
        return "_files"
    if values == "dirs":
        return "_files -/"
    if isinstance(values, tuple):
        return "(" + " ".join(values) + ")"
    return " "


def _zfunction(context: str) -> str:
    return "_thermite_" + re.sub(r"[^a-z0-9]+", "_", context)


def _zspecs(command: Command) -> list[str]:
    specs = []
    for option in command.options:
        value = f":{_zesc(option.metavar)}:{_zaction(option.values)}" if option.valued else ""
        body = f"[{_zesc(option.help)}]{value}"
        if len(option.names) > 1:
            specs.append(_sq("(" + " ".join(option.names) + ")") + "{" + ",".join(option.names) + "}" + _sq(body))
        else:
            specs.append(_sq(option.names[0] + body))
    if not command.actions:
        for positional in command.positionals:
            prefix = "*:" if positional.many else ("::" if positional.optional else ":")
            specs.append(_sq(f"{prefix}{_zesc(positional.name)}:{_zaction(positional.values)}"))
    return specs


def _zfun(context: str, command: Command) -> list[str]:
    specs = [f"    {spec} \\" for spec in _zspecs(command)]
    if not command.actions:
        return [f"{_zfunction(context)}() {{", "  _arguments -s -S \\", *specs, "    && return 0", "}", ""]
    entries = " ".join(_sq(f"{name}:{_zesc(summary)}") for name, (summary, _) in command.actions.items())
    return [f"{_zfunction(context)}() {{", "  local curcontext=$curcontext state line", "  local -a actions",
            "  _arguments -C -s -S \\", *specs, "    '1: :->action' '*:: :->args' && return 0", "  case $state in",
            f"    action) actions=({entries}); _describe -t actions {_sq(f'thermite {context} action')} actions ;;",
            "    args)", "      case $line[1] in",
            *(f"        {name}) {_zfunction(context + ' ' + name)} ;;" for name in command.actions),
            "      esac ;;", "  esac", "}", ""]


def zsh(table: dict[str, tuple[str, Command]]) -> str:
    lines = ["#compdef thermite",
             f"# zsh completion for thermite {oh_cli.VERSION}, from `thermite completion zsh`. Save it as",
             "# _thermite in a directory on $fpath, before compinit runs.", ""]
    for context, command in contexts(table):
        lines += _zfun(context, command)
    verbs_ = " ".join(_sq(f"{verb}:{_zesc(summary)}") for verb, (summary, _) in table.items())
    lines += ["_thermite() {", "  local curcontext=$curcontext state line", "  local -a verbs",
              f"  verbs=({verbs_} {_sq('help:' + _zesc(HELP))})",
              "  _arguments -C -s \\", *(f"    {spec} \\" for spec in _zspecs(TOP)),
              "    '1: :->verb' '*:: :->args' && return 0", "  case $state in",
              "    verb) _describe -t verbs 'thermite verb' verbs ;;", "    args)", "      case $line[1] in",
              "        help) _describe -t verbs 'thermite verb' verbs ;;",
              *(f"        {verb}) {_zfunction(verb)} ;;" for verb in table), "      esac ;;", "  esac", "}", "",
              '_thermite "$@"']
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# fish


def _fq(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _fish(condition: str, rest: str) -> str:
    return f"complete -c thermite -n {_fq(condition)} {rest}"


def _fish_option(condition: str, option: Option) -> str:
    parts = [f"-l {name[2:]}" if name.startswith("--") else f"-s {name[1:]}" for name in option.names]
    if option.valued:
        if option.values == "files":
            parts.append("-r -F")
        elif option.values == "dirs":
            parts.append("-x -a '(__fish_complete_directories (commandline -ct))'")
        elif isinstance(option.values, tuple):
            parts.append(f"-x -a {_fq(' '.join(option.values))}")
        else:
            parts.append("-x")
    return _fish(condition, " ".join([*parts, f"-d {_fq(option.help)}"]))


def _fish_positionals(condition: str, positionals: list[Positional]) -> list[str]:
    out = []
    for positional in positionals:
        if positional.values == "files":
            out.append(_fish(condition, "-F"))
        elif positional.values == "dirs":
            out.append(_fish(condition, "-x -a '(__fish_complete_directories (commandline -ct))'"))
        elif isinstance(positional.values, tuple):
            out.append(_fish(condition, f"-x -a {_fq(' '.join(positional.values))}"))
    return out


FISH_HELPERS = r'''
# The words typed after thermite that are neither switches nor their values.
function __thermite_words
    set -l tokens (commandline -opc)
    set -e tokens[1]
    set -l skip 0
    for token in $tokens
        if test $skip = 1
            set skip 0
            continue
        end
        if string match -q -- '-*' $token
            contains -- $token $__thermite_valued; and set skip 1
            continue
        end
        echo $token
    end
end

# __thermite_using VERB [ACTION]: the words typed start with these.
function __thermite_using
    set -l words (__thermite_words)
    test (count $words) -ge (count $argv); or return 1
    for i in (seq (count $argv))
        test "$words[$i]" = "$argv[$i]"; or return 1
    end
end

# __thermite_needs N: N words typed so far.
function __thermite_needs
    test (count (__thermite_words)) -eq $argv[1]
end

complete -c thermite -f
'''


def fish(table: dict[str, tuple[str, Command]]) -> str:
    valued = sorted({name for _, command in contexts(table) for option in command.options if option.valued
                     for name in option.names})
    lines = [f"# fish completion for thermite {oh_cli.VERSION}, from `thermite completion fish`. Save it as",
             "# ~/.config/fish/completions/thermite.fish.", "",
             f"set -g __thermite_valued {' '.join(valued)}", FISH_HELPERS.rstrip("\n")]
    lines += [_fish_option("__thermite_needs 0", option) for option in TOP.options]
    lines += [_fish("__thermite_needs 0", f"-a {_fq(verb)} -d {_fq(summary)}") for verb, (summary, _) in table.items()]
    lines.append(_fish("__thermite_needs 0", f"-a help -d {_fq(HELP)}"))
    lines.append(_fish("__thermite_using help; and __thermite_needs 1", f"-a {_fq(' '.join(table))}"))
    for verb, (_, command) in table.items():
        using = f"__thermite_using {verb}"
        top = f"{using}; and __thermite_needs 1" if command.actions else using
        lines += [_fish_option(top, option) for option in command.options]
        lines += _fish_positionals(using, command.positionals)
        for name, (summary, sub) in command.actions.items():
            lines.append(_fish(top, f"-a {_fq(name)} -d {_fq(summary)}"))
            lines += [_fish_option(f"{using} {name}", option) for option in sub.options]
            lines += _fish_positionals(f"{using} {name}", sub.positionals)
    return "\n".join(lines) + "\n"


SCRIPTS = {"bash": bash, "zsh": zsh, "fish": fish}


def main(argv: list[str] | None = None) -> int:
    parser = oh_cli.command("completion", DESCRIPTION, EXAMPLES, NOTES)
    parser.add_argument("shell", choices=SHELLS, metavar="SHELL", help="bash, zsh or fish")
    args = parser.parse_args(argv)
    sys.stdout.write(SCRIPTS[args.shell](verbs()))
    return 0
