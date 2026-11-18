"""The record a script integration keeps of every command it takes, and the
one vocabulary it is kept in.

A rule that sends a device its commands (_device.js commandRule, and the
routing lifecycle actor that takes them in its place) ends each command with
one record: a log line, the device's problem Item and a tally.

    command device=<id> item=<Item> command=<value|<private>|?>
      outcome=<OUTCOMES> code=<CODES or DROPS|-> ms=<duration>

at INFO for a command the device took (applied, sent) and at WARN for any
other. `<id>_problem` (String, in the device's group) then holds the code of
CODES for a command the device did not take, and UNDEF (CLEAR) again after
the next one it takes; a script that polls its device sets it for a read
that fails as well. Its only states are the codes below, each with the fixed
words its state description gives the clients: no script writes free text to
it. `<id>_<integration>_commands` (String, in no group, read by the metrics
exporter alone) holds the tally:

    {"version": 1, "outcomes": {"<outcome>": {"count": n, "ms": total}, ...},
     "reads": "ok" or a code of CODES}

counted since the script loaded. One Item for each script that commands the
device, since each script context counts alone: a receiver's HEOS transport
rule and its routing actor are two.

`reads` is there while the script reads the device (an integration with
READS): what its last read met. A command's problem stays in `<id>_problem`
until the device takes a command, however long ago its cause went, so that
Item says what a person last ran into, not whether the device answers now.
`reads` does: metrics build the device's health check on it and on nothing
a command left behind. It is absent, not `ok`, while the script asks the
device nothing (a TV in standby), so that no reading outlives its read.
"""

from __future__ import annotations

from oh_conf import YamlFile, options_text

#: Why openHAB cannot command, or read, a device: the states of
#: `<id>_problem`, with the words a client shows after the device's label.
#: What a script can tell apart, no finer: `unreachable` is no connection
#: (refused, no route, its Thing or helper offline), `no_answer` a request
#: that was sent and brought no usable answer in time or a connection the
#: device closed, `not_ready` a device that answers but is off, in standby
#: or in a state the command needs changed first.
CODES = {
    "unreachable": "cannot be reached",
    "no_answer": "does not answer",
    "refused": "refused the command",
    "busy": "is busy",
    "not_ready": "is not ready for that command",
    "auth": "is not paired or refuses openHAB",
    "protocol": "answered in a way openHAB does not understand",
    "other": "has a problem",
}
#: The codes of a device that answered: it declined, or cannot do that now.
#: They say nothing of whether it can be reached and used, so a read that
#: meets one leaves the metrics' reading of that as it was while reads went
#: well (thermite_device_read_ok).
ANSWERED = ("refused", "busy", "not_ready")
#: Why a rule dropped a command itself, which says nothing of the device and
#: so never reaches its problem Item: a value the Item does not take, a
#: read-only copy (oh_render.read_only), or routing holding the device.
DROPS = ("invalid", "read_only", "held")
#: How a command ended. `applied`: the device acknowledged it. `sent`: handed
#: to a transport that has no acknowledgement (an IR code, a binding's
#: handler). `dropped`: the rule sent nothing, for a reason of DROPS.
#: `unsent`: nothing reached the device. `refused` and `busy`: the device
#: answered no, or not now. `unknown`: sent, and its effect is not known.
OUTCOMES = ("applied", "sent", "dropped", "unsent", "refused", "busy", "unknown")
#: The state of a problem Item while nothing is wrong.
CLEAR = "UNDEF"
#: The constant of _device.js that table() fills (_io.device()).
TABLE = "COMMANDS"


def problem_item(ident: str) -> str:
    return f"{ident}_problem"


def tally_item(ident: str, integration: str) -> str:
    """The tally Item of the script `integration` runs for the device."""
    return f"{ident}_{integration}_commands"


def table(integration: str) -> dict:
    """TABLE in the copy of _device.js `integration`'s script embeds: the
    vocabulary, and the suffix of the tally Items it posts."""
    return {"problems": list(CODES), "drops": list(DROPS), "outcomes": list(OUTCOMES),
            "tally": f"{integration}_commands"}


def problem(device: dict) -> str | None:
    """The device's problem Item when a script integration commands it (its
    problem() hook), else None."""
    from . import defining  # the registry imports the modules that import this
    return next((name for module in defining("problem") if (name := module.problem(device))), None)


def items(device: dict, integration: str) -> dict[str, dict]:
    """The problem Item and `integration`'s tally Item of a device its script
    commands. The problem Item takes no commands and carries its words; the
    tally is in no group, so no page, feed or check lists it."""
    ident = device["id"]
    return {
        problem_item(ident): {
            "type": "String", "label": "Problem", "icon": "error", "groups": [ident], "tags": ["Status"],
            "metadata": {"stateDescription": {"config": {"readOnly": True, "options": options_text(CODES.items())}}}},
        tally_item(ident, integration): {
            "type": "String", "label": "Command outcomes", "autoupdate": False,
            "metadata": {"stateDescription": {"config": {"readOnly": True}}}},
    }


def render(device: dict, out: YamlFile, integration: str) -> None:
    """Add them to the integration's file. Two script integrations of one
    device share its problem Item, which the first of them defines (in
    problem()'s order)."""
    from . import defining
    first = next((module.NAME for module in defining("problem") if module.problem(device)), None)
    for name, item in items(device, integration).items():
        if name != problem_item(device["id"]) or first == integration:
            out.items[name] = item


def tallies(devices: list[dict], present) -> dict[str, dict]:
    """{tally Item: {"device", "integration"}} of every tally Item among
    `present`, for the metrics exporter's map."""
    from . import REGISTRY
    return {name: {"device": device["id"], "integration": module.NAME}
            for device in devices for module in REGISTRY
            if (name := tally_item(device["id"], module.NAME)) in present}
