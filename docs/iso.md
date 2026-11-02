# Bootable installer ISO

The Thermite ISO combines the pinned Ubuntu Server 24.04.5 LTS amd64
installer with the reviewed Thermite application and a checksum-pinned
PyYAML wheel. It is a hybrid BIOS/UEFI installation image. Build it as an
ordinary user; neither root nor a container daemon is required.

The image installs Ubuntu and the application. It asks the owner to choose
the target disk, network configuration and login account. It contains no
private inventory, default password, API token or automatic disk selection.
The runtime is commissioned after first boot, following
[installation](installation.md), with Internet access for packages, runtime
downloads and container images.

## Build

Install `xorriso` using your build host's package manager. From an installed
Thermite application or reviewed source tree:

```sh
thermite iso fetch --cache .scratch/iso/cache
thermite iso build --cache .scratch/iso/cache --work .scratch/iso/work --out .scratch/iso/thermite.iso
```

In a source checkout, substitute `python3 bin/thermite` for `thermite`.
Allow roughly 12 GB of free build space. Fetch downloads the base image and
PyYAML over HTTPS, then verifies their exact SHA256 values in
`iso/inputs.json`. A corrupt existing cache fails and is not overwritten.
Build reads only those cached inputs. The application wheel is built from
the privacy-checked public payload, including its fictional init templates;
the selected private site is never copied into the image.

The output is `thermite.iso`, `thermite.iso.sha256` and `thermite.iso.json`.
The receipt records the base digest, application release and wheel digests.
Existing outputs are refused. To rebuild, choose a new output filename.
An interrupted build without a checksum/receipt is incomplete. Keep all
three completed files together and verify the image before use:

```sh
cd .scratch/iso
sha256sum --check thermite.iso.sha256
```

The application release is tied to the exact reviewed program bytes. Build
the final image after all application and documentation edits. Remastering
changes the overall ISO hash. The builder checks the repository's pinned
base SHA256; it does not verify Ubuntu's detached signature. That signature
can be verified separately against Ubuntu's release key. The generated
checksum detects changes to your built image.
The builder replays upstream's boot metadata and refreshes Ubuntu's media
checksum list for every replaced or added file.

## Install a machine

1. Attach the ISO to a new VM, or write it to a USB device using a tool that
   shows the destination device and asks before overwriting it. Thermite
   intentionally supplies no command that guesses a USB or system disk.
2. Boot the image and choose the Ubuntu Server install entry. Keep the
   bundled installer version when offered an update. Every normal installer
   screen remains interactive.
3. Select the intended disk, inspect the proposed storage layout, and accept
   Ubuntu's explicit destructive-storage confirmation only when it is right.
   Existing data on selected partitions can be destroyed at this point.
4. Create your own owner login and network configuration. No account or
   password is supplied by Thermite. Standard Ubuntu Server is the reference
   installation source. The base OS and Thermite payload can be installed
   offline; skip unavailable mirrors/updates if necessary.
5. Complete installation, remove the installer medium and reboot from the
   installed disk. Log in with the account you created.

The build adds no `autoinstall` kernel flag, storage plan or identity section.
The native install configuration explicitly keeps all ordinary screens
interactive. Thermite's only installation hook runs after Ubuntu has
installed the selected target: it copies the bundled wheels, verifies them,
creates `/opt/thermite/venv` without pip or network access, and adds the
`thermite` command and login instructions. It does not partition disks or
start runtime services.

First-boot commissioning creates the non-login `thermite:thermite` service
account, UID/GID 9001, and gives it the deployed runtime files. Your login
continues to own the private site and run the CLI. The application container
processes and native access services run under that service identity. The
host's Podman backend and systemd manager remain root-managed. See
[service ownership](service-account.md).

## First boot

The login message points to `/opt/thermite/FIRST-BOOT.txt`. Run
`thermite --version`, then follow [installation](installation.md) from
“Create your private site.” The owner edits the example, initializes private
Git, commits generated configuration, and reviews `thermite install
--dry-run` before installing runtime services.

The ISO's environment contains no pip. To update it using pip, the owner can
install Ubuntu's `python3-venv` package, then run
`sudo /opt/thermite/venv/bin/python3 -m ensurepip` and install a new reviewed
wheel there. Keep application and private site backups and complete the
explicit site upgrade described in [packaging](packaging.md).

## Validation and limits

Offline tests exercise seed safety, boot configuration patching, input
checksums, wheel extraction into a synthetic filesystem and the installed
launcher from outside the source tree. An actual image build additionally
requires the pinned multi-GB base and xorriso. A VM boot test must inspect
the firmware boot, installer UI, storage confirmation and installed first
boot; a successful remaster command alone does not prove those behaviors.

Hardware boot, complete disk installation, Secure Boot, firmware drivers and
runtime commissioning require acceptance on the intended hardware. This
development recipe does not claim those checks solely from offline tests.

The implementation follows the official
[Subiquity interactive configuration](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html),
[installer confirmation behavior](https://canonical-subiquity.readthedocs-hosted.com/en/latest/howto/autoinstall-quickstart.html)
and [xorriso boot image replay](https://manpages.debian.org/trixie/xorriso/xorriso.1.en.html).
Base checksums come from [Ubuntu](https://releases.ubuntu.com/24.04/SHA256SUMS)
and the Python dependency from [PyPI](https://pypi.org/project/PyYAML/6.0.3/).
