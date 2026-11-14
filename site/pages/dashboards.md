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
| Alertmanager | 13612, this machine only | routes monitoring alerts |
| ntfy | 13613, this machine only | keeps local notifications |
| Notification adapter | 13614, this machine only | delivers known alert messages to ntfy |

Prometheus also reads runtime memory, threads, events and rule runs through the Metrics add-on. `thermite test boot` checks that it answers. See [storage requirements](requirements.html#storage-and-metrics) for the retention budget.

## What you see

Open `http://<thermite's address>:13611/`. Anyone on your network can look without logging in. Six dashboards come with it:

- **House health**, the home page: is the house OK? Six tiles answer it at a glance: control, rooms ready, remotes, alerts firing, phone delivery and the host. Below them, **Needs attention now** names each room, problem and device in plain words; then every room's source through the day, switching since yesterday evening, and the alerts firing.
- **Switching and remotes**: how long source changes take and how they end, the routing executor and any quarantined device, the remote gateway's latency, answers, requests in flight and event streams (how many, how long they last, what fails them, and the live updates they carry), what remote pages gave up waiting for, and the room policy worker.
- **Devices**: one row per device, with its Thing, control port, problem and whether a room needs it now; the Things that aren't online, and in what status; then each room's power and source through the day, and listening levels.
- **openHAB runtime**: memory, thread pools, the thingHandler pool, how long its REST API takes to answer, events, the busiest rules, and Things that aren't online.
- **Host**: how many scrape targets are up and which are down, thermite's services and their restarts, alerts firing, CPU, memory, disk space and I/O.
- **Usage**: which sources the house used and for how long, and what each device played, when and in which rooms. It is not a health signal.

Colour means one thing on every dashboard: green is OK, amber a warning, red a failure, grey unknown, slate Off, and tan only "in use". Dashed lines mark service starts and deploys; switch them off at the top of a dashboard.

A device a room needs only now and then, such as a sleeping TV, counts against control health only while a room's selected source uses it. To keep a device you know is broken on the dashboards but out of control health and alerts, mark it `monitor: optional` in the house file:

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

- **Critical**: a required control check failing for five minutes (one alert per room), the runtime unavailable, the remote gateway down, remotes starved of connections, routing commands quarantined, the policy worker stalled, a service down, or the thingHandler pool saturated.
- **Warnings**: remotes timing out, a service restarting repeatedly, monitoring itself broken, low disk space, and the rest. A warning repeats every 12 hours at most.

When the runtime is down, alerts that follow from it are held back, and so are remote time-outs while the gateway is down. Alertmanager routes alerts through a local notification adapter to ntfy. These services listen only on this machine; phone push requires separate setup. A local heartbeat checks that alert evaluation and delivery are working.

The [ports guide](ports.html) lists every listener and its network exposure.
