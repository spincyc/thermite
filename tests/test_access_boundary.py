"""Native commissioning preserves authority and admits only proven callback paths."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_ports
import oh_render
from oh_access import native_boundary as boundary
from oh_cli import generate, sandbox as sandbox_cli

# Explicitly generated locally administered fixture identity, never a device MAC.
DEVICE = {"ip": "192.0.2.20", "mac": bytes((2, 0, 0, 0, 0, 20)).hex(":"), "sonos_udn": "RINCON_02000000001401400"}
ORIGIN = "http://127.0.0.1:13602"


def service(name, namespace="upnp-org"):
    return (f"<service><serviceId>urn:{namespace}:serviceId:{name}</serviceId>"
            f"<eventSubURL>/MediaRenderer/{name}/Event</eventSubURL></service>")


def descriptor(root_service="DeviceProperties", embedded=True):
    udn = DEVICE["sonos_udn"]
    child = (f"<deviceList><device><UDN>uuid:{udn}_MR</UDN><serviceList>"
             + service("AVTransport") + "</serviceList></device></deviceList>") if embedded else ""
    return (f'<root xmlns="{boundary.DEVICE_NAMESPACE}"><device><UDN>uuid:{udn}</UDN>'
            f"<serviceList>{service(root_service, 'sonos-com')}{service('AlarmClock')}</serviceList>"
            f"{child}</device></root>").encode()


def callback_config(port=13615):
    return {"version": 1, "routes": boundary.descriptor_routes(descriptor(), DEVICE),
            "upstream": ORIGIN, "hosts": [f"192.0.2.10:{port}"], "bind_host": "192.0.2.10", "port": port}


class Native:
    def __init__(self):
        self.configs = {boundary.AUTH_CONFIG: {"implicitUserRole": True, "trustedNetworks": "192.0.2.0/24", "allowBasicAuth": False, "cacheExpiration": 321},
                        # As openHAB starts with conf/services/upnp.cfg (oh_render).
                        boundary.UPNP_CONFIG: {"interfaces": "eth1", "streamListenPort": "13602", "httpProxyPort": "13615"}}
        self.calls = []
        self.anonymous = 401
        self.authenticated = 200
        self.put_status = 200
        self.fail_after_put = 0
        self.wait = 0
        self.now = 0
        self.alter_unrelated = False
        self.ignore_put = False
        self.descriptor_status = 200
        self.read_status = {}
        self.item_responses = {True: [], False: []}

    def sleep(self, seconds):
        self.now += seconds

    def request(self, method, url, **options):
        self.calls.append((method, url, copy.deepcopy(options)))
        parsed = urlsplit(url)
        if parsed.hostname == DEVICE["ip"]:
            assert method == "GET"
            assert "Authorization" not in options["headers"]
            return boundary.Response(self.descriptor_status, {"content-type": "text/xml; charset=utf-8"}, descriptor())
        self.now += 0.01
        assert parsed.hostname == "127.0.0.1"
        path = parsed.path
        if path == "/rest/items":
            authenticated = "Authorization" in options["headers"]
            if self.item_responses[authenticated]:
                return self.item_responses[authenticated].pop(0)
            status = self.authenticated if authenticated else self.anonymous
            return boundary.Response(status, {"content-type": "application/json"}, b'[{"name":"room_source"}]')
        assert options["headers"].get("Authorization") == "Bearer private-test-token"
        if method == "PUT":
            previous = copy.deepcopy(self.configs[path])
            if not self.ignore_put:
                self.configs[path] = json.loads(options["body"])
            if self.alter_unrelated:
                self.configs[path]["cacheExpiration"] = 999
            self.wait = self.fail_after_put
            return boundary.Response(self.put_status, {"content-type": "application/json"}, json.dumps(previous).encode())
        if self.wait:
            self.wait -= 1
            return boundary.Response(404, {"content-type": "text/plain"}, b"resource reloading")
        if path in self.read_status:
            return boundary.Response(self.read_status[path], {"content-type": "text/plain"}, b"private-native-response")
        return boundary.Response(200, {"content-type": "application/json"}, json.dumps(self.configs[path]).encode())


class Descriptors(unittest.TestCase):
    def test_only_actual_device_service_pairs_are_admitted(self):
        routes = boundary.descriptor_routes(descriptor(), DEVICE)
        root = DEVICE["sonos_udn"]
        self.assertEqual(routes, {
            f"/upnpcallback/dev/{root}/svc/sonos-com/DeviceProperties/event/cb": DEVICE["ip"],
            f"/upnpcallback/dev/{root}_MR/svc/upnp-org/AVTransport/event/cb": DEVICE["ip"]})
        self.assertNotIn("_MS", " ".join(routes))
        self.assertNotIn("AlarmClock", " ".join(routes))

    def test_root_only_descriptor_does_not_invent_embedded_devices(self):
        routes = boundary.descriptor_routes(descriptor(embedded=False), DEVICE)
        self.assertEqual(len(routes), 1)

    def test_rejects_other_identity_duplicate_udn_wrong_mac_or_foreign_namespace(self):
        source = descriptor()
        cases = [source.replace(DEVICE["sonos_udn"].encode(), b"RINCON_02000000009901400"),
                 source.replace(b"01400_MR", b"01400"),
                 source.replace(b"urn:upnp-org:serviceId:AVTransport", b"urn:evil:serviceId:AVTransport"),
                 source.replace(b"<eventSubURL>/MediaRenderer/", b"<eventSubURL>http://198.51.100.4:1400/"),
                 source.replace(b"<UDN>", b"<UDN>unexpected</UDN><UDN>", 1)]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(boundary.BoundaryError):
                boundary.descriptor_routes(body, DEVICE)
        with self.assertRaises(boundary.BoundaryError):
            boundary.descriptor_routes(source, {**DEVICE, "mac": bytes((2, 0, 0, 0, 0, 153)).hex(":")})

    def test_bounded_xml_rejects_dtd_entities_deep_or_oversized_input(self):
        cases = [b'<!DOCTYPE root [<!ENTITY secret SYSTEM "file:///etc/passwd">]>' + descriptor(),
                 b'<!ENTITY x "payload">' + descriptor(), descriptor()[:-10],
                 b"x" * (boundary.MAX_BODY + 1), b"<x>" * 25 + b"</x>" * 25,
                 descriptor().replace(b"<device>", b"<device>\x00", 1)]
        for body in cases:
            with self.subTest(size=len(body)), self.assertRaises(boundary.BoundaryError):
                boundary.descriptor_routes(body, DEVICE)

    def test_discovery_contacts_only_committed_origin_without_credentials(self):
        native = Native()
        result = boundary.discover_callbacks({"devices": [DEVICE]}, ORIGIN, "192.0.2.10", request=native.request)
        self.assertEqual(result, callback_config())
        self.assertEqual([call[1] for call in native.calls], ["http://192.0.2.20:1400/xml/device_description.xml"])

    def test_redirects_or_missing_device_fail_without_native_mutation(self):
        native = Native()
        for status in (302, 404, 500):
            native.descriptor_status = status
            with self.subTest(status=status), self.assertRaises(boundary.BoundaryError):
                boundary.discover_callbacks({"devices": [DEVICE]}, ORIGIN, "192.0.2.10", request=native.request)
        self.assertTrue(all(call[0] == "GET" for call in native.calls))

    def test_rejects_nonunicast_and_wrong_upstream_adoption(self):
        for address in ("127.0.0.1", "0.0.0.0", "224.0.0.251", "255.255.255.255", "device.invalid"):
            with self.subTest(address=address), self.assertRaises(boundary.BoundaryError):
                boundary.descriptor_routes(descriptor(), {**DEVICE, "ip": address})
        with self.assertRaises(boundary.BoundaryError):
            boundary.validate_callbacks({**callback_config(), "upstream": "http://192.0.2.10:13602"}, ORIGIN)


class Commissioning(unittest.TestCase):
    def setUp(self):
        scratch = AREA / ".scratch/unified-boundary"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.token = Path(self.temporary.name) / "token"
        self.token.write_bytes(b"private-test-token\n")
        self.token.chmod(0o600)
        self.native = Native()

    def configure(self, **kwargs):
        return boundary.configure(ORIGIN, self.token, kwargs.pop("house", {"devices": []}), "192.0.2.10",
                                  request=self.native.request, clock=lambda: self.native.now,
                                  sleep=self.native.sleep, timeout=3, **kwargs)

    def test_disables_grants_preserves_other_configuration_and_checks_both_clients(self):
        result = self.configure()
        self.assertEqual(result, {"native_security": True, "callbacks": None})
        self.assertEqual(self.native.configs[boundary.AUTH_CONFIG], {
            "implicitUserRole": False, "trustedNetworks": "", "allowBasicAuth": False, "cacheExpiration": 321})
        items = [call for call in self.native.calls if "/rest/items?" in call[1]]
        self.assertEqual(len(items), 2)
        self.assertIn("Authorization", items[0][2]["headers"])
        self.assertNotIn("Authorization", items[1][2]["headers"])

    def test_idempotent_repeat_performs_no_put(self):
        self.configure()
        self.native.calls.clear()
        self.configure()
        self.assertFalse(any(call[0] == "PUT" for call in self.native.calls))

    def test_pending_resource_reload_is_retried_but_put_is_not(self):
        self.native.fail_after_put = 2
        self.configure()
        self.assertEqual(sum(call[0] == "PUT" for call in self.native.calls), 1)

    def test_missing_or_non_boolean_security_readback_never_counts_as_done(self):
        self.native.ignore_put = True
        self.native.configs[boundary.AUTH_CONFIG] = {"implicitUserRole": 0, "trustedNetworks": ""}
        with self.assertRaisesRegex(boundary.BoundaryError, r"deadline during REST authentication configuration readback \(implicitUserRole type mismatch\)"):
            self.configure()
        self.assertEqual(sum(call[0] == "PUT" for call in self.native.calls), 1)

    def test_configuration_read_timeout_identifies_resource_and_status_without_payload(self):
        for path, name in ((boundary.AUTH_CONFIG, "REST authentication"), (boundary.UPNP_CONFIG, "jUPnP")):
            with self.subTest(path=path):
                self.native = Native()
                self.native.read_status[path] = 503
                with self.assertRaisesRegex(boundary.BoundaryError, name + r" configuration read \(last HTTP status 503\)") as raised:
                    self.configure(existing_callbacks=callback_config())
                message = str(raised.exception)
                for private in ("private-native-response", "private-test-token", "192.0.2.10", str(self.token)):
                    self.assertNotIn(private, message)
                self.assertFalse(any(method == "PUT" and url.endswith(path) for method, url, _ in self.native.calls))

    def test_configuration_reload_timeout_identifies_readback_and_last_status(self):
        self.native.fail_after_put = 10
        with self.assertRaisesRegex(boundary.BoundaryError, r"REST authentication configuration readback \(last HTTP status 404\)"):
            self.configure()
        self.assertEqual(sum(call[0] == "PUT" for call in self.native.calls), 1)

    def test_unconfirmed_update_has_no_repeat(self):
        self.native.put_status = 500
        with self.assertRaisesRegex(boundary.BoundaryError, "not confirmed"):
            self.configure()
        self.assertEqual(sum(call[0] == "PUT" for call in self.native.calls), 1)

    def test_rejects_unrelated_setting_drift(self):
        self.native.alter_unrelated = True
        with self.assertRaisesRegex(boundary.BoundaryError, "Unrelated"):
            self.configure()

    def test_items_resource_reload_after_readback_retries_only_verification(self):
        for status in (404, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.native = Native()
                self.native.item_responses[True] = [boundary.Response(status, {}, b"private-native-response")]
                result = self.configure(existing_callbacks=callback_config())
                self.assertTrue(result["native_security"])
                items = [call for call in self.native.calls if "/rest/items?" in call[1]]
                self.assertEqual(len(items), 3)
                self.assertTrue(all("Authorization" in call[2]["headers"] for call in items[:2]))
                self.assertNotIn("Authorization", items[-1][2]["headers"])
                puts = [url for method, url, _ in self.native.calls if method == "PUT"]
                self.assertEqual(puts, [ORIGIN + boundary.AUTH_CONFIG])
                self.assertGreaterEqual(self.native.now, 1)
                self.assertLess(self.native.now, 3)

    def test_anonymous_access_waits_for_authentication_to_apply_without_repeating_writes(self):
        self.native.item_responses[False] = [
            boundary.Response(200, {"content-type": "application/json"}, b"[]"),
            boundary.Response(404, {}, b"private-native-response")]
        self.assertTrue(self.configure()["native_security"])
        items = [call for call in self.native.calls if "/rest/items?" in call[1]]
        self.assertEqual(len(items), 4)
        self.assertIn("Authorization", items[0][2]["headers"])
        self.assertTrue(all("Authorization" not in call[2]["headers"] for call in items[1:]))
        self.assertEqual(sum(call[0] == "PUT" for call in self.native.calls), 1)
        self.assertLess(self.native.now, 3)

    def test_unproven_items_access_exhausts_the_original_deadline_without_repeating_writes(self):
        for authenticated in (True, False):
            for status in ((404, 500, 502, 503, 504) if authenticated else (200, 404, 500, 502, 503, 504)):
                with self.subTest(authenticated=authenticated, status=status):
                    self.native = Native()
                    if authenticated:
                        self.native.authenticated = status
                    else:
                        self.native.anonymous = status
                    label = "authenticated" if authenticated else "anonymous"
                    with self.assertRaisesRegex(boundary.BoundaryError, label + r" Item access \(last HTTP status " + str(status) + r"\)") as raised:
                        self.configure()
                    self.assertEqual(self.native.now, 3)
                    self.assertEqual(sum(call[0] == "PUT" for call in self.native.calls), 1)
                    for private in ("private-native-response", "private-test-token", "room_source", str(self.token)):
                        self.assertNotIn(private, str(raised.exception))

    def test_items_redirects_and_malformed_success_are_terminal_for_both_clients(self):
        for authenticated in (True, False):
            for response in (boundary.Response(302, {}, b""),
                             boundary.Response(200, {"content-type": "application/json"}, b"{"),
                             boundary.Response(200, {"content-type": "application/json"}, b"{}"),
                             boundary.Response(200, {"content-type": "text/plain"}, b"[]")):
                with self.subTest(authenticated=authenticated, response=response):
                    self.native = Native()
                    self.native.item_responses[authenticated] = [response]
                    with self.assertRaises(boundary.BoundaryError):
                        self.configure()
                    self.assertLess(self.native.now, 1, "invalid verification must fail without polling")
                    items = [call for call in self.native.calls if "/rest/items?" in call[1]
                             and ("Authorization" in call[2]["headers"]) == authenticated]
                    self.assertEqual(len(items), 1)

    def test_internal_credential_rejection_is_terminal_and_preserves_the_saved_token(self):
        for status in (401, 403):
            self.native = Native()
            self.native.authenticated = status
            with self.subTest(status=status), self.assertRaisesRegex(boundary.BoundaryError, "rejected the internal credential"):
                self.configure()
            self.assertLess(self.native.now, 1)
            self.assertEqual(self.token.read_bytes(), b"private-test-token\n")
            self.assertEqual(sum("/rest/items?" in call[1] for call in self.native.calls), 1)

    def test_sonos_reads_back_the_rendered_callback_port_and_never_writes_jupnp(self):
        before = copy.deepcopy(self.native.configs[boundary.UPNP_CONFIG])
        result = self.configure(house={"devices": [DEVICE]})
        self.assertEqual(result["callbacks"], callback_config())
        self.assertEqual(self.native.configs[boundary.UPNP_CONFIG], before)
        self.assertFalse(any(method == "PUT" and url.endswith(boundary.UPNP_CONFIG) for method, url, _ in self.native.calls))

    def test_jupnp_port_other_than_the_rendered_string_fails_at_once_without_a_write(self):
        # A decimal, a JSON number or another port: openHAB did not start with
        # conf/services/upnp.cfg, and a runtime write would restart jUPnP.
        for value in ("13615.0", 13615, 13615.0, True, "13616", None):
            with self.subTest(value=value):
                self.native = Native()
                if value is None:
                    del self.native.configs[boundary.UPNP_CONFIG]["httpProxyPort"]
                else:
                    self.native.configs[boundary.UPNP_CONFIG]["httpProxyPort"] = value
                with self.assertRaisesRegex(boundary.BoundaryError, r"has not applied conf/services/upnp\.cfg") as raised:
                    self.configure(existing_callbacks=callback_config())
                self.assertNotIn("13615", str(raised.exception))
                self.assertLess(self.native.now, 1, "a missing setting must fail without polling")
                self.assertFalse(any(method == "PUT" and url.endswith(boundary.UPNP_CONFIG)
                                     for method, url, _ in self.native.calls))

    def test_without_callbacks_jupnp_is_neither_read_nor_written(self):
        del self.native.configs[boundary.UPNP_CONFIG]["httpProxyPort"]
        self.assertIsNone(self.configure()["callbacks"])
        self.assertFalse(any(url.endswith(boundary.UPNP_CONFIG) for _, url, _ in self.native.calls))

    def test_adoption_preserves_exact_routes_listener_and_relocated_port(self):
        config = callback_config(14614)
        self.native.configs[boundary.UPNP_CONFIG]["httpProxyPort"] = "14614"
        result = self.configure(house={"devices": [DEVICE]}, existing_callbacks=config)
        self.assertEqual(result["callbacks"], config)
        self.assertFalse(any(DEVICE["ip"] in call[1] for call in self.native.calls))
        self.native = Native()
        with self.assertRaisesRegex(boundary.BoundaryError, "callback relay's port"):
            self.configure(house={"devices": [DEVICE]}, existing_callbacks=config)

    def test_wrong_existing_namespace_is_not_silently_replaced(self):
        self.native.configs[boundary.UPNP_CONFIG]["callbackURI"] = "http://localhost/other"
        with self.assertRaisesRegex(boundary.BoundaryError, "namespace"):
            self.configure(existing_callbacks=callback_config())
        self.assertFalse(any(method == "PUT" and url.endswith(boundary.UPNP_CONFIG) for method, url, _ in self.native.calls))

    def test_native_credentials_never_go_to_nonloopback_url(self):
        with self.assertRaises(boundary.BoundaryError):
            boundary.configure("http://192.0.2.10:13602", self.token, {}, "192.0.2.10", request=self.native.request)
        self.assertEqual(self.native.calls, [])


def nft_snapshot():
    return {"nftables": [{"metainfo": {"version": "test"}},
        {"table": {"family": "inet", "name": "thermite_native", "handle": 9}},
        {"chain": {"family": "inet", "table": "thermite_native", "name": "input", "type": "filter", "hook": "input", "prio": -10, "policy": "accept"}},
        {"rule": {"family": "inet", "table": "thermite_native", "chain": "input", "handle": 10, "expr": [
            {"match": {"op": "!=", "left": {"meta": {"key": "iifname"}}, "right": "lo"}},
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": {"set": [13602, 13603, 13604, 13605, 13624]}}},
            {"reject": {"type": "tcp reset"}}]}}]}


def house(controls, udn=True):
    """A house whose one device is DEVICE, listing `controls`."""
    device = {"id": "example_arc", "room": "example_lounge", "kind": "soundbar", "make": "Sonos", "model": "Arc",
              **DEVICE, "controls": controls}
    if not udn:
        del device["sonos_udn"]
    return {"version": 1, "rooms": {"example_lounge": {"label": "Example Lounge", "tag": "Room"}}, "devices": [device]}


class RenderedProxyPort(unittest.TestCase):
    """openHAB starts with the relay's port (conf/services/upnp.cfg) exactly
    when commissioning maps callback routes; nothing changes it at runtime."""

    def test_rendered_exactly_when_discovery_maps_callback_routes(self):
        cases = {"observed": (house({"observe": ["sonos"]}), True),
                 "controlled": (house({"observe": ["sonos"], "control": ["sonos"]}), True),
                 "identity only": (house({"observe": ["network_ping"]}), True),
                 "no sonos": (house({"observe": ["network_ping"]}, udn=False), False)}
        for name, (data, relayed) in cases.items():
            with self.subTest(name):
                self.assertEqual(oh_render.validate(data), [])
                files = oh_render.render(data)
                routes = boundary.discover_callbacks(data, ORIGIN, "192.0.2.10", request=Native().request)
                self.assertEqual(routes is not None, relayed)
                self.assertEqual(oh_render.UPNP_CFG in files, relayed)

    def test_port_is_the_effective_allocation_as_a_decimal_string(self):
        data = house({"observe": ["sonos"]})
        line = oh_render.build(data, host={"gateway": {"port": 14601}}).render()[oh_render.UPNP_CFG].splitlines()[-1]
        # ConfigDispatcher passes the text after "=" on as a String, which
        # jUPnP's Integer.parseInt takes; "13615.0" would be rejected.
        self.assertEqual(line, f"org.jupnp:httpProxyPort={oh_ports.CALLBACK_PORT}")
        host = {"http_port": 14602}
        moved = {**oh_ports.effective(host), "callbacks": 14615}
        with mock.patch.object(oh_ports, "effective", return_value=moved) as effective:
            files = oh_render.build(data, host=host).render()
        effective.assert_called_with(host)
        self.assertIn("org.jupnp:httpProxyPort=14615\n", files[oh_render.UPNP_CFG])
        with self.assertRaisesRegex(ValueError, "collides"):
            oh_render.build(data, host={"guest_http": {"port": oh_ports.CALLBACK_PORT}})

    def test_generate_allocates_from_the_host_file(self):
        scratch = AREA / ".scratch/upnp-proxy-generate"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary)
            (root / "instance").mkdir()
            (root / "versions.json").write_text((AREA / "versions.json").read_text())
            (root / "instance/host.yaml").write_text((AREA / "instance/host.yaml").read_text())
            (root / "instance/devices.yaml").write_text(json.dumps(house({"observe": ["sonos"]})))
            output = root / "output"
            with mock.patch.object(generate, "AREA", root), mock.patch.object(oh_render, "build", wraps=oh_render.build) as build, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                generate.main(["--house", str(root / "instance/devices.yaml"), "--conf", str(output / "conf"),
                               "--root", str(output)])
            self.assertEqual(build.call_args.kwargs["host"], json.loads((AREA / "instance/host.yaml").read_text()))
            self.assertEqual((output / "conf" / oh_render.UPNP_CFG).read_text().splitlines()[-1],
                             f"org.jupnp:httpProxyPort={oh_ports.CALLBACK_PORT}")

    def test_runtimes_without_the_relay_never_get_its_port(self):
        files = oh_render.render(house({"observe": ["sonos"]}))
        # The boot check and the read-only sandbox: jUPnP has no network.
        for paths in (None, oh_render.SESSION_FILES):
            self.assertEqual(oh_render.unaddressed(oh_render.read_only(files), paths)[oh_render.UPNP_CFG], oh_render.NO_UPNP)
        # The live sandbox: Sonos events come to its own port.
        self.assertEqual(oh_render.unrelayed(files), {path: text for path, text in files.items() if path != oh_render.UPNP_CFG})
        with mock.patch.object(sandbox_cli.lib, "mirror_conf") as mirror:
            sandbox_cli.mirror(None, True)
        self.assertIs(mirror.call_args.args[2], oh_render.unrelayed)


class NativeFilter(unittest.TestCase):
    def test_owned_table_only_and_matching_live_rule(self):
        rules = boundary.firewall_rules({})
        self.assertNotIn("flush ruleset", rules)
        self.assertEqual(rules.count("delete table inet thermite_native"), 1)
        self.assertIn('iifname != "lo" tcp dport { 13602, 13603, 13604, 13605, 13624 } reject with tcp reset', rules)
        self.assertIsNone(boundary.verify_firewall(json.dumps(nft_snapshot()), boundary.firewall_ports({})))

    def test_altered_hook_prior_rule_extra_chain_or_ports_fail(self):
        for kind in ("hook", "priority", "port", "extra_port", "extra_rule", "extra_chain", "early_accept", "wrong_interface", "dormant"):
            value = nft_snapshot()
            objects = value["nftables"]
            chain, rule = objects[2]["chain"], objects[3]["rule"]
            if kind == "hook": chain["hook"] = "output"
            elif kind == "priority": chain["prio"] = 0
            elif kind == "port": rule["expr"][1]["match"]["right"]["set"].pop()
            elif kind == "extra_port": rule["expr"][1]["match"]["right"]["set"].append(22)
            elif kind == "extra_rule": objects.append(copy.deepcopy(objects[3]))
            elif kind == "extra_chain": objects.append(copy.deepcopy(objects[2]))
            elif kind == "early_accept": rule["expr"].insert(0, {"accept": None})
            elif kind == "wrong_interface": rule["expr"][0]["match"]["op"] = "=="
            elif kind == "dormant": objects[1]["table"]["flags"] = ["dormant"]
            with self.subTest(kind=kind), self.assertRaises(boundary.BoundaryError):
                boundary.verify_native_filter(value, 13602, 13603)

    def test_duplicate_json_keys_and_wrong_types_fail(self):
        with self.assertRaises(boundary.BoundaryError):
            boundary.verify_firewall('{"nftables":[],"nftables":[]}', boundary.firewall_ports({}))
        with self.assertRaises(boundary.BoundaryError):
            boundary.verify_native_filter({"nftables": None}, 13602, 13603)

    def test_listener_proof_requires_loopback_http_and_filter_for_lsp(self):
        good = "LISTEN 0 50 127.0.0.1:13602 0.0.0.0:*\nLISTEN 0 50 [::1]:13603 [::]:*\n"
        self.assertEqual(boundary.verify_native_listeners(good, http_port=13602, https_port=13603), {"native_listeners": True})
        lsp = good + "LISTEN 0 50 *:13605 *:*\n"
        with self.assertRaises(boundary.BoundaryError):
            boundary.verify_native_listeners(lsp, http_port=13602, https_port=13603)
        boundary.verify_native_listeners(lsp, http_port=13602, https_port=13603, filtered=True)
        for snapshot in (good.replace("127.0.0.1", "0.0.0.0"), good.splitlines()[0], ""):
            with self.subTest(snapshot=snapshot), self.assertRaises(boundary.BoundaryError):
                boundary.verify_native_listeners(snapshot, http_port=13602, https_port=13603, filtered=True)

    def test_listener_proof_accepts_mapped_loopback_and_rejects_mapped_lan_addresses(self):
        good = "LISTEN 0 50 [::ffff:127.0.0.1]:13602 [::]:*\nLISTEN 0 50 [::ffff:127.0.0.1]:13603 [::]:*\n"
        self.assertEqual(boundary.verify_native_listeners(good, http_port=13602, https_port=13603), {"native_listeners": True})
        for address in ("192.0.2.1", "0.0.0.0"):
            with self.subTest(address=address), self.assertRaises(boundary.BoundaryError):
                boundary.verify_native_listeners(good.replace("127.0.0.1", address), http_port=13602, https_port=13603)


if __name__ == "__main__":
    unittest.main()
