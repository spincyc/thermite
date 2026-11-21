# The house file

`instance/devices.yaml` in your private site says what the house is: its rooms, its devices, which integration reads and commands each device, and which source each room can show. thermite generates everything else from it. [Get started](start.html) shows a first one.

This page lists every key the file may have. It is made from the declaration `thermite check house` checks the file against, so it names every key the file takes and none it refuses. A key that is not here is refused, with the nearest name that is.

A value is text, a number, true or false, a list or a mapping. Write text that could be read as something else in quotes: `"on"`, `"03:00"`, `"2.2"`. `thermite check house` names the line of any value that does not mean one thing.

{{house_keys}}

## Signal paths and measurements

Signal & audio shows the configured path and the measurements available at each hop. Configure an audio fork with `routing.rooms.<room>.audio_link`, for example `audio_link: {from: {device: desk_switch, port: optical}, transport: optical}`. The destination is the room's `audio` device, with `audio_input` identifying its selected input. The upstream device must already be on that source's path. A room with an audio device but no known connection still shows that endpoint with wiring unknown. Thermite does not invent a cable from the display to the amplifier.

Connector numbers use the device's own numbering. A receiver input name is a logical selector and can be reassigned to a different physical jack. Passive `diagnostics.observers` can qualify an edge with `after_port`, `before_port`, `after_zone` and `before_zone`. An unqualified device pair must identify only one connection across all configured route variants.

The clients distinguish current formats, physical link status, EDID capabilities, processing settings and track metadata. A setting or advertised capability cannot establish the current format. Each missing measurement retains its hop and input/output label. No format telemetry, no response, an invalid response, no signal, silence and a stale reading mean different things. Values from another input, from before a route change, or without source association remain explicitly unassociated. Native scalar values without measurement timestamps say age unknown; fetching them does not make them fresh.

VRROOM HDMI audio reports describe the named HDMI output, not a measurement of its optical output. WiiM track metadata likewise does not establish the received optical format. Main-zone AVR measurements do not describe zone 2, and source names alone do not select a shared receiver's synced route: the confirmed route lifecycle selects that variant.

## Checking as you type

Editors that understand JSON Schema complete and check the file as you type: start it with `# yaml-language-server: $schema=https://spincyc.github.io/thermite/house.schema.json`. The schema is made from the same declaration. It knows the names of the keys and the kinds of value; `thermite check house` checks the rest, such as that a room id names a room.
