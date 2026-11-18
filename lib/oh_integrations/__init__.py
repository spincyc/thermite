"""Integrations: one module per way openHAB talks to a kind of device.

Each module owns everything about its integration, and tells the generic
code about it through hooks: the names oh_integrations._hooks.HOOKS
declares, each with what it is for. Every module defines the required ones
(NAME, PHASE, FILE, HEADER, ADDONS, ASSETS, validate(), render()) and any of
the others it has something to say for. The generic code reads a hook as an
attribute of a module of REGISTRY, which answers one it does not define with
the hook's default, and finds the modules that define one with defining();
it imports no integration by name (tests/test_hooks.py holds both the
table and its readers to this).

A module renders exactly the devices that list NAME under controls.<PHASE>.
Adding an integration is one module, one line in REGISTRY, its name in the
`order` of each hook it defines that only one integration answers for a
device, and its test. Modules whose names start with an underscore are
helpers shared by integrations, not integrations.
"""

from __future__ import annotations

from . import _hooks
from . import (androidtv_googletv, avpro_telnet, bk808_cec, bk808_http, cast, denonmarantz_http, drhdmi_http, globalcache_ir, heos, network_ping, pyatv_http, sonos,
               sony_adcp, sony_ip_control, sony_rest, ukm404_http, unifi_protect, vrroom_http, vrroom_telnet, wiim_http)

#: Implemented integrations, in render order; a room page lists each device's
#: points in this order. Which of two a device lists is asked first for a
#: hook only one answers is that hook's `order` (_hooks.HOOKS), not this.
REGISTRY = (network_ping, sony_rest, sony_ip_control, cast, androidtv_googletv, pyatv_http, sonos, denonmarantz_http, heos,
            wiim_http, sony_adcp, bk808_http, ukm404_http, vrroom_http, vrroom_telnet, avpro_telnet, unifi_protect, drhdmi_http, globalcache_ir, bk808_cec)
for _module in REGISTRY:
    _module.__getattr__ = _hooks.absent

PHASES = ("observe", "control")

#: Names devices.yaml may already use for integrations not implemented yet,
#: by phase. They validate but render nothing. A name moves from here to
#: REGISTRY when its module exists.
PLANNED = {
    "observe": frozenset(),
    "control": frozenset({
        "androidtv_shieldtv",
        "adb", "telnet",
        # A contact closure from a relay module (such as a Global Caché
        # iTach IP2CC) replacing a Control4 controller's relay port.
        "relay",
    }),
}


def allowed() -> dict[str, set[str]]:
    """Every integration name devices.yaml may use, per phase."""
    names = {phase: set(PLANNED[phase]) for phase in PHASES}
    for module in REGISTRY:
        names[module.PHASE].add(module.NAME)
        if module.CONTROL:
            names["control"].add(module.NAME)
    return names


def defining(hook: str) -> tuple:
    """The integrations that define `hook`: in the hook's `order` where one
    answers for a device, so the first that does is the one; in render
    order where each has its say."""
    order = _hooks.HOOKS[hook].order
    modules = [module for module in REGISTRY if hook in vars(module)]
    if order is not None:
        modules.sort(key=lambda module: order.index(module.NAME) if module.NAME in order else len(order))
    return tuple(modules)


def powered(device: dict, items: dict[str, dict]) -> dict[str, str]:
    """The power Item each of a device's readings needs on: what its
    integrations' powered_by() declare, else the device's only power Switch.
    items maps the device's Item names to their Items. A power Item, a power
    state and the problem text need nothing on."""
    def power(item: dict) -> bool:
        return "Power" in (item.get("tags") or [])
    switches = [name for name, item in items.items() if power(item) and str(item.get("type")).split(":")[0] == "Switch"]
    declared = {}
    for module in defining("powered_by"):
        if module.NAME in ((device.get("controls") or {}).get(module.PHASE) or []):
            declared.update(module.powered_by(device))
    needs = {}
    for name, item in items.items():
        need = declared.get(name, switches[0] if len(switches) == 1 else None)
        if need in items and not power(item) and not name.endswith("_problem"):
            needs[name] = need
    return needs


def declared(hook: str, modules=REGISTRY) -> tuple:
    """Every value of the tuple `hook` across `modules`, in their order,
    without repeats. The modules may be more than the integrations: a
    feature declares its house keys and Items under the same names, and
    answers nothing for a name it lacks."""
    default = _hooks.HOOKS[hook].default
    return tuple(dict.fromkeys(value for module in modules for value in getattr(module, hook, default)))
