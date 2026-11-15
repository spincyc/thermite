# Music

A host whose `instance/host.yaml` has `music:` runs
[Music Assistant](https://music-assistant.io) beside openHAB. It plays TIDAL
and your own music libraries on the house's Sonos and WiiM speakers, alone or
grouped. Thermite runs the version `versions.json` pins (`music_assistant`,
2.10.12, by image digest) and keeps its state. The gateway claims Music
Assistant and configures it; see the Music page. A host without `music:` runs
no Music Assistant and installs nothing of it.

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

Commit the change, then run `thermite upgrade --config DIR` (or `thermite
install`). It pulls the image, about 760 MB, before it stops anything.
Upstream recommends 2 GB of RAM for Music Assistant; plan it on top of the
[stack's own](requirements.md).

## Mount the libraries

Thermite reads the libraries but never mounts them: making each directory
available is yours to do, before Music Assistant starts. Mount a NAS share
from `/etc/fstab`, read-only, so that it is there whenever the host boots:

```text
192.0.2.20:/volume1/music  /mnt/music  nfs  ro,_netdev,nofail  0  0
```

The `thermite` account, UID 9001, must be able to read the files. Music
Assistant sees each library at `/media/<name>`, read-only. Quadlet makes the
service require the mounts its libraries are on: Music Assistant starts after
them, does not start while one of them fails to mount or a library's
directory is missing, and stops when one is unmounted. Once the share is back,
`sudo systemctl start thermite-music.service` starts it again. A share mounted
by hand, outside `/etc/fstab`, is not waited for: until it is mounted the
library looks empty.

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
- Music Assistant handles a stop itself and has 30 seconds to close its
  library database and save its settings.
- The image has no health check, and the unit adds none. The install waits up
  to 10 minutes for Music Assistant to run (an upgrade may migrate its library
  first) with its API on loopback alone. `thermite health --production` and
  `thermite status` check the same, and that its streams answer.

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
failed upgrade's rollback, or `thermite revert`, puts back the state the older
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
