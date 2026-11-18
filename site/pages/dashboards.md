# Dashboards

See device availability, room history and host health in Grafana, from a phone or computer on your network.

## What runs

`thermite install` sets up these monitoring services from images pinned by digest. Their application processes run as the service account. Only Grafana accepts connections from other machines.

| Service | Port | What it does |
|---|---|---|
| Prometheus | 13607, this machine only | collects metrics every 30 seconds, and device ports every 2 minutes; retains 395 days or 12 GB, whichever comes first |
| node_exporter | 13608, this machine only | the machine itself: CPU, memory, disks, temperatures, and thermite's services; also the gateway's, policy worker's and callback relay's own metrics, which they write to files rather than serve |
| blackbox_exporter | 13609, this machine only | whether each device answers on the port thermite controls it through (except where a connection would cost the device a session or a TLS handshake: Android TV, Cast, WiiM, AVPro and Sony projector ports, which their Things' status covers), and whether the remote gateway answers over HTTPS |
| thermite's exporter | 13610, this machine only | each device's status, power, input and volume, by room |
| Grafana | 13611, your network | the dashboards |

Prometheus also reads runtime memory, threads, events and rule runs through the Metrics add-on. `thermite test boot` checks that it answers. See [storage requirements](requirements.html#storage-and-metrics) for the retention budget.

## What you see

Open `http://<thermite's address>:13611/`. Anyone on your network can look without logging in. Six dashboards come with it:

- **House health**, the home page: is the house OK? Tiles answer it at a glance. First the house's: control, rooms ready, speakers ready where the house has speakers, and remotes; **Needs attention now**, right below them, names each room, problem and device in plain words. Then monitoring's: alerts firing and the host, with the alerts that are firing and switching since yesterday evening.
- **Switching and remotes**: how long source changes take and how they end, the routing executor and any quarantined device, the remote gateway's latency, answers, requests in flight and event streams (how many, how long they last, what fails them, and the live updates they carry), what remote pages gave up waiting for, and the room policy worker.
- **Devices**: one row per device, those in trouble first, with its Thing, control port, whether it answers thermite's reads, ping and whether a room needs it now. **Last problem** says the remotes still show what its last failed read or command met; that stays until the device takes a command, so it is not a reading of the device now. Then the Things that aren't online, and in what status; the commands thermite's scripts took for each device and how they ended; every room's sources, power and speakers through the day, and listening levels. **Room**, at the top, keeps the dashboard to the rooms you choose.
- **openHAB runtime**: memory, thread pools, the thingHandler pool, how long its REST API takes to answer, events, the busiest topics and rules, and the Things that were away longest.
- **Host**: how many scrape targets are up and which are down, thermite's services and their restarts, alerts firing, CPU, memory, disk space and I/O.
- **Usage**: each room's source through the day, which sources the house used and for how long, what each device played, when and in which rooms, and where speakers played. It is not a health signal.

Colour means one thing on every dashboard, in tiles, tables and charts alike: green is OK, amber a warning, red a failure, grey unknown, slate Off, and tan use: a source selected, a device on, or how much of something a chart measures. A chart's lines of one kind are shades of tan, some dashed; point at the chart to read which is which. A red band across a chart is a limit. Dashed vertical lines mark service starts and deploys on the dashboards with charts; switch them off at the top.

A tile, or a room in a table, is a link where another dashboard says more: a room opens Devices for that room. **Dashboards**, at the top, lists them all.

Control health is made of checks, and each reads what holds now: a device's Thing is online, its control port accepts a connection, it is on while a room uses it, and, where thermite reads the device itself (a Sony TV while it is on, an Apple TV, a Sony projector, an AVPro matrix, a VRROOM), those reads are answered and the device is paired. A command a device didn't take is not a check. The remote says why after the device's label until the device takes a command, and **Devices** lists it under Problem, but nothing alerts: that note stays long after its cause has gone, and a TV that declines a key in standby is not broken. Whether a device answers a ping (`network_ping` under its `observe`) is on **Devices** as well and is not a check either: a device that sleeps answers none, and one that answers a ping with its control port closed fails the check of that port.

A device a room needs only now and then, such as a sleeping TV, counts against control health only while a room's selected source uses it. A device that stops answering often takes the room's source reading with it, so a room that no longer reports its source keeps the one it last reported, for three days at most: the failure stays that room's until the device answers again. A room that reports Off needs none of its devices from that moment, and one whose source hasn't been read since thermite started needs none either. To keep a device you know is broken on the dashboards but out of control health and alerts, mark it `monitor: optional` in the house file:

```yaml
rooms:
  living_room: {label: Living Room}

devices:
  - id: living_room_tv
    room: living_room
    kind: tv
    make: Sony
    model: Example TV
    name: Living Room TV
    ip: 192.0.2.20
    monitor: optional
    controls:
      observe: [sony_rest]
      control: [sony_ip_control]
```

To change anything else in Grafana, log in as `admin`. `thermite install` generated the password and saved a copy for you next to thermite's API token. Grafana takes it the first time it starts; change it later on Grafana's profile page.

## Changing them

The dashboards are made from the house file, like thermite's configuration. Add a device or a room, then:

```sh
thermite generate        # writes the dashboards and Prometheus' targets too
thermite deploy          # installs them; the services pick them up by themselves
```

Grafana won't save edits to these dashboards: change the house file instead, so the next generate doesn't undo your work.

## Yoke observers

Yoke monitoring is optional. Run an observer independently on the Thermite host:

```sh
yoke metrics --listen 127.0.0.1 --http-port 9754
```

Add its HTTP endpoint to `instance/host.yaml` in your private site, preserving the other host settings:

```yaml
metrics:
  yoke_targets:
    - "127.0.0.1:9754"
```

Then generate and deploy as above. Prometheus scrapes the observer's `/metrics` every **30 seconds**; Yoke samples and caches its observations every **1 second** by default. A **Yoke** dashboard appears with observer scrape health, announcement liveness, last announced state, time since announcement, and observed control-frame and event rates. These optional observations stay separate from the house's control-health result.

One observer on the Thermite host can hear existing Yoke multicast announcements across its VLAN, without adding YWP traffic. Device up means a recent announcement; it does not verify hardware or media delivery. The state is the last accepted announcement even while a device is silent. Counters describe retained observations and reset on observer restart or peer eviction; missing data stays unknown. Observers and saved routes operate independently of Thermite.

Each target must be a unique `hostname:port`, `IPv4:port` or `[IPv6]:port`, for example `[2001:db8::10]:9754`. This list supports HTTP `/metrics` endpoints and rejects credentials, URL paths and missing ports. Yoke requires `--expose` for a non-loopback listener. For authenticated TLS access to a remote collector, provide a local HTTP listener on the Thermite host through an operator-managed TLS proxy or tunnel, and scrape that local listener.

Yoke's `yoke_host_*` measurements describe the **collector host**. Run a separate observer on each appliance when you need that appliance's host statistics; a remote observer's CPU and memory do not describe the appliances it hears. Thermite's existing node_exporter remains the controller host's comprehensive monitor.

Omit `yoke_targets` or set it to `[]` to disable the optional scrape job and generated dashboard. As with other generated files, `generate` reports a previously generated `metrics/grafana/dashboards/yoke.json` as a stray after disabling it; remove that file before the next deployment.

## Already installed?

On a machine installed before the dashboards existed:

```sh
thermite upgrade --dry-run   # what it would do: directories, units, the deploy, the services
thermite upgrade             # do it; it asks before anything that needs sudo
thermite status              # Grafana's address and whether Prometheus reaches everything
```

## Alerts

Prometheus evaluates a fixed set of alerts. Each names a room or service in plain words, and waits a few minutes so a blip doesn't alert:

- **Critical**: a required control check failing for five minutes (one alert per check, with its room and device), the runtime unavailable, the remote gateway down, remotes starved of connections, routing commands quarantined, the policy worker stalled, a service down, or the thingHandler pool saturated.
- **Warnings**: a required control check with no reading for ten minutes (most often a room in use whose source can't be read), remotes timing out, a service restarting repeatedly, a running service whose metrics file has stopped updating, monitoring itself broken, low disk space, and the rest.

The checks of monitoring itself (Prometheus reaching each of its targets, node_exporter reading the services' states, Grafana answering) are not part of control health, and a failing one is a warning: rooms can still be controlled.

When the runtime is down, alerts that follow from it are held back, and so are remote time-outs while the gateway is down, and a room's unread checks while one of its checks is failing.

Alerts appear in the thermite app: Home shows the ones firing now, critical ones first, each with its room, device and check where it has them. For an alert that names a device, `thermite device ID` on the server shows everything about that device in one place: what watches it, each reading and check as it stands now, and where its log lines are. The gateway reads them from Prometheus on this machine, and nothing is sent to a phone or to any other service. Everyone who can open the app sees the alerts about a room or a device; alerts about the house as a whole (a service, the host, monitoring itself) are shown to an administrator. Home also says when monitoring itself isn't answering or has stopped evaluating its alerts, so an empty list is never mistaken for a healthy house: it says "Nothing is firing" only while monitoring is evaluating. Alerts a viewer isn't shown are counted ("and 2 more that are not shown here"), and a room an alert is about is marked in Home's list of rooms. Home reads the alerts when it is opened and every half minute while it is shown; it makes no sound. House health and Host count and list the alerts firing too, every one of them, those held back included: their count can be higher than Home's.

The [ports guide](ports.html) lists every listener and its network exposure.
