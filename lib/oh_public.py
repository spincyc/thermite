"""Repeatable portable source export, with a fictional house and no runtime state.

An explicit source manifest is reviewed when a new module is added. The
private checkout's inventory is used only to reject private identifiers;
it is never copied into the public artifact.
"""
from __future__ import annotations

import ast
import io
import html
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import tempfile
import tokenize

import oh_cli
import oh_secrets
from oh_conf import House
from oh_features import remote_model, remote_cameras, remote_topology, remote_diagnostics, remote_activity

MANIFEST = ".thermite-export.json"
SOURCE_FILES = (
    'bin/thermite',
    'lib/oh_access/README.md',
    'lib/oh_access/__init__.py',
    'lib/oh_access/callback_service.py',
    'lib/oh_access/callbacks.py',
    'lib/oh_access/client.py',
    'lib/oh_access/files.py',
    'lib/oh_access/service.py',
    'lib/oh_access/staging.py',
    'lib/oh_access/worker.py',
    'lib/oh_adcp_sim.py',
    'lib/oh_atv.py',
    'lib/oh_atv_sim.py',
    'lib/oh_av_sim.py',
    'lib/oh_brand/__init__.py',
    'lib/oh_brand/palettes/contrast.json',
    'lib/oh_brand/palettes/thermite.json',
    'lib/oh_brand/theme.schema.json',
    'lib/oh_cli/__init__.py',
    'lib/oh_cli/access.py',
    'lib/oh_cli/apple_tv.py',
    'lib/oh_cli/cameras.py',
    'lib/oh_cli/completion.py',
    'lib/oh_cli/deploy.py',
    'lib/oh_cli/discover.py',
    'lib/oh_cli/docs.py',
    'lib/oh_cli/generate.py',
    'lib/oh_cli/health.py',
    'lib/oh_cli/history.py',
    'lib/oh_cli/install.py',
    'lib/oh_cli/lint.py',
    'lib/oh_cli/pair.py',
    'lib/oh_cli/remote.py',
    'lib/oh_cli/rollback.py',
    'lib/oh_cli/sandbox.py',
    'lib/oh_cli/status.py',
    'lib/oh_cli/test.py',
    'lib/oh_cli/theme.py',
    'lib/oh_cli/uninstall.py',
    'lib/oh_cli/upgrade.py',
    'lib/oh_cli/validate.py',
    'lib/oh_cli/version.py',
    'lib/oh_climate/__init__.py',
    'lib/oh_climate/nuheat_api.py',
    'lib/oh_conf.py',
    'lib/oh_deploy.py',
    'lib/oh_discover.py',
    'lib/oh_docs.py',
    'lib/oh_features/__init__.py',
    'lib/oh_features/all_off/__init__.py',
    'lib/oh_features/all_off/all_off.js',
    'lib/oh_features/model.py',
    'lib/oh_features/overview.py',
    'lib/oh_features/remote.py',
    'lib/oh_features/remote_activity.py',
    'lib/oh_features/remote_cameras.py',
    'lib/oh_features/remote_diagnostics.py',
    'lib/oh_features/remote_model.py',
    'lib/oh_features/remote_topology.py',
    'lib/oh_features/routing/__init__.py',
    'lib/oh_features/routing/routing.js',
    'lib/oh_features/threadpools.py',
    'lib/oh_googletv_sim.py',
    'lib/oh_identity/README.md',
    'lib/oh_identity/__init__.py',
    'lib/oh_identity/apps.py',
    'lib/oh_identity/asgi.py',
    'lib/oh_identity/auth.py',
    'lib/oh_identity/composition.py',
    'lib/oh_identity/migrations/0001_initial.py',
    'lib/oh_identity/migrations/__init__.py',
    'lib/oh_identity/models.py',
    'lib/oh_identity/policy_store.py',
    'lib/oh_identity/runtime.py',
    'lib/oh_identity/services.py',
    'lib/oh_identity/settings.py',
    'lib/oh_identity/state.py',
    'lib/oh_identity/streams.py',
    'lib/oh_identity/transport.py',
    'lib/oh_identity/urls.py',
    'lib/oh_identity/views.py',
    'lib/oh_install/__init__.py',
    'lib/oh_install/model.py',
    'lib/oh_install/runner.py',
    'lib/oh_install/steps.py',
    'lib/oh_install/system.py',
    'lib/oh_integrations/__init__.py',
    'lib/oh_integrations/_diagnostics.py',
    'lib/oh_integrations/_instr.py',
    'lib/oh_integrations/_pairing.py',
    'lib/oh_integrations/androidtv_googletv/__init__.py',
    'lib/oh_integrations/androidtv_googletv/androidtv_googletv.js',
    'lib/oh_integrations/avpro_telnet/__init__.py',
    'lib/oh_integrations/avpro_telnet/avpro_telnet.js',
    'lib/oh_integrations/bk808_http.py',
    'lib/oh_integrations/cast.py',
    'lib/oh_integrations/denonmarantz_http.py',
    'lib/oh_integrations/drhdmi_http.py',
    'lib/oh_integrations/network_ping.py',
    'lib/oh_integrations/pyatv_http/__init__.py',
    'lib/oh_integrations/pyatv_http/pyatv_http.js',
    'lib/oh_integrations/sonos.py',
    'lib/oh_integrations/sonos_control.js',
    'lib/oh_integrations/sony_adcp/__init__.py',
    'lib/oh_integrations/sony_adcp/sony_adcp.js',
    'lib/oh_integrations/sony_ip_control/__init__.py',
    'lib/oh_integrations/sony_ip_control/sony_ip_control.js',
    'lib/oh_integrations/sony_rest.py',
    'lib/oh_integrations/ukm404_http.py',
    'lib/oh_integrations/unifi_protect.py',
    'lib/oh_integrations/vrroom_http.py',
    'lib/oh_integrations/wiim_http/__init__.py',
    'lib/oh_integrations/wiim_http/hex-to-utf8.js',
    'lib/oh_access_mode.py',
    'lib/oh_inventory.py',
    'lib/oh_logging.py',
    'lib/oh_matrix_sim.py',
    'lib/oh_metrics/__init__.py',
    'lib/oh_metrics/dashboards.py',
    'lib/oh_metrics/exporter.py',
    'lib/oh_public.py',
    'lib/oh_render.py',
    'lib/oh_room_policy/__init__.py',
    'lib/oh_room_policy/model.py',
    'lib/oh_room_policy/plan.py',
    'lib/oh_room_policy/schedule.py',
    'lib/oh_room_policy/worker.py',
    'lib/oh_sandbox.py',
    'lib/oh_secrets.py',
    'lib/oh_semantic.py',
    'lib/oh_site.py',
    'lib/oh_site_contract.py',
    'lib/oh_sonos_sim.py',
    'lib/oh_sony_sim.py',
    'lib/oh_testing/__init__.py',
    'lib/oh_testing/boot.py',
    'lib/oh_testing/controls.py',
    'lib/oh_testing/routing.py',
    'lib/requirements-pyatv.txt',
    'lib/requirements-site.txt',
    'site/FAQ.md',
    'site/README.md',
    'site/examples/harbor.json',
    'site/pages.yml',
    'site/pages/colors.md',
    'site/pages/dashboards.md',
    'site/pages/index.md',
    'site/pages/start.md',
    'site/site.css',
    'site/site.js',
    'site/template.html',
    'tui/remote/administration.js',
    'tui/remote/app.js',
    'tui/remote/cameras.js',
    'tui/remote/controller.js',
    'tui/remote/credentials.js',
    'tui/remote/events.js',
    'tui/remote/grid.js',
    'tui/remote/landing.js',
    'tui/remote/main.js',
    'tui/remote/package-lock.json',
    'tui/remote/package.json',
    'tui/remote/paint.js',
    'tui/remote/shared.js',
    'tui/remote/store.js',
    'tui/remote/styles.js',
    'tui/remote/theme.js',
    'tui/remote/topology.js',
    'tui/remote/torch.js',
    'tui/remote/view.js',
    'versions.json',
    'web/remote/access-config.js',
    'web/remote/access-ui.js',
    'web/remote/access.css',
    'web/remote/access.html',
    'web/remote/access.js',
    'web/remote/activity.js',
    'web/remote/appearance.js',
    'web/remote/apple-touch-icon.png',
    'web/remote/backend.js',
    'web/remote/cameras.css',
    'web/remote/cameras.html',
    'web/remote/cameras.js',
    'web/remote/diagnostics.js',
    'web/remote/home.html',
    'web/remote/home.js',
    'web/remote/icon.svg',
    'web/remote/index.html',
    'web/remote/log.js',
    'web/remote/manifest.webmanifest',
    'web/remote/pair.css',
    'web/remote/pair.html',
    'web/remote/pair.js',
    'web/remote/remote.css',
    'web/remote/remote.js',
    'web/remote/remotes.js',
    'web/remote/security.html',
    'web/remote/session.js',
    'web/remote/settings.html',
    'web/remote/shell.css',
    'web/remote/shell.js',
    'web/remote/theme.css',
    'web/remote/topology.css',
    'web/remote/topology.html',
    'web/remote/topology.js',
    'web/remote/users.html',
)
OMITTED = {"control4", "dhcp-reservations", "matrix-settings"}
PROTOCOL_IPS = {"0.0.0.0", "127.0.0.1", "224.0.0.251", "239.255.255.250", "255.255.255.255"}
IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
MAC = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![\w:])")
CODE_WORDS = {"control4", "sddp", "bk-808", "wattbox", "shield", "fire tv", "alarm.com", "qolsys", "myq",
              "ratgdo", "lutron", "grafik", "subvolume", "btrfs", "adr 0", "homelab", "factory/",
              "automation/openhab", "make openhab-", "office"}


