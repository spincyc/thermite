# Bootable installer ISO

The Thermite ISO uses the pinned official Arch Linux 2026.11.01 x86_64
installation medium and its native `archinstall` guided installer. It adds
the reviewed Thermite application wheel and first-boot instructions. Build
it as an ordinary user with `xorriso` and `mtools`; no root, loop device or
container daemon is required. The resulting image retains upstream BIOS
and UEFI boot support. Secure Boot is not supplied by this recipe.

The owner chooses the target disk, storage layout, hostname, network and
login credentials. No disk, password, private house or API token is embedded.
Arch installation needs Internet access for signed OS packages. The bundled
application installs after the OS is ready, without pip or a separate
application download. Runtime commissioning follows first boot and requires
Internet access for runtime downloads and container images.
The live medium is pinned; installed Arch OS package versions follow the
current repositories and are not frozen by the ISO's provenance receipt.

For a stock Arch installation followed by the standalone application,
start with [installation](installation.md). An optional Ubuntu Server ISO
retains the offline OS and application installation described below.

## Build

Install `xorriso` and `mtools` with the build host's package manager. From an
installed Thermite application or reviewed source tree:

```sh
thermite iso fetch --cache .scratch/iso/cache
thermite iso build --cache .scratch/iso/cache --work .scratch/iso/work --out .scratch/iso/thermite.iso
```

In a source checkout, substitute `python3 bin/thermite` for `thermite`.
Allow roughly 6 GB of free space for the Arch base, staging and result.
Fetch downloads the fixed HTTPS base image and verifies the exact SHA256
in `iso/inputs.json`. A corrupt existing cache fails and is not overwritten.
Build uses the verified cache and the privacy-reviewed public application
payload. The selected private site is never copied into the image.

The outputs are `thermite.iso`, `thermite.iso.sha256` and
`thermite.iso.json`. The receipt records the distribution, base digest,
application release and wheel digests. Existing outputs are refused. Choose
a new filename when rebuilding. An interrupted image without its checksum
and receipt is incomplete. Keep the three completed files together and
verify the image before use:

```sh
cd .scratch/iso
sha256sum --check thermite.iso.sha256
```

Build the final image after all application and documentation edits: the
application release identifies the exact reviewed program bytes. Remastering
changes the overall ISO hash. The builder checks the pinned upstream SHA256;
it does not verify detached release signatures. Those can be verified
separately with the distribution's release key. The generated checksum
checks the image you built.

The builder replays upstream's hybrid boot metadata. For Arch it updates
both BIOS Syslinux entries and UEFI systemd-boot entries, including the
copies inside the appended EFI FAT partition. It preserves Arch's source
UUID and signed live filesystem. It also updates the GRUB loopback entries
used when another bootloader opens the ISO as a file.

## Install a machine with Arch

1. Attach the ISO to a new VM, or write it to USB with a tool that displays
   the destination and asks before overwriting it. Thermite supplies no
   command that guesses a USB or system disk.
2. Boot the `Thermite installation (Arch Linux)` entry. Its live console
   explains the process before opening the native guided installer.
3. Connect the live system to the Internet. Wired DHCP is normally automatic.
   For Wi-Fi, press Ctrl-C, configure the connection with `iwctl`, then run
   `bash /run/archiso/bootmnt/thermite/arch-install.sh`. Press Enter to run
   the plugin-loading, network, keyring and package dependency checks. A failed preflight
   exits before the disk installer starts. A later network failure can still
   interrupt installation; successful preflight cannot guarantee connectivity
   for the entire package transfer.
4. In `archinstall`, choose mirrors, disk configuration, a bootloader,
   hostname, locale and timezone. Select the intended disk and review the
   proposed layout. Native Arch owns partitioning and its final destructive
   confirmation. Nothing selects a disk or starts installation automatically.
