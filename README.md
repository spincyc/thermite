<p align="center"><picture><source media="(prefers-color-scheme: light)" srcset="docs/assets/torch-light.svg"><img src="docs/assets/torch.svg" width="80" alt="thermite's shallow bowl of fire"></picture></p>

<h1 align="center">thermite</h1>

<p align="center">Home automation you can own.</p>

Describe your rooms, devices and connections in one file. thermite builds your [openHAB](https://www.openhab.org/) configuration and gives you controls in a web app and a terminal.

## See it

<p><img src="docs/assets/screenshots/web-remote.png" width="100%" alt="thermite web remote in dark mode, showing the fictional Example West room and its player controls"></p>

Rooms, sources and device controls in the web app.

<p><img src="docs/assets/screenshots/terminal-remote.png" width="100%" alt="thermite terminal remote showing the same fictional room and player"></p>

The same controls in your terminal. Both screenshots show **demo mode with fictional inventory** in the actual clients. [Capture recipe](https://spincyc.github.io/thermite/screenshots.html).

## What you get

- **Checked deployments.** Validate the configuration, check openHAB startup, and restore an earlier deployment with `thermite rollback`.
- **Local control.** Core integrations communicate on your network. Optional cloud integrations use their providers' services.
- **Your own site.** Keep real inventory and an exact application release pin in private Git. Generated runtime files stay outside Git.

## Choose an installation

| Where thermite will run | Start here |
|---|---|
| Stock Arch Linux, or an existing Ubuntu Server | [Server installation](https://spincyc.github.io/thermite/installation.html) |
| A dedicated machine | [Build and install the standalone ISO](https://spincyc.github.io/thermite/iso.html), with Arch by default or an optional Ubuntu base |
| A development machine, with fictional devices | [Try the example](https://spincyc.github.io/thermite/start.html#try-the-example) |

This repository contains the application, tests and fictional example. `thermite init` creates your separate private site; installing the application starts no service.

Use an always-on x86-64 host. The [system requirements](https://spincyc.github.io/thermite/requirements.html) cover sizing, networking and tested configurations; [service ownership](https://spincyc.github.io/thermite/service-account.html) covers permissions and upgrades.

## Explore

- [Get started](https://spincyc.github.io/thermite/start.html): try the example, install, and connect your devices.
- [Commands](https://spincyc.github.io/thermite/commands.html): every command, with examples. `thermite help <command>` works in your shell.
- [Questions](FAQ.md): requirements, privacy and upgrades.
- [Colors](https://spincyc.github.io/thermite/colors.html): light and dark modes, accessible themes and the bowl mark.
- [Hardware endpoint design](design/hardware-endpoints.md): proposed standalone USB and IR endpoints.
- [Handheld remote research](design/handheld-remote.md): flashable remote hardware and its radio link.

Under active development, version `0.1.0.dev0`. All rights reserved.
