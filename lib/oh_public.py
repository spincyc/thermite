"""Repeatable portable source export, with a fictional house and no runtime state.

An explicit source manifest is reviewed when a new module is added. The
private checkout's inventory is used only to reject private identifiers;
it is never copied into the public artifact.
"""
from __future__ import annotations

import ast
import html
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import struct
import sys
import tempfile

import oh_cli
import oh_secrets

MANIFEST = ".thermite-export.json"
PROTOCOL_IPS = {"0.0.0.0", "127.0.0.1", "224.0.0.251", "239.255.255.250", "255.255.255.255"}
IP = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
MAC = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![\w:])")
CODE_WORDS = {"control4", "sddp", "bk-808", "wattbox", "shield", "fire tv", "alarm.com", "qolsys", "myq",
              "ratgdo", "lutron", "grafik", "subvolume", "btrfs", "adr 0", "homelab", "factory/",
              "automation/openhab", "make openhab-", "office"}


#: Per-file exceptions to guard(), each reviewed. Addresses a file may name
#: besides PROTOCOL_IPS and the documentation ranges: the discovery probe's
#: public resolver.
ALLOWED_ADDRESSES = {"lib/oh_discover.py": frozenset({".".join(["1"] * 4)})}
#: Installation fixtures, which use separate guest-loopback devices.
LOOPBACK_FILES = frozenset({"tests/e2e/fixture.py", "tests/e2e/dummy-tv.py", "tests/e2e/check_access.py",
                            "tests/test_house_config.py"})
#: Integrations whose remote_of() returns a remote layout's id
#: (web/remote/remotes.js), which may also be a private word.
LAYOUT_RETURN_FILES = frozenset({"lib/oh_integrations/globalcache_ir.py", "lib/oh_integrations/denonmarantz_http.py",
                                 "lib/oh_integrations/bk808_cec.py"})


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
    from oh_ports import HTTP_PORT, HTTPS_PORT
    return {"timezone": "Etc/UTC", "http_port": HTTP_PORT, "https_port": HTTPS_PORT,
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
    for relative in ("instance/private-words.txt", "site/private-words.txt"):
        words = area / relative
        if words.is_file():
            facts.update(line.strip().lower() for line in words.read_text().splitlines()
                         if line.strip() and not line.lstrip().startswith("#") and line.strip().lower() not in CODE_WORDS)
    allowed = {d["id"] for d in demo()["devices"]} | {r["label"].lower() for r in demo()["rooms"].values()}
    allowed |= {d["name"].lower() for d in demo()["devices"]} | {"computer"}
    return {f for f in facts - allowed - CODE_WORDS if len(f) >= 4 or f in identifiers}


def _pattern(facts):
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(v) for v in sorted(facts, key=len, reverse=True))
                      + r")(?=$|_|[^\w])", re.IGNORECASE) if facts else None


def bundle(doc_files: dict[str, bytes], area: Path = oh_cli.PROGRAM_ROOT) -> dict[str, bytes]:
    """The reviewed application files (oh_package.public_files: the
    .thermite-export.json manifest, or an installed release's) and the docs,
    with a manifest of them all, in memory; refused if guard() finds
    anything. No destination writes."""
    import oh_package
    files = {**oh_package.public_files(Path(area)), **doc_files}
    files[MANIFEST] = _data({"version": 1, "paths": sorted(files)})
    problems = guard(files, area)
    if problems:
        raise PublicError("public source refused:\n" + "\n".join(problems))
    return files


