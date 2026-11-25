# The host file

`instance/host.yaml` in your private site says how the host running thermite is configured: its time zone, installation root, service account ids, listener ports, gateway addresses, optional plain-HTTP Guest listener, metrics overrides, Music Assistant libraries and mounted network shares. [Get started](start.html) shows how a private site is created and deployed.

This page lists every key the file may have. It is made from the declaration `thermite check` and `thermite install` check the file against, so it names every key the file takes and none it refuses.

A value is text, a number, true or false, a list or a mapping. Write text that could be read as something else in quotes: `"3.1.1"`, `"on"`, `"010"`. `instance/host.yaml` is read with the same strict YAML rules as [the house file](house-file.html), naming the line of any value that does not mean one thing.

{{host_keys}}

## Checking as you type

Editors that understand JSON Schema complete and check the file as you type: start it with `# yaml-language-server: $schema=https://spincyc.github.io/thermite/host.schema.json`. The schema is made from the same declaration. It knows the names of the keys and the kinds of value; `thermite check` checks the rest, such as valid IANA time zones, non-colliding ports and allowed mount points.