5. Create your own normal user with a password and sudo access. Use that
   account for Thermite; reserve `thermite` for the non-login service account
   created during later commissioning. Keep the preselected packages listed
   under [Packages](#packages).
   NetworkManager is preselected for the installed network; configure the
   connection appropriate to your machine. These settings remain visible
   and editable in the native menu.
6. Select Install and inspect the native summary and storage confirmation.
   Accept only when the selected disk and layout are correct. Existing data
   on selected partitions can be destroyed at this point.
7. Wait for the Thermite application success message before the native
   post-installation Reboot menu. Remove the installation medium and boot
   the installed disk. Log in as your owner account.

The local Arch startup hook runs on the live medium's first virtual console.
A serial-only console can invoke the same script manually. Keep the bundled
`archinstall` 4.5; the wrapper refuses an incompatible installer version.
Its configuration contains required packages and a network-manager choice,
with no storage or identity configuration and no silent-install flag.

The supported `on_genfstab` plugin hook runs after the native installer has
installed the OS, bootloader, network and owner account, before its reboot
menu. It checks that the target is a mounted Arch installation with a normal
owner login, copies the media payload into `/opt/thermite-media`, then runs
the bootstrap inside that target with `arch-chroot`. Bootstrap verifies the
application wheel, creates `/opt/thermite/venv`, checks system PyYAML is
within `>=6,<7`, and adds the `thermite` launcher, login instructions and
a login profile that puts `~/.local/bin` first on PATH.
It uses the signed OS `python-yaml` package to match Arch's Python ABI.
A failed hook fails installation; it must not be treated as a completed
Thermite image. No runtime services or house are deployed by this hook.

## Packages

`iso/packages.json` is the one list of packages the Arch image installs.
The image renders archinstall's package list and its live preflight from it,
and a test holds this table to it. The first tier is everything
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
files. Reboot from the installed disk and log in as your chosen owner.

## First boot

The login instructions are also in `/opt/thermite/FIRST-BOOT.txt`. Log in as
your owner account and connect to the Internet. On the Ubuntu image, first
make sure the prerequisites are present; nothing changes when they already
are:

```sh
sudo apt-get update && sudo apt-get install python3-venv git
```

Then create the private site, replace its fictional inventory, validate and
commit it, and deploy it with one approval:

```sh
thermite init "$HOME/thermite-site"
cd "$HOME/thermite-site"
thermite check --config "$HOME/thermite-site"
git init --initial-branch=main
git add .
git commit -m 'Initialize private Thermite site'
thermite install --config "$HOME/thermite-site" --dry-run
thermite install --config "$HOME/thermite-site"
```

Edit `instance/devices.yaml` and `instance/host.yaml` before `thermite check`.
Installation prints the HTTPS URL and the certificate authority file each
browser or terminal client must trust once. Installation switches
`~/.local/bin/thermite` to the application it verified; log out and back in
afterwards. On the Arch image, `/etc/profile.d/thermite.sh` then puts
`~/.local/bin` first on PATH, as Ubuntu's default `~/.profile` does once that
directory exists, so `thermite` runs that application rather than the image's
`/usr/local/bin/thermite`.
[Installation](installation.md#create-your-private-site) describes each step.

Commissioning creates the non-login `thermite:thermite` service identity,
UID/GID 9001. Your owner login continues to own private Git and run the CLI.
Log out and back in for its new group membership to apply. See
[service ownership](service-account.md).

The ISO application environment contains no pip. The first
`thermite install` prepares Thermite's own owner-managed application
environment and selects it through `~/.local/bin/thermite`; update later with
`thermite upgrade --config "$HOME/thermite-site"`
([application releases](packaging.md#update-and-roll-back)). Arch system
upgrades can change Python's minor version; the next install or upgrade
rebuilds an application environment built for an older one. Keep application
and private-site backups.

## Validation and limits

Offline checks cover input digests, refusal of unsafe outputs, native
storage/identity defaults, both boot configuration formats, EFI partition
bounds, owner-account checks, hook-failure propagation, safe wheel extraction,
the installed launcher outside the source tree and the login profile's PATH
order under the available POSIX shells. A real remaster also
requires the pinned image and the build tools.

A complete acceptance run must boot the image in firmware, inspect the
visible guided installer, select an explicitly disposable blank disk,
confirm its storage plan, complete OS installation and log in after removing
the ISO. Then commission a fictional private house and inspect the actual
services and remote. A successful remaster or unit suite alone does not
prove that complete path. Firmware, storage drivers and physical hardware
need acceptance on the intended machine as well.

The implementation follows the official
[Arch release](https://archlinux.org/releng/releases/2026.11.01/),
[Arch live startup hook](https://github.com/archlinux/archiso/blob/master/configs/releng/airootfs/root/.automated_script.sh),
[Arch boot image construction](https://github.com/archlinux/archiso/blob/master/archiso/mkarchiso),
[Archinstall 4.5 guided flow](https://github.com/archlinux/archinstall/blob/4.5/archinstall/scripts/guided.py)
and [plugin hook](https://github.com/archlinux/archinstall/blob/4.5/archinstall/lib/installer.py).
The Ubuntu variant follows the official
[Subiquity interactive configuration](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html)
and [confirmation behavior](https://canonical-subiquity.readthedocs-hosted.com/en/latest/howto/autoinstall-quickstart.html).
Boot metadata is preserved with
[xorriso replay](https://www.gnu.org/software/xorriso/man_1_xorriso.html).
Ubuntu checksums come from [Ubuntu](https://releases.ubuntu.com/24.04/SHA256SUMS)
and its Python dependency from [PyPI](https://pypi.org/project/PyYAML/6.0.3/).
