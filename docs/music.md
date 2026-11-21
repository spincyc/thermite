# Music

A host whose `instance/host.yaml` has `music:` runs
[Music Assistant](https://music-assistant.io) beside openHAB. It plays TIDAL
and your own music libraries on the house's Sonos and WiiM speakers, alone or
grouped. Thermite runs the version `versions.json` pins (`music_assistant`,
2.10.12, by image digest) and keeps its state. The gateway claims Music
Assistant and configures it; Remotes plays it in a room's Listen. A host
without `music:` runs no Music Assistant and installs nothing of it.

## Turn it on

Add `music:` to `instance/host.yaml`, naming each library and the host
directory it is in:

```yaml
music:
  libraries:
    flac: /mnt/music/flac
    mp3: /mnt/music/mp3
```

A library's name is lowercase letters, digits and `_`, starting with a letter,
at most 32 characters. Its directory is a normalized absolute path of letters,
digits and `._@+-`, and no two libraries share one. `music: {}` runs Music
Assistant with no library, for TIDAL alone. `port` and `stream_port` move its
two listeners; see [ports](#ports).

A private site pins its own native versions: copy the `music_assistant` entry
from Thermite's `versions.json` into the site's, or generating refuses the
host. Commit both changes, then run `thermite upgrade --config DIR` (or
`thermite install`). It pulls the image, about 760 MB, before it stops anything.
Upstream recommends 2 GB of RAM for Music Assistant; plan it on top of the
[stack's own](requirements.md).

## Mount the libraries

Each library's directory must be there before Music Assistant starts. For a
NAS, declare its share in `instance/host.yaml` and Thermite mounts it, at
boot and before Music Assistant (see
[network shares](installation.md#network-shares)):

```yaml
shares:
  music:
    type: cifs
    server: nas.example
    share: music
    where: /mnt/music
music:
  libraries:
    flac: /mnt/music/flac
    mp3: /mnt/music/mp3
```

A library may also be on a local disk, or on a share you mount yourself from
`/etc/fstab`; Thermite then only reads it.

The `thermite` account, UID 9001, must be able to read the files: an SMB
share Thermite mounts gives them to it, an NFS export must let UID 9001
read. Music Assistant sees each library at `/media/<name>`, read-only.
Quadlet makes the service require the mounts its libraries are on, so Music
Assistant starts after them (after a Thermite share's wait for the NAS's
name, too), and does not start while one of them cannot be mounted or a
library's directory is missing. An unreachable share therefore fails an
install's or upgrade's start of Music Assistant, and with it the install
transaction, which rolls back. Stopping a share's mount unit stops Music
Assistant too; unmounting it by hand does not.

Music Assistant sees only what was mounted when it started: a share mounted,
or mounted again, after it started stays invisible to it, and the library
looks empty. Thermite's shares are mounted before it starts, and never
mounted on first access for that reason. After mounting one yourself,
restart it:

```sh
sudo systemctl restart thermite-music.service
```

Thermite never relabels your files. Without podman's `:z` or `:Z` volume
options a source keeps its SELinux label, so on an SELinux host label the
directories for containers (`container_file_t`) or mount the share with a
`context=` option. Ubuntu and Arch, with AppArmor or neither, need nothing.

## What Thermite runs

`thermite-music.service` is a Quadlet container, `thermite-music`, installed,
upgraded, started, stopped, reported and removed like Thermite's other
services: it is part of `thermite.target` and runs in `thermite.slice`.

- It runs as the `thermite` account, with no capabilities, no new privileges
  and umask 0077. It uses the host's network: speakers are found by mDNS and
  SSDP, and fetch what they play from it.
- Its state is `runtime/music/` (`/srv/thermite/runtime/music` by default),
  0700, mounted at `/data`: its settings, library database, accounts and
  credentials, artwork cache (up to about 500 MB) and `musicassistant.log`.
- Before each start, the container's Python pins the two listeners in
  `settings.json`: the API on 127.0.0.1, the streams on every address, on
  Thermite's ports. Every other setting stays as it is. A new state also
  marks the providers Music Assistant would set up by itself as done, so that
  it sets up none: the gateway sets up the ones the house uses.
- A new Music Assistant's first account is claimed without a password, by
  whoever asks first; the gateway asks as soon as Music Assistant runs. Until
  then, for the seconds after its first start, any program on the host could
  claim it first. Its API listens on loopback alone, behind the native
  firewall, so nothing on the LAN can.
- Music Assistant handles a stop itself and has 30 seconds to close its
  library database and save its settings.
- The image has no health check, and the unit adds none. The install waits up
  to 10 minutes for Music Assistant to run (an upgrade may migrate its library
  first) with its API on loopback alone. `thermite status` checks the same, and
  that its streams answer.

## Ports

| Port | Listener | Binding |
| --- | --- | --- |
| 13627 | Music Assistant's API and web interface; `music: port` moves it | 127.0.0.1 only; the gateway is its one client |
| 13628 | Audio streams; `music: stream_port` moves it | Every address: the speakers fetch what they play from it |

The managed native firewall table blocks non-loopback traffic to the API's
port too, and the unit reloads it before every start: Music Assistant falls
back to every address when it cannot listen where its settings say, and the
firewall still refuses the LAN there. Health and status report such a
fallback. Music Assistant also opens listeners of its own; see
[ports](ports.md).

Health and status reach the streams on 127.0.0.1, from the host itself. A
host with its own firewall (ufw, firewalld) must allow, from the LAN, the
stream port, TCP 8927 (Sendspin) and mDNS (UDP 5353), or the speakers cannot
play while every check passes.

## AirPlay timing

AirPlay 2 times its audio with PTP, on UDP ports 319 and 320. Those ports are
privileged and Music Assistant runs with no capability, so it falls back to
NTP timing and says so in its log ("PTP timing is degraded"). AirPlay
speakers grouped with others may drift a little more. Thermite leaves it so.
If you want PTP, let every unprivileged program bind ports from 319 up, then
restart Music Assistant:

```sh
echo 'net.ipv4.ip_unprivileged_port_start=319' | sudo tee /etc/sysctl.d/60-unprivileged-ports.conf
sudo sysctl --system
sudo systemctl restart thermite-music.service
```

That setting is the host's, not Thermite's: any program may then take a port
from 319 to 1023 before the service it belongs to does.

## Upgrades and backups

A new Music Assistant migrates its library database and settings on its first
start, and has no way back. When `versions.json` pins a new image, the install
transaction copies `runtime/music/` aside before Music Assistant starts, so a
failed upgrade's rollback, or `thermite rollback --restore-state`, puts back the state the older
image wrote. The room the upgrade checks for includes that copy; while the pin
stays the same, the state stays in place and is not copied.

Removing `music:` removes the unit and keeps `runtime/music/`; adding it back
picks the state up again. Delete it by hand once you no longer want it.
`thermite uninstall` keeps it; `thermite install --clean` and `thermite
uninstall --purge` delete it.

The installation root's snapshot or backup includes `runtime/music/`. For a
consistent copy of it alone, stop Music Assistant first; its library database
is SQLite, written as it plays:

```sh
sudo systemctl stop thermite-music.service
sudo tar -C /srv/thermite/runtime -czf music-backup.tar.gz music
sudo systemctl start thermite-music.service
```

Keep the copy private: `settings.json` holds TIDAL's tokens and the key that
decrypts them. If `settings.json` and its `.backup` cannot be read,
Music Assistant does not start and its journal says so; restore one, or move
both aside to start afresh.
