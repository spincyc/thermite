# Questions

### What is thermite?

Home automation built around openHAB. Describe your rooms, devices and connections in one file; thermite generates the configuration and gives you controls in a web app and a terminal.

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

One always-on x86-64 Linux machine or VM. For the full stack, plan on at least 2 cores, 4 GiB RAM and a 64 GB SSD for light use; 4 cores, 8 GiB RAM and 128 GB is the recommended starting point. These are estimates, not benchmarked capacity limits. Existing servers need additional free space for state and container images; see the [system requirements](https://spincyc.github.io/thermite/requirements.html).

Arch Linux is the primary [server installation](https://spincyc.github.io/thermite/installation.html) example and the default [standalone ISO](https://spincyc.github.io/thermite/iso.html) base. Ubuntu Server 24.04 is also documented, including an optional Ubuntu ISO. Each path keeps your private site separate from the application. The complete installer currently requires x86-64; ARM images for some dependencies do not make the full stack ARM-compatible.

### Are the screenshots from a real house?

They show the actual clients in demo mode with fictional inventory. The simulated controls send no commands to real devices. You can reproduce the images with the [capture recipe](https://spincyc.github.io/thermite/screenshots.html), or try the terminal with `thermite remote --mock`.

### Which Linux account runs the services?

The non-login account `thermite:thermite`, UID/GID 9001, runs all application services and owns their deployed data. You continue using your own login for the CLI, terminal remote, builds and private Git. The host's systemd manager and Podman backend remain root-managed. Existing installations migrate the older `openhab` account name while keeping the numeric IDs and data paths; see [service ownership](https://spincyc.github.io/thermite/service-account.html).

### Where does my house configuration belong?

In a private site, separate from the installed application and the public repository. Keep real inventory, host settings and generated outputs in private Git; credentials and runtime state stay outside Git. `init` starts with fictional devices and activates no service. The [getting-started guide](https://spincyc.github.io/thermite/start.html) explains the boundary.

### How do application upgrades and rollback work?

A private site pins the application's version and exact release digest. Install the reviewed new package, run `thermite init --upgrade /path/to/site`, then generate, validate and privately commit before deployment. Configuration and state are preserved. Rollback uses the older private revision's exact cached application release, so keep that cache. Runtime version pins are separate; no missing artifact is silently substituted.

### Why "thermite", and why a bowl?

The name refers to aluminothermic metalworking. The shallow bowl, metal rim and small flame express the craft of joining parts into something useful. The [design notes](https://spincyc.github.io/thermite/colors.html) explain the historical reference and shared drawing.

### Can I use it?

thermite is under active development. Start with the sample house and the checks in [Get started](https://spincyc.github.io/thermite/start.html); review the project's current terms before redistributing it. All rights are reserved.
