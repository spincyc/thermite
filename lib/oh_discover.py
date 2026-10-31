"""Read-only discovery of the AV devices on the LAN.

Nothing here commands a device. It pings, reads the kernel neighbour table,
sends the discovery queries devices answer anyway (SSDP, Control4's SDDP,
PlayStation DDP and mDNS through avahi-browse), opens and closes TCP
connections to a fixed list of control ports, and fetches UPnP descriptions.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

GROUP = "239.255.255.250"
SSDP_PORT = 1900
SDDP_PORT = 1902
#: PlayStation Device Discovery: a PS5 answers on 9302, a PS4 on 987, awake or resting.
DDP_PORTS = (9302, 987)
DDP_SEARCH = "SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
OUI_DB = Path("/usr/share/hwdata/oui.txt")

#: Control ports of the AV products in this house and their common peers.
PORTS = (
    22, 23, 80, 443, 1400, 1443, 1883, 1902, 1925, 1926, 3000, 3001, 4352,
    4998, 5000, 5020, 5021, 5555, 6466, 6467, 7000, 7345, 8001, 8002, 8008,
    8009, 8060, 8080, 8102, 8443, 8750, 9000, 9197, 10101, 14999, 20060,
    36669, 49152, 50001, 52235, 60128,
)
#: Ports that mark a host as AV or control gear rather than infrastructure.
AV_PORTS = {
    23, 1400, 1902, 3000, 3001, 4998, 5555, 6466, 6467, 7000, 8001, 8002,
    8008, 8009, 8060, 8102, 10101, 14999, 20060, 60128,
}
MDNS_AV = (
    "_airplay", "_raop", "AirPlay", "AirTunes", "_googlecast", "_sonos",
    "_androidtvremote2", "_nv_shield_remote", "_heos", "Amazon Fire TV",
    "_spotify-connect", "_linkplay",
)


def parse_headers(payload: str) -> tuple[str, dict[str, str]]:
    """Start line and headers of an SSDP or SDDP datagram (quotes stripped)."""
    lines = payload.replace("\r\n", "\n").split("\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip().strip('"')
    return lines[0].strip(), headers


def _unescape(text: str) -> str:
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text)


def parse_avahi(lines: list[str]) -> dict[str, dict]:
    """IPv4 address -> host names, service types and identifying TXT keys."""
    keep = {"model", "md", "fn", "manufacturer", "am", "mdl", "rpmd", "ty"}
    found: dict[str, dict] = {}
    for line in lines:
        if not line.startswith("="):
            continue
        fields = line.rstrip("\n").split(";", 9)
        if len(fields) < 9 or ":" in fields[7] or "." not in fields[7]:
            continue
        name, kind, host, address = _unescape(fields[3]), fields[4], fields[6], fields[7]
        entry = found.setdefault(address, {"hosts": [], "services": [], "names": [], "txt": {}})
        for key, value in (("hosts", host), ("services", kind), ("names", name)):
            if value not in entry[key]:
                entry[key].append(value)
        for pair in re.findall(r'"([^"]*)"', fields[9] if len(fields) > 9 else ""):
            key, _, value = pair.partition("=")
            if key.lower() in keep and value:
                entry["txt"][key] = value
    return found


def load_oui(path: Path = OUI_DB) -> dict[str, str]:
    table: dict[str, str] = {}
    if not Path(path).is_file():
        return table
    pattern = re.compile(r"^([0-9A-F]{2})-([0-9A-F]{2})-([0-9A-F]{2})\s+\(hex\)\s+(.+)$")
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            table["".join(match.groups()[:3])] = match.group(4).strip()
    return table


def vendor(mac: str, oui: dict[str, str]) -> str:
    if not mac:
        return ""
    if int(mac[:2], 16) & 2:
        return "(private address)"
    return oui.get(mac.replace(":", "")[:6].upper(), "?")


def local_interface() -> tuple[str, ipaddress.IPv4Network]:
    """This host's address on the default route and that link's network."""
    route = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"],
                           capture_output=True, text=True, check=True).stdout
    address = re.search(r"\bsrc (\S+)", route).group(1)
    addrs = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                           capture_output=True, text=True, check=True).stdout
    for match in re.finditer(r"inet (\S+)", addrs):
        interface = ipaddress.IPv4Interface(match.group(1))
        if str(interface.ip) == address:
            return address, interface.network
    raise RuntimeError(f"no interface carries {address}")


def ping_sweep(network: ipaddress.IPv4Network, workers: int = 256) -> set[str]:
    def ping(address: str) -> tuple[str, bool]:
        result = subprocess.run(["ping", "-c1", "-W1", address],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return address, result.returncode == 0
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        return {a for a, ok in pool.map(ping, map(str, network.hosts())) if ok}


def neighbours() -> dict[str, str]:
    out = subprocess.run(["ip", "-4", "neigh", "show"], capture_output=True,
                         text=True, check=True).stdout
    return {m.group(1): m.group(2).lower()
            for m in re.finditer(r"^(\S+) dev \S+ lladdr (\S+)", out, re.MULTILINE)}


def probe_ports(hosts: list[str], ports=PORTS, timeout: float = 1.5,
                concurrency: int = 800) -> dict[str, list[int]]:
    async def one(host: str, port: int, gate: asyncio.Semaphore):
        async with gate:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            except (OSError, asyncio.TimeoutError):
                return host, port, False
            writer.close()
            return host, port, True

    async def run():
        gate = asyncio.Semaphore(concurrency)
        return await asyncio.gather(*(one(h, p, gate) for h in hosts for p in ports))

    found: dict[str, list[int]] = {}
    for host, port, ok in asyncio.run(run()):
        if ok:
            found.setdefault(host, []).append(port)
    return {h: sorted(p) for h, p in found.items()}


def _multicast_socket(local: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
    sock.bind((local, 0))
    sock.settimeout(0.3)
    return sock


def ssdp_search(local: str, wait: float = 8.0) -> dict[str, dict]:
    sock = _multicast_socket(local)
    for target in ("ssdp:all", "upnp:rootdevice", "roku:ecp"):
        message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                   f'MAN: "ssdp:discover"\r\nMX: 3\r\nST: {target}\r\n\r\n')
        sock.sendto(message.encode(), (GROUP, SSDP_PORT))
    found: dict[str, dict] = {}
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            data, (address, _) = sock.recvfrom(65535)
        except socket.timeout:
            continue
        _, headers = parse_headers(data.decode(errors="replace"))
        entry = found.setdefault(address, {"server": [], "locations": [], "devices": []})
        for key, field in (("server", "server"), ("locations", "location")):
            if headers.get(field) and headers[field] not in entry[key]:
                entry[key].append(headers[field])
    for entry in found.values():
        entry["devices"] = describe(entry["locations"])
    return found


def describe(locations: list[str]) -> list[dict]:
    """friendlyName/manufacturer/model of every UPnP device description."""
    wanted = ("friendlyName", "manufacturer", "modelName", "modelNumber", "deviceType")
    devices: list[dict] = []
    for location in locations:
        try:
            tree = ET.fromstring(urllib.request.urlopen(location, timeout=3).read())
        except Exception:  # noqa: BLE001 -- a broken description is not fatal
            continue
        for node in tree.iter():
            if node.tag.rsplit("}", 1)[-1] != "device":
                continue
            info = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in node
                    if c.tag.rsplit("}", 1)[-1] in wanted}
            if info and info not in devices:
                devices.append(info)
    return devices


def sddp_search(local: str, wait: float = 15.0) -> dict[str, dict]:
    """Control4 SDDP: answers to a search plus unsolicited NOTIFY ALIVE."""
    listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    listen.bind(("", SDDP_PORT))
    listen.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                      struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton(local)))
    listen.settimeout(0.3)
    search = _multicast_socket(local)
    search.sendto(f'SEARCH * SDDP/1.0\r\nHost: "{local}:{SDDP_PORT}"\r\n\r\n'.encode(),
                  (GROUP, SDDP_PORT))
    found: dict[str, dict] = {}
    end = time.monotonic() + wait
    while time.monotonic() < end:
        for sock in (search, listen):
            try:
                data, (address, _) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            start, headers = parse_headers(data.decode(errors="replace"))
            if not start.startswith("SEARCH"):
                found[address] = {k: headers[k] for k in
                                  ("type", "manufacturer", "model", "driver", "host")
                                  if k in headers}
    return found


def parse_ddp(payload: str) -> dict[str, str]:
    """A PlayStation's answer to a DDP search: host-type, host-name, host-id,
    system-version, and its state (200: on, 620: resting)."""
    start, headers = parse_headers(payload)
    entry = {key: headers[key] for key in ("host-type", "host-name", "host-id", "system-version") if key in headers}
    code = start.split()[1] if len(start.split()) > 1 else ""
    entry["state"] = {"200": "on", "620": "standby"}.get(code, start)
    return entry


def ddp_search(network: ipaddress.IPv4Network, wait: float = 3.0) -> dict[str, dict]:
    """The PlayStations that answer a DDP search broadcast on the network.
    They show no AV port, SSDP or mDNS, so nothing else finds them."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    for port in DDP_PORTS:
        try:
            sock.sendto(DDP_SEARCH.encode(), (str(network.broadcast_address), port))
        except OSError:
            continue
    found: dict[str, dict] = {}
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            data, (address, _) = sock.recvfrom(4096)
        except socket.timeout:
            continue
        found[address] = parse_ddp(data.decode(errors="replace"))
    sock.close()
    return found


