# Questions

### What is thermite?

Software that runs a house. You describe the house once, and thermite sets up openHAB, the open-source home hub, to match. It also gives you remotes for it.

### Do I need to know openHAB?

No. You edit the house file; thermite writes openHAB's files. Knowing openHAB helps when something goes wrong, but you don't need it to start.

### What can it control?

TVs, streaming boxes, AV receivers, projectors, speakers, and HDMI and USB switchers, each over its own network control. More kinds of devices are on the way.

### Does it need the internet?

Core AV controls run on your own network. Downloads and cloud-based integrations need internet access. Some devices also need their maker's app for setup.

### Can trying it break my house?

Generating and validating configuration do not send device commands. Deployment installs a committed configuration after its native boot check (`thermite test boot`); `thermite rollback` restores the previous deployment. Remotes, pairing and explicit device commands act on the selected runtime, so use the mock remote or a sandbox when trying controls without changing the house.

### Where are passwords kept?

Outside the house file and Git, in private service and client stores. Site passwords are stored as hashes; terminal clients keep revocable tokens in owned private files. `thermite lint` scans source and configuration for accidentally embedded credentials.

### Can I change the colors?

Yes. `--theme contrast` gives the most contrast. A theme file of your own can change only what you want, and `thermite theme check` warns you when something would be hard to read. See [Colors](https://spincyc.github.io/thermite/colors.html).

### Does it work with Apple Home?

Not yet. Bringing the house into Apple Home through openHAB is planned.

### What does it need to run?

One Linux machine that stays on, with Podman, Python 3.11 or later and Git; Node.js for the terminal remote. Install the reviewed application in a dedicated Python environment, then use `thermite init` to create a separate private site. `thermite install` prepares the native runtime only after its explicit checks and owner-controlled steps.

### Where does my house configuration belong?

In a private site, separate from the installed application and the public repository. Keep real inventory, host settings and generated outputs in private Git; credentials and runtime state stay outside Git. `init` starts with fictional devices and activates no service. The [getting-started guide](https://spincyc.github.io/thermite/start.html) explains the boundary.

### How do application upgrades and rollback work?

A private site pins the application's version and exact release digest. Install the reviewed new package, run `thermite init --upgrade /path/to/site`, then generate, validate and privately commit before deployment. Configuration and state are preserved. Rollback uses the older private revision's exact cached application release, so keep that cache. Runtime version pins are separate; no missing artifact is silently substituted.

### Why "thermite", and why a bowl?

The name refers to aluminothermic metalworking. The shallow bowl, metal rim and small flame express the craft of joining parts into something useful. The [design notes](https://spincyc.github.io/thermite/colors.html) explain the historical reference and shared drawing.

### Can I use it?

thermite is under active development. Start with the sample house and the checks in [Get started](https://spincyc.github.io/thermite/start.html); review the project's current terms before redistributing it. All rights are reserved.
