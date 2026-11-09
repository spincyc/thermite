"""Pure compilation of explicit routing paths and their consumption boundary.

The base table is supplied by routing.table(); this module never calls it.
Only fields with an ordered executor enter resources. Missing consumer
predicates widen protection and are exposed in coverage for validation.
"""
from __future__ import annotations

from collections import defaultdict


def active_requirements(data: dict, room: str, choice: str) -> dict[str, str]:
    """The selected path, excluding command-scoped parking and bypassed stages.

    Kept independent of executor support so policy verification and lifecycle
    compilation can consume the same wiring contract.
    """
    from . import (OFF, STAGES, commands, inputs, power, stages, usb_host, wake,
                   _port_item)

    if choice == OFF:
        return {}
    routing = data["routing"]
    entry = routing["rooms"][room]
    devices = {device["id"]: device for device in data["devices"]}
    source = routing["sources"][choice]
    chosen_inputs = inputs(data, room, choice)
    input_items = {item for item, _ in chosen_inputs}
    # A stage the choice does not pass through stays as it is.
    bypassed_power = ({stage["power"] for _, stage in stages(data, room) if stage["input"] not in input_items}
                      - {item for item, _ in wake(data, choice)})
    local_stage = any(choice in (entry.get(key, {}).get("sources") or {}) for key in STAGES)
    local_switch = choice in (entry.get("hdmi_switch", {}).get("inputs") or {})
    usb_items = set()
    participating = set()
    only = (entry.get("usb_only") or {}).get(choice)
    for matrix, ports in (entry.get("usb") or {}).items():
        usb_items.update(_port_item(devices, matrix, port) for port in ports)
        moved = ports if only is None else only.get(matrix, [])
        if usb_host(source, matrix, routing["usb_off_host"]) != routing["usb_off_host"]:
            participating.update(_port_item(devices, matrix, port) for port in moved)
    master_items = {_port_item(devices, routing["hdmi_matrix"], entry[key])
                    for key in ("hdmi_output", "hdmi_audio_output") if key in entry}
    switch_device = entry.get("hdmi_switch", {}).get("device")
    result = {}
    for item, value in power(data, room, choice) + commands(data, room, choice) + chosen_inputs:
        if item in usb_items and item not in participating:
            continue
        if item in master_items and (local_stage or local_switch):
            continue
        if local_stage and switch_device and item.startswith(switch_device + "_"):
            continue
        if item in bypassed_power:
            continue
        result[item] = str(value)
    return result


