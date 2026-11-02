# Thermite listening ports

Thermite's default TCP listeners occupy **13600–13622**, anchored at the
HTTPS application on **13600**. `lib/oh_ports.py` owns the allocation.
`instance/host.yaml` keeps the site's native HTTP/HTTPS choices and optional
`metrics: <service>_port` overrides. Generation rejects invalid values and
collisions, including collisions with the sandbox, boot test and docs preview.
Explicit overrides may leave the default run; update clients and firewall
rules together when using them.

| Port | Listener | Binding / use |
| --- | --- | --- |
| 13600 | Authenticated application HTTPS | Configured address; TLS required |
| 13601 | Native runtime HTTP | Existing native address policy; production callbacks may need LAN access |
| 13602 | Native runtime HTTPS | Existing native address policy |
| 13603 | Native Karaf console | 127.0.0.1 only |
| 13604 | Native language server | Native runtime address policy; openHAB has no bind-address setting |
| 13605 | Apple TV helper HTTP | 127.0.0.1 only |
| 13606 | Prometheus HTTP | 127.0.0.1 only |
| 13607 | node_exporter HTTP | 127.0.0.1 only |
| 13608 | blackbox_exporter HTTP | 127.0.0.1 only |
| 13609 | Thermite metrics exporter HTTP | 127.0.0.1 only |
| 13610 | Grafana HTTP | Existing LAN dashboard access |
| 13611 | Alertmanager HTTP | 127.0.0.1 only; cluster listener disabled |
| 13612 | ntfy HTTP | 127.0.0.1 only |
| 13613 | Notification adapter HTTP | 127.0.0.1 only |
| 13614 | Optional verified UPnP callback ingress HTTP | Explicit LAN IPv4 address; no listener starts without a private verified map |
| 13615 | Sandbox HTTP | 127.0.0.1 by default; explicit live mode allows LAN access |
| 13616 | Sandbox HTTPS | Same sandbox address policy |
| 13617 | Sandbox Karaf console | 127.0.0.1 only |
| 13618 | Boot-check HTTP | 127.0.0.1 only |
| 13619 | Boot-check HTTPS | 127.0.0.1 only |
| 13620 | Boot-check Karaf console | 127.0.0.1 only |
| 13621 | Development docs preview HTTP | 127.0.0.1 only; `--port 0` explicitly requests an ephemeral port |
| 13622 | Development remote preview HTTP | Explicit loopback-only Python static-server command |

Authenticated access is the application entry point. Choosing a new port does
not seal native listeners or activate the optional callback ingress. Follow
[installation.md](installation.md) before treating that service as the security boundary.
The callback map must explicitly choose its listener port and exact advertised
Host; 13614 is the allocated value, not an implicit activation default.

The installer and upgrade configure the stopped native console in
`userdata/etc/org.apache.karaf.shell.cfg`, preserving other settings and fixing
its address to loopback. Generated `conf/services/runtime.cfg` sets
`org.openhab.lsp:port=13604`. Sandboxes retain their LSP bundle blacklist.
The native language server's lack of an address setting is visible in the
[pinned openHAB 5.2.3 source](https://github.com/openhab/openhab-core/blob/5.2.3/bundles/org.openhab.core.model.lsp/src/main/java/org/openhab/core/model/lsp/internal/ModelServer.java).
The console's independent setting is documented by its
[pinned distribution configuration](https://github.com/openhab/openhab-distro/blob/5.2.3/distributions/openhab/src/main/resources/userdata/etc/org.apache.karaf.shell.cfg).

The default migration moves native HTTP/HTTPS from 8080/8443, Karaf from
8101, LSP from 5007, the helper from 8099, Prometheus from 9090,
node_exporter from 9100, blackbox_exporter from 9115, the exporter from 9480,
Grafana from 3000, Alertmanager from 9093, ntfy from 2586, notifications from
9481, authenticated access from 9443, sandbox HTTP/HTTPS/console from
18080/18443/18101, boot-check HTTP/HTTPS/console from 18081/18444/18102,
and the docs preview from 8000. Regenerate the private site, review generated
units, install/upgrade them, and update staged access/callback configurations,
bookmarks and firewall rules. Existing runtime state is not rewritten by
`generate` alone.

## Why this range

Checked on 2026-11-02: the
[IANA Service Name and Transport Protocol Port Number Registry](https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.csv)
has no service assignment for **13401–13719**. The chosen run avoids registered service
ports, the usual application defaults displaced above, and Kubernetes' default
[30000–32767 NodePort range](https://kubernetes.io/docs/concepts/services-networking/service/#type-nodeport).
13000 was considered but already has open-source users, including
[Prysm's QUIC listener](https://github.com/OffchainLabs/prysm/discussions/14728)
and the [AgentTeams dashboard](https://github.com/agentteams-group/agentteams-dashboard).
Targeted searches found no known open-source default allocation for the chosen
13600–13622 run. This is a local convention, not an IANA reservation or a
promise that no other program uses those numbers. Startup preconditions still
check whether required ports are occupied.

Device destinations keep their protocol ports: receiver HTTP 8080, Sonos
1400, Google TV 6466/6467, Cast 8009, matrix telnet 23 and other device
protocols do not move. Discovery multicast/broadcast ports (including mDNS,
SSDP and vendor discovery), dynamically negotiated device/media sockets and test fixture
servers bound to port 0 remain protocol or operating-system allocations.