class PublicError(ValueError):
    pass


def demo() -> dict:
    def device(ident, room, kind, number, **extra):
        return {"id": ident, "room": room, "kind": kind, "name": ident.replace("_", " ").title(),
                "ip": f"192.0.2.{number}", "mac": "02:00:00:00:00:" + format(number, "02x"), **extra}
    return {"version": 1, "rooms": {"example_west": {"label": "Example West", "tag": "Room"},
                                     "example_east": {"label": "Example East", "tag": "Room"}},
            "devices": [
                device("example_tv", "example_west", "tv", 10, make="Sony", model="Example TV",
                       controls={"observe": ["sony_rest"], "control": ["sony_ip_control"]}),
                device("example_player", "example_west", "streamer", 11, make="Apple", model="Example Player",
                       controls={"control": ["pyatv_http"]}),
                device("example_display", "example_east", "tv", 12, make="Sony", model="Example Display",
                       controls={"observe": ["sony_rest"], "control": ["sony_ip_control"]}),
                device("example_computer", "example_east", "computer", 13, model="Computer", controls={}),
                device("example_matrix", "example_west", "hdmi_matrix", 14, model="HDMI matrix",
                       controls={"control": ["bk808_http"]})],
            "routing": {"hdmi_matrix": "example_matrix", "hdmi_off_input": 8, "usb_off_host": 4,
                        "sources": {"example_player": {"label": "Player"}, "example_computer": {"label": "Computer", "hdmi_input": 1}},
                        "rooms": {
                            "example_west": {"hdmi_output": 1, "sources": ["example_player", "example_computer"],
                                             "display": {"device": "example_tv", "input": "hdmi2", "sources": {"example_player": "hdmi1"}}},
                            "example_east": {"hdmi_output": 2, "sources": ["example_computer"],
                                             "display": {"device": "example_display", "input": "hdmi2"}}}}}


