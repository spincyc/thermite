"""Offline checks for optional independently operated Yoke observers."""

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_deploy
import oh_metrics
import oh_public
from oh_cli import generate
from oh_metrics import dashboards, yoke


class Targets(unittest.TestCase):
    def test_absent_and_empty_targets_disable_scraping(self):
        for host in ({}, {"metrics": None}, {"metrics": {}}, {"metrics": {"yoke_targets": []}}):
            with self.subTest(host=host):
                self.assertEqual(yoke.targets(host), ())
                self.assertIsNone(yoke.scrape_job(host))

    def test_explicit_destinations_are_sorted_without_mutation(self):
        configured = ["observer.example:9754", "[2001:db8::1]:9754", "192.0.2.10:9754",
                      "localhost:1", "observer.example.:65535"]
        host = {"metrics": {"yoke_targets": configured}}
        before = copy.deepcopy(host)
        self.assertEqual(yoke.targets(host), tuple(sorted(configured)))
        self.assertEqual(host, before)
        self.assertEqual(yoke.scrape_job(host), {
            "job_name": "yoke", "static_configs": [{"targets": sorted(configured)}]})

    def test_invalid_configuration_types_are_rejected(self):
        for metrics in (False, [], "observer.example:9754"):
            with self.subTest(metrics=metrics), self.assertRaisesRegex(ValueError, "metrics:"):
                yoke.targets({"metrics": metrics})
        for value in (None, False, 9754, "observer.example:9754", {}, [None], [9754], [{}], [[]]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "metrics.yoke_targets"):
                yoke.targets({"metrics": {"yoke_targets": value}})

    def test_malformed_or_ambiguous_addresses_are_rejected(self):
        values = ["", "observer.example", ":9754", "observer.example:", "observer.example:0",
                  "observer.example:65536", "observer.example:-1", "observer.example:true",
                  "observer.example:9_754", "observer.example:９７５４", "observer.example:000001",
                  "https://observer.example:9754", "user@observer.example:9754",
                  "user:password@observer.example:9754", "observer.example:9754/metrics",
                  "observer.example:9754?query=1", "observer.example:9754#fragment",
                  " observer.example:9754", "observer.example:9754\n", "observer. example:9754",
                  "[::1]", "::1:9754", "[192.0.2.10]:9754", "[::gg]:9754",
                  "[fe80::1%eth0]:9754", "observer..:9754", "observer..example:9754",
                  "-observer.example:9754", "observer_.example:9754", "*.example:9754",
                  "192.0.2." + "999:9754"]
        for value in values:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "metrics.yoke_targets\\[0\\]"):
                yoke.targets({"metrics": {"yoke_targets": [value]}})

    def test_duplicate_destinations_include_equivalent_spelling(self):
        for values in (["observer.example:9754"] * 2,
                       ["Observer.Example:9754", "observer.example.:9754"],
                       ["[2001:db8::1]:9754", "[2001:db8:0:0:0:0:0:1]:9754"],
                       ["localhost:9754", "localhost:09754"]):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "duplicate Yoke target"):
                yoke.targets({"metrics": {"yoke_targets": values}})


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = oh_public.demo()
        cls.host = oh_public.demo_host()
        cls.baseline = oh_metrics.render(cls.inventory, cls.host)

    def test_default_output_is_identical_with_empty_targets(self):
        host = copy.deepcopy(self.host)
        host["metrics"]["yoke_targets"] = []
        self.assertEqual(oh_metrics.render(self.inventory, host), self.baseline)
        self.assertNotIn("grafana/dashboards/yoke.json", self.baseline)
        jobs = yaml.safe_load(self.baseline[oh_metrics.PROMETHEUS_YML])["scrape_configs"]
        self.assertNotIn("yoke", [job["job_name"] for job in jobs])

    def test_enabled_targets_add_only_the_optional_job_and_dashboard(self):
        host = copy.deepcopy(self.host)
        host["metrics"]["yoke_targets"] = ["192.0.2.20:9754", "127.0.0.1:9754"]
        rendered = oh_metrics.render(self.inventory, host)
        changed = {path for path in rendered if rendered[path] != self.baseline.get(path)}
        self.assertEqual(changed, {oh_metrics.PROMETHEUS_YML, "grafana/dashboards/yoke.json"})
        document = yaml.safe_load(rendered[oh_metrics.PROMETHEUS_YML])
        jobs = document["scrape_configs"]
        self.assertEqual([job for job in jobs if job["job_name"] == "yoke"], [{
            "job_name": "yoke", "static_configs": [{"targets": ["127.0.0.1:9754", "192.0.2.20:9754"]}]}])
        self.assertEqual([job for job in jobs if job["job_name"] != "yoke"],
                         yaml.safe_load(self.baseline[oh_metrics.PROMETHEUS_YML])["scrape_configs"])
        self.assertEqual(document["global"]["scrape_interval"], "30s")
        self.assertEqual(document["global"]["scrape_timeout"], "10s")
        self.assertEqual(oh_metrics.services(host), oh_metrics.services(self.host))
        host["metrics"]["yoke_targets"].reverse()
        self.assertEqual(oh_metrics.render(self.inventory, host), rendered)

    def test_dashboard_uses_the_published_observation_metrics(self):
        board = dashboards.yoke()
        self.assertEqual(board["uid"], "thermite-yoke")
        self.assertEqual(board["refresh"], "30s")
        queries = [target["expr"] for panel in board["panels"] for target in panel.get("targets", [])]
        metrics = ("up", "yoke_device_up", "yoke_device_state", "yoke_device_last_seen_age_seconds",
                   "yoke_device_control_frames_total", "yoke_device_control_bytes_total",
                   "yoke_device_events_total", "yoke_observer_datagrams_total")
        for metric in metrics:
            with self.subTest(metric=metric):
                self.assertTrue(any(metric + '{job="yoke"}' in query for query in queries))
        self.assertTrue(all('job="yoke"' in query for query in queries))
        self.assertTrue(all("or vector(0)" not in query for query in queries))
        self.assertTrue(all("rate(" in query and "[5m]" in query for query in queries if "_total" in query))
        state = next(panel for panel in board["panels"] if panel["title"] == "Last announced state")
        options = state["transformations"][0]["options"]
        self.assertTrue(options["includeByName"]["instance"])
        self.assertFalse(options["excludeByName"]["instance"])
        self.assertNotIn("yoke", dashboards.control_health())

    def test_host_loader_rejects_bad_targets_before_generation_writes(self):
        scratch = AREA / ".scratch/yoke-metrics-integration"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary)
            (root / "instance").mkdir()
            (root / "versions.json").write_text((AREA / "versions.json").read_text())
            host = copy.deepcopy(self.host)
            host["metrics"]["yoke_targets"] = ["observer.example:9754/metrics"]
            (root / "instance/host.yaml").write_text(json.dumps(host))
            house = root / "instance/devices.yaml"
            house.write_text(json.dumps(self.inventory))
            with self.assertRaisesRegex(ValueError, "metrics.yoke_targets"):
                oh_deploy.load(root)
            output = root / "output"
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(generate, "AREA", root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = generate.main(["--house", str(house), "--conf", str(output / "conf"),
                                        "--root", str(output)])
            self.assertEqual(result, 2)
            self.assertIn("nothing generated", err.getvalue())
            self.assertFalse(output.exists())
