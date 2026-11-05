# Bootable installer ISO

The Thermite ISO is the pinned official Arch Linux 2026.11.01 x86_64
installation medium with Thermite's own installer added. It boots into a
terminal that looks like `thermite tui`, asks a few questions, installs Arch
Linux with `archinstall` 4.5 behind the scenes, and on the installed host's
first boot sets up an empty Thermite house by itself. Build it as an
ordinary user with `xorriso` and `mtools`; no root, loop device or container
daemon is required. The image keeps upstream BIOS and UEFI boot support.
Secure Boot is not supported: turn it off in the firmware.

No disk, password, private house or API token is embedded; the installer
asks for each and erases a disk only after its name is typed back. Arch
installation needs Internet access for signed OS packages, and the first
boot needs it for Thermite's runtime downloads and container images. The
live medium is pinned; installed Arch package versions follow the current
repositories and are not frozen by the image's provenance receipt.

For a stock Arch installation followed by the standalone application, start
with [installation](installation.md). An optional Ubuntu Server image keeps
Ubuntu's own interactive installer, described below.

## Build

Install `xorriso` and `mtools` with the build host's package manager. From an
installed Thermite application or reviewed source tree:

```sh
thermite iso fetch --cache .scratch/iso/cache
thermite iso build --cache .scratch/iso/cache --work .scratch/iso/work --out .scratch/iso/thermite.iso
```

In a source checkout, substitute `python3 bin/thermite` for `thermite`.
Allow roughly 6 GB of free space for the Arch base, staging and result.

Fetch downloads the fixed HTTPS inputs in `iso/inputs.json` and checks each
digest. A corrupt existing cache fails and is not overwritten.

