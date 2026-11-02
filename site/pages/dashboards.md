# Dashboards

thermite keeps a history of your house and shows it in Grafana on the controller host. You can see when a TV stopped answering, which source a room was on last night, or whether the runtime is short of memory, from any phone or computer on your network.

## What runs

`thermite install` sets up monitoring services on that host, each from an image pinned by digest, with no privileges. Only Grafana can be reached from other machines.

| Service | Port | What it does |
|---|---|---|
| Prometheus | 13606, this machine only | collects everything below every 30 seconds and keeps 13 months of it, at most 12 GB |
| node_exporter | 13607, this machine only | the machine itself: CPU, memory, disks, temperatures, and thermite's services |
| blackbox_exporter | 13608, this machine only | whether each device answers on the port thermite controls it through |
| thermite's exporter | 13609, this machine only | each device's status, power, input and volume, by room |
| Grafana | 13610, your network | the dashboards |
| Alertmanager | 13611, this machine only | routes monitoring alerts |
| ntfy | 13612, this machine only | keeps local notifications |
| Notification adapter | 13613, this machine only | delivers known alert messages to ntfy |

Prometheus also reads the runtime's Metrics add-on, which thermite enables in every configuration: memory, threads, events and rule runs. `thermite test boot` checks that it answers.

## What you see

Open `http://<thermite's address>:13610/`. Anyone on your network can look without logging in. Four dashboards come with it:

- **House**: every room and its devices, and what is down right now. It is the home page.
- **Devices by kind**: receivers, TVs, projectors, switchers and the rest, a row each.
- **thermite runtime**: memory, threads, events, rule runs, and Things that aren't online.
- **Host**: the machine, its disks, and every service and probe.

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

Prometheus evaluates alerts for sustained control failures, missing runtime telemetry, low disk space and failed services. Alertmanager routes them through a local notification adapter to ntfy. These services listen only on this machine; phone push requires separate setup. A local heartbeat checks that alert evaluation and delivery are working.

The [ports guide](ports.html) lists every listener and its network exposure.