def avahi_browse(timeout: int = 25) -> list[str]:
    if not shutil.which("avahi-browse"):
        return []
    result = subprocess.run(["timeout", str(timeout), "avahi-browse", "-a", "-r", "-t", "-p"],
                            capture_output=True, text=True)
    return result.stdout.splitlines()


def is_av(host: dict) -> bool:
    if set(host.get("ports") or []) & AV_PORTS or host.get("sddp") or host.get("ssdp") or host.get("ddp"):
        return True
    services = " ".join((host.get("mdns") or {}).get("services", []))
    return any(marker in services for marker in MDNS_AV)


def compare(inventory: dict, hosts: dict[str, dict]) -> dict[str, list]:
    """How a scan differs from devices.yaml, matched by MAC address."""
    known = list(inventory.get("devices") or []) + list(inventory.get("later") or [])
    by_mac = {d["mac"]: d for d in known if d.get("mac")}
    seen_macs = {h.get("mac"): ip for ip, h in hosts.items() if h.get("mac")}
    report: dict[str, list] = {"moved": [], "missing": [], "unknown": [], "address_reused": []}
    for mac, device in sorted(by_mac.items(), key=lambda kv: kv[1]["id"]):
        if mac not in seen_macs:
            report["missing"].append(device["id"])
        elif seen_macs[mac] != device.get("ip"):
            report["moved"].append({"id": device["id"], "inventory": device.get("ip"),
                                    "seen": seen_macs[mac]})
    by_ip = {d.get("ip"): d for d in known}
    for ip, host in sorted(hosts.items(), key=lambda kv: tuple(map(int, kv[0].split(".")))):
        mac = host.get("mac")
        if ip in by_ip and mac and by_ip[ip].get("mac") and by_ip[ip]["mac"] != mac:
            report["address_reused"].append({"ip": ip, "inventory": by_ip[ip]["id"], "mac": mac})
        if mac and mac not in by_mac and is_av(host):
            report["unknown"].append({"ip": ip, "mac": mac, "vendor": host.get("vendor", "")})
    return report
