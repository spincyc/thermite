# System requirements

Use one always-on x86-64 Linux machine, or a VM with the resources below.
Arch Linux is the primary installation path: install Thermite on stock Arch,
or use the Arch-based [Thermite ISO](iso.md). Ubuntu Server 24.04 LTS amd64
is an alternative for an existing server and the optional Ubuntu ISO.

## Plan the whole stack

These are **Thermite planning estimates**, not benchmarked capacity limits
or upstream minimums. They cover the operating system, openHAB, the access
services, device helpers, Prometheus, Grafana, exporters and notifications
for light household use. They do not establish a supported device count.

| Resource | Minimum planning profile | Recommended starting profile |
|---|---|---|
| CPU | 2 x86-64 cores or vCPUs | 4 x86-64 cores or vCPUs |
| RAM | 4 GiB | 8 GiB |
| Dedicated machine or VM disk | 64 GB SSD | 128 GB SSD |
| Additional free disk on an existing server | 40 GB | 80 GB |

For a VM, reserve these resources for the guest and leave capacity for its
host. On a shared server, leave CPU and RAM available beyond other
workloads. The 4 GiB profile leaves limited headroom; prefer 8 GiB when
choosing a new allocation. More bindings, complex rules, concurrent
dashboards, camera streams and other applications can require more.
Observe CPU, available RAM, storage growth and response times during your
actual use before relying on a final allocation.

A dedicated GPU is not required for the core automation stack. Camera
streaming and transcoding need separate sizing. A monitor and keyboard are
useful for installation; routine administration can use SSH and a browser.

## Software and installation

The host needs systemd, Podman 4.4 or later, a kernel with overlayfs,
Python 3.11 or later, Git and make. Node.js and npm support terminal controls
and runtime checks. The [server guide](installation.md) prepares the
application environment and the runtime installer installs service packages.
Run the CLI as your own account, with sudo available for the root commands
it presents. Application services use `thermite:thermite`, UID/GID 9001;
those IDs must be available or already belong to Thermite.

The complete installer currently accepts x86-64 only. Some container pins
also include ARM images, but the locked Apple TV helper includes miniaudio
without a Linux aarch64 wheel. ARM and 32-bit machines are not installation
targets for this release.

Arch installation needs Internet access for signed OS packages. Runtime
commissioning on either distribution needs downloads of pinned runtimes,
Python dependencies and container images. The optional Ubuntu ISO can
install its bundled OS and application offline; that does not make runtime
commissioning offline. Building an ISO needs separate build space as
described in the [ISO guide](iso.md).

## Storage and metrics

Keep service state on a local POSIX filesystem, such as ext4 or XFS.
Do not put the Prometheus data directory on NFS. SSD storage is preferable
for the database and frequent writes.

The default metrics policy is **395 days or 12 GB, whichever is reached
first**, at a 30-second scrape interval. It does not guarantee 395 days of
history. Prometheus interprets its `12GB` setting as 12 GiB. Reserve at
least 15 GiB for Prometheus alone: its retention limit is not a filesystem
quota, and compaction, the write-ahead log and current head data need room.
Upstream recommends keeping the retention limit at no more than 80–85% of
allocated Prometheus storage. [Prometheus storage guidance](https://prometheus.io/docs/prometheus/latest/storage/)

The remaining disk budget covers container images, runtime downloads,
application releases, native state, logs and backup staging. On an existing
server, check free space on both the configured state filesystem and
Podman's container-storage filesystem; free space on another mount does
not satisfy either requirement. Retain backups outside this machine and
budget separately for long-term archives. Raising retention or collecting
more time series requires reviewing storage again.

## Network

Prefer wired Ethernet with a stable address and working DNS and time
synchronization. A VM needs bridged LAN access or an equivalent arrangement
that you have verified for multicast discovery and device callbacks.
Plain NAT and port forwarding alone do not establish that behavior.
Thermite's containers use host networking; UPnP discovery is limited to the
local subnet. [openHAB networking](https://www.openhab.org/docs/installation/docker.html#explanation-of-arguments-passed-to-docker)

Allow the selected services through your host firewall and provide trusted
TLS and a resolvable hostname for authenticated access. See the
[ports](ports.md) and [installation](installation.md#set-up-https-and-accounts)
guides. Pass through any USB radio or serial adapter your integrations need.

## Basis and review

Reviewed **2026-11-02** for application `0.1.0.dev0`, openHAB **5.2.3**,
Grafana **13.3.0**, Prometheus **3.15.1**, and the default retention above.
The installation baselines are Arch Linux x86-64 (2026.11.01 ISO; installed
packages follow Arch's rolling repositories) and Ubuntu Server 24.04.5 LTS
amd64. Runtime pins are recorded in `versions.json`; ISO inputs are recorded
in their input manifests.

The estimates account for these upstream facts:

- [openHAB's stable installation guide](https://www.openhab.org/docs/installation/) requires a 64-bit OS and Java 21. Its [x86 hardware guidance](https://www.openhab.org/docs/installation/openhabian.html#hardware) recommends at least 4 GB RAM, with 8 GB preferable.
- [Grafana's current guidance](https://grafana.com/docs/grafana/latest/setup-grafana/installation/#hardware-recommendations) gives 512 MB and one core as an evaluation floor. Its small-deployment baseline is 2 cores and 2–4 GB for Grafana alone, excluding Prometheus. Household usage may be lighter; these figures do not certify Thermite's minimum profile.
- [Ubuntu's 24.04 amd64 requirements](https://ubuntu.com/server/docs/reference/installation/system-requirements/) list 1.5 GB RAM and 5 GB disk for ISO installation, and suggest 3 GB RAM and 25 GB disk. Those are OS figures, not the application stack's budget.

End-to-end acceptance of the Thermite ISO, stock Arch plus Thermite, and
Ubuntu Server plus Thermite with a fictional house is pending. Offline
checks do not prove machine installation or runtime commissioning.

Review this page when application/runtime versions, helper wheel support,
ISO inputs, supported installation paths, metrics retention or enabled
services change. Update the baseline date and acceptance evidence with that
review.