def compile_descriptor(data: dict, base_table: dict, caps: dict) -> dict:
    """Return a finite lifecycle descriptor without rendering or mutations."""
    from . import (OFF, stages, usb_host, wake, switcher,
                   _port_item, _switch_item)

    routing = data.get("routing")
    if not routing:
        return {"version": 1, "resources": {}, "endpoints": {}, "sources": {}, "consumers": [], "coverage": {}}
    devices = {device["id"]: device for device in data["devices"]}
    sources = routing["sources"]
    resources = {}
    endpoints = {}
    consumers = []
    coverage = {source: {"complete": True, "unsupported": []} for source in sources}
    source_policy = {}
    resource_uses = defaultdict(lambda: defaultdict(set))
    release_values = {}
    exclusive = set()
    stage_power = {stage["power"] for room in routing["rooms"] for _, stage in stages(data, room)}
    source_wakes = {source: wake(data, source) for source in sources}
    # Every power Item a route switches: its stages' and its sources'.
    power_items = stage_power | {item for steps in source_wakes.values() for item, _ in steps}
    # A USB cleanup policy must not replace ordinary source wake controls
    # with a stricter, firmware-dependent power-query contract. Only automatic
    # source standby opts source-only power into that execution authority.
    legacy_power = {item for source, steps in source_wakes.items() if sources[source].get("idle", "preserve") == "preserve"
                    for item, _ in steps} - stage_power

    def supported(item):
        cap = caps.get(item)
        return bool(item not in legacy_power and cap and cap.get("terminal") is True and cap.get("probe") is True)

    def include(item):
        if not supported(item):
            return False
        if item not in resources:
            cap = caps[item]
            resources[item] = {"key": cap["key"], "values": list(map(str, cap["field"]["allowed"])),
                               "capable": True, "exclusive": False, "uses": {}, "requires": {}}
            if "settle" in cap["field"]:
                resources[item]["settleMs"] = dict(cap["field"]["settle"])
            # Optional, as settle: only a declared one changes the descriptor.
            if cap["field"].get("independent") is True:
                resources[item]["independent"] = True
        return True

    def missing(source, items):
        row = coverage[source]
        row["complete"] = False
        row["unsupported"] = sorted(set(row["unsupported"]) | set(items))

    def consumer(source, requirements):
        if not requirements:
            missing(source, ["unresolved_path"])
            source_policy[source]["unresolved"] = True
            return
        for item, value in requirements.items():
            resource_uses[item][str(value)].add(source)
        unsupported = sorted(item for item in requirements if not include(item))
        known = {item: str(value) for item, value in requirements.items() if supported(item)}
        if unsupported:
            missing(source, unsupported)
        if known:
            row = {"source": source, "requirements": known}
            if unsupported:
                row["unresolved"] = unsupported
            if row not in consumers:
                consumers.append(row)
        else:
            # There is no supported predicate that can rule this path out.
            # This is permanent conservative protection, not an empty AND
            # misrepresented as a verified active consumer.
            source_policy[source]["unresolved"] = True

    for source, definition in sources.items():
        wake_steps = source_wakes[source]
        item = wake_steps[0][0] if wake_steps else None
        capable = bool(definition.get("idle", "preserve") == "standby" and item and include(item))
        source_policy[source] = {"power": item if capable else None,
                                 "idle": definition.get("idle", "preserve"), "capable": capable}
        if capable:
            resources[item]["source"] = source
            release_values[item] = "OFF"

    # A source menu limits commands, not physical matrix reachability. An
    # unmodeled output can still be selected at the matrix's front panel.
    matrix_sources = defaultdict(lambda: defaultdict(set))
    matrix_outputs = defaultdict(set)
    matrix_feeds = set()
    device_stages = defaultdict(dict)
    for source, definition in sources.items():
        if "hdmi_input" in definition:
            matrix_sources[routing["hdmi_matrix"]][source].add(definition["hdmi_input"])
    for entry in routing["rooms"].values():
        if "hdmi_output" in entry:
            matrix_outputs[routing["hdmi_matrix"]].update(entry[key] for key in ("hdmi_output", "hdmi_audio_output") if key in entry)
        switch = entry.get("hdmi_switch", {})
        if "output" in switch:
            matrix_outputs[switch["device"]].add(switch["output"])
            for source, input_ in switch["inputs"].items():
                matrix_sources[switch["device"]][source].add(input_)
            if "hdmi_output" in entry and "matrix_input" in switch:
                matrix_feeds.add(switch["device"])
    for matrix, connected in matrix_sources.items():
        count = switcher(devices[matrix]).ports(devices[matrix])[1]
        opaque = set(range(1, count + 1)) - matrix_outputs[matrix]
        if opaque:
            reachable = set(connected)
            if matrix in matrix_feeds:
                reachable.update(matrix_sources.get(routing["hdmi_matrix"], {}))
            for source in reachable:
                missing(source, ["unmanaged_hdmi:" + _port_item(devices, matrix, port) for port in sorted(opaque)])
                source_policy[source]["unresolved"] = True

    # Consumer paths use physical selectors and stage power, never the
    # source's own power. Enumerate physical alternatives independently of
    # the one command path. Video and receiver audio are separate legs.
    for room, entry in routing["rooms"].items():
        raw = base_table["rooms"][room]
        stage_rows = {(spec["device"], spec.get("zone")): stage for spec, stage in stages(data, room)}
        for (device, zone), stage in stage_rows.items():
            device_stages[device][zone] = stage

        def stage_fields(kind, value):
            spec = entry.get(kind, {})
            stage = stage_rows.get((spec.get("device"), spec.get("zone")))
            if stage is None:
                return {}
            return {stage["power"]: "ON", **({stage["input"]: str(value)} if value is not None else {})}

        def feed_fields(kind, source):
            spec = entry.get(kind, {})
            # Overrides name another configured feed connector. The default
            # remains physically reachable, including when this source also
            # has a local connector or is absent from the endpoint's menu.
            values = dict.fromkeys([spec.get("input"), spec.get("input_by_source", {}).get(source)])
            return [stage_fields(kind, value) for value in values if value is not None] or [{}]

        def local_fields(kind, source):
            spec = entry.get(kind, {})
            value = (spec.get("sources") or {}).get(source)
            return [stage_fields(kind, value)] if value is not None else []

        def upstream(source, audio=False):
            choice = sources[source]
            switch = entry.get("hdmi_switch")
            paths = []
            matrix_feed = {}
            if switch:
                local_inputs = (matrix_sources[switch["device"]].get(source, set()) if "output" in switch else
                                [switch["inputs"][source]] if source in switch["inputs"] else [])
                paths.extend({_switch_item(devices, switch): str(input_)} for input_ in sorted(local_inputs))
                if "matrix_input" in switch:
                    matrix_feed[_switch_item(devices, switch)] = str(switch["matrix_input"])
                elif paths:
                    return paths
            output = entry.get("hdmi_audio_output" if audio and "hdmi_audio_output" in entry else "hdmi_output")
            if output is not None and choice.get("hdmi_input") is not None:
                matrix_feed[_port_item(devices, routing["hdmi_matrix"], output)] = str(choice["hdmi_input"])
                paths.append(matrix_feed)
            return paths

        for source in sources:
            receiver_spec = entry.get("receiver", {})
            display_local = local_fields("display", source)
            receiver_local = local_fields("receiver", source)
            displays = feed_fields("display", source)
            receivers = feed_fields("receiver", source)
            receiver_present = (receiver_spec.get("device"), receiver_spec.get("zone")) in stage_rows
            paths = upstream(source)
            for display in display_local:
                consumer(source, display)
            for receiver in receiver_local:
                consumer(source, receiver)
                for display in displays:
                    consumer(source, {**receiver, **display})
            for path in paths:
                for display in displays:
                    for receiver in ([{}] if "hdmi_audio_output" in entry else receivers):
                        consumer(source, {**path, **receiver, **display})
            if receiver_present:
                for path in upstream(source, audio=True):
                    for receiver in receivers:
                        consumer(source, {**path, **receiver})
            if source in entry["sources"] and not (paths or display_local or receiver_local):
                missing(source, ["video_path:" + room])
                source_policy[source]["unresolved"] = True

        all_usb = {}
        for matrix, ports in (entry.get("usb") or {}).items():
            for port in ports:
                item = _port_item(devices, matrix, port)
                all_usb[item] = (matrix, port)
                exclusive.add(item)
                release_values[item] = str(routing["usb_off_host"])
                include(item)
            owned_ports = {port for spec in routing["rooms"].values() for port in (spec.get("usb") or {}).get(matrix, [])}
            opaque_ports = set(range(1, switcher(devices[matrix]).ports(devices[matrix])[1] + 1)) - owned_ports
            for source, definition in sources.items():
                host = usb_host(definition, matrix, routing["usb_off_host"])
                if host == routing["usb_off_host"]:
                    continue
                for port in ports:
                    consumer(source, {_port_item(devices, matrix, port): str(host)})
                if opaque_ports:
                    missing(source, ["unmanaged_usb:" + _port_item(devices, matrix, port) for port in sorted(opaque_ports)])
                    source_policy[source]["unresolved"] = True

        choices = {}
        for choice, boundary_commands in raw["choices"].items():
            active = active_requirements(data, room, choice)
            deactivation = {}
            release = {}
            preserve = []
            for item, value in boundary_commands:
                value = str(value)
                include(item)
                if item not in active:
                    release[item] = value
            if choice == OFF:
                for _, stage in stages(data, room):
                    deactivation[stage["power"]] = "OFF"
                if not deactivation:
                    deactivation = {item: value for item, value in release.items() if item not in all_usb}
                for item, value in raw["after"][choice]:
                    if supported(item) and item in power_items:
                        release[item] = str(value)
            for item, value in active.items():
                include(item)
            # usb_only omissions preserve by default. The park policy is
            # explicit even with an older caller-supplied base table.
            moved = {item for item, _ in boundary_commands}
            for item in all_usb:
                if item in active or item in moved:
                    continue
                if choice != OFF and entry.get("usb_unused", "preserve") == "park":
                    release[item] = str(routing["usb_off_host"])
                else:
                    preserve.append(item)
            unmanaged = sorted({item for item in active if not supported(item)})
            deactivate_unknown = sorted(item for item in deactivation if not supported(item))
            choices[choice] = {"requirements": {item: value for item, value in active.items() if supported(item)},
                               "release": {item: value for item, value in release.items() if supported(item)},
                               "preserve": [item for item in preserve if supported(item)],
                               "off": choice == OFF,
                               "deactivate": {item: value for item, value in deactivation.items() if supported(item)},
                               "uses": [] if choice == OFF else [choice],
                               "complete": not unmanaged, "unmanaged": unmanaged,
                               "deactivateComplete": not deactivate_unknown,
                               "unmanagedDeactivation": deactivate_unknown}
            for item, value in raw["choices"][OFF]:
                release_values.setdefault(item, str(value))
            for _, stage in stages(data, room):
                release_values[stage["power"]] = "OFF"
        # A follower's synced route is its own with its zone on the leader's
        # input, and leases the leader's matrix output at the source: the
        # reference count that keeps the leader from moving it, and the
        # planner from parking it, while either room holds it. Its holder
        # never writes it and releases it only for a leader that released it
        # (lifecycle.js).
        sync = raw.get("sync")
        for source, synced in (sync["choices"] if sync else {}).items():
            route = dict(choices[source], requirements={
                item: synced["value"] if item == sync["input"] else value
                for item, value in choices[source]["requirements"].items()})
            if include(sync["hdmi"]):
                route["leases"] = {sync["hdmi"]: str(sources[source]["hdmi_input"])}
            choices[synced["plan"]] = route
        endpoints[room] = {"choices": choices}

    # AVR inputs are physical connectors shared by configured zones. A zone
    # can select another zone's local source or matrix feed from its own
    # front panel, independently of both Source menus and the donor's power.
    # A zone's `follows` input (stage()) additionally plays the current main
    # input; another zone cannot select it as a connector.
    for zones in device_stages.values():
        main = zones.get(None)
        paths = [(donor, row) for donor in zones.values() for row in consumers if donor["input"] in row["requirements"]]
        for zone, stage in zones.items():
            for donor, path in paths:
                value = path["requirements"][donor["input"]]
                if donor["input"] != stage["input"] and value != donor.get("follows") and value in stage["inputs"]:
                    requirements = {item: target for item, target in path["requirements"].items()
                                    if item not in (donor["power"], donor["input"])}
                    consumer(path["source"], {**requirements, stage["power"]: "ON", stage["input"]: value})
                if main is not None and donor is main and zone is not None and "follows" in stage:
                    requirements = {item: target for item, target in path["requirements"].items() if item != main["power"]}
                    consumer(path["source"], {**requirements, stage["power"]: "ON", stage["input"]: stage["follows"]})

    for item, resource in resources.items():
        resource["exclusive"] = item in exclusive
        resource["uses"] = {value: sorted(uses) for value, uses in resource_uses[item].items()}
        if item in release_values:
            resource["release"] = release_values[item]
        # Source power belongs to overall route readiness. Matrix/USB
        # selection can proceed while a source wakes; only a stage input
        # query/write needs that stage's own power first.
    for room in routing["rooms"]:
        for _, stage in stages(data, room):
            if stage["input"] not in resources or stage["power"] not in resources:
                continue
            for value in resources[stage["input"]]["values"]:
                resources[stage["input"]]["requires"].setdefault(value, {})[stage["power"]] = "ON"
    # A consumer predicate can name a stage shared by several sources. Only
    # retain dependencies present in every path that actually targets this
    # value; never infer that all those sources must be powered together.
    for item, resource in resources.items():
        for value, dependencies in list(resource["requires"].items()):
            applicable = [route["requirements"] for endpoint in endpoints.values() for route in endpoint["choices"].values()
                          if route["requirements"].get(item) == value]
            resource["requires"][value] = {dependency: target for dependency, target in dependencies.items()
                                           if applicable and all(requirements.get(dependency) == target for requirements in applicable)}
        resource["requires"] = {value: dependencies for value, dependencies in resource["requires"].items() if dependencies}
    return {"version": 1, "resources": resources, "endpoints": endpoints,
            "sources": source_policy, "consumers": consumers, "coverage": coverage}