def _screenshots(files: dict[str, bytes]) -> dict[str, str]:
    """Permit only the two reviewed captures of the public fictional inventory.

    Their manifest travels with source and built docs. It records provenance
    and catches accidental image substitution; image contents are reviewed
    when updating the captures, since the text privacy scan cannot read pixels.
    """
    manifests = [files[path] for path in (
        "site/assets/screenshots/manifest.json", "docs/assets/screenshots/manifest.json") if path in files]
    if not manifests:
        return {}
    if any(value != manifests[0] for value in manifests):
        raise ValueError("screenshot manifests differ")
    data = json.loads(manifests[0])
    inventory = (json.dumps(demo(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    names = {"web-remote.png", "terminal-remote.png"}
    if (data.get("version") != 1 or data.get("mode") != "mock" or data.get("inventory") != "oh_public.demo()"
            or data.get("inventory_sha256") != hashlib.sha256(inventory).hexdigest()
            or set(data.get("files", {})) != names):
        raise ValueError("screenshots must use the public demo inventory")
    approved = {}
    for name in names:
        entry = data["files"][name]
        for prefix in ("site/assets/screenshots/", "docs/assets/screenshots/"):
            path = prefix + name
            if path not in files:
                continue
            image = files[path]
            if (len(image) < 24 or image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR"
                    or list(struct.unpack(">II", image[16:24])) != [entry["width"], entry["height"]]
                    or hashlib.sha256(image).hexdigest() != entry["sha256"]):
                raise ValueError(f"screenshot differs from its capture manifest: {path}")
            approved[path] = entry["sha256"]
    return approved


def guard(files: dict[str, bytes], area: Path = oh_cli.AREA, *, facts: set[str] | None = None) -> list[str]:
    """Scan every text file, including Python/YAML/bin/workflows/locks."""
    import oh_brand
    import oh_entry
    binary_artifacts = oh_entry.approved_artifacts()
    facts = _private(Path(area)) if facts is None else facts
    pattern = _pattern(facts)
    allowed_macs = {d["mac"] for d in demo()["devices"]}
    layouts_source = files.get("web/remote/remotes.js", b"").decode()
    layout_body = layouts_source.partition("\n  remotes: {")[2]
    declarations = list(re.finditer(r"^    ([a-z][a-z0-9_]*): \{", layout_body, re.MULTILINE))
    layout_ids = {match.group(1) for match in declarations}
    body_offset = len(layouts_source) - len(layout_body)
    layout_spans = [(body_offset + match.start(1), body_offset + match.end(1)) for match in declarations]
    problems = []
    try:
        screenshots = _screenshots(files)
    except (ValueError, KeyError, TypeError, AttributeError):
        screenshots = {}
        problems.append("screenshots: invalid or inconsistent capture manifest")
    for name, value in sorted(files.items()):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name.startswith(("var/", "build/", "conf/", "metrics/", "deploy/")):
            problems.append(f"{name}: forbidden export path")
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            if name in binary_artifacts and hashlib.sha256(value).hexdigest() == binary_artifacts[name]:
                continue
            if name not in screenshots and (name != "web/remote/apple-touch-icon.png" or value != oh_brand.png()):
                problems.append(f"{name}: unapproved binary asset")
            continue
        for match in IP.findall(text):
            allowed = match in PROTOCOL_IPS or match in ALLOWED_ADDRESSES.get(name, ())
            try:
                address = ipaddress.ip_address(match)
                allowed |= address.is_loopback and name in LOOPBACK_FILES
                allowed |= any(address in ipaddress.ip_network(prefix) for prefix in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
            except ValueError:
                pass
            if not allowed:
                problems.append(f"{name}: non-documentation address")
        for match in MAC.findall(text):
            if not (name == "instance/devices.yaml" and match.lower() in allowed_macs):
                problems.append(f"{name}: non-demo MAC address")
        layout_returns = []
        if name in LAYOUT_RETURN_FILES:
            offsets = [0]
            for row in text.splitlines(keepends=True): offsets.append(offsets[-1] + len(row))
            for function in ast.parse(text).body:
                if not isinstance(function, ast.FunctionDef) or function.name != "remote_of": continue
                for statement in function.body:
                    if not isinstance(statement, ast.Return): continue
                    value = statement.value
                    choices = [value]
                    if isinstance(value, ast.IfExp) and ast.unparse(value.test) == "heos.enabled(device)":
                        choices = [value.body, value.orelse]
                    if all(isinstance(choice, ast.Constant) and choice.value in layout_ids for choice in choices):
                        layout_returns += [(offsets[choice.lineno - 1] + choice.col_offset,
                                            offsets[choice.end_lineno - 1] + choice.end_col_offset) for choice in choices]
        def technical(match):
            token = match.group().lower()
            line = text[text.rfind("\n", 0, match.start()) + 1:text.find("\n", match.end()) if "\n" in text[match.end():] else len(text)]
            return ((name == "web/remote/remotes.js"
                     and any(start <= match.start() and match.end() <= end for start, end in layout_spans))
                    or (name == "tui/remote/view.js" and token in layout_ids
                        and (".includes(target.remote)" in line or "remote.id ===" in line))
                    or (name == "lib/oh_features/remote_model.py" and token in layout_ids
                        and re.search(r'"[a-z_]+": "' + re.escape(token) + '"', line))
                    or any(start <= match.start() and match.end() <= end for start, end in layout_returns))
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
    if name.endswith(".java"):
        return oh_secrets.scan_text(text, code=True)
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
    if name.endswith((".js", ".cjs", ".mjs")):
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
    allowed = {MANIFEST, "AGENTS.md", "README.md", "FAQ.md", "Makefile", "requirements.txt", "versions.json", ".gitignore", "pyproject.toml", "CHANGELOG.md",
               ".github/workflows/pages.yml", ".github/workflows/ci.yml"}
    if name not in allowed and path.parts[0] not in {"docs", "bin", "lib", "site", "web", "tui", "tests", "instance", "iso"}:
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
