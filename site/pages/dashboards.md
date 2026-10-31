# Dashboards

thermite keeps a history of your house and shows it in Grafana on the controller host. You can see when a TV stopped answering, which source a room was on last night, or whether the runtime is short of memory, from any phone or computer on your network.

## What runs

`thermite install` sets up five monitoring services on that host, each from an image pinned by digest, with no privileges. Only Grafana can be reached from other machines.

| Service | Port | What it does |
|---|---|---|
| Prometheus | 9090, this machine only | collects everything below every 30 seconds and keeps 13 months of it, at most 12 GB |
| node_exporter | 9100, this machine only | the machine itself: CPU, memory, disks, temperatures, and thermite's services |
| blackbox_exporter | 9115, this machine only | whether each device answers on the port thermite controls it through |
| thermite's exporter | 9480, this machine only | each device's status, power, input and volume, by room |
| Grafana | 3000, your network | the dashboards |

Prometheus also reads the runtime's Metrics add-on, which thermite enables in every configuration: memory, threads, events and rule runs. `thermite test boot` checks that it answers.

## What you see

Open `http://<thermite's address>:3000/`. Anyone on your network can look without logging in. Four dashboards come with it:

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

## Already installed?

On a machine installed before the dashboards existed:

```sh
thermite upgrade --dry-run   # what it would do: directories, units, the deploy, the services
thermite upgrade             # do it; it asks before anything that needs sudo
thermite status              # Grafana's address and whether Prometheus reaches everything
```

There are no alerts yet; the dashboards only show.
