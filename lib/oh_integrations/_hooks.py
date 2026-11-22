"""The contract of an integration: every name generic code may read from an
integration module (a hook), what kind of thing it is, what it is for, and
what a module that does not define it reads as.

An integration module defines the required hooks and whichever others it
has something to say for. Generic code reads a hook as an attribute of the
module, `module.stage`, defined or not: each module of the registry answers
a name it lacks through absent(), with the hook's default, so nothing
tests for a hook with hasattr() or carries a default of its own. A name
that is not in HOOKS is no hook: reading it from a module that lacks it is
an UndeclaredHook error, never a quiet None, and a module that defines a
name nothing else uses fails tests/test_hooks.py. So a hook is declared
here before anything can read it, and a misspelt one cannot hide.

Where a device lists two integrations that both answer a hook only one can
answer (which gives it its remote, say), the hook's `order` says who is
asked first (oh_integrations.defining()). The registry's own order is the
order things are rendered in, and decides nothing else.

What an integration keeps for itself (its tables, its helpers, what its own
tools and tests read) is not a hook: it is read from the module by whoever
imports that module by name, which generic code does not.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from oh_shape import Key

CONSTANT, FUNCTION, KEYS = "constant", "function", "keys"
#: What a hook of each kind is in a module that defines it. Another kind of
#: entry is one more line here, and its check. KEYS is a declaration of
#: house-file keys: {key: oh_shape.Key}, the key's kind of value and what
#: it is for.
KINDS = {CONSTANT: lambda value: not callable(value), FUNCTION: callable,
         KEYS: lambda value: isinstance(value, dict) and all(
             isinstance(name, str) and isinstance(key, Key) for name, key in value.items())}


class UndeclaredHook(Exception):
    """A name read from an integration module that neither the module nor
    HOOKS defines. Not an AttributeError: hasattr() and getattr() with a
    default would turn that into "this integration has none"."""


@dataclass(frozen=True)
class Hook:
    name: str
    kind: str
    #: What it is for, and what its value or answer holds.
    doc: str
    #: What a module that does not define it reads as.
    default: object = None
    #: Every integration defines it.
    required: bool = False
    #: For a hook one integration answers per device: the names of the
    #: integrations that define it, in the order they are asked
    #: (oh_integrations.defining()). None where every integration that
    #: defines it has its say, in render order.
    order: tuple[str, ...] | None = None
    #: The hooks a module that defines this one defines too: a reader that
    #: found this one reads those without asking.
    needs: tuple[str, ...] = ()

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: {self.kind!r} is not a kind of hook")
        object.__setattr__(self, "doc", inspect.cleandoc(self.doc))


_TABLE = (
    # What every integration is.
    Hook("NAME", CONSTANT, "the name devices.yaml lists under controls.<PHASE>", required=True),
    Hook("PHASE", CONSTANT, 'the controls phase it implements ("observe" or "control")', required=True),
    Hook("FILE", CONSTANT, "its output file, relative to conf/", required=True),
    Hook("HEADER", CONSTANT, "the comment lines that open that file", required=True),
    Hook("ADDONS", CONSTANT, "the add-ons it needs (oh_conf.Addon)", required=True),
    Hook("ASSETS", CONSTANT, "{path under conf/: file beside the module} to copy, often {}", required=True),
    Hook("validate", FUNCTION, """
        validate(device) -> list[str]: problems with this integration's
        fields in one device; called for every device, so a field is checked
        wherever it appears""", required=True),
    Hook("render", FUNCTION, """
        render(device, house, out): adds one device's Things and Items to
        out (an oh_conf.YamlFile), and, once, any Thing its devices share,
        such as a bridge. A module renders exactly the devices that list
        NAME under controls.<PHASE>""", required=True),

    # Hooks that preserve dormant integrations and validate gateways.
    Hook("NEEDS_IP", CONSTANT, """
        False: its endpoint is an explicit transport rather than the target
        device; every other integration requires the device's own ip""", default=True),
    Hook("addons", FUNCTION, """
        addons(devices) -> Addon sequence: conditional add-ons; otherwise
        static ADDONS is unchanged"""),
    Hook("validate_house", FUNCTION, """
        validate_house(data) -> list[str]: cross-device wiring and
        dependency validation before rendering; it runs last, on a house
        whose every device and section (routing, say) has passed its own
        validation"""),
    Hook("transport_addresses", FUNCTION, """
        transport_addresses(device) -> {exactThingUID: [IPv4, ...]}:
        validated endpoint authority for that integration's own Things
        only; unrelated Things retain their inventory address checks"""),
    Hook("powered_by", FUNCTION, """
        powered_by(device) -> {Item: power Item}: the power Item each of
        its readings needs on, for a device with more than one (a
        receiver's zones); a device with one power Item has every reading
        follow it without this (oh_integrations.powered()). Metrics tell a
        reading with no value while that power reads OFF as the device
        being off (thermite_item_device_off), not unknown, and the activity
        feed leaves it to the power's own line"""),
    Hook("relayed", FUNCTION, """
        relayed(device) -> bool: whether the gateway's callback relay
        (oh_access.native_boundary) receives this device's UPnP events;
        oh_render points jUPnP at the relay when the house has any"""),

    # A module that switches a matrix, so house-wide features
    # (oh_features.routing) can drive any matrix without knowing its protocol.
    Hook("ports", FUNCTION, "ports(device) -> (inputs, outputs) of a matrix"),
    Hook("switch_item", FUNCTION, """
        switch_item(device, output) -> str: the Number Item holding the
        input routed to that output of a matrix; it is commanded with an
        input number""", order=("bk808_http", "ukm404_http", "avpro_telnet"), needs=("ports", "COMHEADS", "POLL_S")),
    Hook("POLL_S", CONSTANT, """
        seconds between the reads of a matrix's or an HDMI switch's Items
        (switch_item(), input_item()): routing.js holds the input a room's
        Item last read through one read that finds nothing, for at most
        oh_features.routing.MISSED of these"""),
    Hook("COMHEADS", CONSTANT, """
        all a matrix or switch integration ever sends, named as its
        stand-in from lib/oh_matrix_sim.SIMS[NAME] records it"""),
    Hook("SETUP_COMHEADS", CONSTANT, """
        those of COMHEADS that set up a connection and move no port
        (bin/thermite test routing counts every other comhead but a get as
        a write)""", default=(), needs=("COMHEADS",)),
    Hook("thing_uid", FUNCTION, """
        thing_uid(device) -> the UID of the device's Thing: with url(), a
        matrix's or a switch's that bin/thermite test routing points at the
        stand-in"""),
    Hook("url", FUNCTION, "url(address) -> the URL of a matrix at HOST[:PORT] (thing_uid())", needs=("thing_uid",)),
    Hook("aim", FUNCTION, """
        aim(files, device, address) -> files: the rendered files with the
        integration's own rule, or its Thing, talking to the device at
        HOST:PORT instead (bin/thermite test routing and controls point it
        at a stand-in)"""),

    # An HDMI matrix that can present a chosen EDID on each input.
    Hook("EDID", CONSTANT, "{name: code} of the EDIDs a matrix can present", needs=("edid_item", "copy_edid")),
    Hook("edid_item", FUNCTION, """
        edid_item(device, input) -> str: the Number Item holding the
        input's EDID code; it is commanded with a code""", needs=("EDID",)),
    Hook("copy_edid", FUNCTION, "copy_edid(output) -> str: the EDID name that copies the display on that output",
         needs=("EDID",)),

    # An HDMI matrix whose outputs can each scale the picture their own way.
    Hook("SCALER", CONSTANT, "{name: code} of what an output's scaler can do", needs=("scaler_item",)),
    Hook("scaler_item", FUNCTION, """
        scaler_item(device, output) -> str: the Number Item holding the
        output's scaler code; it is commanded with a code""", needs=("SCALER",)),

    # An HDMI matrix that reports whether something is cabled to each output.
    Hook("hotplug_item", FUNCTION, """
        hotplug_item(device, output) -> str: the Switch Item that reads,
        at every read of the matrix, whether that output finds a display's
        hot-plug line raised (ON) or not (OFF), UNDEF while the matrix
        cannot be read (oh_integrations._hotplug). routing.js shows the
        Source of a room on that output as unknown, and why, while it keeps
        finding none with the room's devices on""", needs=("switch_item",)),

    # A module that switches an HDMI switch in front of one display (a
    # room's hdmi_switch in the routing) provides these instead of ports()
    # and switch_item(), with COMHEADS and thing_uid() and url(), or aim(),
    # as a matrix does; a room's hdmi_switch may also be one output of a
    # matrix.
    Hook("INPUTS", CONSTANT, """
        the input numbers the display behind an HDMI switch can be shown,
        in the switch's own numbering. Read only from the integration that
        answers input_item(): others keep their own inputs under the name"""),
    Hook("input_item", FUNCTION, """
        input_item(device) -> str: the Number Item holding the input an
        HDMI switch shows; it is commanded with an input number""",
         order=("vrroom_telnet",), needs=("INPUTS", "COMHEADS", "POLL_S")),

    # A module that powers a room's receiver or display and sets its input
    # (routing.rooms.<room> receiver and display).
    Hook("stage", FUNCTION, """
        stage(device, zone) -> {"power": Item, "input": Item, "inputs":
        [...]}: the Switch Item commanded ON and OFF, and read back, of one
        zone (None: the device's only or main one), the Item commanded with
        an input and read back, and the inputs it takes; None for a zone
        the device lacks. routing.js sends these only to a device that
        lists the module under control. A zone that can play whatever the
        main zone plays adds "follows": the input that does so""",
         order=("sony_ip_control", "denonmarantz_http", "sony_adcp"), needs=("READ_BACK",)),
    Hook("READ_BACK", CONSTANT, """
        seconds from a command to the reading that shows it: routing.js
        holds a room's Source at least that long for its receiver or
        display (oh_features.routing.hold_ms), and bin/thermite test
        controls waits that long for a read-back"""),
    Hook("READBACK_INPUTS", CONSTANT, """
        every input value its routed input Items read back; the remote's
        route diagnosis may report these""", default=()),
    Hook("wake_item", FUNCTION, """
        wake_item(device) -> the Item ON wakes a source with when a room
        chooses it (oh_features.routing), or None""", order=("androidtv_googletv", "pyatv_http")),

    # A module whose fields the routing lifecycle's ordered executor drives
    # (oh_features.routing.lifecycle_io).
    Hook("fields", FUNCTION, """
        fields(device) -> {Item: declaration}: each field of a device that
        lists the module under control, declared as a dict of
          kind      "http": the generic HTTP actor writes and reads it
                    through its Thing's channel; any other kind: the
                    module's own rule drives it (rule_path())
          values    the values it can be commanded with
          thing, channel
                    its Thing's UID and channel id (http)
          ack       what acknowledges a write (http): "result", an answer
                    of {"result": 1}; "readback", only a read showing the
                    value
          settle    optional {value: ms}: how long an accepted write of the
                    value may take to show in a read, and a transition
                    toward it may last (at most 300000)
          independent
                    optional, True when a command to it can change no other
                    field of the device (a matrix output a command names
                    alone): its unknown outcome quarantines it alone, until
                    a read of it answers; otherwise the whole device waits
          manual    optional: how many Items the device's actor takes
                    manual commands on, when more than its fields
        and any other key its own rule reads (zone, point); every key but
        manual reaches the actor, values as `allowed`""",
         order=("sony_ip_control", "pyatv_http", "denonmarantz_http", "sony_adcp", "bk808_http", "ukm404_http",
                "vrroom_telnet", "avpro_telnet")),
    Hook("rule_path", FUNCTION, """
        rule_path(device) -> str: for a field of any kind but http, its
        rule's path under conf/, which its files() renders with the line
        `const ROUTING_EXECUTOR = null;` once; the lifecycle fills it in
        with the actor's configuration""", needs=("files",)),

    # A module whose device has fixed settings.
    Hook("STATUS", CONSTANT, """
        the status comheads whose answers report a device's fixed settings.
        Read only from the integration that answers plan(): others keep
        their own status requests under the name"""),
    Hook("plan", FUNCTION, """
        plan(device, status, routed_scalers) -> [(what changes, request
        body), ...]: for bin/thermite matrix-settings, which reads STATUS,
        prints the plan and sends the bodies only when asked to. It leaves
        alone the scaler of the outputs in routed_scalers, which the routing
        sets (oh_features.routing.routed_scalers())""", order=("bk808_http",), needs=("STATUS",)),

    Hook("CONTROL", CONSTANT, """
        True: an observe module implements the control phase too. A device
        that lists NAME under both observe and control gets Items that send
        commands; one that lists it under observe only stays display-only""", default=False),

    # A module with a rule of its own, which talks to its devices itself or
    # does what their binding cannot.
    Hook("files", FUNCTION, """
        files(devices, house) -> {path under conf/: text}: files rendered
        from all the devices it renders at once, such as its rule under
        automation/js/ (_script.rule() renders one from its source, and
        _script.per_device() one for each device)"""),
    Hook("RULE", CONSTANT, "its rule's path under conf/, or the path its devices' rules are named after"),
    Hook("NEEDS", CONSTANT, """
        the integration modules whose Things and Items its rule needs
        beside its own: bin/thermite test controls keeps their files""", default=()),

    # A module whose rule takes its devices' commands (_device.js
    # commandRule) keeps the record of each one (_commands): its render()
    # adds the record's Items (_commands.render) and its script embeds the
    # shared device part under its NAME (_io.embed, _io.shared).
    Hook("problem", FUNCTION, """
        problem(device) -> the device's problem Item
        (_commands.problem_item) when its rule commands the device, else
        None: the remote page and the metrics show the code it holds. Two
        script integrations of one device share the Item, which the first
        of them in this order defines""",
         order=("sony_ip_control", "androidtv_googletv", "pyatv_http", "sonos", "heos", "sony_adcp", "vrroom_telnet",
                "avpro_telnet", "ir", "bk808_cec")),

    # A control module that can restart its devices without cutting their
    # power, for nightly reboots (oh_features.reboots).
    Hook("reboot", FUNCTION, """
        reboot(device) -> {"item": the Item that restarts the device when
        commanded "reboot", "answers": Items with a value while it answers,
        "things": Things ONLINE while it answers (optional), "signs": more
        Things that go offline while it restarts (optional), "idle": the
        readings that all hold while nothing uses it, each {"item", "is"},
        or {"item", "field", "is", "maxAge"} for a field of a diagnostics
        snapshot, with "unreadIsIdle": True where a missing snapshot is a
        hang that the restart cures; "rooms": True (optional) for an HDMI
        matrix that reports nothing of its use; "hung" (optional):
        {"things": Things whose OFFLINE while it answers is a hang that a
        restart cures, "seconds": how long they stay so first}, for
        `reboot: when_hung`. oh_features.reboots adds, for a USB matrix,
        that each room whose ports it switches is Off, and with "rooms",
        that each room showing one of its outputs is Off}""",
         order=("heos", "ukm404_http", "vrroom_telnet", "avpro_telnet")),

    # A module whose Items tell what its devices play, for what is playing
    # (oh_features.playing).
    Hook("playing", FUNCTION, """
        playing(device) -> {"power": the Item whose OFF means nothing plays
        (optional), "apps": Items naming the app, each {"item", "names":
        {value: app name} (optional), "idle": values that mean nothing
        plays (optional), "package": True where any other value is an
        Android package (optional), "when" (optional, as a title's)},
        "titles": Items of the title, each {"title", "artist", "series",
        "season", "episode" (each but title optional), "when": {"item",
        "is": [values]} while it holds (optional)}}"""),
    Hook("group", FUNCTION, """
        group(device) -> {"id": the String Item of the player's own group
        ID, "leader": the String Item of the group ID of the leader it
        follows, empty while it follows none}, or None: a player that
        follows another in a multiroom group plays what its leader plays,
        whatever it reports itself (oh_metrics: its playing state and what
        it plays are its leader's)""", order=("wiim_http",)),

    # A control module whose devices Music Assistant plays on (oh_music).
    Hook("music", FUNCTION, """
        music(device) -> {"provider": the Music Assistant player provider
        that drives it, and what matches the device to that provider's
        player: "udn" (the player's ID), or "mac" and "ip" (the MAC it
        reports, else its address)}, or None for a device it does not
        control; the contract adds the device's address for Music
        Assistant's discovery (oh_site_contract.music)""", order=("sonos", "wiim_http")),

    # A control module gives the remote page what it drives, and may give an
    # audio device controls of its own (Listen on the remote; the gateway's
    # device route, oh_identity.runtime, commands what both declare).
    Hook("remote", FUNCTION, """
        remote(device) -> {"power", "mute": Switch Items, "volume":
        {"item", "min", "max", "step"} on the device's own scale, "input":
        {"item", "options": [[value, label], ...]}, "key": the String Item
        a remote button id (KEYS, or keys(device)) is sent to; each
        optional} or None""",
         order=("sony_ip_control", "androidtv_googletv", "pyatv_http", "sonos", "denonmarantz_http", "wiim_http",
                "sony_adcp", "ir", "bk808_cec")),
    Hook("REMOTE_LAYOUT", CONSTANT, """
        the remote page's layout for its devices (remotes.js), unless it
        provides remote_of(device)"""),
    Hook("remote_of", FUNCTION, "remote_of(device) -> the layout of this device's remote, where its devices' differ"),
    Hook("remote_availability", FUNCTION, """
        remote_availability(device) -> {"keyButtons": the buttons that
        work, "note": what the page says of the rest}, for a remote that
        sends keys and reads nothing back, or None; a device without a
        remote() shows the layout of the first that answers""",
         order=("ir", "bk808_cec"), needs=("remote_of",)),
    Hook("KEYS", CONSTANT, "the remote button ids its key Item takes, unless it provides keys(device)"),
    Hook("keys", FUNCTION, "keys(device) -> the remote button ids this device's key Item takes, where its devices' differ"),
    Hook("command_keys", FUNCTION, """
        command_keys(device) -> the button ids the gateway lets through to
        the key Item, where another integration's rule takes some of them
        too (a receiver's HEOS transport keys); else keys() or KEYS"""),
    Hook("audio", FUNCTION, """
        audio(device) -> what its page adds to remote(), or None (each key
        optional):
          transport  button ids of its keys the page shows as transport
                     (previous, play_pause, next, tv)
          state      {"item", "playing": [values while it plays], "silent"
                     (optional): {"item", "is": [values]} while which it
                     plays nothing unless it names a track, and before
                     whose first reading what it plays is not known: a
                     soundbar's TV input, which reads as playing with the
                     TV silent}
          track      {"title", "artist", "album", "service" (Item names,
                     free text), "duration" (seconds), "sample_rate" (Hz),
                     "bit_depth" (bits): plain Number Items}
          settings   [{"item", "label", "section": "playback", "sound" or
                     "device", "kind": "switch", "enum" with "options":
                     [[value, label], ...] it is sent and "reads"
                     (optional) it may only show, or "number" with "min",
                     "max", "step" and "unit" (optional), "when" (optional,
                     display only): {"item", "is": [values]} while it
                     applies}], each commanded and read back
          facts      [{"item", "label", "unit" (optional), "quiet"
                     (optional): true for a reading that drifts with every
                     poll, such as a temperature, which the activity feed
                     leaves out}], read only
        A page shows a setting or fact only while its Item has a value:
        what a model lacks reads as nothing. The feed leaves out a track's
        details beyond its title and artist""", order=("sonos", "wiim_http")),
    Hook("diagnostics", FUNCTION, """
        diagnostics(device) -> {"items": the snapshot Items it reads,
        "readings": what each holds}, the read-only signal diagnostics the
        remote page shows for the device (oh_features.remote_diagnostics),
        or None. Every enabled provider contributes; no provider hides another.
        A snapshot is {version:1, observedAt:controller acquisition time in ms,
        values:{...}}; invalid replies use status:'unavailable' and a bounded
        reason code. A reading's path and selector.path are relative to that
        entire envelope. selector.node names the expected input/output on the
        path, and a missing selector is unknown association, never a match.
        kind distinguishes format, link, capability, setting and metadata.
        unknown, noSignal and silence list distinct provider sentinel values.
        transports limits a reading to the named physical transport(s).
        scalar reads an existing Item without inventing acquisition time: its
        age and route association remain unknown. Reading an Item over REST
        is not a new hardware measurement""", order=("denonmarantz_http", "bk808_http", "vrroom_telnet",
                                                      "vrroom_http", "drhdmi_http", "sony_adcp", "avpro_telnet",
                                                      "sonos", "wiim_http")),
    Hook("native_power_observation", FUNCTION, """
        native_power_observation(device) -> {"item": its power Item,
        "reported_at": the Item holding when the device itself reported
        it}, or None: the room policy verifies such a power only by a
        native reading (oh_room_policy.plan)"""),

    # Facts about a vendor's devices live in its module, never in a generic
    # one, so no table outside this package names an integration.
    Hook("DEVICE_KEYS", KEYS, """
        {key: oh_shape.Key} of the device fields it reads: each one's kind
        of value and what it is for, which the house-file reference page
        and schema are made from (validate() checks the values, in its own
        words); the house file refuses any device key no module or the
        inventory core (oh_inventory) declares""", default=()),
    Hook("HOUSE_KEYS", KEYS, "{key: oh_shape.Key} of the top-level house-file keys it reads, likewise", default=()),
    Hook("NEEDS_MAC", CONSTANT, """
        True: it derives a device's identity from its mac, so a device
        listing it needs one""", default=False),
    Hook("HOUSE_ITEMS", CONSTANT, "Items it renders that belong to the house, not a device", default=()),
    Hook("ITEM_ONLY_RULES", CONSTANT, """
        rules under automation/js/ it renders that open no connection, so
        hold no RULE_SENDS (oh_render.read_only)""", default=()),
    Hook("SESSIONS", CONSTANT, """
        True: it keeps a session or subscription open with each device. The
        sandbox, which still reads the other devices, addresses its Things
        nowhere (oh_render.unaddressed) and leaves the sessions to the
        runtime""", default=False),
    Hook("DISCOVER_PORTS", CONSTANT, """
        TCP ports that mark a host on the LAN as AV gear (bin/thermite
        discover)""", default=()),
    Hook("DISCOVER_MDNS", CONSTANT, "mDNS service names or labels that do (DISCOVER_PORTS)", default=()),
    Hook("HANDLER_POOL", CONSTANT, """
        the openHAB thingHandler threads its binding needs while it blocks
        them; oh_features.threadpools raises the pool when any device lists
        such a module""", needs=("HANDLER_POOL_WHY",)),
    Hook("HANDLER_POOL_WHY", CONSTANT, "why (HANDLER_POOL): the words the generated file gives",
         needs=("HANDLER_POOL",)),

    # What metrics read of a device (oh_metrics.signals()).
    Hook("PORT", CONSTANT, "the device's TCP port metrics probe besides its Things'"),
    Hook("PROBE_PORTS", CONSTANT, """
        its TCP ports metrics probe, for an integration with several, or
        whose Things name none""", default=()),
    Hook("PROBED", CONSTANT, """
        False: metrics never probe its devices: it has no fixed port, or a
        connection costs the device a session or a TLS handshake""", default=True),
    Hook("READS", CONSTANT, """
        True: its rule reads its devices itself and says what each read met
        (_device.js health and problem.read). Metrics keep that as
        thermite_device_read_ok, the reading that says whether such a
        device answers, and a health check. A rule that asks a device
        nothing for a while says so (problem.unread), and the reading is
        unknown meanwhile""", default=False),
    Hook("transport", FUNCTION, """
        transport(device) -> Thing UID or None: the Thing its rule sends
        the device's commands through, where that Thing is named after
        another device (a source's CEC goes through the matrix): its status
        is the device's own signal (oh_metrics.signals()), and a check"""),
    Hook("THING_FOLLOWS_DEVICE", CONSTANT, """
        False: its Things' status says nothing of the device (a ping Thing
        is ONLINE while nothing answers). Metrics leave them out of
        thermite_thing_online, and no check reads them""", default=True),
    Hook("ROUTE_OPTIONAL", CONSTANT, "True: its Things and ports never gate a routed source's health", default=False),
)
HOOKS = {hook.name: hook for hook in _TABLE}
if len(HOOKS) != len(_TABLE):
    raise ValueError("a hook is declared twice")


def absent(name: str) -> object:
    """What an integration module answers for a name it does not define:
    the hook's default. The registry makes this each module's __getattr__."""
    if name.startswith("_"):
        # Python's own lookups (__path__, __wrapped__) and a module's private names.
        raise AttributeError(name)
    hook = HOOKS.get(name)
    if hook is None:
        raise UndeclaredHook(f"{name} is not a hook of an integration: declare it in oh_integrations._hooks.HOOKS, "
                             "or read it from the module that defines it")
    if hook.required:
        raise AttributeError(f"an integration must define {name}")
    return hook.default