def demo_host() -> dict:
    return {"state_root": "/srv/thermite", "timezone": "Etc/UTC", "http_port": 8080, "https_port": 8443,
            "uid": 9001, "gid": 9001, "metrics": {"units": []}}


def _data(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode()


def _private(area: Path) -> set[str]:
    import yaml
    path = area / "instance/devices.yaml"
    house = yaml.safe_load(path.read_text()) if path.is_file() else {}
    identifiers = {d["id"].lower() for d in (house or {}).get("devices", [])}
    facts = set(identifiers)
    for room in (house or {}).get("rooms", {}).values():
        label = str(room.get("label") or "").lower()
        if " " in label:
            facts.add(label)
    for d in (house or {}).get("devices", []):
        name = str(d.get("name") or "").lower()
        products = {str(d.get("make") or "").lower(), str(d.get("model") or "").lower(),
                    (str(d.get("make") or "") + " " + str(d.get("model") or "")).strip().lower()}
        if name and name not in products:
            facts.add(name)
    words = area / "site/private-words.txt"
    if words.is_file():
        facts.update(line.strip().lower() for line in words.read_text().splitlines()
                     if line.strip() and not line.lstrip().startswith("#") and line.strip().lower() not in CODE_WORDS)
    facts.add(socket.gethostname().split(".")[0].lower())
    allowed = {d["id"] for d in demo()["devices"]} | {r["label"].lower() for r in demo()["rooms"].values()}
    allowed |= {d["name"].lower() for d in demo()["devices"]} | {"computer"}
    return {f for f in facts - allowed - CODE_WORDS if len(f) >= 4 or f in identifiers}


def _pattern(facts):
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(v) for v in sorted(facts, key=len, reverse=True))
                      + r")(?=$|_|[^\w])", re.IGNORECASE) if facts else None


def _replace_spans(text, spans):
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for start, end, value in sorted(spans, reverse=True):
        a = offsets[start[0] - 1] + start[1]
        b = offsets[end[0] - 1] + end[1]
        text = text[:a] + value + text[b:]
    return text


