"""What each device plays (oh_features.playing): what its integrations read,
the two Items and the rule that joins them, and where the remotes show it.

A fictional house with documentation addresses. The production rule runs
against stub openHAB and a fake clock in tests/playing_harness.js; the
shared web and terminal session runs on its mock backend.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_render as render  # noqa: E402
import oh_site_contract  # noqa: E402
from oh_conf import House  # noqa: E402
from oh_features import playing, remote_activity, remote_model  # noqa: E402
from oh_integrations import androidtv_googletv  # noqa: E402

SHIELD = {"id": "example_shield", "room": "example_lounge", "kind": "streamer", "make": "NVIDIA",
          "model": "SHIELD TV Pro", "ip": "192.0.2.31", "controls": {"observe": ["cast"], "control": ["androidtv_googletv"]}}
SPEAKER = {"id": "example_speaker", "room": "example_lounge", "kind": "soundbar", "make": "Sonos", "model": "Arc",
           "ip": "192.0.2.20", "mac": bytes((2, 0, 0, 0, 0, 20)).hex(":"), "sonos_udn": "RINCON_02000000001401400",
           "controls": {"observe": ["sonos"], "control": ["sonos"]}}
AMP = {"id": "example_amp", "room": "example_desk", "kind": "streaming_amp", "make": "WiiM", "model": "Amp Pro",
       "ip": "192.0.2.32", "controls": {"observe": ["wiim_http"]}}
TV = {"id": "example_tv", "room": "example_lounge", "kind": "tv", "make": "Sony", "model": "XR-65A80L",
      "ip": "192.0.2.60", "controls": {"observe": ["network_ping", "cast"]}}
PC = {"id": "example_pc", "room": "example_desk", "kind": "computer", "ip": "192.0.2.70",
      "controls": {"observe": ["network_ping"]}}
HOUSE = {
    "version": 1,
    "rooms": {"example_desk": {"label": "Example Desk", "tag": "Room"},
              "example_lounge": {"label": "Example Lounge", "tag": "Room"}},
    "devices": [SHIELD, SPEAKER, AMP, TV, PC],
}


def house(devices=None):
    data = copy.deepcopy(HOUSE)
    if devices is not None:
        data["devices"] = devices
    return data


def entries(data):
    return {entry["id"]: entry for entry in playing.table(House(data))["devices"]}


class Reads(unittest.TestCase):
    def test_the_fictional_house_is_valid(self):
        self.assertEqual(render.validate(house()), [])

    def test_each_device_reads_what_its_integrations_report(self):
        found = entries(house())
        self.assertEqual(sorted(found), ["example_amp", "example_shield", "example_speaker", "example_tv"])
        shield = found["example_shield"]
        self.assertEqual((shield["app"], shield["title"], shield["power"]),
                         ("example_shield_playing_app", "example_shield_playing_title", "example_shield_android_power"))
        # The app on screen first: it says whether anything plays, not the cast session.
        self.assertEqual([source["item"] for source in shield["apps"]], ["example_shield_android_app", "example_shield_cast_app"])
        self.assertTrue(shield["apps"][0]["package"])
        self.assertEqual(shield["apps"][0]["names"]["com.netflix.ninja"], "Netflix")
        self.assertIn("com.google.android.backdrop", shield["apps"][0]["idle"])
        self.assertEqual(shield["apps"][1]["idle"], ["Backdrop", "TV"])
        self.assertEqual(shield["titles"], [{"title": "example_shield_cast_title", "artist": "example_shield_cast_artist",
                                             "series": "example_shield_cast_series", "season": "example_shield_cast_season",
                                             "episode": "example_shield_cast_episode"}])
        self.assertEqual(found["example_speaker"]["titles"], [{"title": "example_speaker_title", "artist": "example_speaker_artist",
                                                               "when": {"item": "example_speaker_state", "is": ["PLAYING"]}}])
        self.assertEqual(found["example_amp"]["titles"][0]["when"], {"item": "example_amp_status", "is": ["play"]})
        self.assertIsNone(found["example_tv"]["power"])
        self.assertIsNone(playing.items(PC))

    def test_every_app_name_is_one_package_and_no_idle_package_is_named(self):
        self.assertFalse(set(androidtv_googletv.APP_NAMES) & set(androidtv_googletv.IDLE_APPS))
        for package in [*androidtv_googletv.APP_NAMES, *androidtv_googletv.IDLE_APPS]:
            self.assertRegex(package, r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


class Render(unittest.TestCase):
    def test_two_free_text_items_per_device_and_one_rule_that_reads_only_items_that_exist(self):
        conf = render.build(house())
        files = conf.render()
        items = {name: item for file in conf.yaml.values() for name, item in file.items.items()}
        self.assertEqual(items["example_shield_playing_app"], {"type": "String", "label": "Playing App",
                                                               "groups": ["example_shield"], "tags": ["Status", "App"]})
        self.assertEqual(items["example_shield_playing_title"]["tags"], ["Status", "Info"])
        self.assertNotIn("example_pc_playing_app", items)
        for entry in playing.table(House(house()))["devices"]:
            names = [entry["power"], *[source["item"] for source in entry["apps"]]]
            for source in entry["titles"]:
                names += [source.get(key) for key in ("title", "artist", "series", "season", "episode")]
                names.append((source.get("when") or {}).get("item"))
            for name in filter(None, names):
                self.assertIn(name, items, f"{entry['id']} reads {name}")
        self.assertEqual(items["example_shield_cast_season"]["channel"], "chromecast:chromecast:example_shield:seasonNumber")
        text = files[playing.RULE]
        self.assertTrue(text.startswith("// Generated by bin/thermite generate from lib/oh_features/playing/playing.js"))
        self.assertNotIn(playing.MARK, text)
        self.assertIn(playing.RULE, render.ITEM_ONLY_RULES)
        self.assertEqual(render.read_only(files)[playing.RULE], text)
        self.assertIn("jsscripting", conf.addons.get("automation", set()))

    def test_a_house_where_nothing_tells_what_plays_has_no_rule(self):
        files = render.render(house(devices=[PC]))
        self.assertNotIn(playing.RULE, files)
        self.assertNotIn(playing.ITEMS, files)

    def test_the_remotes_read_them_and_a_guest_may_be_kept_from_them(self):
        data = house()
        model = remote_model.build(House(data))
        self.assertEqual(model["devices"]["example_shield"]["playing"],
                         {"app": "example_shield_playing_app", "title": "example_shield_playing_title"})
        self.assertNotIn("playing", model["devices"]["example_pc"])
        contract = oh_site_contract.compile(House(data), render.build(data))
        for name in ("example_shield_playing_app", "example_shield_playing_title"):
            self.assertTrue(contract["states"][name].get("free_text"), name)
            self.assertLessEqual(playing.MAX_TEXT, contract["states"][name]["max_length"])
            self.assertIn(name, contract["views"]["remote"]["statechanged"])
            self.assertNotIn("commands", contract["states"][name])
        # The activity feed reports the readings they join, not them again.
        feed = remote_activity.manifest(House(data), render.build(data))["items"]
        self.assertIn("example_shield_cast_title", feed)
        self.assertFalse({"example_shield_playing_app", "example_shield_playing_title"} & set(feed))


@unittest.skipUnless(shutil.which("node"), "node is required for the production playing rule")
class Rule(unittest.TestCase):
    def test_the_production_rule(self):
        result = subprocess.run([shutil.which("node"), str(AREA / "tests/playing_harness.js"), str(playing.SOURCE)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(json.loads(result.stdout)["passed"], 12)


@unittest.skipUnless(shutil.which("node"), "needs node for the shared web/TUI session")
class Remotes(unittest.TestCase):
    def test_the_session_and_the_terminal_show_what_the_source_plays_else_the_rooms_audio(self):
        script = r'''
const assert = require("node:assert/strict");
const Main = require("./tui/remote/main"), Shared = require("./tui/remote/shared");
console.info = console.log = () => {};
const text = (frame, key) => {
  const region = frame.regions.find(entry => entry.key === key);
  return frame.grid.rows.slice(region.y, region.y + region.h).map(row => row.slice(region.x, region.x + region.w).map(cell => cell.ch).join("")).join("\n");
};
const all = frame => frame.grid.rows.map(row => row.map(cell => cell.ch).join("")).join("\n");
(async () => {
  const shared = Shared.load();
  const parts = Main.assemble(Main.parse(["--mock", "--still", "--latency", "0"]), { shared, setInterval: () => 0, clearInterval() {} });
  const { session, backend, controller } = parts;
  await controller.start("example_west");
  await session.chooseSource("example_player");
  const room = session.room("example_west");
  assert.equal(session.view(room, session.state("example_west")).playing, null);
  backend.updatePlaying("example_player", "app", "Example App");
  backend.updatePlaying("example_player", "title", "Example Show S1E2 · Pilot");
  assert.equal(session.view(room, session.state("example_west")).playing, "Example App · Example Show S1E2 · Pilot");
  assert.match(all(controller.frame(149, 40)), /Player · Example App · Example Show S1E2/, "Home: after the source");
  controller.key(null, { name: "f2" });
  assert.match(text(controller.frame(149, 40), "remote"), /▶ Example App · Example Show S1E2 · Pilot/);
  // An app with no title is the app alone; UNDEF is nothing.
  backend.updatePlaying("example_player", "title", "UNDEF");
  assert.equal(session.view(room, session.state("example_west")).playing, "Example App");
  backend.updatePlaying("example_player", "app", "UNDEF");
  assert.equal(backend.playingOf("example_player"), null);
  assert.doesNotMatch(text(controller.frame(149, 40), "remote"), /▶ Example/);
  // Off, the room's audio device may still play.
  backend.updatePlaying("example_tv", "title", "Example Track — Example Artist");
  await session.chooseSource("off");
  assert.equal(session.view(room, session.state("example_west")).playing, "Example Track — Example Artist");
  controller.stop();
})().then(() => process.exit(0), (error) => { console.error(error); process.exit(1); });
'''
        env = {key: value for key, value in os.environ.items() if key != "THERMITE_REMOTE_HOUSE_FILE"}
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=AREA, env=env, capture_output=True, text=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
