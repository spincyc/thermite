# Thermite listening ports

Thermite's default TCP listeners occupy **13600–13626**: the optional
plain-HTTP Guest controls on **13600**, the HTTPS application on **13601**,
and every other listener after them, in the order below. `lib/oh_ports.py`
owns the allocation.
`instance/host.yaml` keeps the site's native HTTP/HTTPS choices and optional
`gateway: port`, `metrics: <service>_port` and `guest_http: port` overrides.
Generation rejects invalid values and
collisions, including collisions with the sandbox, boot test and docs preview.
Explicit overrides may leave the default range, and a default an override
frees may be taken by another override. Commit changes and run
`thermite install --config DIR` or `thermite upgrade --config DIR`; the same
workflow updates the native boundary and gateway together. Gateway and callback
listeners are checked against the complete allocation and native upstream.
When any listener port changes, the workflow stops every Thermite service
before it installs the new units, since one service's new port may be
another's old one, then starts each on its new port. It records the
allocation in the site's `var/listeners.json` once all of them answer, so a
run that stops early stops them all again next time.

| Port | Listener | Binding / use |
| --- | --- | --- |
| 13600 | Optional plain-HTTP Guest controls | Listed `guest_http.listen` addresses, admitting only `guest_http.clients`; no listener without `guest_http` |
| 13601 | Application HTTPS with toggleable authentication; `gateway: port` moves it | Configured address; TLS required |
| 13602 | Native runtime HTTP | 127.0.0.1 only; verified device callbacks use the relay |
| 13603 | Native runtime HTTPS | 127.0.0.1 only |
| 13604 | Native Karaf console | 127.0.0.1 only |
| 13605 | Native language server | Non-loopback access blocked by the managed native firewall |
| 13606 | Apple TV helper HTTP | 127.0.0.1 only |
| 13607 | Prometheus HTTP | 127.0.0.1 only |
| 13608 | node_exporter HTTP | 127.0.0.1 only |
| 13609 | blackbox_exporter HTTP | 127.0.0.1 only |
| 13610 | Thermite metrics exporter HTTP | 127.0.0.1 only |
| 13611 | Grafana HTTP | Existing LAN dashboard access |
| 13612 | Alertmanager HTTP | 127.0.0.1 only; cluster listener disabled |
| 13613 | ntfy HTTP | 127.0.0.1 only |
| 13614 | Notification adapter HTTP | 127.0.0.1 only |
| 13615 | Optional verified UPnP callback ingress HTTP | Explicit LAN IPv4 address; no listener starts without a private verified map |
| 13616 | Sandbox HTTP | 127.0.0.1 by default; explicit live mode allows LAN access |
| 13617 | Sandbox HTTPS | Same sandbox address policy |
| 13618 | Sandbox Karaf console | 127.0.0.1 only |
| 13619 | Boot-check HTTP | 127.0.0.1 only |
| 13620 | Boot-check HTTPS | 127.0.0.1 only |
| 13621 | Boot-check Karaf console | 127.0.0.1 only |
| 13622 | Development docs preview HTTP | 127.0.0.1 only; `--port 0` explicitly requests an ephemeral port |
| 13623 | Development remote preview HTTP | Explicit loopback-only Python static-server command |
| 13624 | Native Karaf shutdown control | 127.0.0.1 only |
| 13625 | Sandbox Karaf shutdown control | 127.0.0.1 only |
| 13626 | Boot-check Karaf shutdown control | 127.0.0.1 only |