def _neutral_prose(text: str, facts: set[str]) -> str:
    """Change comments/docstrings only; unknown executable private facts fail the gate."""
    pattern = _pattern(facts)
    return pattern.sub("example", text) if pattern else text


def _python(text: str, facts: set[str], *, cli=False, site=False) -> str:
    tree = ast.parse(text)
    spans = []
    lines = text.splitlines(keepends=True)
    def point(line, byte_column):
        return line, len(lines[line - 1].encode()[:byte_column].decode())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            doc = node.body[0]
            if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant) and isinstance(doc.value.value, str):
                new = _neutral_prose(doc.value.value, facts)
                if new != doc.value.value:
                    spans.append((point(doc.lineno, doc.col_offset), point(doc.end_lineno, doc.end_col_offset), repr(new)))
        if cli and isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERBS" for t in node.targets):
            if not isinstance(node.value, ast.Dict):
                raise PublicError("CLI registry is no longer a literal dictionary")
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and key.value in OMITTED:
                    spans.append(((key.lineno, 0), (value.end_lineno + 1, 0), ""))
        if site and isinstance(node, ast.FunctionDef) and node.name == "house_facts":
            public_defaults = {"utc", "etc/utc", "/srv/thermite"}
            for device in demo()["devices"]:
                public_defaults.update(str(device.get(k) or "").lower() for k in ("id", "name", "make", "model"))
            public_defaults.update(room["label"].lower() for room in demo()["rooms"].values())
            expression = "facts - set(" + repr(sorted(public_defaults)) + ")"
            canonical = ast.dump(ast.parse(expression, mode="eval").body)
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)
                       and n.value.id == "facts"]
            returns += [n for n in ast.walk(node) if isinstance(n, ast.Return) and ast.dump(n.value) == canonical]
            if len(returns) != 1:
                raise PublicError("docs fact collection changed; review public-default exemption")
            node = returns[0]
            spans.append((point(node.lineno, node.col_offset), point(node.end_lineno, node.end_col_offset),
                          "return " + expression))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            new = _neutral_prose(token.string, facts)
            if new != token.string:
                spans.append((token.start, token.end, new))
    out = _replace_spans(text, spans)
    parsed = ast.parse(out)
    if cli:
        registry = next(n.value for n in parsed.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "VERBS" for t in n.targets))
        assert not OMITTED & {k.value for k in registry.keys}
    return out


def _scanner(text):
    """Only the known documented shell placeholder is not a credential."""
    tree = ast.parse(text)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "scan_text")
    prefix = '    if not code:\n        import html\n        text = html.unescape(text).replace("Bearer $(cat var/sandbox/api-token)", "<token>")\n'
    if ast.dump(function.body[1]) == ast.dump(ast.parse(prefix.lstrip()).body[0]):
        return text
    at = function.body[1].lineno - 1
    lines = text.splitlines(keepends=True)
    lines.insert(at, prefix)
    out = "".join(lines)
    ast.parse(out)
    return out


def _js_comments(text, facts):
    token = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/')
    return token.sub(lambda m: _neutral_prose(m.group(), facts) if m.group().startswith(("//", "/*")) else m.group(), text)


def _pair_mock(text):
    arrays = [["example_player", "pyatv_http", "Example Player", "Apple TV", "Example West", 1, 4, "digits", True,
               "unpaired", "Ask for a code while standing where you can see the screen."],
              ["example_tv", "sony_ip_control", "Example TV", "Sony TV", "Example West", 1, 4, "digits", True,
               "paired", "Check Normal IP authentication on the TV before asking for a code."],
              ["example_streamer", "androidtv_googletv", "Example Streamer", "Google TV", "Example East", 3, 6,
               "hex", True, "paired", "Wake the screen with its own remote, then ask for a code."]]
    pattern = r"(function mockItems\(\) \{\s*const devices = )\[[\s\S]*?\];"
    text, count = re.subn(pattern, lambda m: m[1] + json.dumps(arrays) + ";", text)
    if count != 1:
        raise PublicError("pairing demo changed; review the portable fixture")
    text, count = re.subn(r'const items = \[\{ name: "loc_[a-z_]+_source", state: "off" \}\];',
                          'const items = [{ name: "loc_example_west_source", state: "off" }];', text)
    if count != 1:
        raise PublicError("pairing demo source changed; review its portable fixture")
    return text


