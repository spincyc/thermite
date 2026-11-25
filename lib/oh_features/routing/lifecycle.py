"""Pure compilation of explicit routing paths and their consumption boundary.

The base table is supplied by routing.table(); this module never calls it.
Only fields with an ordered executor enter resources. Missing consumer
predicates widen protection and are exposed in coverage for validation.
All room wiring is derived from oh_features.routing.hops.
"""
from __future__ import annotations

from collections import defaultdict

from oh_conf import House

from . import hops


def active_requirements(house: House, room: str, choice: str) -> dict[str, str]:
    """The selected path, excluding command-scoped parking and bypassed stages.

    Kept independent of executor support so policy verification and lifecycle
    compilation can consume the same wiring contract.
    """
    return hops.compile_room_path(house, room, choice).active_requirements()


def compile_descriptor(house: House, base_table: dict, caps: dict) -> dict:
    """Return a finite lifecycle descriptor without rendering or mutations."""
    routing = house.data.get("routing")
    if not routing:
        return {"version": 1, "resources": {}, "endpoints": {}, "sources": {}, "consumers": [], "coverage": {}}
    devices = house.by_id
    sources = routing["sources"]
    graph = hops.compile_house(house)
    resources: dict = {}
    endpoints: dict = {}
    consumers: list[dict] = []
    coverage = {source: {"complete": True, "unsupported": []} for source in sources}
    source_policy: dict = {}
    resource_uses = defaultdict(lambda: defaultdict(set))
    release_values: dict[str, str] = {}
    exclusive: set[str] = set()

    stage_power = {
        hop.power_item
        for paths in graph.rooms.values()
        for hop in paths[hops.OFF].stage_hops()
        if not hop.passive and hop.power_item
    }
    source_wakes = {
        source: ([[item, "ON"]] if (item := hops.source_wake_item(devices, source)) else [])
        for source in sources
    }
    # Every power Item a route switches: its stages' and its sources'.
    power_items = stage_power | {item for steps in source_wakes.values() for item, _ in steps}
    # A USB cleanup policy must not replace ordinary source wake controls
    # with a stricter, firmware-dependent power-query contract. Only automatic
    # source standby opts source-only power into that execution authority.
    legacy_power = {
        item for source, steps in source_wakes.items()
        if sources[source].get("idle", "preserve") == "preserve"
        for item, _ in steps
    } - stage_power

    def supported(item: str) -> bool:
        cap = caps.get(item)
        return bool(item not in legacy_power and cap and cap.get("terminal") is True and cap.get("probe") is True)

    def include(item: str) -> bool:
        if not supported(item):
            return False
        if item not in resources:
            cap = caps[item]
            resources[item] = {"key": cap["key"], "values": list(map(str, cap["field"]["values"])),
                               "capable": True, "exclusive": False, "uses": {}, "requires": {}}
            if "settle" in cap["field"]:
                resources[item]["settleMs"] = dict(cap["field"]["settle"])
            # Optional, as settle: only a declared one changes the descriptor.
            if cap["field"].get("independent") is True:
                resources[item]["independent"] = True
        return True

    def missing(source: str, items: list[str]) -> None:
        row = coverage[source]
        row["complete"] = False
        row["unsupported"] = sorted(set(row["unsupported"]) | set(items))

    def consumer(source: str, requirements: dict[str, str]) -> None:
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
            row: dict = {"source": source, "requirements": known}
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
        if capable and item is not None:
            resources[item]["source"] = source
            release_values[item] = "OFF"

    # A source menu limits commands, not physical matrix reachability. An
    # unmodeled output can still be selected at the matrix's front panel.
    matrix_sources: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    matrix_outputs: dict[str, set[int]] = defaultdict(set)
    matrix_upstream: dict[str, set[str]] = defaultdict(set)
    device_stages: dict[str, dict[int | None, dict]] = defaultdict(dict)
    hdmi_matrix_id = routing.get("hdmi_matrix")
    if isinstance(hdmi_matrix_id, str):
        for source, definition in sources.items():
            if "hdmi_input" in definition:
                matrix_sources[hdmi_matrix_id][source].add(definition["hdmi_input"])
    for paths in graph.rooms.values():
        off_path = paths[hops.OFF]
        for mhop in off_path.matrix_hops():
            matrix_outputs[mhop.device].add(mhop.output)
        prev_upstream: list[str] = [hdmi_matrix_id] if (off_path.matrix_hops() and isinstance(hdmi_matrix_id, str)) else []
        for shop in off_path.switch_hops():
            if shop.output is not None:
                matrix_outputs[shop.device].add(shop.output)
                for src, inp in shop.inputs_map:
                    matrix_sources[shop.device][src].add(inp)
                if shop.park_value is not None:
                    matrix_upstream[shop.device].update(prev_upstream)
                prev_upstream = [*prev_upstream, shop.device]
            else:
                if shop.park_value is not None:
                    prev_upstream = [*prev_upstream]
    for matrix, connected in matrix_sources.items():
        mod = hops.switcher(devices[matrix])
        if mod is None:
            continue
        count = mod.ports(devices[matrix])[1]
        opaque = set(range(1, count + 1)) - matrix_outputs[matrix]
        if opaque:
            reachable = set(connected)
            seen_up: set[str] = set()
            stack = list(matrix_upstream.get(matrix, ()))
            while stack:
                up = stack.pop()
                if up in seen_up:
                    continue
                seen_up.add(up)
                reachable.update(matrix_sources.get(up, {}))
                stack.extend(matrix_upstream.get(up, ()))
            for source in reachable:
                missing(source, ["unmanaged_hdmi:" + hops.port_item(devices, matrix, port) for port in sorted(opaque)])
                source_policy[source]["unresolved"] = True

    # Consumer paths use physical selectors and stage power, never the
    # source's own power. Enumerate physical alternatives independently of
    # the one command path. Video and receiver audio are separate legs.
    for room, room_paths in graph.rooms.items():
        raw = base_table["rooms"][room]
        off_path = room_paths[hops.OFF]
        room_stages = off_path.stage_hops()
        room_switches = off_path.switch_hops()
        video_matrix = next((h for h in off_path.matrix_hops() if h.role == "video"), None)
        audio_matrix = next((h for h in off_path.matrix_hops() if h.role == "audio"), None)
        split_audio = audio_matrix is not None

        for shop_stage in room_stages:
            if not shop_stage.passive and shop_stage.power_item and shop_stage.input_item:
                stage_dict: dict = {
                    "power": shop_stage.power_item,
                    "input": shop_stage.input_item,
                    "inputs": list(shop_stage.inputs_allowed),
                }
                if shop_stage.follows is not None:
                    stage_dict["follows"] = shop_stage.follows
                device_stages[shop_stage.device][shop_stage.zone] = stage_dict

        # ruff: disable[B023] (called in this iteration)
        def stage_fields(stage_hop: hops.StageHop, value: str | None) -> dict[str, str]:
            if stage_hop.passive or not stage_hop.power_item:
                return {}
            out = {stage_hop.power_item: "ON"}
            if value is not None and stage_hop.input_item:
                out[stage_hop.input_item] = str(value)
            return out

        def feed_fields(stage_hop: hops.StageHop, source: str) -> list[dict[str, str]]:
            if stage_hop.passive or not stage_hop.power_item:
                return [{}]
            by_src = dict(stage_hop.by_source)
            values = dict.fromkeys([stage_hop.feed_input, by_src.get(source)])
            return [stage_fields(stage_hop, val) for val in values if val is not None] or [{}]

        def local_fields(stage_hop: hops.StageHop, source: str) -> list[dict[str, str]]:
            if stage_hop.passive or not stage_hop.power_item:
                return []
            val = dict(stage_hop.local_sources).get(source)
            return [stage_fields(stage_hop, val)] if val is not None else []

        def upstream(source: str, audio: bool = False) -> list[dict[str, str]]:
            choice = sources[source]
            paths: list[dict[str, str]] = []
            # Each switch in the cascade may carry `source` locally, then pass it
            # through every downstream switch's `park_value` (matrix_input).
            for idx, sw in enumerate(room_switches):
                local_map = dict(sw.inputs_map)
                local_inputs = (
                    sorted(matrix_sources[sw.device].get(source, set()))
                    if sw.output is not None
                    else ([local_map[source]] if source in local_map else [])
                )
                if not local_inputs:
                    continue
                downstream: dict[str, str] = {}
                reachable = True
                for later in room_switches[idx + 1:]:
                    if later.park_value is None:
                        reachable = False
                        break
                    downstream[later.item] = str(later.park_value)
                if reachable:
                    for inp in local_inputs:
                        paths.append({sw.item: str(inp), **downstream})
            # Primary HDMI matrix path flowing through all switches (if any).
            mhop = audio_matrix if (audio and audio_matrix is not None) else video_matrix
            if mhop is not None and choice.get("hdmi_input") is not None:
                chain: dict[str, str] = {}
                reachable = True
                if not (audio and split_audio):
                    for sw in room_switches:
                        if sw.park_value is None:
                            reachable = False
                            break
                        chain[sw.item] = str(sw.park_value)
                if reachable:
                    chain[mhop.item] = str(choice["hdmi_input"])
                    paths.append(chain)
            return paths
        # ruff: enable[B023]

        receiver_hop = next((h for h in room_stages if h.role == "receiver"), None)
        soundbar_hop = next((h for h in room_stages if h.role == "soundbar"), None)
        display_hops = [h for h in room_stages if h.role == "display"]
        receiver_present = receiver_hop is not None and not receiver_hop.passive and bool(receiver_hop.power_item)
        soundbar_present = soundbar_hop is not None and not soundbar_hop.passive and bool(soundbar_hop.power_item)

        offered_sources = set(routing["rooms"][room]["sources"])
        for source in sources:
            display_locals = [(dh, local_fields(dh, source)) for dh in display_hops]
            soundbar_local = local_fields(soundbar_hop, source) if soundbar_hop is not None else []
            receiver_local = local_fields(receiver_hop, source) if receiver_hop is not None else []
            display_feeds = [feed_fields(dh, source) for dh in display_hops] if display_hops else [[{}]]
            soundbar_feeds = feed_fields(soundbar_hop, source) if soundbar_hop is not None else [{}]
            receiver_feeds = feed_fields(receiver_hop, source) if receiver_hop is not None else [{}]
            paths = upstream(source)

            any_local_display = False
            for _, dlocals in display_locals:
                for df in dlocals:
                    any_local_display = True
                    consumer(source, df)
            for sbf in soundbar_local:
                consumer(source, sbf)
                for df_list in display_feeds:
                    for df in df_list:
                        consumer(source, {**sbf, **df})
            for recf in receiver_local:
                consumer(source, recf)
                for sbf in soundbar_feeds:
                    if soundbar_present:
                        consumer(source, {**recf, **sbf})
                    for df_list in display_feeds:
                        for df in df_list:
                            consumer(source, {**recf, **sbf, **df})
            for path in paths:
                for df_list in display_feeds:
                    for df in df_list:
                        for sbf in soundbar_feeds:
                            for recf in ([{}] if split_audio else receiver_feeds):
                                consumer(source, {**path, **recf, **sbf, **df})
            if receiver_present:
                for path in upstream(source, audio=True):
                    for recf in receiver_feeds:
                        consumer(source, {**path, **recf})
            if soundbar_present:
                for path in paths:
                    for sbf in soundbar_feeds:
                        for recf in ([{}] if split_audio else receiver_feeds):
                            consumer(source, {**path, **recf, **sbf})
            if source in offered_sources and not (paths or any_local_display or soundbar_local or receiver_local):
                missing(source, ["video_path:" + room])
                source_policy[source]["unresolved"] = True

        all_usb: dict[str, tuple[str, int]] = {}
        seen_usb_matrices: set[str] = set()
        for uhop in off_path.usb_hops():
            all_usb[uhop.item] = (uhop.device, uhop.port)
            exclusive.add(uhop.item)
            release_values[uhop.item] = str(uhop.park_value)
            include(uhop.item)
            seen_usb_matrices.add(uhop.device)
        for usb_matrix in seen_usb_matrices:
            owned_ports = {
                uhop.port
                for paths_map in graph.rooms.values()
                for uhop in paths_map[hops.OFF].usb_hops()
                if uhop.device == usb_matrix
            }
            usb_mod = hops.switcher(devices[usb_matrix])
            if usb_mod is None:
                continue
            opaque_ports = set(range(1, usb_mod.ports(devices[usb_matrix])[1] + 1)) - owned_ports
            room_ports = [uhop.port for uhop in off_path.usb_hops() if uhop.device == usb_matrix]
            off_host = routing["usb_off_host"]
            for source, definition in sources.items():
                host = hops.usb_host(definition, usb_matrix, off_host)
                if host == off_host:
                    continue
                for port in room_ports:
                    consumer(source, {hops.port_item(devices, usb_matrix, port): str(host)})
                if opaque_ports:
                    missing(source, ["unmanaged_usb:" + hops.port_item(devices, usb_matrix, port) for port in sorted(opaque_ports)])
                    source_policy[source]["unresolved"] = True

        choices: dict[str, dict] = {}
        for choice, boundary_commands in raw["choices"].items():
            room_path = room_paths[choice]
            active = room_path.active_requirements()
            deactivation: dict[str, str] = {}
            release: dict[str, str] = {}
            preserve: list[str] = []
            for item, value in boundary_commands:
                value_str = str(value)
                include(item)
                if item not in active:
                    release[item] = value_str
            if choice == hops.OFF:
                for stage_hop in room_stages:
                    if not stage_hop.passive and stage_hop.power_item:
                        deactivation[stage_hop.power_item] = "OFF"
                if not deactivation:
                    deactivation = {item: value for item, value in release.items() if item not in all_usb}
                for item, value in raw["after"][choice]:
                    if supported(item) and item in power_items:
                        release[item] = str(value)
            for item in active:
                include(item)
            # usb_only omissions preserve by default; park policy is explicit on each UsbPortHop.
            moved = {item for item, _ in boundary_commands}
            for uhop in room_path.usb_hops():
                if uhop.item in active or uhop.item in moved:
                    continue
                if choice != hops.OFF and uhop.unused_policy == "park":
                    release[uhop.item] = str(uhop.park_value)
                else:
                    preserve.append(uhop.item)
            unmanaged = sorted({item for item in active if not supported(item)})
            deactivate_unknown = sorted(item for item in deactivation if not supported(item))
            choices[choice] = {
                "requirements": {item: value for item, value in active.items() if supported(item)},
                "release": {item: value for item, value in release.items() if supported(item)},
                "preserve": [item for item in preserve if supported(item)],
                "off": choice == hops.OFF,
                "deactivate": {item: value for item, value in deactivation.items() if supported(item)},
                "uses": [] if choice == hops.OFF else [choice],
                "complete": not unmanaged,
                "unmanaged": unmanaged,
                "deactivateComplete": not deactivate_unknown,
                "unmanagedDeactivation": deactivate_unknown,
            }
            for item, value in raw["choices"][hops.OFF]:
                release_values.setdefault(item, str(value))
            for stage_hop in room_stages:
                if not stage_hop.passive and stage_hop.power_item:
                    release_values[stage_hop.power_item] = "OFF"

        # The executor can run independent matrix fields concurrently. Verify
        # the scaler before switching the receiver feed to a source so that
        # its picture is processed as the route expects from the first frame.
        if audio_matrix is not None and audio_matrix.scaler_item:
            scaler_item = audio_matrix.scaler_item
            output_item = audio_matrix.item
            for route in choices.values():
                required = route["requirements"]
                if output_item in required and scaler_item in required and output_item in resources:
                    resources[output_item]["requires"][required[output_item]] = {scaler_item: required[scaler_item]}

        # A follower's synced route is its own with its zone on the leader's
        # input, and leases the leader's matrix output at the source.
        for source, room_path in room_paths.items():
            if room_path.sync is None:
                continue
            sync = room_path.sync
            route = dict(choices[source], requirements={
                item: sync.leader_input_value if item == sync.follower_input_item else value
                for item, value in choices[source]["requirements"].items()
            })
            if include(sync.leader_hdmi_item):
                route["leases"] = {sync.leader_hdmi_item: str(sync.source_hdmi_input)}
            choices[sync.plan_name] = route
        endpoints[room] = {"choices": choices}

    # Receiver inputs are physical connectors shared by configured zones. A zone
    # can select another zone's local source or matrix feed from its own
    # front panel, independently of both Source menus and the donor's power.
    for zones in device_stages.values():
        main = zones.get(None)
        donor_paths = [(donor, row) for donor in zones.values() for row in consumers if donor["input"] in row["requirements"]]
        for zone, stage in zones.items():
            for donor, path in donor_paths:
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

    for room_paths in graph.rooms.values():
        for stage_hop in room_paths[hops.OFF].stage_hops():
            if stage_hop.passive or not stage_hop.input_item or not stage_hop.power_item:
                continue
            if stage_hop.input_item not in resources or stage_hop.power_item not in resources:
                continue
            for value in resources[stage_hop.input_item]["values"]:
                resources[stage_hop.input_item]["requires"].setdefault(value, {})[stage_hop.power_item] = "ON"

    for item, resource in resources.items():
        for value, dependencies in list(resource["requires"].items()):
            applicable = [route["requirements"] for endpoint in endpoints.values() for route in endpoint["choices"].values()
                          if route["requirements"].get(item) == value]
            resource["requires"][value] = {
                dependency: target for dependency, target in dependencies.items()
                if applicable and all(requirements.get(dependency) == target for requirements in applicable)
            }
        resource["requires"] = {value: dependencies for value, dependencies in resource["requires"].items() if dependencies}

    return {"version": 1, "resources": resources, "endpoints": endpoints,
            "sources": source_policy, "consumers": consumers, "coverage": coverage}
