<p align="center"><picture><source media="(prefers-color-scheme: light)" srcset="docs/assets/torch-light.svg"><img src="docs/assets/torch.svg" width="80" alt="thermite's shallow bowl of fire"></picture></p>

<h1 align="center">thermite</h1>

<p align="center">Run your whole house on open software.</p>

thermite turns one plain file describing your house (its rooms, TVs, speakers and switches) into a working [openHAB](https://www.openhab.org/) setup, with a remote for your phone and one for your terminal. It replaces closed, dealer-installed systems with parts you own.

## What you get

- **One house file.** Rooms, devices and how they connect, in one place. Everything else is made from it.
- **Safe changes.** Every change boots on a throwaway openHAB first; a bad one rolls back with one command.
- **A remote on every screen.** The same remote on a phone, a desktop and a terminal.
- **Local by default.** Core integrations talk directly to devices on your network. Optional cloud providers require their own services and accounts.
- **Your colors.** A dark and a light mode, or a theme of your own.

## Source and your private site

This public checkout contains the program and a fictional example, not a production house. Keep your real inventory and host settings in a separate private site checkout. Production deployment installs a committed revision: generate and commit `conf/`, `metrics/` and `deploy/` only in that private checkout before using the deployment recipe below. Those outputs are ignored here; do not publish your house configuration upstream.

## In four commands

```sh
thermite generate       # build openHAB's setup from the house file
thermite test boot      # try it on a throwaway openHAB
thermite deploy         # install it
thermite remote         # the remote, in your terminal
```

`thermite help` lists every command; `thermite help <command>` explains one, with examples.

## Learn more

- [The docs](https://spincyc.github.io/thermite/): getting started, every command, colors.
- [Questions](FAQ.md), answered briefly.
- [The bowl and colors](https://spincyc.github.io/thermite/colors.html): one drawing, charcoal and pewter, with copper heat.

## Status

Under active development. The guide describes the available tools and their checks. All rights reserved.

## Run the example

The included `instance/` describes fictional devices at documentation-only addresses. Replace those files with your own inventory before using a real runtime.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest tests.test_public -q
.venv/bin/python bin/thermite generate
.venv/bin/python bin/thermite validate
```

Configuration, documentation and these tests run offline. Running a native test runtime or installing services is a separate explicit command and requires the pinned downloads. The terminal remote additionally needs Node.js and its locked package; the Apple TV helper has its own hashed dependency lock.