PUBLIC_TEST = '''"""Portable offline checks; no device or runtime process is started."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import oh_cli
import oh_brand
import oh_inventory
import oh_render
from oh_features import remote_model

def command(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            result = oh_cli.run("thermite", argv)
        except SystemExit as error:
            result = error.code
    if result:
        raise AssertionError(" ".join(argv) + " failed: " + err.getvalue() + out.getvalue())

class Portable(unittest.TestCase):
    def test_every_available_help_and_themes(self):
        command(["--help"])
        for verb in oh_cli.VERBS:
            command([verb, "--help"])
        command(["theme", "check", *oh_brand.builtin()])

    def test_sample_generation_and_editing(self):
        scratch = AREA / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            root = Path(temporary)
            house = root / "devices.yaml"
            shutil.copyfile(AREA / "instance/devices.yaml", house)
            args = ["--house", str(house), "--conf", str(root / "conf")]
            command(["generate", *args, "--root", str(root)])
            command(["generate", "--check", *args, "--root", str(root)])
            command(["validate", *args])
            data = oh_inventory.load(house)
            data["devices"][0]["name"] = "Renamed example display"
            data["devices"][0]["volume_max"] = 73
            house.write_text(json.dumps(data))
            command(["generate", *args, "--root", str(root)])
            model = remote_model.literal((root / "conf/html/remote/house.js").read_text())
            self.assertEqual(model["devices"][data["devices"][0]["id"]]["label"], "Renamed example display")
            self.assertEqual(model["devices"][data["devices"][0]["id"]]["control"]["volume"]["max"], 73)
            self.assertEqual(set(model["devices"]), {d["id"] for d in data["devices"]})

    def test_docs_and_lint(self):
        scratch = AREA / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            command(["docs", "build", "--out", temporary])
            self.assertTrue((Path(temporary) / "index.html").is_file())
        command(["lint"])
'''

MAKEFILE = '''PYTHON ?= python3
.PHONY: help check generate docs
help:
\t@$(PYTHON) bin/thermite --help
check:
\t@PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest tests.test_public -q
generate:
\t@$(PYTHON) bin/thermite generate
docs:
\t@$(PYTHON) bin/thermite docs build --out .scratch/site
'''


