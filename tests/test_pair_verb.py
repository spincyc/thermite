"""thermite pair: reading the Sony TVs' pairing, and --check's retirement.

No network: the house and thermite's REST API are fakes.
"""
from pathlib import Path
import contextlib
import io
import json
import os
import sys
import types
import unittest
from unittest import mock

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_paths
from oh_cli import pair
from oh_integrations import sony_ip_control

NAME = sony_ip_control.NAME


def tv(ident, name):
    return {"id": ident, "name": name, "ip": "192.0.2.20", "controls": {"control": [NAME]}}


LOUNGE, DEN = tv("lounge_tv", "Lounge TV"), tv("den_tv", "Den TV")
APPLE = {"id": "lounge_atv", "name": "Lounge Apple TV", "ip": "192.0.2.21", "controls": {}}


class Http:
    """thermite's REST API, as far as pair reads Items: their states."""

    base = "http://192.0.2.1:13602"

    def __init__(self, states):
        self.states = states
        self.read = []

    def try_get_json(self, path):
        item = path.rsplit("/", 1)[1]
        self.read.append(item)
        if item not in self.states:
            return 404, None
        return 200, {"state": self.states[item]}


class Pair(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop(oh_cli.URL_VARIABLE, None)
        house = types.SimpleNamespace(devices=[LOUNGE, DEN, APPLE],
                                      by_id={d["id"]: d for d in (LOUNGE, DEN, APPLE)})
        self.enterContext(mock.patch.object(pair, "House", mock.Mock(load=mock.Mock(return_value=house))))
        self.http = Http({"lounge_tv_pairing_status": "paired", "lounge_tv_problem": "NULL",
                          "den_tv_pairing_status": "unpaired", "den_tv_problem": "UNDEF"})
        self.openhab = self.enterContext(mock.patch.object(pair, "openhab", return_value=self.http))
        self.enterContext(mock.patch.object(oh_paths, "require_compatible"))

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                status = oh_cli.main(["pair", *argv])
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def test_without_device_it_lists_every_sony_tvs_pairing(self):
        status, out, err = self.run_main()
        self.assertEqual((status, err), (1, ""))
        self.assertEqual(out, "Lounge TV (lounge_tv, 192.0.2.20): paired\nDen TV (den_tv, 192.0.2.20): not paired\n")
        self.openhab.assert_called_once_with(False)

    def test_device_status_reads_that_tv_alone(self):
        status, out, err = self.run_main("lounge_tv", "--status", "--sandbox")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, "Lounge TV (lounge_tv, 192.0.2.20): paired\n")
        self.openhab.assert_called_once_with(True)
        self.assertNotIn("den_tv_pairing_status", self.http.read)

    def test_a_problem_is_named_and_is_not_paired(self):
        self.http.states["lounge_tv_problem"] = "no answer"
        status, out, _ = self.run_main("lounge_tv", "--status")
        self.assertEqual((status, out), (1, "Lounge TV (lounge_tv, 192.0.2.20): paired (no answer)\n"))

    def test_the_old_check_warns_and_does_the_same(self):
        for old, new in ((["--check"], []), (["--check", "lounge_tv"], ["lounge_tv", "--status"]),
                         (["--check", "--json"], ["--json"])):
            with self.subTest(old=old):
                expected = self.run_main(*new)
                status, out, err = self.run_main(*old)
                self.assertEqual((status, out), expected[:2])
                self.assertEqual(err, "thermite pair: --check is now --status; --check goes in the next release\n")
        self.assertNotIn("--check", oh_cli.parser_of("pair").format_help())

    def test_json_lists_each_tv_with_its_label_and_whether_it_is_paired(self):
        self.http.states["den_tv_problem"] = "no answer"
        status, out, err = self.run_main("--json")
        self.assertEqual((status, err), (1, ""))
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out), {"target": "production", "paired": False, "tvs": [
            {"id": "lounge_tv", "label": "Lounge TV", "paired": True, "state": "paired", "problem": None},
            {"id": "den_tv", "label": "Den TV", "paired": False, "state": "not paired", "problem": "no answer"}]})
        self.http.states["den_tv_pairing_status"], self.http.states["den_tv_problem"] = "paired", "NULL"
        status, out, _ = self.run_main("den_tv", "--status", "--json", "--sandbox")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out), {"target": "sandbox", "paired": True, "tvs": [
            {"id": "den_tv", "label": "Den TV", "paired": True, "state": "paired", "problem": None}]})

    def test_json_names_a_tv_whose_items_cannot_be_read(self):
        del self.http.states["den_tv_pairing_status"]
        status, out, _ = self.run_main("--json")
        self.assertEqual(status, 2)
        den = json.loads(out)["tvs"][1]
        self.assertEqual((den["id"], den["paired"]), ("den_tv", False))
        self.assertIn("has no Item den_tv_pairing_status", den["error"])

    def test_wrong_combinations_are_refused(self):
        for argv in (("lounge_tv", "1234", "--status"), ("lounge_tv", "1234", "--json"), ("lounge_tv", "--json")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_main(*argv)[0], 2)
        status, _, err = self.run_main("lounge_atv", "--status")
        self.assertEqual(status, 2)
        self.assertIn("not a TV under sony_ip_control control: lounge_atv (those are: lounge_tv, den_tv)", err)
        self.openhab.assert_not_called()

    def test_device_alone_still_pairs_it(self):
        with mock.patch.object(pair, "pair_sony", return_value=0) as pairing:
            status, _, err = self.run_main("lounge_tv", "1234")
        self.assertEqual((status, err), (0, ""))
        pairing.assert_called_once_with(LOUNGE, self.http, "1234")


if __name__ == "__main__":
    unittest.main()
