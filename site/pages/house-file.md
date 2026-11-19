# The house file

`instance/devices.yaml` in your private site says what the house is: its rooms, its devices, which integration reads and commands each device, and which source each room can show. thermite generates everything else from it. [Get started](start.html) shows a first one.

This page lists every key the file may have. It is made from the declaration `thermite validate` checks the file against, so it names every key the file takes and none it refuses. A key that is not here is refused, with the nearest name that is.

A value is text, a number, true or false, a list or a mapping. Write text that could be read as something else in quotes: `"on"`, `"03:00"`, `"2.2"`. `thermite validate` names the line of any value that does not mean one thing.

{{house_keys}}

## Checking as you type

Editors that understand JSON Schema complete and check the file as you type: start it with `# yaml-language-server: $schema=https://spincyc.github.io/thermite/house.schema.json`. The schema is made from the same declaration. It knows the names of the keys and the kinds of value; `thermite validate` checks the rest, such as that a room id names a room.