def bundle(doc_files: dict[str, bytes], area: Path = oh_cli.AREA) -> dict[str, bytes]:
    """Build the complete reviewed artifact in memory. No destination writes."""
    import oh_brand
    import oh_render
    area = Path(area)
    facts = _private(area)
    out = dict(doc_files)
    for name in SOURCE_FILES:
        path = area / name
        if path.is_symlink() or not path.is_file():
            raise PublicError(f"portable source missing or not a regular file: {name}")
        value = path.read_bytes()
        if path.suffix == ".py":
            value = _python(value.decode(), facts, cli=name == "lib/oh_cli/__init__.py",
                            site=name == "lib/oh_site.py").encode()
            if name == "lib/oh_secrets.py":
                value = _scanner(value.decode()).encode()
        elif path.suffix == ".js":
            value = _js_comments(value.decode(), facts).encode()
        if name == "web/remote/pair.js":
            value = _pair_mock(value.decode()).encode()
        out[name] = value
    data = demo()
    problems = oh_render.validate(data)
    if problems:
        raise PublicError("invalid public sample: " + "; ".join(problems))
    conf = oh_render.build(data)
    house = House(data)
    out["instance/devices.yaml"], out["instance/host.yaml"] = _data(data), _data(demo_host())
    out["web/remote/house.js"] = remote_model.script(house).encode()
    for filename, manifest in [("activity.json", remote_activity.manifest(house, conf)),
                               ("diagnostics.json", remote_diagnostics.manifest(house, conf)),
                               ("cameras.json", remote_cameras.manifest(house)),
                               ("topology.json", remote_topology.manifest(house, conf))]:
        out[f"web/remote/{filename}"] = _data(manifest)
    out["web/remote/README.md"] = b"# Web remote\n\nRun `bin/thermite generate` after editing the house file. The generated page is served by the runtime under /static/remote/. Source previews use the fictional sample; `?backend=mock` sends no commands.\n"
    out["tui/remote/README.md"] = b"# Terminal remote\n\nRun `bin/thermite remote --mock` for a fictional house without device commands. Node.js and the locked blessed package are required. Run `bin/thermite generate` after editing the house file; the terminal prefers that generated house model.\n"
    # Development-only identity fixtures are deliberately outside this source
    # manifest. Keep the public instructions executable from the shipped tree.
    identity_readme = out["lib/oh_identity/README.md"].decode()
    private_tests = (
        "Run the actual tests (including real-file SQLite concurrency, no runtime/device\n"
        "calls): set THERMITE_IDENTITY_STATE to a 0700 scratch directory, PYTHONPATH=lib,\n"
        "DJANGO_SETTINGS_MODULE=oh_identity.test_settings, then\n"
        "`python -m django test oh_identity --noinput`. This package's tests are explicit\n"
        "Django tests; they do not silently skip when the framework is absent.\n")
    portable_tests = (
        "## Testing\n\nRun `make check` from the repository root for the shipped portable\n"
        "configuration, documentation and offline smoke checks. Full identity\n"
        "development fixtures and their Django test settings are not included\n"
        "in this portable source export.\n")
    if identity_readme.endswith(private_tests) and identity_readme.count(private_tests) == 1:
        identity_readme = identity_readme[:-len(private_tests)] + portable_tests
    elif not (identity_readme.endswith(portable_tests) and identity_readme.count(portable_tests) == 1):
        raise PublicError("Identity test instructions changed; review portable README adaptation")
    out["lib/oh_identity/README.md"] = identity_readme.encode()
    out["Makefile"] = MAKEFILE.encode()
    out["requirements.txt"] = b"# Base configuration and documentation dependency. Optional helpers have their own locks.\nPyYAML>=6.0,<7\n"
    out[".gitignore"] = b"/.scratch/\n/var/\n/build/\n/conf/\n/metrics/\n/deploy/\n/.venv/\n__pycache__/\n*.pyc\nnode_modules/\n"
    out["tests/__init__.py"] = b""
    out["tests/test_public.py"] = PUBLIC_TEST.encode()
    workflow = out.get(".github/workflows/pages.yml", b"").decode()
    marker = "      - run: test -f docs/index.html && test -f docs/commands.html"
    if workflow.count(marker) != 1:
        raise PublicError("Pages workflow changed; review portable test insertion")
    workflow = re.sub(r"      # docs/ is built[\s\S]*?(?=      - run: test -f docs/index.html)", "", workflow)
    out[".github/workflows/pages.yml"] = workflow.replace(marker,
        "      - run: python3 -m pip install -r requirements.txt\n"
        "      - run: make check\n" + marker).encode()
    readme = out.get("README.md", b"# thermite\n").decode()
    readme = readme.replace("Early. For now this repository holds the docs; the code follows. All rights reserved.",
                            "Under active development. The portable source and fictional example are included. All rights reserved.")
    private_site = """## Source and your private site

This public checkout contains the program and a fictional example, not a production house. Keep your real inventory and host settings in a separate private site checkout. Production deployment installs a committed revision: generate and commit `conf/`, `metrics/` and `deploy/` only in that private checkout before using the deployment recipe below. Those outputs are ignored here; do not publish your house configuration upstream.

"""
    readme = readme.replace("## In four commands", private_site + "## In four commands")
    readme += """\n## Run the example\n\nThe included `instance/` describes fictional devices at documentation-only addresses. Replace those files with your own inventory before using a real runtime.\n\n```sh\npython3 -m venv .venv\n.venv/bin/pip install -r requirements.txt\n.venv/bin/python -m unittest tests.test_public -q\n.venv/bin/python bin/thermite generate\n.venv/bin/python bin/thermite validate\n```\n\nConfiguration, documentation and these tests run offline. Running a native test runtime or installing services is a separate explicit command and requires the pinned downloads. The terminal remote additionally needs Node.js and its locked package; the Apple TV helper has its own hashed dependency lock.\n"""
    out["README.md"] = readme.encode()
    # Default source previews are canonical generated assets, never an opaque image copy.
    out["web/remote/theme.css"] = oh_brand.css().encode()
    out["web/remote/icon.svg"] = oh_brand.svg(tile=True).encode()
    out["web/remote/apple-touch-icon.png"] = oh_brand.png()
    if hasattr(oh_brand, "mark_svg"):
        out["web/remote/mark.svg"] = oh_brand.mark_svg().encode()
        out["web/remote/mark-light.svg"] = oh_brand.mark_svg(mode="light").encode()
    out[MANIFEST] = _data({"version": 1, "paths": sorted([*out, MANIFEST])})
    problems = guard(out, area)
    if problems:
        raise PublicError("public source refused:\n" + "\n".join(problems))
    return out