HTTPS is the application entry point whether authentication is on or off.
[Moving the HTTPS application](#moving-the-https-application) explains its
override.
The Guest port is the one application listener without TLS, and it serves
Guests only: see [Guest access over plain HTTP](installation.md#guest-access-over-plain-http).
The managed firewall table leaves it alone; a host with its own firewall opens
it from the guest network only.
Installation restricts native HTTP/HTTPS to loopback and blocks non-loopback
traffic to native HTTP, HTTPS, Karaf, LSP and shutdown ports in the dedicated
`inet thermite_native` nftables table. It leaves other firewall tables intact,
verifies the active policy and reapplies it before every native runtime start.
An unknown existing table or modified policy requires review before replacement.
For Sonos, the workflow derives exact callback routes from committed identities
and live device descriptions; existing verified routes and their LAN address
are retained, on the allocation's callback port and native upstream.
The relay accepts only the verified NOTIFY traffic, never general native API access.
For a house with Sonos devices, generated `conf/services/upnp.cfg` sets
`org.jupnp:httpProxyPort=13615`, so openHAB starts advertising the relay's
port; the boot check and sandbox, which have no relay, replace or drop it.

The installer and upgrade configure the stopped native console in
`userdata/etc/org.apache.karaf.shell.cfg`, preserving other settings and fixing
its address to loopback. Generated `conf/services/runtime.cfg` sets
`org.openhab.lsp:port=13605`. Sandboxes retain their LSP bundle blacklist.
The native language server's lack of an address setting is visible in the
[pinned openHAB 5.2.3 source](https://github.com/openhab/openhab-core/blob/5.2.3/bundles/org.openhab.core.model.lsp/src/main/java/org/openhab/core/model/lsp/internal/ModelServer.java).
The console's independent setting is documented by its
[pinned distribution configuration](https://github.com/openhab/openhab-distro/blob/5.2.3/distributions/openhab/src/main/resources/userdata/etc/org.apache.karaf.shell.cfg).

Karaf also opens a separate shutdown-control socket. Thermite sets its port
and loopback address in each instance's `userdata/etc/custom.properties`,
preserving the upstream stop command instead of accepting a random listener.
Native, sandbox and boot-check instances use distinct ports so they can
coexist. The behavior is defined by the pinned Karaf
[shutdown setup](https://github.com/apache/karaf/blob/karaf-4.4.11/main/src/main/java/org/apache/karaf/main/InstanceHelper.java)
and [stop command](https://github.com/apache/karaf/blob/karaf-4.4.11/main/src/main/java/org/apache/karaf/main/Stop.java).

The default migration moves native HTTP/HTTPS from 8080/8443, Karaf from
8101, LSP from 5007, the helper from 8099, Prometheus from 9090,
node_exporter from 9100, blackbox_exporter from 9115, the exporter from 9480,
Grafana from 3000, Alertmanager from 9093, ntfy from 2586, notifications from
9481, authenticated access from 9443, sandbox HTTP/HTTPS/console from
18080/18443/18101, boot-check HTTP/HTTPS/console from 18081/18444/18102,
and the docs preview from 8000. Regenerate the private site, review generated
units, run the unified install/upgrade, and update bookmarks as needed. Existing runtime state is not rewritten by
`generate` alone.

## Moving the HTTPS application

`gateway: {port: N}` in `instance/host.yaml` moves the HTTPS application, the
gateway everyone signs in to. It is a mapping named for the gateway, beside
`metrics:` and `guest_http:`, so it cannot be mistaken for the top-level
`http_port` and `https_port`, which are the native runtime's loopback
listeners. The product default is 13601; a moved gateway frees it for
another override.

```yaml
gateway:
  port: 16301
```

Commit the change and run `thermite upgrade --config DIR`. The upgrade stops
the gateway, so its old listeners close, then starts the new release on the
configured ports; it takes the port from `instance/host.yaml` each time, never
from the release it replaces, and its HTTPS readiness check and the verified
address it saves for terminals use the new port. Its certificate names hosts,
not ports, so client trust is unchanged. Origins include the port, though:

- Bookmarks and home-screen apps (an iPhone's **Add to Home Screen**) keep the
  old address; add them again at the new one. Site data a browser keeps per
  origin, such as the light/dark choice, starts afresh.
- `thermite access login` credentials and `--ca-file` choices are saved per
  address: log in once more, and pass `--ca-file` once more, at the new one.
- On the server, `thermite remote --config DIR` follows the verified address
  automatically, and `thermite tui --config DIR` uses the site's port.
  Elsewhere, pass `--url HOST:PORT`.
- The managed firewall table leaves the gateway's ports alone. A host with its
  own firewall opens the new HTTPS port and closes the old one.

`thermite status` shows the HTTPS address on the configured port, and reports
a problem while the installed gateway still serves another.

## Why this range

Checked on 2026-11-02: the
[IANA Service Name and Transport Protocol Port Number Registry](https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.csv)
has no service assignment for **13401–13719**. The chosen run avoids registered service
ports, the usual application defaults displaced above, and Kubernetes' default
[30000–32767 NodePort range](https://kubernetes.io/docs/concepts/services-networking/service/#type-nodeport).
13000 was considered but already has open-source users, including
[Prysm's QUIC listener](https://github.com/OffchainLabs/prysm/discussions/14728)
and the [AgentTeams dashboard](https://github.com/agentteams-group/agentteams-dashboard).
Targeted searches found no known open-source default allocation for
13600–13625, the run first chosen; 13626, added later, lies in the same
unassigned block. This is a local convention,
not an IANA reservation or a promise that no other program uses those
numbers. Startup preconditions still check whether required ports are
occupied.

Device destinations keep their protocol ports: receiver HTTP 8080, Sonos
1400, Google TV 6466/6467, Cast 8009, matrix telnet 23 and other device
protocols do not move. Discovery multicast/broadcast ports (including mDNS,
SSDP and vendor discovery), dynamically negotiated device/media sockets and test fixture
servers bound to port 0 remain protocol or operating-system allocations.
