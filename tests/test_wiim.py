"""A WiiM player over its LinkPlay HTTP API (oh_integrations.wiim_http): its
two Things, what each request answers, the commands it sends and the
inline transformations that read and send them, and its stand-in
(oh_av_sim.Player).

A fictional desk with a documentation address; the transformations run in
node as openHAB's JS transformation runs them, with `input` bound.
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_av_sim as av_sim  # noqa: E402
import oh_inventory  # noqa: E402
import oh_metrics  # noqa: E402
import oh_render as render  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import remote_activity  # noqa: E402
from oh_integrations import wiim_http  # noqa: E402

AMP = {"id": "example_amp", "room": "example_desk", "kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro",
       "ip": "192.0.2.32", "controls": {"observe": ["wiim_http"], "control": ["wiim_http"]}}


def house(**changes):
    return {"version": 1, "rooms": {"example_desk": {"label": "Example Desk", "tag": "Room"}},
            "devices": [{**AMP, **changes}]}


def built(data):
    conf = render.build(data)
    return conf.yaml[wiim_http.FILE]


def conf_things_items(conf) -> tuple[dict, dict]:
    """Every Thing and Item of a built conf, as oh_inventory.cross_check takes them."""
    things = {uid: {"file": path, "body": body} for path, file in conf.yaml.items() for uid, body in file.things.items()}
    items = {name: {"file": path, "body": body} for path, file in conf.yaml.items() for name, body in file.items.items()}
    return things, items


def script(transformation: str) -> str:
    """The script of an inline JS transformation, JS(|...)."""
    assert transformation.startswith("JS(|") and transformation.endswith(")"), transformation
    return transformation[len("JS(|"):-1]


@unittest.skipUnless(shutil.which("node"), "node runs the inline transformations")
class Transformations(unittest.TestCase):
    def run_js(self, transformation: str, inputs: list[str]) -> list:
        program = ("const cases = JSON.parse(process.argv[1]); const code = process.argv[2];"
                   "console.log(JSON.stringify(cases.map((input) => eval(code))));")
        result = subprocess.run([shutil.which("node"), "-e", program, json.dumps(inputs), script(transformation)],
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout)

    def test_a_reading_with_no_value_is_empty_never_null(self):
        # openHAB keeps an Item's last state when a transformation yields
        # null; "" makes a number UNDEF.
        self.assertEqual(self.run_js(wiim_http.SECONDS, ["215000", "0", "999", "", "unknow", "-5"]),
                         ["215", "0", "0", "", "", ""])
        self.assertEqual(self.run_js(wiim_http.CELSIUS, ["38", "31.5", "-998", "", "x"]), ["38", "31.5", "", "", ""])
        battery = [json.dumps({"BleRemoteControl": "1", "BleRemoteConnected": "1", "BleRemoteBatterylevel": "40"}),
                   json.dumps({"BleRemoteControl": "1", "BleRemoteConnected": "0", "BleRemoteBatterylevel": "40"}),
                   json.dumps({"BleRemoteControl": "1", "BleRemoteConnected": "1", "BleRemoteBatterylevel": ""})]
        self.assertEqual(self.run_js(wiim_http.REMOTE_BATTERY, battery), ["40", "", ""])

    def test_the_service_is_the_vendor_or_the_network_modes_name_and_nothing_for_an_input(self):
        cases = [{"mode": "31", "vendor": ""}, {"mode": "10", "vendor": "Amazon Music"},
                 {"mode": "43", "vendor": "Spotify"}, {"mode": "10", "vendor": ""}, {"mode": "99", "vendor": " "}]
        self.assertEqual(self.run_js(wiim_http.service_js(), [json.dumps(case) for case in cases]),
                         ["Spotify", "Amazon Music", "", "", "Multiroom"])

    def test_a_mini_passes_on_only_the_inputs_it_has(self):
        mini = {**AMP, "model": "Mini"}
        self.assertEqual(self.run_js(wiim_http.input_js(mini), ["wifi", "line-in", "bluetooth", "optical", "HDMI"]),
                         ["wifi", "line-in", "bluetooth", None, None])

    def test_each_command_passes_only_a_value_it_lists(self):
        self.assertEqual(self.run_js(wiim_http.play_mode_js(), ["4", "0", "2", "5", "-1", "x"]),
                         ["4", "0", "2", None, None, None])
        self.assertEqual(self.run_js(wiim_http.commands(AMP)["sub_level"][1], ["-15", "15", "8", "16", "3.5", "", "ON"]),
                         ["-15", "15", "8", None, None, None, None])
        # "&" goes doubly escaped: the binding's URI builder turns "%%" into "%".
        self.assertEqual(self.run_js(wiim_http.eq_preset_js(), ["R&B", "Bass Booster", "Rock", "Nope", "rock"]),
                         ["R%%26B", "Bass Booster", "Rock", None, None])


class Render(unittest.TestCase):
    def test_a_controlled_player_has_a_thing_for_what_plays_and_one_for_itself(self):
        out = built(house())
        player, device = out.things["http:url:example_amp"], out.things["http:url:device:example_amp"]
        self.assertEqual((player["config"]["refresh"], device["config"]["refresh"]),
                         (wiim_http.REFRESH, wiim_http.DEVICE_REFRESH))
        for thing in (player, device):
            self.assertEqual(thing["config"]["baseURL"], "https://192.0.2.32/httpapi.asp?")
        requests = {name: channel["config"]["stateExtension"] for name, channel in
                    {**player["channels"], **device["channels"]}.items()}
        self.assertEqual(requests["album"], "command=getPlayerStatus")
        self.assertEqual(requests["eq_preset"], "command=EQGetBand")
        self.assertEqual(requests["sample_rate"], "command=getMetaInfo")
        self.assertEqual(requests["cpu_temperature"], "command=getStatusEx")
        self.assertEqual(requests["sub_level"], "command=getSubLPF")
        self.assertEqual(requests["light"], "command=LED_SWITCH_GET")
        sent = {name: channel["config"].get("commandExtension") for name, channel in
                {**player["channels"], **device["channels"]}.items() if channel["config"].get("commandExtension")}
        self.assertEqual(sent, {
            "volume": "command=setPlayerCmd:vol:%2$s", "mute": "command=setPlayerCmd:mute:%2$s",
            "input": "command=setPlayerCmd:switchmode:%2$s", "play_mode": "command=setPlayerCmd:loopmode:%2$s",
            "eq": "command=EQ%2$s", "eq_preset": "command=EQLoad:%2$s", "sub_level": "command=setSubLPF:level:%2$s",
            "light": "command=LED_SWITCH_SET:%2$s", "key": "command=setPlayerCmd:%2$s"})
        for name, channel in {**player["channels"], **device["channels"]}.items():
            if name not in sent:
                self.assertEqual(channel["config"]["mode"], "READONLY", name)
            # One cache per request: whichever channel makes it must read again after a command.
            self.assertTrue(channel["config"].get("refreshAfterCommand"), name)
        self.assertEqual(player["channels"]["eq"]["config"]["onValue"], "On")
        # A key reads the status, so a press reads it again, and keeps nothing of it.
        key = player["channels"]["key"]["config"]
        self.assertNotIn("mode", key)
        self.assertEqual((key["stateExtension"], key["stateTransformation"]), ("command=getPlayerStatus", ["JS(|null)"]))

    def test_items_say_what_they_are_and_commanded_ones_list_what_they_take(self):
        items = built(house()).items
        # Plain numbers, unit in the display pattern: the gateway passes plain numbers only.
        self.assertNotIn("example_amp_position", items)
        self.assertEqual((items["example_amp_duration"]["type"], items["example_amp_duration"]["format"]),
                         ("Number", "%d s"))
        self.assertEqual(items["example_amp_cpu_temperature"]["tags"], ["Measurement", "Temperature"])
        self.assertTrue(all(":" not in item["type"] for item in items.values()))
        self.assertEqual(items["example_amp_sub_level"]["tags"], ["Setpoint", "Level"])
        self.assertEqual(items["example_amp_service"]["tags"], ["Status", "App"])
        mode = items["example_amp_play_mode"]
        self.assertEqual(mode["tags"], ["Control", "Mode"])
        self.assertFalse(mode["autoupdate"])
        self.assertIn("5=Shuffle and repeat one", mode["metadata"]["stateDescription"]["config"]["options"])
        self.assertNotIn("5=", mode["metadata"]["commandDescription"]["config"]["options"])
        preset = items["example_amp_eq_preset"]["metadata"]["commandDescription"]["config"]["options"]
        self.assertIn("R&B=R&B", preset)
        self.assertEqual(items["example_amp_sub_level"]["metadata"]["stateDescription"]["config"],
                         {"min": -15, "max": 15, "step": 1, "pattern": "%d dB"})

    def test_every_display_pattern_is_a_java_format(self):
        """A lone % (the remote battery's unit) fails every render of its Item."""
        spec = re.compile(r"%%|%[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z]")
        items = built(house()).items
        for name, item in items.items():
            described = ((item.get("metadata") or {}).get("stateDescription") or {}).get("config") or {}
            for text in (item.get("format"), described.get("pattern")):
                if text:
                    self.assertNotIn("%", spec.sub("", text), name)
        self.assertEqual(items["example_amp_remote_battery"]["format"], "%d %%")

    def test_a_player_under_observe_only_reads_everything_and_sends_nothing(self):
        out = built(house(controls={"observe": ["wiim_http"]}))
        channels = {**out.things["http:url:example_amp"]["channels"],
                    **out.things["http:url:device:example_amp"]["channels"]}
        self.assertNotIn("key", channels)
        for name, channel in channels.items():
            self.assertEqual(channel["config"]["mode"], "READONLY", name)
            self.assertNotIn("commandExtension", channel["config"], name)
        self.assertNotIn("example_amp_key", out.items)
        self.assertEqual(out.items["example_amp_play_mode"]["tags"], ["Status", "Mode"])

    def test_both_things_are_aimed_at_a_stand_in(self):
        files = render.render(house())
        aimed = wiim_http.aim(files, AMP, "127.0.0.1:18080")[wiim_http.FILE]
        self.assertEqual(aimed.count("baseURL: http://127.0.0.1:18080/httpapi.asp?"), 2)
        self.assertNotIn("192.0.2.32", aimed)

    def test_each_thing_is_filed_under_the_player(self):
        data = house()
        conf = render.build(data)
        self.assertEqual(oh_inventory.cross_check(data, *conf_things_items(conf)), [])
        self.assertEqual(oh_metrics.exporter_map(data, conf)["devices"]["example_amp"]["things"],
                         ["http:url:device:example_amp", "http:url:example_amp"])

    def test_temperatures_and_the_remotes_battery_are_kept_as_their_own_points(self):
        kept = {spec["point"] for name, spec in oh_metrics.exporter_map(house())["items"].items()
                if name.startswith("example_amp_")}
        self.assertEqual(kept, {"volume", "mute", "input", "temperature", "battery", "playing"})

    def test_the_feed_leaves_out_what_drifts_with_every_poll(self):
        data = house()
        feed = remote_activity.manifest(House(data), render.build(data))["items"]
        self.assertLessEqual({"example_amp_firmware", "example_amp_internet", "example_amp_eq_preset"}, set(feed))
        self.assertFalse({"example_amp_cpu_temperature", "example_amp_remote_battery", "example_amp_sample_rate"}
                         & set(feed))

    def test_the_presets_and_play_modes_are_the_players_own(self):
        self.assertEqual(wiim_http.EQ_PRESETS, av_sim.Player.EQ_PRESETS)
        self.assertLessEqual({value for value, _ in wiim_http.PLAY_MODES_READ}, set(av_sim.Player.LOOPS))

    def test_a_mini_has_three_inputs_and_neither_subwoofer_nor_temperatures(self):
        mini = {**AMP, "kind": "audio_streamer", "model": "Mini"}
        out = built(house(**mini))
        device = out.things["http:url:device:example_amp"]["channels"]
        self.assertFalse({"sub", "sub_level", "sub_crossover", "sub_phase", "cpu_temperature",
                          "board_temperature"} & set(device) | {name for name in out.items if "_sub" in name})
        # No channel reads getSubLPF, so the binding never polls it.
        self.assertNotIn("command=getSubLPF", {c["config"]["stateExtension"] for c in device.values()})
        options = out.items["example_amp_input"]["metadata"]["commandDescription"]["config"]["options"]
        self.assertEqual(options, "wifi=Wi-Fi,bluetooth=Bluetooth,line-in=Line in")
        self.assertEqual(wiim_http.remote(mini)["input"]["options"],
                         [["wifi", "Wi-Fi"], ["bluetooth", "Bluetooth"], ["line-in", "Line in"]])
        page = wiim_http.audio(mini)
        self.assertEqual([entry["item"] for entry in page["settings"]],
                         ["example_amp_play_mode", "example_amp_eq", "example_amp_eq_preset", "example_amp_light"])
        self.assertFalse({"example_amp_sub", "example_amp_cpu_temperature"} & {e["item"] for e in page["facts"]})

    def test_a_model_it_does_not_know_is_refused(self):
        self.assertEqual(wiim_http.validate(AMP), [])
        self.assertEqual(wiim_http.validate({**AMP, "model": "WiiM Amp Pro"}),
                         ["example_amp: wiim_http knows no model 'WiiM Amp Pro' (one of Amp Pro, Mini)"])
        self.assertTrue(render.validate(house(model="Ultra")))
        # Under control alone (itself refused) too.
        controlled = {**AMP, "model": "Ultra", "controls": {"control": ["wiim_http"]}}
        self.assertIn("example_amp: wiim_http knows no model 'Ultra' (one of Amp Pro, Mini)",
                      wiim_http.validate(controlled))

    def test_an_unknown_model_named_a_rooms_audio_is_refused_not_a_crash(self):
        # The routing feature asks the WiiM for its remote's inputs: only
        # once the integrations have found every device well formed.
        data = json.loads((AREA / "instance/devices.yaml").read_text())
        data["devices"].append({**AMP, "model": "Ultra", "room": "example_east", "ip": "192.0.2.41"})
        data["routing"]["rooms"]["example_east"]["audio"] = AMP["id"]
        self.assertIn("example_amp: wiim_http knows no model 'Ultra' (one of Amp Pro, Mini)", render.validate(data))

    def test_each_model_is_the_kind_it_is(self):
        self.assertEqual(wiim_http.validate({**AMP, "model": "Mini", "kind": "audio_streamer"}), [])
        self.assertEqual(wiim_http.validate({**AMP, "model": "Mini"}),
                         ["example_amp: a WiiM Mini is kind audio_streamer, not 'streaming_amp'"])
        self.assertEqual(wiim_http.validate({**AMP, "kind": "streamer"}),
                         ["example_amp: a WiiM Amp Pro is kind streaming_amp, not 'streamer'"])

    def test_the_stand_in_has_what_each_model_has(self):
        # oh_av_sim's Player answers as the integration expects each model to.
        self.assertEqual(set(av_sim.Player.MODELS), set(wiim_http.MODELS))
        for model, spec in av_sim.Player.MODELS.items():
            device = {**AMP, "model": model, "kind": wiim_http.MODELS[model]["kind"]}
            with self.subTest(model=model):
                self.assertEqual(set(spec["modes"]), {name for name, _ in wiim_http.inputs(device)})
                self.assertEqual(spec["sub"], wiim_http.has(device, "sub"))
                self.assertEqual(spec["temperatures"], wiim_http.has(device, "cpu_temperature"))
                self.assertEqual(wiim_http.MODES.get(spec["mode"]), "line-in" if model == "Mini" else "optical")


class Served(unittest.TestCase):
    """The player() a test case makes, behind its HTTP server."""
    def setUp(self):
        self.sim = self.player()
        self.server = av_sim.Server(self.sim).start()
        self.addCleanup(self.server.stop)

    def player(self) -> av_sim.Player:
        return av_sim.Player(title="Example Song", album="Example Album", position=61000, duration=215000)

    def get(self, query: str) -> str:
        with urllib.request.urlopen(f"http://{self.server.address}/httpapi.asp?{query}", timeout=5) as answer:
            return answer.read().decode()


class StandIn(Served):

    def test_it_reads_the_query_as_a_web_server_does(self):
        # Unescaped, "&" ends the command: the player loads nothing.
        self.assertIn("Failed", self.get("command=EQLoad:R&B"))
        self.assertEqual(self.sim.eq_preset, "Flat")
        self.assertEqual(self.get("command=EQLoad:R%26B"), '{"status":"OK"}')
        self.assertEqual(json.loads(self.get("command=EQGetBand"))["Name"], "R&B")
        self.get("command=EQOff")
        self.assertEqual(json.loads(self.get("command=EQGetBand"))["EQStat"], "Off")

    def test_it_takes_play_mode_sub_level_and_light_and_reads_them_back(self):
        self.assertEqual(self.get("command=setPlayerCmd:loopmode:3"), "OK")
        self.assertEqual(json.loads(self.get("command=getPlayerStatus"))["loop"], "3")
        self.assertEqual(self.get("command=setSubLPF:level:-4"), "OK")
        self.assertEqual(json.loads(self.get("command=getSubLPF"))["level"], -4)
        self.assertIn("Failed", self.get("command=setSubLPF:level:16"))
        self.get("command=LED_SWITCH_SET:0")
        self.assertEqual(self.get("command=LED_SWITCH_GET"), "0")
        status = json.loads(self.get("command=getPlayerStatus"))
        self.assertEqual((status["curpos"], status["totlen"], bytes.fromhex(status["Album"]).decode()),
                         ("61000", "215000", "Example Album"))


class MiniStandIn(Served):
    def player(self) -> av_sim.Player:
        return av_sim.Player(model="Mini")

    def test_it_answers_as_a_mini_does(self):
        status = json.loads(self.get("command=getStatusEx"))
        self.assertEqual((status["project"], status["plm_support"]), ("Muzo_Mini", "0x300006"))
        self.assertNotIn("temperature_cpu", status)
        self.assertNotIn("plugged", json.loads(self.get("command=getSubLPF")))
        self.assertIn("Failed", self.get("command=setSubLPF:level:-4"))
        self.assertEqual(self.get("command=setPlayerCmd:switchmode:optical"), "Failed")
        self.assertEqual(self.get("command=setPlayerCmd:switchmode:bluetooth"), "OK")
        self.assertEqual(json.loads(self.get("command=getPlayerStatus"))["mode"], "41")
        self.assertEqual(json.loads(self.get("command=EQGetBand"))["source_name"], "bluetooth")


if __name__ == "__main__":
    unittest.main()