def guard(files: dict[str, bytes], area: Path = oh_cli.AREA, *, facts: set[str] | None = None) -> list[str]:
    """Scan every text file, including Python/YAML/bin/workflows/locks."""
    import oh_brand
    facts = _private(Path(area)) if facts is None else facts
    pattern = _pattern(facts)
    allowed_macs = {d["mac"] for d in demo()["devices"]}
    layouts_source = files.get("web/remote/remotes.js", b"").decode()
    layout_body = layouts_source.partition("\n  remotes: {")[2]
    layout_ids = set(re.findall(r"^    ([a-z][a-z0-9_]*): \{", layout_body, re.MULTILINE))
    problems = []
    for name, value in sorted(files.items()):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name.startswith(("var/", "build/", "conf/", "metrics/", "deploy/")):
            problems.append(f"{name}: forbidden export path")
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            if name != "web/remote/apple-touch-icon.png" or value != oh_brand.png():
                problems.append(f"{name}: unapproved binary asset")
            continue
        for match in IP.findall(text):
            allowed = match in PROTOCOL_IPS or (name == "lib/oh_discover.py" and match == ".".join(["1"] * 4))
            try:
                address = ipaddress.ip_address(match)
                allowed |= any(address in ipaddress.ip_network(prefix) for prefix in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
            except ValueError:
                pass
            if not allowed:
                problems.append(f"{name}: non-documentation address")
        for match in MAC.findall(text):
            if not (name == "instance/devices.yaml" and match.lower() in allowed_macs):
                problems.append(f"{name}: non-demo MAC address")
        def technical(match):
            token = match.group().lower()
            line = text[text.rfind("\n", 0, match.start()) + 1:text.find("\n", match.end()) if "\n" in text[match.end():] else len(text)]
            return ((name == "web/remote/remotes.js" and token in layout_ids
                     and re.fullmatch(r"\s*" + re.escape(token) + r": \{", line))
                    or (name == "tui/remote/view.js" and token in layout_ids
                        and (".includes(target.remote)" in line or "remote.id ===" in line))
                    or (name == "lib/oh_features/remote_model.py" and token in layout_ids
                        and re.search(r'"[a-z_]+": "' + re.escape(token) + '"', line)))
        if pattern and any(not technical(match) for match in pattern.finditer(text)):
            problems.append(f"{name}: private identifier or person/host fact")
        # The strongest credential patterns are never bypassed by a lint comment.
        if any(regex.search(text) for _, regex in oh_secrets.PATTERNS):
            problems.append(f"{name}: credential signature")
        checked = html.unescape(text.replace(oh_secrets.ALLOW, ""))
        checked = checked.replace("Bearer $(cat var/sandbox/api-token)", "<token>")
        checked = re.sub(r'(["\'])Bearer \1(?=\s*\+)', lambda m: m[1] + "<token>" + m[1], checked)
        try:
            for line, reason in _credentials(name, checked):
                problems.append(f"{name}:{line}: {reason}")
        except (SyntaxError, ValueError):
            problems.append(f"{name}: invalid source syntax")
    return sorted(set(problems))


_CREDENTIAL = re.compile(r"(?i)^(?:password|passwd|passphrase|secret|token|api_?key|apitoken|psk|pre_shared_key|authorization|cookie|pin)$")


def _credentials(name, text):
    def literal(key, value):
        return (isinstance(key, str) and _CREDENTIAL.fullmatch(key) and isinstance(value, (str, bytes))
                and bool(value) and not oh_secrets.PLACEHOLDER.fullmatch(str(value)) and value not in {"Bearer", "Basic"})
    if name.endswith(".py") or name == "bin/thermite":
        found = []
        tree = ast.parse(text)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        def simulated_pin(node):
            if name != "lib/oh_testing/controls.py":
                return False
            if isinstance(node, ast.keyword) and node.arg == "pin" and node.value.value == "0000":
                call = parents.get(node)
                return isinstance(call, ast.Call) and ast.unparse(call.func) == "sony_sim.TV"
            if isinstance(node, ast.Assign) and node.value.value == "8429" and ast.unparse(node.targets[0]) == "tv.sim.pin":
                parent = parents.get(node)
                while parent is not None:
                    if isinstance(parent, ast.FunctionDef):
                        return parent.name == "step_pairing"
                    parent = parents.get(parent)
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    key = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
                    if literal(key, node.value.value) and not simulated_pin(node):
                        found.append((node.lineno, "literal credential"))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(value, ast.Constant) and literal(key.value, value.value):
                        found.append((value.lineno, "literal credential"))
            if (isinstance(node, ast.keyword) and isinstance(node.value, ast.Constant)
                    and literal(node.arg, node.value.value) and not simulated_pin(node)):
                found.append((node.lineno, "literal credential"))
        return found
    if name.endswith(".js"):
        strings = list(re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/', text))
        pattern = re.compile(r'''(?i)(?:^|[\s{,;])(?:const\s+|let\s+|var\s+)?["']?(password|passwd|passphrase|secret|token|api_?key|apitoken|psk|authorization|cookie|pin)["']?\s*[:=]\s*(["'])(.*?)\2''', re.MULTILINE)
        return [(text.count("\n", 0, m.start()) + 1, "literal credential") for m in pattern.finditer(text)
                if literal(m[1], m[3]) and not any(s.start() < m.start() < s.end() for s in strings)]
    if name.endswith((".json", ".yaml", ".yml")):
        import yaml
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            document = None
        found = []
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if literal(key, item):
                        found.append((1, "literal credential"))
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(document)
        return found + oh_secrets.scan_text(text)
    return oh_secrets.scan_text(text)


def _target(root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PublicError("unsafe managed export path")
    allowed = {MANIFEST, "README.md", "FAQ.md", "Makefile", "requirements.txt", "versions.json", ".gitignore",
               ".github/workflows/pages.yml"}
    if name not in allowed and path.parts[0] not in {"docs", "bin", "lib", "site", "web", "tui", "tests", "instance"}:
        raise PublicError("path is outside the managed public source trees")
    target = root / name
    if any(p.is_symlink() for p in [target, *target.parents] if p != root.parent):
        raise PublicError("export refuses symlink targets")
    return target


def compare(root: Path, files: dict[str, bytes]) -> tuple[list[str], list[str]]:
    root = Path(root)
    old = root / MANIFEST
    previous = []
    if old.exists():
        data = json.loads(old.read_text())
        if data.get("version") != 1 or not isinstance(data.get("paths"), list):
            raise PublicError("unknown public export manifest")
        previous = data["paths"]
    for name in [*previous, *files]:
        if not isinstance(name, str):
            raise PublicError("invalid managed export path")
        _target(root, name)
    stale = sorted(name for name, data in files.items() if not (root / name).is_file()
                   or (root / name).read_bytes() != data)
    stray = sorted(name for name in previous if name not in files and (root / name).is_file())
    return stale, stray


def write(root: Path, files: dict[str, bytes]) -> tuple[list[str], list[str]]:
    problems = guard(files)
    if problems:
        raise PublicError("public source refused:\n" + "\n".join(problems))
    stale, stray = compare(root, files)
    for name in stale:
        path = _target(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[name])
        path.chmod(0o755 if name == "bin/thermite" else 0o644)
    for name in stray:
        _target(root, name).unlink()
    return stale, stray


def smoke(files: dict[str, bytes], area: Path = oh_cli.AREA) -> dict:
    """Exercise only portable operations in an isolated, network/process-guarded tree."""
    scratch = Path(area) / ".scratch"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="public-smoke-", dir=scratch) as folder:
        root = Path(folder)
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        hook = root / ".scratch/hooks"
        hook.mkdir(parents=True)
        calls = hook / "forbidden.jsonl"
        hook.joinpath("sitecustomize.py").write_text('''import json,os,socket,subprocess
from pathlib import Path
def refuse(kind):
 def blocked(*args,**kwargs):
  with open(os.environ["THERMITE_SMOKE_CALLS"],"a") as log:log.write(json.dumps({"kind":kind})+"\\n")
  raise RuntimeError("portable smoke forbids "+kind)
 return blocked
socket.socket.connect=refuse("network")
socket.socket.connect_ex=refuse("network")
socket.socket.sendto=refuse("network")
socket.socket.bind=refuse("network")
socket.socket.listen=refuse("network")
socket.create_connection=refuse("network")
socket.getaddrinfo=refuse("network")
original=subprocess.Popen
def popen(args,*a,**kw):
 if isinstance(args,(list,tuple)) and Path(str(args[0])).name=="git":
  raise FileNotFoundError("Git deliberately unavailable in portable smoke")
 return refuse("process")()
subprocess.Popen=popen
os.system=refuse("process")
for method in ("execv","execve","execvp","execvpe","execl","execlp","execlpe","fork","forkpty","posix_spawn","posix_spawnp","spawnl","spawnle","spawnlp","spawnlpe","spawnv","spawnve","spawnvp","spawnvpe"):
 if hasattr(os,method):setattr(os,method,refuse("process"))
''')
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(hook),
               "THERMITE_SMOKE_CALLS": str(calls)}
        result = subprocess.run([sys.executable, "-m", "unittest", "tests.test_public", "-q"], cwd=root,
                                env=env, capture_output=True, text=True, timeout=90)
        attempted = calls.read_text().splitlines() if calls.exists() else []
        if result.returncode or attempted:
            raise PublicError("portable smoke failed:\n" + result.stdout + result.stderr +
                              ("forbidden network/process operation attempted" if attempted else ""))
        return {"pass": True, "networkCalls": 0, "runtimeProcesses": 0, "tests": 3,
                "checks": ["all public help", "theme check", "sample generate/check/validate", "edited sample model", "docs build", "lint"]}
