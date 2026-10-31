# Questions

### What is thermite?

Software that runs a house. You describe the house once, and thermite sets up openHAB, the open-source home hub, to match. It also gives you remotes for it.

### Do I need to know openHAB?

No. You edit the house file; thermite writes openHAB's files. Knowing openHAB helps when something goes wrong, but you don't need it to start.

### What can it control?

TVs, streaming boxes, AV receivers, projectors, speakers, and HDMI and USB switchers, each over its own network control. More kinds of devices are on the way.

### Does it need the internet?

No. It talks to your devices on your own network. Some devices need their maker's app once, to set them up.

### Can trying it break my house?

Not by accident. Every change boots on a throwaway openHAB first (`thermite test boot`), and nothing reaches the house until you run `thermite deploy`. If a change turns out wrong, `thermite rollback` puts the previous one back.

### Where are passwords kept?

Outside the house file, in a private folder only openHAB can read. They never go into Git, and `thermite lint` checks that none slipped in.

### Can I change the colors?

Yes. `--theme contrast` gives the most contrast. A theme file of your own can change only what you want, and `thermite theme check` warns you when something would be hard to read. See [Colors](https://spincyc.github.io/thermite/colors.html).

### Does it work with Apple Home?

Not yet. Bringing the house into Apple Home through openHAB is planned.

### What does it need to run?

One Linux machine that stays on, with Podman, Python 3.11 or later and Git; Node.js for the terminal remote. `thermite install` sets up openHAB itself.

### Why "thermite", and why a bowl?

The name refers to aluminothermic metalworking. The shallow bowl, metal rim and small flame express the craft of joining parts into something useful. The [design notes](https://spincyc.github.io/thermite/colors.html) explain the historical reference and shared drawing.

### Can I use it?

thermite is under active development. Start with the sample house and the checks in [Get started](https://spincyc.github.io/thermite/start.html); review the project's current terms before redistributing it. All rights are reserved.
