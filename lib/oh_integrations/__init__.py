"""Integrations: one module per way openHAB talks to a kind of device.

Each module owns everything about its integration and provides:

    NAME      the name devices.yaml lists under controls.<PHASE>
    PHASE     the controls phase it implements ("observe" or "control")
    FILE      its output file, relative to conf/
    HEADER    the comment lines that open that file
    ADDONS    the add-ons it needs (oh_conf.Addon)
    ASSETS    {path under conf/: file beside the module} to copy, often {}
    validate(device) -> list[str]
              problems with this integration's fields in one device; called
              for every device, so a field is checked wherever it appears
    render(device, house, out)
              adds one device's Things and Items to out (an oh_conf.YamlFile),
              and, once, any Thing its devices share, such as a bridge

Optional provider hooks preserve dormant integrations and validate gateways:

    NEEDS_IP = False
              its endpoint is an explicit transport rather than the target
              device; all ordinary providers still require the device IP
    addons(devices) -> Addon sequence
              conditional add-ons; otherwise static ADDONS is unchanged
    validate_house(data) -> list[str]
              cross-device wiring/dependency validation before rendering
    transport_addresses(device) -> {exactThingUID: [IPv4, ...]}
              validated endpoint authority for that provider's own Things
              only; unrelated Things retain their inventory address checks

A module that switches a matrix also provides

    ports(device) -> (inputs, outputs)
    switch_item(device, output) -> str
              the Number Item holding the input routed to that output; it
              is commanded with an input number
    COMHEADS  all it ever sends, named as its stand-in from
              lib/oh_matrix_sim.SIMS[NAME] records it
    thing_uid(device), url(address)
              the Thing's UID and the URL of a matrix at HOST[:PORT]
              (bin/thermite test routing points the Thing at the stand-in),
              or, for a matrix that a rule of its own talks to,
    aim(files, device, address) -> files
              the rendered files with the rule talking to the matrix at
              HOST:PORT instead

and an HDMI matrix that can present a chosen EDID on each input

    EDID      {name: code}
    edid_item(device, input) -> str
              the Number Item holding the input's EDID code; it is
              commanded with a code
    copy_edid(output) -> str
              the EDID name that copies the display on that output

so house-wide features (oh_features.routing) can drive any matrix without
knowing its protocol. A module that switches an HDMI switch in front of one
display (a room's hdmi_switch in the routing) provides, instead of ports()
and switch_item(),

    INPUTS    the input numbers the display can be shown, in the switch's own
              numbering
    input_item(device) -> str
              the Number Item holding the input shown; it is commanded with
              an input number

and COMHEADS, thing_uid() and url() as a matrix does; a room's hdmi_switch
may also be one output of a matrix. A module that
powers a room's receiver or display and sets its input (routing.rooms.<room>
receiver and display) provides

    stage(device, zone) -> {"power": Item, "input": Item, "inputs": [...]}
              the Switch Item commanded ON and OFF, and read back, of one
              zone (None: the device's only or main one), the Item
              commanded with an input and read back, and the inputs it
              takes; None for a zone the device lacks. routing.js sends
              these only to a device that lists the module under control.
              A zone that can play whatever the main zone plays adds
              "follows": the input that does so

A module whose fields the routing lifecycle's ordered executor drives
(oh_features.routing.lifecycle_io) provides

    fields(device) -> {Item: declaration}
              each field of a device that lists the module under control,
              declared as a dict of
        kind      "http": the generic HTTP actor writes and reads it
                  through its Thing's channel; any other kind: the
                  module's own rule drives it (rule_path() below)
        values    the values it can be commanded with
        thing, channel
                  its Thing's UID and channel id (http)
        ack       what acknowledges a write (http): "result", an answer of
                  {"result": 1}; "readback", only a read showing the value
        settle    optional {value: ms}: how long an accepted write of the
                  value may take to show in a read, and a transition toward
                  it may last (at most 300000)
        manual    optional: how many Items the device's actor takes manual
                  commands on, when more than its fields
              and any other key its own rule reads (zone, point); every
              key but manual reaches the actor, values as `allowed`
    rule_path(device) -> str
              for any kind but http: its rule's path under conf/, which
              its files() renders with the line
              `const ROUTING_EXECUTOR = null;` once; the lifecycle fills it
              in with the actor's configuration

A module whose
device has fixed settings also provides

    STATUS    the status comheads whose answers report them
    plan(device, status) -> [(what changes, request body), ...]
              for bin/thermite matrix-settings, which reads STATUS, prints the
              plan and sends the bodies only when asked to

An observe module may also provide

    CONTROL = True
              it implements the control phase too: a device that lists NAME
              under both observe and control gets Items that send commands;
              one that lists it under observe only stays display-only

and a module with a rule of its own, which talks to its devices itself or
does what their binding cannot, provides

    files(devices, house) -> {path under conf/: text}
              files rendered from all the devices it renders at once, such
              as its rule under automation/js/

Facts about a vendor's devices live in its module, never in a generic one:
the generic code reads these optional attributes through REGISTRY
(declared() for the tuples), so no table outside this package names an
integration.

    DEVICE_KEYS   device fields it reads (validate() checks them); the house
                  file refuses any device key no module or the inventory
                  core (oh_inventory) declares
    HOUSE_KEYS    top-level house-file keys it reads, likewise
    NEEDS_MAC = True
                  it derives a device's identity from its mac, so a device
                  listing it needs one
    HOUSE_ITEMS   Items it renders that belong to the house, not a device
    ITEM_ONLY_RULES
                  rules under automation/js/ it renders that open no
                  connection, so hold no RULE_SENDS (oh_render.read_only)
    REMOTE_LAYOUT the remote page's layout for its devices, unless it
                  provides remote_of(device)
    READBACK_INPUTS
                  every input value its routed input Items read back; the
                  remote's route diagnosis may report these
    PORT, PROBE_PORTS
                  the device's TCP ports metrics probes besides its Things'
                  (PROBE_PORTS for an integration with several, or whose
                  Things name none)
    PROBED = False
                  metrics never probes its devices: it has no fixed port,
                  or a connection costs the device a session or a TLS
                  handshake
    SESSIONS = True
                  it keeps a session or subscription open with each device:
                  the sandbox, which still reads the other devices, addresses
                  its Things nowhere (oh_render.unaddressed) and leaves the
                  sessions to the runtime
    PROBLEM_CLEAR_STATE
                  the state its <id>_problem Item holds for "nothing wrong"
    ROUTE_OPTIONAL = True
                  its Things and ports never gate a routed source's health
    DISCOVER_PORTS, DISCOVER_MDNS
                  TCP ports and mDNS service names or labels that mark a
                  host on the LAN as AV gear (bin/thermite discover)
    HANDLER_POOL, HANDLER_POOL_WHY
                  the openHAB thingHandler threads its binding needs while
                  it blocks them, and why; oh_features.threadpools raises
                  the pool when any device lists such a module

A module renders exactly the devices that list NAME under controls.<PHASE>.
Adding an integration is one module, one line in REGISTRY, and its test.
Modules whose names start with an underscore are helpers shared by
integrations, not integrations.
"""

from __future__ import annotations

from . import (androidtv_googletv, avpro_telnet, bk808_cec, bk808_http, cast, denonmarantz_http, drhdmi_http, globalcache_ir, heos, network_ping, pyatv_http, sonos,
               sony_adcp, sony_ip_control, sony_rest, ukm404_http, unifi_protect, vrroom_http, wiim_http)

#: Implemented integrations, in render order; a room page lists each device's
#: points in this order.
REGISTRY = (network_ping, sony_rest, sony_ip_control, cast, androidtv_googletv, pyatv_http, sonos, denonmarantz_http, heos,
            wiim_http, sony_adcp, bk808_http, ukm404_http, vrroom_http, avpro_telnet, unifi_protect, drhdmi_http, globalcache_ir, bk808_cec)

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
        if getattr(module, "CONTROL", False):
            names["control"].add(module.NAME)
    return names


def declared(attribute: str, modules=REGISTRY) -> tuple:
    """Every value of the optional tuple `attribute` across `modules`, in
    their order, without repeats."""
    return tuple(dict.fromkeys(value for module in modules for value in getattr(module, attribute, ())))
