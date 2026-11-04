"""The stand-ins stay up under a client that misbehaves, the checks that
serve them rewrite conf/ only under the instance lock, and the Control4
summary tells a deliberate multi-item link from an ambiguous match."""
import json
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_av_sim as av_sim  # noqa: E402
import oh_control4 as control4  # noqa: E402
import oh_matrix_sim as matrix_sim  # noqa: E402
import oh_sony_sim as sony_sim  # noqa: E402
from oh_testing import controls, routing  # noqa: E402


def raw_status(address: str, request: bytes, timeout: float = 3) -> int:
    """The HTTP status of the answer to one hand-made request."""
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=timeout) as conn:
        conn.sendall(request)
        answer = b""
        while b"\r\n" not in answer:
            chunk = conn.recv(4096)
            if not chunk:
                break
            answer += chunk
    return int(answer.split(b" ", 2)[1])


def post(address: str, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://{address}{path}", data=json.dumps(body).encode(),
                                     headers=headers or {}, method="POST")
    with urllib.request.urlopen(request, timeout=3) as reply:
        return reply.status, json.loads(reply.read())


def wait_for(predicate, timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class MatrixSimTest(unittest.TestCase):
    def serve(self, idle_s: float) -> tuple[matrix_sim.BK808, matrix_sim.Server]:
        matrix = matrix_sim.BK808()
        matrix.idle_s = idle_s
        server = matrix_sim.Server(matrix).start()
        self.addCleanup(server.stop)
        return matrix, server

    def idle(self, server: matrix_sim.Server) -> socket.socket:
        host, _, port = server.address.rpartition(":")
        conn = socket.create_connection((host, int(port)), timeout=3)
        self.addCleanup(conn.close)
        self.assertTrue(wait_for(lambda: server.httpd.connection is not None), "the idle connection was not taken")
        return conn

    def test_an_idle_connection_holds_the_next_request_only_idle_s(self):
        matrix, server = self.serve(0.3)
        self.idle(server)
        began = time.monotonic()
        status, reply = post(server.address, matrix_sim.PATH, {"comhead": "get video status"})
        self.assertEqual((status, reply["comhead"]), (200, "get video status"))
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertEqual(matrix.comheads(), ["get video status"])

    def test_stop_returns_with_an_idle_connection_open(self):
        _, server = self.serve(30)
        self.idle(server)
        stopped = threading.Thread(target=server.stop, daemon=True)
        began = time.monotonic()
        stopped.start()
        stopped.join(3)
        self.assertFalse(stopped.is_alive(), "stop() is still waiting on the idle connection")
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertFalse(server.thread.is_alive())

    def test_a_bad_content_length_gets_400_and_the_matrix_keeps_answering(self):
        matrix, server = self.serve(2)
        for length in (b"abc", b"-1", b"1_0", b"+5", str(matrix_sim.MAX_BODY + 1).encode()):
            with self.subTest(length=length):
                status = raw_status(server.address, b"POST " + matrix_sim.PATH.encode() + b" HTTP/1.0\r\n"
                                    b"Content-Length: " + length + b"\r\n\r\n")
                self.assertEqual(status, 400)
        status, reply = post(server.address, matrix_sim.PATH, {"comhead": "get video status"})
        self.assertEqual((status, reply["comhead"]), (200, "get video status"))
        self.assertEqual(matrix.comheads(), ["get video status"])


class SonySimTest(unittest.TestCase):
    def setUp(self):
        paired = "stand-in-fixture"
        self.tv = sony_sim.TV(cookie=paired)
        self.server = sony_sim.Server(self.tv).start()
        self.addCleanup(self.server.stop)
        self.headers = {"Cookie": f"auth={paired}"}

    def call(self, path: str, body: dict) -> dict:
        status, answer = post(self.server.address, path, body, self.headers)
        self.assertEqual(status, 200)
        return answer

    def test_malformed_params_get_illegal_argument_and_the_tv_keeps_answering(self):
        illegal = [3, "Illegal Argument"]
        cases = [
            ("/sony/system", {"method": "setPowerStatus", "id": 55}),
            ("/sony/system", {"method": "setPowerStatus", "params": "on", "id": 55}),
            ("/sony/system", {"method": "setPowerStatus", "params": {"status": True}, "id": 55}),
            ("/sony/system", {"method": "setPowerStatus", "params": [5], "id": 55}),
            ("/sony/system", {"method": "setPowerStatus", "params": [{"status": "on"}], "id": 55}),
            ("/sony/system", {"method": "setPowerStatus", "params": [{}], "id": 55}),
            ("/sony/audio", {"method": "setAudioMute", "params": [], "id": 55}),
            ("/sony/audio", {"method": "setAudioMute", "params": [[True]], "id": 55}),
            ("/sony/audio", {"method": "setAudioVolume", "params": ["5"], "id": 55}),
            ("/sony/audio", {"method": "setAudioVolume", "params": [{"target": "speaker", "volume": "²"}],
                             "id": 55}),
            ("/sony/avContent", {"method": "setPlayContent", "params": [{"uri": 5}], "id": 55}),
            ("/sony/avContent", {"method": "setPlayContent", "params": None, "id": 55}),
        ]
        for path, body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.call(path, body), {"error": illegal, "id": 55})
        self.assertEqual(self.call("/sony/system", {"method": "setPowerStatus", "params": [7]}),
                         {"error": illegal, "id": None})
        self.assertEqual(self.tv.sets(), [])
        self.assertEqual(self.call("/sony/system", {"method": "setPowerStatus", "params": [{"status": False}],
                                                    "id": 1}), {"result": [], "id": 1})
        self.assertEqual(self.tv.sets(), ["setPowerStatus False"])
        self.assertFalse(self.tv.power)

    def test_invalid_json_and_bad_lengths_get_400(self):
        for body in (b"{", b"[1]", b'{"params": []}', b"[" * 5000):
            with self.subTest(body=body[:8]):
                request = urllib.request.Request(f"http://{self.server.address}/sony/system", data=body,
                                                 headers=self.headers, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                caught.exception.close()
                self.assertEqual(caught.exception.code, 400)
        for length in (b"abc", b"-1", str(sony_sim.MAX_BODY + 1).encode()):
            with self.subTest(length=length):
                self.assertEqual(raw_status(self.server.address, b"POST /sony/system HTTP/1.0\r\n"
                                            b"Content-Length: " + length + b"\r\n\r\n"), 400)
        self.assertEqual(self.call("/sony/system", {"method": "getPowerStatus", "params": [], "id": 2}),
                         {"result": [{"status": "active"}], "id": 2})


class AvSimTest(unittest.TestCase):
    def test_a_bad_content_length_gets_400_and_the_receiver_keeps_answering(self):
        receiver = av_sim.Receiver()
        server = av_sim.Server(receiver).start()
        self.addCleanup(server.stop)
        for length in (b"abc", b"-1", str(av_sim.MAX_BODY + 1).encode()):
            with self.subTest(length=length):
                self.assertEqual(raw_status(server.address, b"POST /goform/AppCommand.xml HTTP/1.0\r\n"
                                            b"Content-Length: " + length + b"\r\n\r\n"), 400)
        self.assertEqual(receiver.requests, [])
        body = b'<?xml version="1.0"?><tx><cmd id="1">GetMuteStatus</cmd></tx>'
        request = urllib.request.Request(f"http://{server.address}/goform/AppCommand.xml", data=body, method="POST")
        with urllib.request.urlopen(request, timeout=3) as reply:
            self.assertIn(b"<mute>off</mute>", reply.read())


class ConfUnderLockTest(unittest.TestCase):
    def test_a_run_the_lock_refuses_starts_nothing_and_leaves_conf_alone(self):
        held = mock.MagicMock()
        held.__enter__.side_effect = routing.lib.SandboxError("held by another run")
        for module, attr, server_of in ((routing, "servers", lambda s: s), (controls, "stand_ins", lambda s: s.server)):
            with self.subTest(module=module.__name__):
                check, entry = mock.MagicMock(), mock.MagicMock()
                setattr(check, attr, {"fixture": entry})
                with mock.patch.object(module.inventory, "load", return_value={"routing": {}}), \
                        mock.patch.object(module.render, "validate", return_value=[]), \
                        mock.patch.object(module, "Check", return_value=check), \
                        mock.patch.object(module.lib, "InstanceLock", return_value=held), \
                        mock.patch.object(module.lib, "wipe_dir") as wipe, \
                        mock.patch.object(module.render, "write") as write:
                    with self.assertRaisesRegex(module.lib.SandboxError, "held by another run"):
                        module.run_check(mock.Mock(house=None, timeout=1), None, lambda message: None)
                wipe.assert_not_called()
                write.assert_not_called()
                check.seed.assert_not_called()
                server_of(entry).start.assert_not_called()
                server_of(entry).stop.assert_called_once_with()


ROOM = {"id": 1, "typeName": "room", "name": "Den"}
TV = {"id": 10, "typeName": "device", "name": "TV", "roomId": 1, "proxy": "tv", "model": "X"}
BUTTON = {"id": 11, "typeName": "device", "name": "TV button", "roomId": 1, "proxy": "uibutton"}
IR_TV = {"id": 12, "typeName": "device", "name": "TV IR", "roomId": 1, "proxy": "tv", "model": "X"}


class Control4Test(unittest.TestCase):
    def summary(self, items: list[dict], devices: list[dict]) -> tuple[dict, dict, str]:
        inventory = {"rooms": {"den": {}}, "devices": devices}
        result = control4.devices(items, {}, inventory, {})
        return result, {d["c4_id"]: d for d in result["devices"]}, control4.markdown(Path("items-x.json"), result,
                                                                                     inventory)

    def test_a_c4_item_list_links_one_device_to_its_items_without_ambiguity(self):
        result, found, text = self.summary([ROOM, TV, BUTTON], [{"id": "den_tv", "room": "den", "c4_item": [10, 11]}])
        self.assertTrue(found[11]["virtual"])
        self.assertEqual((found[10]["matches"], found[11]["matches"]), (["den_tv"], ["den_tv"]))
        self.assertEqual(result["shared_matches"], {})
        self.assertNotIn("## Ambiguous matches", text)
        result, found, text = self.summary([ROOM, TV, IR_TV], [{"id": "den_tv", "room": "den", "c4_item": [10, 12]}])
        self.assertEqual(result["shared_matches"], {})
        self.assertNotIn("## Ambiguous matches", text)

    def test_markdown_names_a_software_driver_among_shared_matches(self):
        result, _, _ = self.summary([ROOM, TV, BUTTON], [{"id": "den_tv", "room": "den", "c4_item": [10, 11]}])
        result["shared_matches"] = {"den_tv": [10, 11]}
        text = control4.markdown(Path("items-x.json"), result, {"rooms": {"den": {}}, "devices": []})
        self.assertIn("- den_tv is matched by 2 Control4 devices: TV [10] in Den; TV button [11] in Den", text)

    def test_two_devices_on_one_item_and_a_guess_shared_by_two_items_stay_ambiguous(self):
        result, found, text = self.summary([ROOM, TV], [{"id": "den_tv", "room": "den", "c4_item": 10},
                                                        {"id": "den_tv_2", "room": "den", "c4_item": [10]}])
        self.assertEqual(found[10]["matches"], ["den_tv", "den_tv_2"])
        self.assertIn("- TV [10] in Den: den_tv, den_tv_2", text)
        result, found, text = self.summary([ROOM, TV, IR_TV], [{"id": "den_tv", "room": "den", "model": "X"}])
        self.assertEqual(result["shared_matches"], {"den_tv": [10, 12]})
        self.assertIn("- den_tv is matched by 2 Control4 devices: TV [10] in Den; TV IR [12] in Den", text)


if __name__ == "__main__":
    unittest.main()
