# Questions

### What is thermite?

Home automation built around openHAB. Describe your rooms, devices and connections in one file; thermite generates the configuration and gives you controls in a web app and a terminal.

### Do I need to know openHAB?

No. You edit the house file; thermite writes openHAB's files. Familiarity helps with troubleshooting.

### What can it control?

TVs, streaming boxes, AV receivers, projectors, speakers, and HDMI and USB switchers with compatible control interfaces.

### Does it need the internet?

Core AV controls run on your own network. Downloads and cloud-based integrations need internet access. Some devices also need their maker's app for setup.

### Can trying it break my house?

Generation and validation send no device commands. Try controls with `thermite remote --mock` or a sandbox; live remotes and pairing act on the selected runtime. Deployment requires committed configuration and a native boot check. `thermite rollback` restores the previous deployment.

### Where are passwords kept?

Outside the house file and Git, in private service and client stores. Site passwords are stored as hashes; terminal clients keep revocable tokens in owned private files. `thermite lint` scans source and configuration for accidentally embedded credentials.

### Can I change the colors?

Yes. Use `--theme contrast` or a custom theme. `thermite theme check` checks readability. See [Colors](https://spincyc.github.io/thermite/colors.html).

### Does it work with Apple Home?

Not yet. Bringing the house into Apple Home through openHAB is planned.

### What does it need to run?

An always-on x86-64 Linux machine or VM, running Arch Linux or Ubuntu Server 24.04. The [system requirements](https://spincyc.github.io/thermite/requirements.html) give planning estimates and tested configurations. Use the [server guide](https://spincyc.github.io/thermite/installation.html) for an existing OS, or the [standalone ISO](https://spincyc.github.io/thermite/iso.html) for a dedicated machine. The complete stack does not currently support ARM.

### Are the screenshots from a real house?

They show the actual clients with fictional inventory and simulated controls. See the [capture recipe](https://spincyc.github.io/thermite/screenshots.html), or try `thermite remote --mock`.

### Which Linux account runs the services?

Application services use the non-login account `thermite:thermite`, UID/GID 9001. Use your own login for the CLI and private Git; systemd and Podman's backend remain root-managed. The [service ownership guide](https://spincyc.github.io/thermite/service-account.html) covers permissions and migration from the older `openhab` account.

### Where does my house configuration belong?

In a private site created by `thermite init`. Commit inventory, host settings and version pins to private Git. Generated output, credentials and runtime state stay outside Git. See [Get started](https://spincyc.github.io/thermite/start.html).

### How do application upgrades and rollback work?

Install the reviewed package, run `thermite init --upgrade /path/to/site`, then generate, validate and privately commit before deployment. Configuration and state are preserved. Rollback needs the older revision's exact cached application release; retain that cache. See [Application releases](https://spincyc.github.io/thermite/packaging.html) for the full workflow and separate runtime pins.

### Why "thermite", and why a bowl?

The name and bowl draw on aluminothermic metalworking. See the [design notes](https://spincyc.github.io/thermite/colors.html).

### Can I use it?

thermite is under active development. Start with the sample house and the checks in [Get started](https://spincyc.github.io/thermite/start.html); review the project's current terms before redistributing it. All rights are reserved.