| Input | Pinned by |
|---|---|
| Arch Linux 2026.11.01 live ISO | SHA-256 from the [Arch release](https://archlinux.org/releng/releases/2026.11.01/) |
| Node.js 26.13.0 LTS, `linux-x64` tarball | SHA-256 from its [`SHASUMS256.txt`](https://nodejs.org/dist/v26.13.0/SHASUMS256.txt) |
| blessed 0.1.81, the npm tarball | sha512 integrity from the npm registry, the same as `tui/remote/package-lock.json` |

The npm registry publishes no SHA-256 for a package, so blessed is pinned by
the integrity value `thermite tui` already locks; a test holds the two
equal. Build checks every input again and stages the privacy-reviewed public
application payload: the application wheel, `iso/packages.json`, the
installer core (`iso/installer.py`), its terminal (`tui/installer` with the
`tui/remote` modules it draws with), the theme and torch as `thermite tui`
gets them, the Node.js and blessed archives, and their digests in
`payload.json`. The selected private site is never copied into the image.

To install over SSH as well as on the console, add one public key:

```sh
thermite iso build --cache .scratch/iso/cache --work .scratch/iso/work --out .scratch/iso/thermite.iso \
  --authorized-key ~/.ssh/id_ed25519.pub
```

The file must hold exactly one OpenSSH public key without options. The live
system then accepts SSH as `root` with that key alone, straight into the
installer, and the installed host gives it to the owner and turns password
SSH off. Without a key the live system's SSH stays as Arch ships it, which
accepts no login until someone sets a root password at the console.

The outputs are `thermite.iso`, `thermite.iso.sha256` and
`thermite.iso.json`. The receipt records every input's digest, the
application release, the payload and the key's fingerprint. Existing
outputs are refused. Choose a new filename when rebuilding. An interrupted
image without its checksum and receipt is incomplete. Keep the three
completed files together and verify the image before use:

```sh
cd .scratch/iso
sha256sum --check thermite.iso.sha256
```

Build the final image after all application and documentation edits: the
application release identifies the exact reviewed program bytes. The
builder checks the pinned digests; it does not verify detached release
signatures, which can be checked separately with each publisher's key.

The builder replays upstream's hybrid boot metadata. It updates both BIOS
Syslinux entries and UEFI systemd-boot entries, including the copies inside
the appended EFI FAT partition, to run Thermite's boot hook. It preserves
Arch's source UUID and signed live filesystem, and the GRUB loopback
entries used when another boot loader opens the ISO as a file.

## Install a machine

1. Attach the ISO to a new VM, or write it to USB with a tool that displays
   the destination and asks before overwriting it. Thermite supplies no
   command that guesses a USB or system disk.
2. Boot the `Thermite installation (Arch Linux)` entry. The installer starts
   on the first console. With an image key, `ssh root@<address>` from the
   key's machine opens the same installer; the console's welcome screen
   shows the key's fingerprint and its address. Only one of them installs
   at a time: the other is told who holds it and can follow
   `/run/thermite-installer/installer.log`.
3. Answer its steps. Tab and the arrows move, Enter goes on, Esc goes back,
   Ctrl-T switches between dark and light, Ctrl-C leaves for a shell
   (`thermite-installer` starts it again):

   | Step | Asks for | Notes |
   |---|---|---|
   | Welcome | | This machine: firmware, processor, memory, disks, network, warnings |
   | Network | Wi-Fi network and passphrase, if there is no cable | The installed host joins the same Wi-Fi |
   | Host name | default `thermite` | If `thermite.local` already answers on the network, `thermite-2` (or the next free one) is suggested; Enter twice keeps a taken name |
   | Owner | user name and password | Your login, in `wheel` with sudo; `thermite` is reserved for the service account |
   | Root password | | Console and emergency use only; it must differ from the owner's |
   | Region | time zone, language, console keyboard | Type to search a list |
   | Disk | the whole disk to install on | Disks smaller than 32 GB, read-only, in use or holding the installer are not offered; less than 64 GB warns |
   | System | Btrfs (default) or ext4; `linux-lts` (default) or `linux` | Both kernels are installed; the other is the fallback boot entry |
   | Review | the disk's name, typed back | Nothing is written before this |

4. The progress screen follows the installation: a package check before the
   disk is touched, then partitioning, the base system, the boot loader, the
   owner, Thermite's packages and services, and Thermite's own part. A
   failed run says why and returns to the review; the disk may already have
   been changed.
5. When it is done, remove the installation medium and press Enter to
   restart.

The installer core (`iso/installer.py`) checks every answer, writes
`archinstall`'s configuration and credentials (yescrypt hashes only, in a
file only root can read, deleted after the run), runs `archinstall` without
its menus, and logs to `/run/thermite-installer/installer.log`. Passwords
and the Wi-Fi passphrase never reach a command line, a log or the
installed host's records. A dropped SSH login does not stop a running
installation.

The layout is a 1 GiB boot partition (`/boot`, FAT32) and the system:
Btrfs with the subvolumes `@` (`/`), `@home`, `@log` (`/var/log`) and `@pkg`
(`/var/cache/pacman/pkg`), compressed with zstd, or a single ext4 file
system. UEFI hosts get systemd-boot and a GPT disk, BIOS hosts GRUB and an
MBR disk; either boots the chosen kernel by default. Swap is zram.

## The installed host

Thermite's `archinstall` plugin runs after the OS, boot loader, owner,
packages and services are installed, with the target still mounted. It
installs the application from the medium into `/opt/thermite/venv` with
the `thermite` launcher, then configures:

- `sshd` enabled, with `PermitRootLogin no`; with an image key, that key in
  the owner's `~/.ssh/authorized_keys` and password logins off.
- `sudo` for the `wheel` group, checked by `visudo`.
- `avahi-daemon` and `nss-mdns`, so `.local` names resolve; time
  synchronization, zram swap and the journal capped at 2 GB.
- `xterm-256color` for a terminal the host has no terminfo for, in
  `/etc/profile.d/thermite-term.sh`; `~/.local/bin` first on PATH.
- The units listed under [Packages](#packages) as disabled stay disabled.
- `/var/log/thermite-install`: `receipt.json` (application release, input
  digests, the key's fingerprint), `answers.json` without secrets, and the
  installer's own log. `archinstall`'s log is `/var/log/archinstall/install.log`.

## First boot

The `thermite-first-boot` unit runs once, after the network is up:

1. As root, it adds a temporary sudo rule letting the owner run `thermite
   install` without a password.
2. As the owner, it waits up to 15 minutes for the Internet, creates an
   empty house with `thermite init --empty ~/thermite-site` in the time
   zone you chose, commits it to a new Git repository, and runs `thermite
   install --config ~/thermite-site`, answering its one approval. Any other
   question is declined, and a password prompt stops it.
3. As root, whatever happened, it removes the sudo rule, writes the login
   message and disables itself.

Each interactive login shows its status: while it runs (with `journalctl
-fu thermite-first-boot.service`), why it stopped, or once that it
finished. Its log is `/var/log/thermite-install/first-boot.log`. After a
failure, `sudo systemctl start thermite-first-boot.service` tries again and
skips what is done; `/opt/thermite/FIRST-BOOT.txt` also has the steps by
hand. The house starts empty: add your rooms and equipment to
`instance/devices.yaml` and run `thermite install` again.

The first `thermite install` prepares Thermite's own owner-managed
application environment from PyPI and selects it through
`~/.local/bin/thermite`; log out and back in afterwards so `thermite` runs
it. Commissioning creates the non-login `thermite:thermite` service
identity, UID/GID 9001; see [service ownership](service-account.md).
Update later with `thermite upgrade --config "$HOME/thermite-site"`
([application releases](packaging.md#update-and-roll-back)). Arch system
upgrades can change Python's minor version; the next install or upgrade
rebuilds an application environment built for an older one. Keep
application and private-site backups.

## Packages

`iso/packages.json` is the one list of packages the Arch image installs.
The installer writes `archinstall`'s package list and its package check from
it, and a test holds this table to it. The first tier is everything
`thermite install`'s Packages step installs, so that step finds nothing to
do; install itself still checks only those. The other tiers are for the
person at the console or on SSH.

| Tier | Packages |
|---|---|
| Thermite's own requirements | `podman` `runc` `util-linux` `git` `make` `python` `python-yaml` `nodejs` `npm` `openssl` `nftables` |
| Host services | `sudo` `openssh` `rsync` `avahi` `nss-mdns` `networkmanager` `wpa_supplicant` `zram-generator` `pacman-contrib` `kernel-modules-hook` |
| Owner's picks | `btop` `emacs-nox` `jq` `miller` |
| Operator tools | `nmap` `tcpdump` `bind` `arp-scan` `mtr` `traceroute` `ethtool` `socat` `openbsd-netcat` `inetutils` `ngrep` `usbutils` `picocom` `dmidecode` `go-yq` `ripgrep` `less` `htop` `lsof` `strace` `iotop-c` `smartmontools` `nvme-cli` `ncdu` `lm_sensors` `tmux` `bash-completion` `man-db` `man-pages` `nano` `vim` `tree` `pv` |
| Terminal descriptions | `kitty-terminfo` `ghostty-terminfo` `rxvt-unicode-terminfo` `foot-terminfo` |
| Btrfs only | `compsize` |

Some of these packages ship units that stay disabled: `named` (bind is here
for `dig`), `nftables` (Arch's stock ruleset admits only SSH; Thermite loads
its own native-port boundary), `rsyncd` (rsync runs over SSH), `smartd`,
`avahi-dnsconfd`, `gpm`, the `lm_sensors` units and the `inetutils` servers.

## Optional Ubuntu Server image

Pass `--base ubuntu` to both commands:

```sh
thermite iso fetch --base ubuntu --cache .scratch/iso/cache
thermite iso build --base ubuntu --cache .scratch/iso/cache --work .scratch/iso/work --out .scratch/iso/thermite-ubuntu.iso
```

This uses the pinned Ubuntu Server 24.04.5 LTS amd64 installer and an
ABI-pinned PyYAML wheel. Only `xorriso` is needed for this variant; allow
roughly 12 GB of free build space. Its base OS and application installation
can complete offline. Skip unavailable mirrors or updates if necessary.
Runtime commissioning still needs Internet access.

Boot Ubuntu's install entry, keep the bundled installer version, choose the
disk and owner login, and review Ubuntu's destructive-storage confirmation.
Every normal installer screen remains interactive. No `autoinstall` kernel
flag, storage plan or identity defaults are added. Late commands copy the
bundled media into the installed target, install `python3-venv` and `git`
there when the media or a mirror provides them, then verify the bundled wheels
and create the application environment without pip or network access. An
offline installation without those packages still completes; the first-boot
instructions give the one command to install them before `thermite install`.
The builder refreshes Ubuntu's media checksum list for replaced and added
files. Reboot from the installed disk and log in as your chosen owner. Follow
`/opt/thermite/FIRST-BOOT.txt` from there: the Ubuntu image has no
first-boot unit.

## Validation and limits

Offline checks cover the input pins and their digests (Node.js and blessed
included, with downloads mocked), the staged medium and its payload, the
key check and fingerprint, answer checking, `archinstall`'s configuration
against its 4.5 schema and layout rules, the installation run with a fake
`archinstall` (no secret on disk afterwards), the installer terminal driven
through every step against the real core at four sizes in both themes, the
plugin's changes to a fake target, the first-boot unit's sudo rule and its
pseudo-terminal approval, both boot configuration formats, EFI partition
bounds, safe wheel extraction and the login profile's PATH order under the
available POSIX shells. A real remaster also requires the pinned image and
the build tools.

A complete acceptance run must boot the image in firmware (UEFI and BIOS),
answer the installer on the console and over SSH, install to an explicitly
disposable disk, log in after removing the ISO, watch the first boot finish
and inspect the services and remote. A successful remaster or unit suite
alone does not prove that path. Firmware, storage drivers and physical
hardware need acceptance on the intended machine as well.

The implementation follows the official
[Arch release](https://archlinux.org/releng/releases/2026.11.01/),
[Arch live startup hook](https://github.com/archlinux/archiso/blob/master/configs/releng/airootfs/root/.automated_script.sh),
[Arch boot image construction](https://github.com/archlinux/archiso/blob/master/archiso/mkarchiso),
[Archinstall 4.5 guided flow](https://github.com/archlinux/archinstall/blob/4.5/archinstall/scripts/guided.py),
its [configuration](https://github.com/archlinux/archinstall/blob/4.5/examples/config-sample.json)
and [plugin hook](https://github.com/archlinux/archinstall/blob/4.5/archinstall/lib/installer.py).
The Ubuntu variant follows the official
[Subiquity interactive configuration](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html)
and [confirmation behavior](https://canonical-subiquity.readthedocs-hosted.com/en/latest/howto/autoinstall-quickstart.html).
Boot metadata is preserved with
[xorriso replay](https://www.gnu.org/software/xorriso/man_1_xorriso.html).
Node.js comes from [nodejs.org](https://nodejs.org/dist/v26.13.0/), blessed
from the [npm registry](https://registry.npmjs.org/blessed/0.1.81).
Ubuntu checksums come from [Ubuntu](https://releases.ubuntu.com/24.04/SHA256SUMS)
and its Python dependency from [PyPI](https://pypi.org/project/PyYAML/6.0.3/).
