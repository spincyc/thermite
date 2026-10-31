# Colors and the bowl

thermite shares one bowl mark and one theme across the web, documentation and terminal. The default combines black and charcoal, pewter edges and warm off-white text. Copper marks interaction; a little amber suggests heat. Light mode uses paper-gray and darker metal. Use a built-in theme, or change only the roles you need.

## The bowl

<div class="torches">{{torch_sizes}}</div>

The broad, shallow vessel has a clear metal rim and a small asymmetric flame. One set of paths supplies SVG, PNG icons and terminal exports. The terminal uses braille dots for fire and quadrant blocks for metal; its Home view adds restrained motion, which `--still` stops.

The name recalls aluminothermic metalworking. [Goldschmidt's history](https://www.goldschmidt.com/about-us/history/) dates its industrial process to 1895 and its first tramway welds in Essen to 1899. The bowl is our graphic interpretation of a metalworking vessel: simple parts joined with care.

<div>{{terminal_large}}</div>

```sh
thermite theme show                          # in your terminal, in your theme
thermite theme export svg --out bowl.svg     # for a page of your own
```

## Built-in themes

Try one on this site; the choice sticks until you change it.

{{palettes}}

## Roles

A theme gives each role a color, in both modes.

{{roles}}

## Your own theme

A theme file names itself, says which theme it starts from, and changes only what it wants. This one keeps the bowl and turns the controls teal:

```json
{{harbor}}
```

Check it. thermite tells you about unknown roles and badly written colors, and about text too faint to read on its background:

```sh
thermite theme check harbor.json
thermite theme show --theme harbor.json --light
thermite theme export css --theme harbor.json
```

The contrast it asks for follows WCAG, the web's accessibility guidelines:

{{contrast}}

Editors that understand JSON Schema complete and check theme files as you type: start the file with `"$schema": "https://spincyc.github.io/thermite/theme.schema.json"`. Download the [schema](theme.schema.json), the [thermite theme](themes/thermite.json) or [harbor](examples/harbor.json).
